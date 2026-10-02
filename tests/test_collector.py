"""Unit tests for the spread-tick collector storage + tick building (no network).

A fake client serves canned ``rwa/tokens`` / ``rwa/price`` OCResult envelopes;
storage is exercised against real SQLite + JSONL files in a tmp dir.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stockgap import collector as collector_mod
from stockgap.collector import SpreadCollector

UNIVERSE = [
    {  # rich leg: implied 105 (210/2)
        "binanceChainId": "56", "tokenContractAddress": "0xAAA", "platformId": "ondo",
        "tokenSymbol": "TSLAon", "underlyingTicker": "TSLA", "tokenToShareRatio": "2",
        "statusInfo": {"openState": True, "marketStatus": "open", "reasonCode": "TRADING"},
    },
    {  # cheap leg: implied 100
        "binanceChainId": "56", "tokenContractAddress": "0xBBB", "platformId": "bstock",
        "tokenSymbol": "TSLAB", "underlyingTicker": "TSLA", "tokenToShareRatio": "1",
        "statusInfo": {"openState": False, "marketStatus": "closed", "reasonCode": "CLOSED"},
    },
    {  # single issuer, no cross-issuer row expected
        "binanceChainId": "56", "tokenContractAddress": "0xCCC", "platformId": "ondo",
        "tokenSymbol": "AAPLon", "underlyingTicker": "AAPL", "tokenToShareRatio": "1",
        "statusInfo": {"openState": True, "marketStatus": "open", "reasonCode": "TRADING"},
    },
]

PRICE_ROWS = [
    {"binanceChainId": "56", "tokenContractAddress": "0xAAA", "platformId": "ondo",
     "tokenPrice": "210", "referencePrice": "102", "tokenPriceUpdatedAt": 1790899916470},
    {"binanceChainId": "56", "tokenContractAddress": "0xBBB", "platformId": "bstock",
     "tokenPrice": "100", "referencePrice": "101", "tokenPriceUpdatedAt": 1790899916470},
    {"binanceChainId": "56", "tokenContractAddress": "0xCCC", "platformId": "ondo",
     "tokenPrice": "150", "referencePrice": "148", "tokenPriceUpdatedAt": 1790899916470},
]


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
        return 404, {}, ""


class TestBuildTickRows(unittest.TestCase):
    def setUp(self):
        self.collector = SpreadCollector(
            db_path=":memory:", jsonl_path=Path(tempfile.mkdtemp()) / "t.jsonl",
            client=FakeClient(),
        )

    def test_spread_rows_use_ratio_normalization(self):
        spreads, cross = self.collector.build_tick_rows(UNIVERSE, PRICE_ROWS, tick_id=1)
        by_sym = {r["token_symbol"]: r for r in spreads}
        self.assertEqual(by_sym["TSLAon"]["implied_share_price"], "105")
        self.assertTrue(by_sym["TSLAon"]["spread_bps"].startswith("294.1176"))
        self.assertEqual(by_sym["TSLAB"]["implied_share_price"], "100")

    def test_cross_issuer_row_for_multi_platform_underlying(self):
        spreads, cross = self.collector.build_tick_rows(UNIVERSE, PRICE_ROWS, tick_id=1)
        self.assertEqual(len(cross), 1)
        row = cross[0]
        self.assertEqual(row["underlying_ticker"], "TSLA")
        self.assertEqual(row["rich_symbol"], "TSLAon")
        self.assertEqual(row["cheap_symbol"], "TSLAB")
        self.assertEqual(row["n_issuers"], 2)

    def test_market_status_recorded(self):
        spreads, _ = self.collector.build_tick_rows(UNIVERSE, PRICE_ROWS, tick_id=1)
        by_sym = {r["token_symbol"]: r for r in spreads}
        self.assertEqual(by_sym["TSLAon"]["market_status"], "open")
        self.assertEqual(by_sym["TSLAB"]["open_state"], 0)

    def test_rows_carry_source_endpoint_and_fetched_at(self):
        spreads, cross = self.collector.build_tick_rows(
            UNIVERSE, PRICE_ROWS, tick_id=1, fetched_at="2026-10-02T00:00:00.000Z")
        for r in spreads + cross:
            self.assertEqual(r["source_endpoint"],
                             "/api/v1/dex/market/rwa/price")
            self.assertEqual(r["fetched_at"], "2026-10-02T00:00:00.000Z")

    def test_missing_ratio_row_skipped(self):
        universe = UNIVERSE + [{
            "binanceChainId": "56", "tokenContractAddress": "0xDDD", "platformId": "ondo",
            "tokenSymbol": "BADon", "underlyingTicker": "BAD",  # no tokenToShareRatio
            "statusInfo": {"openState": True, "marketStatus": "open", "reasonCode": "TRADING"},
        }]
        prices = PRICE_ROWS + [
            {"binanceChainId": "56", "tokenContractAddress": "0xDDD", "platformId": "ondo",
             "tokenPrice": "150", "referencePrice": "148", "tokenPriceUpdatedAt": 1},
        ]
        spreads, cross = self.collector.build_tick_rows(universe, prices, tick_id=1)
        self.assertEqual({r["token_symbol"] for r in spreads}, {"TSLAon", "TSLAB", "AAPLon"})


class TestStorageRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "ticks.db"
        self.jsonl = self.tmp / "ticks.jsonl"
        self.collector = SpreadCollector(
            db_path=self.db, jsonl_path=self.jsonl, client=FakeClient(),
        )

    def test_sqlite_rows_written(self):
        spreads, cross = self.collector.build_tick_rows(UNIVERSE, PRICE_ROWS, tick_id=7)
        self.collector.record_tick("2026-10-02T00:00:00.000Z", 7, spreads, cross)
        con = sqlite3.connect(self.db)
        n_spreads = con.execute("SELECT COUNT(*) FROM spread_ticks WHERE tick_id=7").fetchone()[0]
        n_cross = con.execute("SELECT COUNT(*) FROM cross_issuer_ticks WHERE tick_id=7").fetchone()[0]
        con.close()
        self.assertEqual(n_spreads, 3)
        self.assertEqual(n_cross, 1)

    def test_jsonl_lines_written(self):
        spreads, cross = self.collector.build_tick_rows(UNIVERSE, PRICE_ROWS, tick_id=7)
        self.collector.record_tick("2026-10-02T00:00:00.000Z", 7, spreads, cross)
        lines = [json.loads(l) for l in self.jsonl.read_text().splitlines()]
        self.assertEqual(len(lines), 4)  # 3 spreads + 1 cross-issuer
        types = sorted(l["type"] for l in lines)
        self.assertEqual(types, ["cross_issuer_tick", "spread_tick", "spread_tick", "spread_tick"])
        self.assertTrue(all(l["tick_id"] == 7 for l in lines))

    def test_run_once_end_to_end(self):
        meta = self.collector.run_once()
        self.assertEqual(meta["n_spreads"], 3)
        self.assertEqual(meta["n_cross"], 1)
        con = sqlite3.connect(self.db)
        total = con.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0]
        con.close()
        self.assertEqual(total, 3)

    def test_tick_ids_increment(self):
        m1 = self.collector.run_once()
        m2 = self.collector.run_once()
        self.assertEqual(m2["tick_id"], m1["tick_id"] + 1)


class TestTickLoopResilience(unittest.TestCase):
    """REGRESSION: a temporarily locked DB must never kill the 24/7 daemon.

    Before the fix the collector died on every 'database is locked' (176 in
    data/collector.log; watchdog restarted it 90x in 18h) — the tick-failure
    handler's own ``_meta_set`` raised too and escaped the loop. On today's
    (pre-fix) code every test here fails: that is the point.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "ticks.db"
        self.jsonl = self.tmp / "ticks.jsonl"

    def _collector(self, client=None):
        # Shrink the lock-wait budget so locked scenarios fail in ms, not 10s.
        with mock.patch("stockgap.db.DEFAULT_BUSY_TIMEOUT_MS", 25):
            return SpreadCollector(
                db_path=str(self.db), jsonl_path=self.jsonl,
                client=client or FakeClient(), tick_interval=1.0,
            )

    def _hold_write_lock(self):
        lock = sqlite3.connect(str(self.db))
        lock.execute("BEGIN IMMEDIATE")
        lock.execute("INSERT INTO collector_meta VALUES ('lock', '1')")
        return lock

    def test_locked_db_survives_failed_tick_then_next_tick_succeeds(self):
        holder = {"lock": None}

        class ReleasingClient(FakeClient):
            """Releases the competing lock at the START of tick 2's write window
            (tick 2's price fetch — the universe is cached across ticks)."""

            price_calls = 0

            def request(self, method, path, params=None):
                if path == collector_mod.PRICE_ENDPOINT:
                    ReleasingClient.price_calls += 1
                    if ReleasingClient.price_calls == 2 and holder["lock"] is not None:
                        holder["lock"].execute("ROLLBACK")  # lock free again for tick 2
                return FakeClient.request(self, method, path, params)

        collector = self._collector(client=ReleasingClient())  # creates the schema
        holder["lock"] = self._hold_write_lock()
        with mock.patch("stockgap.collector.time.sleep"):  # no real waiting
            collector.run_forever(max_ticks=2)  # must SURVIVE, not raise

        # tick 2 ran after the release
        self.assertEqual(ReleasingClient.price_calls, 2)
        con = sqlite3.connect(str(self.db))
        # failed tick fully rolled back (exactly one tick's rows persisted) ...
        self.assertEqual(
            (con.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0],
             con.execute("SELECT COUNT(*) FROM cross_issuer_ticks").fetchone()[0]),
            (3, 1),
        )
        # ... and its tick_id was reclaimed: tick_ids stay contiguous
        self.assertEqual(
            [r[0] for r in con.execute("SELECT DISTINCT tick_id FROM spread_ticks")],
            [1],
        )
        con.close()

    def test_meta_set_never_raises_when_locked(self):
        collector = self._collector()
        lock = self._hold_write_lock()
        try:
            collector._meta_set("probe", "1")  # must swallow the lock error
            self.assertFalse(collector._con.in_transaction)  # rolled back
        finally:
            lock.execute("ROLLBACK")
            lock.close()

    def test_bounded_retry_on_record_tick(self):
        collector = self._collector()
        orig = collector.record_tick
        state = {"calls": 0}

        def locked_twice(ts, tick_id, spreads, cross):
            state["calls"] += 1
            if state["calls"] <= len(collector_mod.RECORD_RETRY_DELAYS):
                raise sqlite3.OperationalError("database is locked")
            return orig(ts, tick_id, spreads, cross)

        collector.record_tick = locked_twice
        with mock.patch("stockgap.collector.time.sleep"):  # short sleeps, fast test
            meta = collector.run_once()
        self.assertEqual(meta["tick_id"], 1)  # same tick, no id burned by retries
        # 2 retries (RECORD_RETRY_DELAYS), then success — never unbounded
        self.assertEqual(state["calls"], len(collector_mod.RECORD_RETRY_DELAYS) + 1)

    def test_failed_tick_reclaims_tick_id(self):
        collector = self._collector()
        orig = collector.record_tick
        state = {"calls": 0}

        def always_locked(*args, **kwargs):
            state["calls"] += 1
            raise sqlite3.OperationalError("database is locked")

        collector.record_tick = always_locked
        with mock.patch("stockgap.collector.time.sleep"):
            with self.assertRaises(sqlite3.OperationalError):
                collector.run_once()
        self.assertEqual(state["calls"], len(collector_mod.RECORD_RETRY_DELAYS) + 1)
        self.assertFalse(collector._con.in_transaction)  # failed tick rolled back
        self.assertEqual(collector._tick_id, 0)  # documented policy: id reclaimed

        collector.record_tick = orig  # lock gone: next tick must reuse the id
        meta = collector.run_once()
        self.assertEqual(meta["tick_id"], 1)

    def test_exits_nonzero_after_max_consecutive_failures(self):
        collector = self._collector()
        collector.max_consecutive_failures = 3

        def boom():
            raise RuntimeError("api down")

        collector.run_once = boom
        with mock.patch("stockgap.collector.time.sleep"):
            with self.assertRaises(SystemExit) as cm:
                collector.run_forever(max_ticks=10)
        self.assertNotEqual(cm.exception.code, 0)  # watchdog sees a real failure


if __name__ == "__main__":
    unittest.main()
