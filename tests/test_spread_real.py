"""Tests for the REAL spread metric vs the INDEPENDENT underlying close.

The bug under test (verified live 2026-10-02, see DX-LOG.md): the
``rwa/price`` ``referencePrice`` is DERIVED (== tokenPrice/tokenToShareRatio),
so the historical ``spread_bps`` against it is structurally ~0 bps for all 488
tokens. The INDEPENDENT reference is ``rwa/underlying-market``'s
``referencePrice`` — which arrives divided AGAIN by the ratio and must be
multiplied back (``stockgap.spread.independent_share_close``) to obtain the
true underlying share close (without it NFLX showed +90000 bps).

Covered here:
* the ×ratio normalization (ratios 10, 1, 0.066667),
* derived vs independent spread distinction (incl. the NFLX regression),
* honest labelling (``reference_kind='derived'|'independent'``) incl. the
  cold-cache fallback and historical-row migration,
* the alert metric switch (``ALERT_METRIC=real|derived``, default ``real``).
"""

import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from stockgap.alerts import (
    find_crossings,
    format_spread_alert,
    load_config,
    metric_column,
)
from stockgap.collector import SpreadCollector
from stockgap.oracle import DERIVED_REFERENCE_SOURCE, REFERENCE_SOURCE, load_reference_closes
from stockgap.spread import gross_spread, implied_share_price, independent_share_close, real_spread

ORACLE_REFERENCE_DDL = """
CREATE TABLE IF NOT EXISTS oracle_reference (
    underlying_ticker TEXT PRIMARY KEY,
    reference_close TEXT NOT NULL,
    source_endpoint TEXT,
    fetched_epoch REAL NOT NULL,
    fetched_at TEXT
);
"""

# Legacy (pre-2026-10-02) spread_ticks shape: no provenance, no real columns.
OLD_SCHEMA = """
CREATE TABLE spread_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    tick_id INTEGER NOT NULL,
    underlying_ticker TEXT,
    token_symbol TEXT,
    token_contract TEXT,
    platform_id TEXT,
    token_price TEXT,
    token_to_share_ratio TEXT,
    reference_price TEXT,
    token_price_updated_at INTEGER,
    implied_share_price TEXT,
    spread_abs TEXT,
    spread_bps TEXT,
    spread_pct TEXT,
    market_status TEXT,
    open_state INTEGER
);
"""

# NFLXon: 1 token = 10 shares (ratio 10) — the +90000 bps regression case.
UNIVERSE = [
    {
        "binanceChainId": "56", "tokenContractAddress": "0xAAA", "platformId": "ondo",
        "tokenSymbol": "NFLXon", "underlyingTicker": "NFLX", "tokenToShareRatio": "10",
        "statusInfo": {"openState": True, "marketStatus": "open", "reasonCode": "TRADING"},
    },
    {
        "binanceChainId": "56", "tokenContractAddress": "0xBBB", "platformId": "ondo",
        "tokenSymbol": "ENLVon", "underlyingTicker": "ENLV", "tokenToShareRatio": "0.066667",
        "statusInfo": {"openState": True, "marketStatus": "open", "reasonCode": "TRADING"},
    },
    {  # cache-cold ticker: no independent close available
        "binanceChainId": "56", "tokenContractAddress": "0xCCC", "platformId": "xstock",
        "tokenSymbol": "COHRx", "underlyingTicker": "COHR", "tokenToShareRatio": "1",
        "statusInfo": {"openState": True, "marketStatus": "open", "reasonCode": "TRADING"},
    },
]

# rwa/price rows: referencePrice is the DERIVED one (== tokenPrice/ratio).
PRICE_ROWS = [
    {"binanceChainId": "56", "tokenContractAddress": "0xAAA", "platformId": "ondo",
     "tokenPrice": "12500", "referencePrice": "1250", "tokenPriceUpdatedAt": 1790899916470},
    {"binanceChainId": "56", "tokenContractAddress": "0xBBB", "platformId": "ondo",
     "tokenPrice": "700", "referencePrice": "10500", "tokenPriceUpdatedAt": 1790899916470},
    {"binanceChainId": "56", "tokenContractAddress": "0xCCC", "platformId": "xstock",
     "tokenPrice": "320.4", "referencePrice": "320.4", "tokenPriceUpdatedAt": 1790899916470},
]

# Per-share independent closes (already normalized ×ratio at fetch time).
REFERENCE_BY_TICKER = {"NFLX": "1180", "ENLV": "10000"}


class FakeClient:
    """Serves canned OCResult envelopes like BinanceWeb3Client.request."""

    def __init__(self, universe=UNIVERSE, price_rows=PRICE_ROWS):
        self.universe = universe
        self.price_rows = price_rows
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, dict(params or {})))
        if path.endswith("/rwa/tokens"):
            return 200, {"code": 0, "msg": "ok", "data": self.universe, "success": True}, ""
        if path.endswith("/rwa/price"):
            wanted = (params or {}).get("tokenContractAddresses", "").split(",")
            rows = [r for r in self.price_rows if r["tokenContractAddress"] in wanted]
            return 200, {"code": 0, "msg": "ok", "data": rows, "success": True}, ""
        return 404, {"code": 1, "msg": "not found"}, ""


class TestRatioNormalization(unittest.TestCase):
    """underlying-market's referencePrice × tokenToShareRatio = per-share close."""

    def test_ratio_10_multiplies_back(self):  # NFLX: 1 token = 10 shares
        self.assertEqual(independent_share_close("125", "10"), Decimal("1250"))

    def test_ratio_1_is_identity(self):
        self.assertEqual(independent_share_close("381.03", "1"), Decimal("381.03"))

    def test_ratio_fractional_multiplies_back(self):  # 0.066667 shares per token
        self.assertEqual(
            independent_share_close("1500", "0.066667"), Decimal("100.0005")
        )

    def test_missing_or_nonpositive_inputs_rejected(self):
        for bad_ref, bad_ratio in [(None, "1"), ("", "1"), ("0", "1"), ("-5", "1"),
                                   ("100", None), ("100", ""), ("100", "0"), ("100", "-2")]:
            with self.assertRaises(ValueError):
                independent_share_close(bad_ref, bad_ratio)


class TestRealSpreadVsDerived(unittest.TestCase):
    """derived referencePrice is a mirror of the token — the independent close is not."""

    def test_derived_reference_is_structurally_zero(self):
        # referencePrice == tokenPrice/ratio ⇒ derived spread ~0 for EVERY token
        derived = gross_spread("12500", "10", "1250")
        self.assertEqual(derived["spread_bps"], 0)

    def test_independent_close_gives_real_gap(self):
        rs = real_spread("12500", "10", "1180")
        self.assertEqual(rs["implied_share_price"], Decimal("1250"))
        self.assertEqual(rs["reference_close_independent"], Decimal("1180"))
        self.assertAlmostEqual(float(rs["spread_bps_real"]), 593.22, delta=0.01)
        self.assertEqual(rs["reference_kind"], "independent")

    def test_nflx_regression_normalized_not_90000bps(self):
        # raw underlying-market value is divided by the ratio AGAIN: ×10 restores
        # the true close (1250) → ~0 bps. Using the raw value gives +90000 bps.
        raw_underlying_ref = "125"
        close = independent_share_close(raw_underlying_ref, "10")
        self.assertEqual(close, Decimal("1250"))
        self.assertAlmostEqual(
            float(real_spread("12500", "10", close)["spread_bps_real"]), 0.0, delta=0.001
        )
        self.assertAlmostEqual(
            float(real_spread("12500", "10", raw_underlying_ref)["spread_bps_real"]),
            90000.0, delta=0.001,
        )

    def test_implied_share_price_unchanged_by_new_metric(self):
        self.assertEqual(implied_share_price("700", "0.066667"),
                         Decimal("700") / Decimal("0.066667"))

    def test_real_spread_rejects_missing_or_zero_close(self):
        for bad in (None, "", "0", "-1"):
            with self.assertRaises(ValueError):
                real_spread("12500", "10", bad)


class TestTickRowLabelling(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self._tmp.name) / "ticks.db")
        self.collector = SpreadCollector(
            db_path=self.db,
            jsonl_path=Path(self._tmp.name) / "ticks.jsonl",
            client=FakeClient(),
        )

    def tearDown(self):
        self.collector._con.close()
        self._tmp.cleanup()

    def test_warm_cache_rows_labelled_independent_with_real_spread(self):
        spreads, _ = self.collector.build_tick_rows(
            UNIVERSE, PRICE_ROWS, tick_id=1,
            reference_by_ticker=dict(REFERENCE_BY_TICKER),
        )
        by_sym = {r["token_symbol"]: r for r in spreads}
        nflx = by_sym["NFLXon"]
        self.assertEqual(nflx["reference_kind"], "independent")
        self.assertEqual(nflx["reference_source"], REFERENCE_SOURCE)
        self.assertEqual(nflx["reference_close_independent"], "1180")
        self.assertAlmostEqual(float(nflx["spread_bps_real"]), 593.22, delta=0.01)
        # old columns untouched (dashboard contract): derived values still there
        self.assertEqual(nflx["reference_price"], "1250")
        self.assertEqual(nflx["spread_bps"], "0")
        self.assertEqual(nflx["implied_share_price"], "1250")

    def test_cold_cache_rows_fall_back_labelled_derived(self):
        spreads, _ = self.collector.build_tick_rows(
            UNIVERSE, PRICE_ROWS, tick_id=1, reference_by_ticker={}
        )
        for r in spreads:
            self.assertIsNone(r["spread_bps_real"], r["token_symbol"])
            self.assertIsNone(r["reference_close_independent"])
            self.assertEqual(r["reference_kind"], "derived")
            self.assertEqual(r["reference_source"], DERIVED_REFERENCE_SOURCE)
            self.assertIsNotNone(r["spread_bps"])  # old metric keeps working

    def test_mixed_cache_labels_per_row(self):
        spreads, _ = self.collector.build_tick_rows(
            UNIVERSE, PRICE_ROWS, tick_id=1,
            reference_by_ticker=dict(REFERENCE_BY_TICKER),
        )
        by_sym = {r["token_symbol"]: r for r in spreads}
        self.assertEqual(by_sym["NFLXon"]["reference_kind"], "independent")
        self.assertEqual(by_sym["ENLVon"]["reference_kind"], "independent")
        self.assertAlmostEqual(float(by_sym["ENLVon"]["spread_bps_real"]), 500.0, delta=1.0)
        # COHR has no cache entry → labelled derived, no fake real value
        self.assertEqual(by_sym["COHRx"]["reference_kind"], "derived")
        self.assertIsNone(by_sym["COHRx"]["spread_bps_real"])

    def test_unusable_close_falls_back_labelled(self):
        spreads, _ = self.collector.build_tick_rows(
            UNIVERSE, PRICE_ROWS, tick_id=1, reference_by_ticker={"NFLX": "0"}
        )
        nflx = [r for r in spreads if r["token_symbol"] == "NFLXon"][0]
        self.assertEqual(nflx["reference_kind"], "derived")
        self.assertIsNone(nflx["spread_bps_real"])

    def test_record_tick_roundtrip_keeps_new_columns(self):
        spreads, cross = self.collector.build_tick_rows(
            UNIVERSE, PRICE_ROWS, tick_id=1,
            reference_by_ticker=dict(REFERENCE_BY_TICKER),
        )
        self.collector.record_tick("2026-10-02T02:00:00.000Z", 1, spreads, cross)
        self.collector._con.row_factory = sqlite3.Row
        try:
            rows = {r["token_symbol"]: dict(r) for r in self.collector._con.execute(
                "SELECT * FROM spread_ticks").fetchall()}
        finally:
            self.collector._con.row_factory = None
        self.assertAlmostEqual(float(rows["NFLXon"]["spread_bps_real"]), 593.22, delta=0.01)
        self.assertEqual(rows["NFLXon"]["reference_kind"], "independent")
        self.assertEqual(rows["NFLXon"]["reference_close_independent"], "1180")
        self.assertEqual(rows["COHRx"]["reference_kind"], "derived")
        self.assertIsNone(rows["COHRx"]["spread_bps_real"])

    def test_run_once_uses_reference_cache_when_warm(self):
        con = sqlite3.connect(self.db)
        con.executescript(ORACLE_REFERENCE_DDL)
        now = time.time()
        for ticker, close in REFERENCE_BY_TICKER.items():
            con.execute(
                "INSERT OR REPLACE INTO oracle_reference"
                " (underlying_ticker, reference_close, source_endpoint, fetched_epoch, fetched_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (ticker, close, "rwa/underlying-market", now, "2026-10-02T02:00:00Z"),
            )
        con.commit()
        con.close()
        meta = self.collector.run_once()
        self.assertEqual(meta["n_spreads"], 3)
        self.assertEqual(meta["n_reference_independent"], 2)
        con = sqlite3.connect(self.db)
        rows = {r[0]: r for r in con.execute(
            "SELECT token_symbol, spread_bps_real, reference_kind FROM spread_ticks")}
        con.close()
        self.assertIsNotNone(rows["NFLXon"][1])
        self.assertEqual(rows["NFLXon"][2], "independent")
        self.assertIsNone(rows["COHRx"][1])
        self.assertEqual(rows["COHRx"][2], "derived")


class TestHistoricalRowMigration(unittest.TestCase):
    def test_old_rows_labelled_derived_and_real_columns_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "old.db")
            con = sqlite3.connect(db)
            con.executescript(OLD_SCHEMA)
            con.execute(
                "INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker,"
                " token_symbol, token_contract, platform_id, token_price,"
                " token_to_share_ratio, reference_price, token_price_updated_at,"
                " implied_share_price, spread_abs, spread_bps, spread_pct,"
                " market_status, open_state) VALUES"
                " ('2026-10-01T00:00:00.000Z', 1, 'NFLX', 'NFLXon', '0xAAA', 'ondo',"
                " '12500', '10', '1250', 0, '1250', '0', '0', '0', 'open', 1)"
            )
            con.commit()
            con.close()

            collector = SpreadCollector(
                db_path=db, jsonl_path=Path(tmp) / "t.jsonl", client=FakeClient()
            )
            try:
                con = sqlite3.connect(db)
                cols = {r[1] for r in con.execute("PRAGMA table_info(spread_ticks)")}
                # additive only: old columns must survive untouched
                for old in ("reference_price", "spread_bps", "spread_pct", "implied_share_price"):
                    self.assertIn(old, cols)
                for new in ("spread_bps_real", "reference_close_independent",
                            "reference_source", "reference_kind"):
                    self.assertIn(new, cols)
                row = dict(zip(
                    [c[0] for c in con.execute("SELECT * FROM spread_ticks").description],
                    con.execute("SELECT * FROM spread_ticks").fetchone(),
                ))
                con.close()
                # backfill is impossible → honest labelling, never a fake real value
                self.assertEqual(row["reference_kind"], "derived")
                self.assertEqual(row["reference_source"], DERIVED_REFERENCE_SOURCE)
                self.assertIsNone(row["spread_bps_real"])
                self.assertIsNone(row["reference_close_independent"])
                self.assertEqual(row["spread_bps"], "0")  # old data intact
            finally:
                collector._con.close()


class TestReferenceCacheLoader(unittest.TestCase):
    def test_fresh_loaded_stale_filtered_missing_table_safe(self):
        con = sqlite3.connect(":memory:")
        self.assertEqual(load_reference_closes(con), {})  # no table at all
        con.executescript(ORACLE_REFERENCE_DDL)
        now = time.time()
        con.execute(
            "INSERT INTO oracle_reference VALUES ('NFLX', '1180', 'rwa/underlying-market', ?, ?)",
            (now, "2026-10-02T02:00:00Z"),
        )
        con.execute(
            "INSERT INTO oracle_reference VALUES ('COHR', '320.4', 'rwa/underlying-market', ?, ?)",
            (now - 3600, "2026-10-02T01:00:00Z"),
        )
        con.commit()
        got = load_reference_closes(con, now_epoch=now, max_age_s=1800.0)
        self.assertEqual(got, {"NFLX": "1180"})  # COHR is stale ⇒ cache-cold
        self.assertEqual(len(load_reference_closes(con, now_epoch=now, max_age_s=None)), 2)
        con.close()


class TestAlertMetricSwitch(unittest.TestCase):
    def test_default_metric_is_real_and_uses_spread_bps_real(self):
        rows = [{"token_symbol": "X", "spread_bps": "5", "spread_bps_real": "200"}]
        hits = find_crossings(rows, {}, 150, lambda r: r["token_symbol"])  # default real
        self.assertEqual(len(hits), 1)

    def test_real_metric_ignores_derived_only_rows(self):
        # historical / cache-cold rows: huge legacy value but NO real metric
        rows = [{"token_symbol": "X", "spread_bps": "900", "spread_bps_real": None,
                 "reference_kind": "derived"}]
        hits = find_crossings(rows, {}, 150, lambda r: r["token_symbol"], metric="real")
        self.assertEqual(hits, [])

    def test_derived_metric_uses_legacy_column(self):
        rows = [{"token_symbol": "X", "spread_bps": "200", "spread_bps_real": "5"}]
        key = lambda r: r["token_symbol"]  # noqa: E731
        self.assertEqual(len(find_crossings(rows, {}, 150, key, metric="derived")), 1)
        self.assertEqual(find_crossings(rows, {}, 150, key, metric="real"), [])

    def test_load_config_default_and_override_and_validation(self):
        self.assertEqual(load_config(environ={}).alert_metric, "real")
        self.assertEqual(
            load_config(environ={"ALERT_METRIC": "derived"}).alert_metric, "derived"
        )
        self.assertEqual(
            load_config(environ={"ALERT_METRIC": "REAL"}).alert_metric, "real"
        )
        with self.assertRaises(ValueError):
            load_config(environ={"ALERT_METRIC": "banana"})

    def test_metric_column_mapping(self):
        self.assertEqual(metric_column("real"), "spread_bps_real")
        self.assertEqual(metric_column("derived"), "spread_bps")
        with self.assertRaises(ValueError):
            metric_column("banana")

    def test_format_spread_alert_shows_real_metric_and_independent_ref(self):
        row = {
            "token_symbol": "NFLXon", "underlying_ticker": "NFLX", "platform_id": "ondo",
            "token_price": "12500", "token_to_share_ratio": "10", "reference_price": "1250",
            "implied_share_price": "1250", "spread_bps": "0", "spread_pct": "0",
            "spread_bps_real": "593.2", "reference_close_independent": "1180",
            "reference_kind": "independent", "reference_source": "rwa/underlying-market",
            "market_status": "open", "open_state": 1, "ts_utc": "2026-10-02T02:00:00.000Z",
        }
        msg = format_spread_alert(row)
        self.assertIn("593.2", msg)
        self.assertIn("Ref independiente", msg)
        self.assertIn("1180", msg)
        self.assertIn("rwa/underlying-market", msg)

    def test_format_spread_alert_flags_derived_reference(self):
        row = {
            "token_symbol": "COHRx", "underlying_ticker": "COHR", "platform_id": "xstock",
            "token_price": "320.4", "token_to_share_ratio": "1", "reference_price": "320.4",
            "implied_share_price": "320.4", "spread_bps": "0", "spread_pct": "0",
            "spread_bps_real": None, "reference_close_independent": None,
            "reference_kind": "derived", "reference_source": DERIVED_REFERENCE_SOURCE,
            "market_status": "open", "open_state": 1, "ts_utc": "2026-10-02T02:00:00.000Z",
        }
        msg = format_spread_alert(row)
        self.assertIn("DERIVADA", msg)
        self.assertIn("NO informativo", msg)


if __name__ == "__main__":
    unittest.main()
