"""Unit tests for the spread-tick collector storage + tick building (no network).

A fake client serves canned ``rwa/tokens`` / ``rwa/price`` OCResult envelopes;
storage is exercised against real SQLite + JSONL files in a tmp dir.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
