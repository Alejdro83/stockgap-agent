"""Dashboard routes: each returns 200 with key content; empty/missing DB never crashes."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from stockgap import dashboard

SPREAD = """CREATE TABLE spread_ticks (id INTEGER PRIMARY KEY, ts_utc TEXT, tick_id INTEGER,
 underlying_ticker TEXT, token_symbol TEXT, token_contract TEXT, platform_id TEXT, token_price TEXT,
 token_to_share_ratio TEXT, reference_price TEXT, implied_share_price TEXT, spread_bps TEXT,
 market_status TEXT, open_state INTEGER{extra})"""
CROSS = """CREATE TABLE cross_issuer_ticks (id INTEGER PRIMARY KEY, ts_utc TEXT, tick_id INTEGER,
 underlying_ticker TEXT, n_issuers INTEGER, cheap_symbol TEXT, cheap_platform TEXT, cheap_implied TEXT,
 rich_symbol TEXT, rich_platform TEXT, rich_implied TEXT, spread_abs TEXT, spread_bps TEXT)"""
ORACLE = """CREATE TABLE oracle_predictions (id INTEGER PRIMARY KEY, snapshot_id INTEGER, snapshot_ts TEXT,
 deviation_flag INTEGER DEFAULT 0)"""


def iso(delta_s=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_db(path, *, extra_cols="", age_s=10, rows=True, reputation=False):
    con = sqlite3.connect(path)
    con.execute(SPREAD.format(extra=extra_cols))
    con.execute(CROSS)
    con.execute(ORACLE)
    con.execute("CREATE TABLE collector_meta (key TEXT PRIMARY KEY, value TEXT)")
    if rows:
        ts = iso(age_s)
        con.execute("INSERT INTO collector_meta VALUES ('last_tick_ts', ?), ('universe_count', '488')", (ts,))
        base = ("INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol, platform_id, "
                "token_price, token_to_share_ratio, reference_price, spread_bps, market_status, open_state) "
                "VALUES (?, 1, ?, ?, ?, '10', ?, '9.5', ?, ?, ?)")
        con.execute(base, (ts, "AAA", "AAAon", "ondo", "1", "120.5", "overnight", 0))
        con.execute(base, (ts, "BBB", "BBBB", "bstock", "1.0023", "-300", "", 1))
        con.execute("INSERT INTO cross_issuer_ticks (ts_utc, tick_id, underlying_ticker, n_issuers, cheap_symbol, "
                    "cheap_platform, cheap_implied, rich_symbol, rich_platform, rich_implied, spread_bps) "
                    "VALUES (?, 1, 'SOXL', 2, 'SOXLon', 'ondo', '154', 'SOXLB', 'bstock', '156', '108.4')", (ts,))
        con.execute("INSERT INTO oracle_predictions (snapshot_id, snapshot_ts) VALUES (1, ?)", (ts,))
    if reputation:
        con.execute("CREATE TABLE oracle_reputation (id INTEGER PRIMARY KEY, underlying_ticker TEXT, token_symbol TEXT, "
                    "predicted_open TEXT, actual_open TEXT, abs_pct_error TEXT, direction_hit INTEGER, oracle_score TEXT)")
        con.execute("INSERT INTO oracle_reputation VALUES (1,'AAA','AAAon','10','10.1','1.0',1,'99')")
    con.commit()
    con.close()


@pytest.fixture
def client_for(tmp_path, monkeypatch):
    def _make(**kw):
        db = tmp_path / "t.db"
        make_db(str(db), **kw)
        monkeypatch.setenv("STOCKGAP_DB", str(db))
        return TestClient(dashboard.app)
    return _make


def test_index_has_tabs_and_loading_text(client_for):
    r = client_for().get("/")
    assert r.status_code == 200
    for t in ("Gaps", "Cross-issuer", "Oracle", "System", "Loading"):
        assert t in r.text


def test_gaps_ranked_with_metric_platform_ratio(client_for):
    r = client_for().get("/frag/gaps")
    assert r.status_code == 200
    assert "spread_bps" in r.text and "ondo" in r.text and "bstock" in r.text
    assert r.text.index("BBBB") < r.text.index("AAAon")  # |-300| ranks first
    assert "ratio 1.0023" in r.text and "ratio 1.0000" not in r.text


def test_gaps_closed_only_filter(client_for):
    r = client_for().get("/frag/gaps?closed_only=1")
    assert "AAAon" in r.text and "BBBB" not in r.text


def test_gaps_prefers_spread_bps_real_when_present(client_for):
    c = client_for(extra_cols=", spread_bps_real TEXT, reference_kind TEXT")
    r = c.get("/frag/gaps")
    assert r.status_code == 200 and "spread_bps_real" in r.text  # falls back per-row when NULL
    assert "AAAon" in r.text


def test_cross(client_for):
    r = client_for().get("/frag/cross")
    assert r.status_code == 200 and "SOXL" in r.text and "108.40 bps" in r.text


def test_oraculo_explainer_count_and_no_reputation(client_for):
    r = client_for().get("/frag/oraculo")
    assert r.status_code == 200
    assert "while the market sleeps" in r.text and "predictions today" in r.text
    assert "No score yet" in r.text


def test_oraculo_with_reputation(client_for):
    r = client_for(reputation=True).get("/frag/oraculo")
    assert "AAAon" in r.text and "No score yet" not in r.text


def test_sistema_green_txs_and_root_cause(client_for):
    r = client_for(age_s=10).get("/frag/sistema")
    assert r.status_code == 200
    assert "GREEN" in r.text and "OK — last tick" in r.text and "Stale data" not in r.text
    for h in dashboard.REAL_TXS:
        assert h in r.text
    assert r.text.count("REVERTED") == 3
    assert "2-second route expiry embedded in the calldata" in r.text
    assert "SPREAD_THRESHOLD_BPS" in r.text


def test_sistema_stale_is_red_with_warning(client_for):
    r = client_for(age_s=900).get("/frag/sistema")
    assert "RED" in r.text and "STALLED" in r.text and "Stale data" in r.text


def test_empty_db_renders_friendly_messages(client_for):
    c = client_for(rows=False)
    assert "No collector ticks yet" in c.get("/frag/gaps").text
    assert "No cross-issuer data" in c.get("/frag/cross").text
    assert "No oracle predictions yet" in c.get("/frag/oraculo").text
    r = c.get("/frag/sistema")
    assert r.status_code == 200 and "NO DATA" in r.text


def test_missing_db_shows_error_banner_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKGAP_DB", str(tmp_path / "nope.db"))
    c = TestClient(dashboard.app)
    for p in ("gaps", "cross", "oraculo", "sistema"):
        r = c.get(f"/frag/{p}")
        assert r.status_code == 200 and "Data error" in r.text


def test_garbage_db_shows_error_banner(tmp_path, monkeypatch):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite file" * 100)
    monkeypatch.setenv("STOCKGAP_DB", str(bad))
    r = TestClient(dashboard.app).get("/frag/gaps")
    assert r.status_code == 200 and "Data error" in r.text


def test_health_badge_levels():
    assert dashboard.health_badge(30)["level"] == "GREEN"
    assert dashboard.health_badge(200)["level"] == "YELLOW"
    assert dashboard.health_badge(400)["level"] == "RED"
    assert dashboard.health_badge(None)["level"] == "RED"


def test_gaps_independent_rows_show_the_numbers_behind_the_real_spread(client_for, tmp_path):
    c = client_for(extra_cols=", spread_bps_real TEXT, reference_kind TEXT, reference_close_independent TEXT")
    con = sqlite3.connect(tmp_path / "t.db")
    con.execute("UPDATE spread_ticks SET reference_kind='independent', reference_close_independent='123.4567', "
                "implied_share_price='120.0', spread_bps_real='-279.0' WHERE token_symbol='AAAon'")
    con.commit()
    con.close()
    r = c.get("/frag/gaps")
    assert "independent close 123.4567" in r.text and "120.0000 per share" in r.text
    assert "vs ref 9.5000" in r.text  # the non-independent row keeps the old line
