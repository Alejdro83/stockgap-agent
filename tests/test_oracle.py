"""Weekend oracle: prediction math, table creation, cutoff logic, scoring."""

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from stockgap import oracle, oracle_score
from stockgap.oracle import OraclePredictor, build_prediction_rows, cutoff_for, next_cutoff
from stockgap.oracle_score import (
    ScoreOracle,
    fetch_real_opens,
    load_real_opens,
    score_prediction,
    write_reputation,
)

UTC = timezone.utc
CFG = {
    "ORACLE_INTERVAL_MIN": "5", "ORACLE_CUTOFF_WEEKDAY": "sunday",
    "ORACLE_CUTOFF_HOUR": "23", "ORACLE_CUTOFF_MINUTE": "59",
    "ORACLE_DEVIATION_FLAG_BPS": "100",
}


def tok(addr, sym, ticker, ratio, status="CLOSED"):
    return {"tokenContractAddress": addr, "tokenSymbol": sym, "underlyingTicker": ticker,
            "platformId": "ondo", "tokenToShareRatio": ratio,
            "statusInfo": {"marketStatus": status}}


def px(addr, token_price, ref):
    return {"tokenContractAddress": addr, "tokenPrice": token_price, "referencePrice": ref}


class FakeFetcher:
    def __init__(self, universe, prices):
        self.universe, self.prices = universe, prices

    def load_universe(self):
        return self.universe

    def fetch_price_rows(self, contracts):
        return self.prices


# ---------- prediction math ----------

def rows_for(universe, prices, flag=Decimal("100")):
    return build_prediction_rows(universe, prices, 1, "2026-10-03T12:00:00Z",
                                 "2026-10-04T23:59:00Z", flag_bps=flag)


def test_ratio_one_deviation():
    r = rows_for([tok("0xa", "AAPLon", "AAPL", "1")], [px("0xa", "105", "100")])[0]
    assert Decimal(r["implied_open_price"]) == Decimal("105")
    assert Decimal(r["deviation_bps"]) == Decimal("500")
    assert r["deviation_flag"] == 1


def test_ratio_ten():
    r = rows_for([tok("0xa", "NFLXon", "NFLX", "10")], [px("0xa", "5020", "500")])[0]
    assert Decimal(r["implied_open_price"]) == Decimal("502")
    assert Decimal(r["deviation_bps"]) == Decimal("40")
    assert r["deviation_flag"] == 0


def test_ratio_fractional_uses_decimal_not_float():
    r = rows_for([tok("0xa", "ENLVon", "ENLV", "0.066667")], [px("0xa", "1.5", "22")])[0]
    expected = Decimal("1.5") / Decimal("0.066667")
    assert Decimal(r["implied_open_price"]) == expected
    ref = Decimal("22")
    assert Decimal(r["deviation_bps"]) == (expected - ref) / ref * 10000
    assert isinstance(r["implied_open_price"], str)


def test_negative_deviation_and_fields_recorded():
    r = rows_for([tok("0xA", "TSLAon", "TSLA", "0.5")], [px("0xa", "48", "100")])[0]
    assert Decimal(r["implied_open_price"]) == Decimal("96")
    assert Decimal(r["deviation_bps"]) == Decimal("-400")
    assert r["underlying_ticker"] == "TSLA" and r["token_symbol"] == "TSLAon"
    assert r["platform_id"] == "ondo" and r["token_to_share_ratio"] == "0.5"
    assert r["last_reference_close"] == "100"
    assert r["source_endpoint"].endswith("/rwa/price") and r["fetched_at"]


def test_invalid_rows_skipped():
    uni = [tok("0xa", "A", "A", "0"), tok("0xb", "B", "B", "1"), tok("0xc", "C", "C", None)]
    prices = [px("0xa", "10", "10"), px("0xb", "", "10"), px("0xc", "10", "10"),
              px("0xzzz", "10", "10")]
    assert rows_for(uni, prices) == []


def test_missing_reference_gives_null_deviation():
    r = rows_for([tok("0xa", "A", "A", "1")], [px("0xa", "10", None)])[0]
    assert r["deviation_bps"] is None and r["deviation_flag"] == 0


# ---------- table + recorder ----------

def test_table_creation_idempotent(tmp_path):
    db = tmp_path / "t.db"
    f = FakeFetcher([tok("0xa", "A", "A", "1")], [px("0xa", "10", "10")])
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    OraclePredictor(db, f, CFG, clock=lambda: now)
    p = OraclePredictor(db, f, CFG, clock=lambda: now)  # second init must not fail
    p.snapshot()
    p2 = OraclePredictor(db, f, CFG, clock=lambda: now)
    assert p2.snapshot()[0] == 2  # snapshot_id continues across restarts
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM oracle_predictions").fetchone()[0] == 2
    cols = {r[1] for r in con.execute("PRAGMA table_info(oracle_predictions)")}
    assert {"snapshot_ts", "underlying_ticker", "token_symbol", "platform_id",
            "token_contract", "token_price", "token_to_share_ratio",
            "implied_open_price", "last_reference_close", "deviation_bps",
            "source_endpoint", "fetched_at"} <= cols


def test_snapshot_tags_cutoff(tmp_path):
    f = FakeFetcher([tok("0xa", "A", "A", "1")], [px("0xa", "10", "10")])
    now = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)  # Friday
    p = OraclePredictor(tmp_path / "t.db", f, CFG, clock=lambda: now)
    p.snapshot()
    ts, cut = sqlite3.connect(tmp_path / "t.db").execute(
        "SELECT snapshot_ts, cutoff_ts FROM oracle_predictions").fetchone()
    assert ts == "2026-10-02T21:00:00Z" and cut == "2026-10-04T23:59:00Z"


# ---------- config ----------

def test_config_precedence(tmp_path):
    f = tmp_path / "o.env"
    f.write_text("# c\nORACLE_INTERVAL_MIN=7\nORACLE_ENABLED=0\nORACLE_CUTOFF_HOUR=22\n")
    cfg = oracle.load_config(f, env={"ORACLE_CUTOFF_HOUR": "21"})
    assert cfg["ORACLE_INTERVAL_MIN"] == "7"      # file beats default
    assert cfg["ORACLE_CUTOFF_HOUR"] == "21"      # env beats file
    assert cfg["ORACLE_CUTOFF_MINUTE"] == "59"    # default
    assert oracle.is_enabled(cfg) is False
    assert oracle.is_enabled(oracle.load_config(tmp_path / "none.env", env={})) is True


def test_shipped_config_file_parses():
    cfg = oracle.load_config(env={})
    assert cfg["ORACLE_INTERVAL_MIN"] == "5" and cfg["ORACLE_ENABLED"] == "1"


# ---------- cutoff selection ----------

def test_next_cutoff_variants():
    fri = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
    assert next_cutoff(fri) == datetime(2026, 10, 4, 23, 59, tzinfo=UTC)
    sun_before = datetime(2026, 10, 4, 23, 58, tzinfo=UTC)
    assert next_cutoff(sun_before) == datetime(2026, 10, 4, 23, 59, tzinfo=UTC)
    sun_at = datetime(2026, 10, 4, 23, 59, tzinfo=UTC)  # strictly after
    assert next_cutoff(sun_at) == datetime(2026, 10, 11, 23, 59, tzinfo=UTC)
    assert cutoff_for(datetime(2026, 10, 5, 0, 1, tzinfo=UTC)) == datetime(2026, 10, 11, 23, 59, tzinfo=UTC)


def test_cutoff_snapshot_picks_last_at_or_before(tmp_path):
    db = tmp_path / "t.db"
    uni = [tok("0xa", "A", "A", "1")]
    times = [datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
             datetime(2026, 10, 4, 23, 55, tzinfo=UTC),
             datetime(2026, 10, 4, 23, 59, tzinfo=UTC),   # exactly cutoff: included
             datetime(2026, 10, 5, 0, 5, tzinfo=UTC)]     # after: belongs to next cutoff
    for i, t in enumerate(times):
        f = FakeFetcher(uni, [px("0xa", str(100 + i), "100")])
        OraclePredictor(db, f, CFG, clock=lambda t=t: t).snapshot()
    snap = ScoreOracle(db).cutoff_snapshot("2026-10-04T23:59:00Z")
    assert len(snap) == 1
    assert snap[0]["snapshot_ts"] == "2026-10-04T23:59:00Z"
    assert snap[0]["implied_open_price"] == "102"


# ---------- scoring math ----------

def test_score_basic():
    s = score_prediction("105", "103", "100")
    assert s["abs_pct_error"] == Decimal("2") / Decimal("103") * 100
    assert s["direction_hit"] == 1 and s["direction_predicted"] == 1


def test_score_direction_miss():
    s = score_prediction("102", "98", "100")  # predicted up, real down
    assert s["direction_hit"] == 0
    assert s["abs_pct_error"] == Decimal("4") / Decimal("98") * 100


def test_score_zero_deviation_edge_cases():
    both_flat = score_prediction("100", "100", "100")
    assert both_flat["direction_hit"] == 1 and both_flat["abs_pct_error"] == 0
    pred_flat_real_up = score_prediction("100", "101", "100")
    assert pred_flat_real_up["direction_hit"] == 0
    pred_up_real_flat = score_prediction("101", "100", "100")
    assert pred_up_real_flat["direction_hit"] == 0


def test_score_rejects_bad_inputs():
    with pytest.raises(ValueError):
        score_prediction("100", "0", "100")
    with pytest.raises(ValueError):
        score_prediction("abc", "100", "100")


def test_score_no_float_artifacts():
    s = score_prediction("0.3", "0.1", "0.2")
    assert s["abs_error"] == Decimal("0.2")


# ---------- real-opens input + fetch helper ----------

def test_load_real_opens_json_and_csv(tmp_path):
    j = tmp_path / "o.json"
    j.write_text(json.dumps({"AAPL": "103.5", "TSLA": 98}))
    assert load_real_opens(j) == {"AAPL": "103.5", "TSLA": "98"}
    c = tmp_path / "o.csv"
    c.write_text("ticker,open\nAAPL,103.5\nTSLA,98\n\n")
    assert load_real_opens(c) == {"AAPL": "103.5", "TSLA": "98"}
    c2 = tmp_path / "o2.csv"
    c2.write_text("AAPL,103.5\n")  # headerless
    assert load_real_opens(c2) == {"AAPL": "103.5"}
    bad = tmp_path / "o.txt"
    bad.write_text("x")
    with pytest.raises(ValueError):
        load_real_opens(bad)


def test_fetch_real_opens_only_open_sessions():
    uni = [tok("0xa", "A", "AAPL", "1", status="regular"),
           tok("0xb", "B", "TSLA", "1", status="overnight"),
           tok("0xc", "C", "NFLX", "10", status="regular")]
    f = FakeFetcher(uni, [px("0xa", "1", "103"), px("0xb", "1", "98"), px("0xc", "1", "500")])
    assert fetch_real_opens(f, regular_statuses=("regular",)) == {"AAPL": "103", "NFLX": "500"}


def test_write_reputation_interface(tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    con.executescript(oracle_score.SCHEMA)
    rec = {"run_id": "r", "scored_at": "2026-10-05T14:00:00Z", "cutoff_ts": "2026-10-04T23:59:00Z",
           "snapshot_ts": "2026-10-04T23:55:00Z", "row_kind": "underlying",
           "underlying_ticker": "AAPL", "predicted_open": "105", "actual_open": "103",
           "abs_pct_error": "1.94", "direction_hit": 1}
    rid = write_reputation(rec, con)
    assert rid >= 1
    got = con.execute("SELECT underlying_ticker, predicted_open, actual_open, direction_hit "
                      "FROM oracle_reputation").fetchone()
    assert got == ("AAPL", "105", "103", 1)


# ---------- end to end ----------

def test_self_test_passes():
    result = oracle_score.self_test()
    assert result["summary"]["n"] == 3


def test_scorer_writes_reputation_per_underlying(tmp_path):
    db = tmp_path / "t.db"
    uni = [tok("0xa", "AAPLon", "AAPL", "1"), tok("0xb", "AAPLx", "AAPL", "1")]
    f = FakeFetcher(uni, [px("0xa", "104", "100"), px("0xb", "106", "100")])
    OraclePredictor(db, f, CFG, clock=lambda: datetime(2026, 10, 4, 23, 50, tzinfo=UTC)).snapshot()
    rows = ScoreOracle(db, clock=lambda: datetime(2026, 10, 5, 14, tzinfo=UTC)).score(
        "2026-10-04T23:59:00Z", {"AAPL": "105"}, run_id="t")
    agg = [r for r in rows if r["row_kind"] == "underlying"]
    assert len(agg) == 1 and agg[0]["predicted_open"] == "105"
    assert Decimal(agg[0]["abs_pct_error"]) == 0 and agg[0]["direction_hit"] == 1
    n = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM oracle_reputation WHERE scored_at IS NOT NULL").fetchone()[0]
    assert n == 3


# ---------- independent reference close (underlying-market) ----------

class RefFetcher(FakeFetcher):
    """Fake that also exposes the independent underlying-market reference."""

    def __init__(self, universe, prices, refs):
        super().__init__(universe, prices)
        self.refs, self.ref_calls = refs, []

    def fetch_underlying_reference(self, contract):
        self.ref_calls.append(contract)
        return self.refs.get(contract)


def test_reference_by_ticker_overrides_derived_reference():
    # rwa/price referencePrice is derived (=implied) so deviation would be 0;
    # the independent underlying close must win.
    uni = [tok("0xa", "AAPLon", "AAPL", "1")]
    r = build_prediction_rows(uni, [px("0xa", "105", "105")], 1, "t", "c",
                              reference_by_ticker={"AAPL": "100"})[0]
    assert r["last_reference_close"] == "100" and Decimal(r["deviation_bps"]) == 500
    assert r["reference_source"] == "rwa/underlying-market"


def test_missing_independent_reference_gives_null_not_circular():
    uni = [tok("0xa", "AAPLB", "AAPL", "1")]
    r = build_prediction_rows(uni, [px("0xa", "105", "105")], 1, "t", "c",
                              reference_by_ticker={})[0]
    assert r["last_reference_close"] is None and r["deviation_bps"] is None
    assert r["reference_source"] is None


def test_predictor_uses_cached_independent_reference(tmp_path):
    db = tmp_path / "t.db"
    uni = [tok("0xa", "AAPLon", "AAPL", "1"), tok("0xb", "AAPLx", "AAPL", "1"),
           tok("0xc", "TSLAon", "TSLA", "0.5")]
    prices = [px("0xa", "105", "105"), px("0xb", "106", "106"), px("0xc", "48", "96")]
    # 0xc has ratio 0.5: underlying-market reports 200, per-share close = 200 * 0.5
    f = RefFetcher(uni, prices, {"0xa": "100", "0xc": "200"})
    cfg = dict(CFG, ORACLE_REF_TTL_S="1800", ORACLE_REF_MAX_PER_TICK="100",
               ORACLE_REF_STAGGER_S="0")
    t0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    p = OraclePredictor(db, f, cfg, clock=lambda: t0)
    p.snapshot(t0)
    assert sorted(f.ref_calls) == ["0xa", "0xc"]  # one call per underlying
    con = sqlite3.connect(db)
    devs = dict(con.execute("SELECT token_symbol, deviation_bps FROM oracle_predictions"))
    assert Decimal(devs["AAPLon"]) == 500 and Decimal(devs["AAPLx"]) == 600
    assert Decimal(devs["TSLAon"]) == Decimal("-400")
    f.ref_calls.clear()
    p.snapshot(datetime(2026, 10, 3, 12, 5, tzinfo=UTC))  # inside TTL → no API calls
    assert f.ref_calls == []
    OraclePredictor(db, f, cfg).snapshot(datetime(2026, 10, 3, 12, 10, tzinfo=UTC))
    assert f.ref_calls == []  # cache persisted in SQLite across processes
    p.snapshot(datetime(2026, 10, 3, 13, 0, tzinfo=UTC))  # past TTL → refresh
    assert sorted(f.ref_calls) == ["0xa", "0xc"]


def test_reference_fetch_falls_back_to_other_issuer_and_caps_per_tick(tmp_path):
    uni = [tok("0xb", "AAPLB", "AAPL", "1"), tok("0xa", "AAPLon", "AAPL", "1"),
           tok("0xc", "TSLAon", "TSLA", "1"), tok("0xd", "NFLXon", "NFLX", "1")]
    prices = [px(a, "10", "10") for a in ("0xa", "0xb", "0xc", "0xd")]
    f = RefFetcher(uni, prices, {"0xa": "9", "0xc": "9", "0xd": "9"})  # 0xb (bstock) → None
    cfg = dict(CFG, ORACLE_REF_TTL_S="1800", ORACLE_REF_MAX_PER_TICK="2",
               ORACLE_REF_STAGGER_S="0")
    p = OraclePredictor(tmp_path / "t.db", f, cfg)
    p.snapshot(datetime(2026, 10, 3, 12, 0, tzinfo=UTC))
    con = sqlite3.connect(tmp_path / "t.db")
    cached = {r[0] for r in con.execute("SELECT underlying_ticker FROM oracle_reference")}
    assert len(cached) == 2  # cap = 2 underlyings refreshed per tick
    for i in (1, 2):  # next ticks finish the warm-up; AAPL falls back 0xb → 0xa
        p.snapshot(datetime(2026, 10, 3, 12, 5 * i, tzinfo=UTC))
    cached = dict(sqlite3.connect(tmp_path / "t.db").execute(
        "SELECT underlying_ticker, reference_close FROM oracle_reference"))
    assert cached == {"AAPL": "9", "TSLA": "9", "NFLX": "9"}


def test_reference_source_column_migrates_old_table(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE oracle_predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id INTEGER NOT NULL,
        snapshot_ts TEXT NOT NULL, cutoff_ts TEXT NOT NULL, underlying_ticker TEXT,
        token_symbol TEXT, token_contract TEXT, platform_id TEXT, token_price TEXT,
        token_to_share_ratio TEXT, implied_open_price TEXT NOT NULL,
        last_reference_close TEXT, deviation_bps TEXT,
        deviation_flag INTEGER NOT NULL DEFAULT 0, market_status TEXT,
        source_endpoint TEXT, fetched_at TEXT);""")
    con.commit(); con.close()
    OraclePredictor(db, FakeFetcher([], []), CFG)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(oracle_predictions)")}
    assert "reference_source" in cols
