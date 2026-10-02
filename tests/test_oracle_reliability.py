"""Oracle write-path reliability (Claude-reviewed SQLite fix, 2026-10-02).

(b) REGRESSION: ``_reference_closes`` used to hold the SQLite write
    transaction across EVERY network fetch (unbounded hold time — up to ~150
    calls with a 0.22s stagger + network latency), which blocked every other
    writer and produced the 'database is locked' storms. The fetch-first
    restructure must keep ``con.in_transaction`` False inside the fetcher and
    write everything in ONE short transaction afterwards, whose hold time is
    logged and must stay well under 1s.
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from stockgap.oracle import OraclePredictor

UTC = timezone.utc
CFG = {
    "ORACLE_INTERVAL_MIN": "5", "ORACLE_CUTOFF_WEEKDAY": "sunday",
    "ORACLE_CUTOFF_HOUR": "23", "ORACLE_CUTOFF_MINUTE": "59",
    "ORACLE_DEVIATION_FLAG_BPS": "100",
    "ORACLE_REF_STAGGER_S": "0",  # no sleeps between fake fetches
}


def tok(addr, sym, ticker, ratio):
    return {"tokenContractAddress": addr, "tokenSymbol": sym, "underlyingTicker": ticker,
            "platformId": "ondo", "tokenToShareRatio": ratio,
            "statusInfo": {"marketStatus": "CLOSED"}}


def px(addr, token_price, ref):
    return {"tokenContractAddress": addr, "tokenPrice": token_price, "referencePrice": ref}


class SpyFetcher:
    """Fake fetcher recording con.in_transaction for every fetch call."""

    def __init__(self, universe, prices, ref=None):
        self.universe, self.prices, self.ref = universe, prices, (ref or {})
        self.con: Any = None  # bound to the predictor's connection after construction
        self.in_txn_during_fetch = []

    def _check(self):
        if self.con is not None:
            self.in_txn_during_fetch.append(self.con.in_transaction)

    def load_universe(self):
        self._check()
        return self.universe

    def fetch_price_rows(self, contracts):
        self._check()
        return self.prices

    def fetch_underlying_reference(self, contract):
        self._check()
        return self.ref.get(str(contract).lower(), {}).get("close")


def test_no_open_transaction_during_any_fetch(tmp_path):
    """(b) NO SQLite transaction may be open while the fetcher is on the network."""
    uni = [tok("0xa", "AAPLon", "AAPL", "1"), tok("0xb", "TSLAon", "TSLA", "1")]
    prices = [px("0xa", "105", "105"), px("0xb", "48", "48")]
    f = SpyFetcher(uni, prices, {"0xa": {"close": "100"}, "0xb": {"close": "50"}})
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    p = OraclePredictor(tmp_path / "t.db", f, dict(CFG, ORACLE_REF_MAX_PER_TICK="10"),
                        clock=lambda: now)
    f.con = p._con

    p.snapshot()

    assert len(f.in_txn_during_fetch) >= 3  # universe + prices + reference fetches
    assert not any(f.in_txn_during_fetch), (
        "fetch ran inside a SQLite transaction — the write lock must never be "
        "held across a network call"
    )
    # the writes still land, in one short transaction after the fetches
    con = sqlite3.connect(tmp_path / "t.db")
    assert con.execute("SELECT COUNT(*) FROM oracle_reference").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM oracle_predictions").fetchone()[0] == 2
    con.close()


def test_write_txn_hold_time_well_under_one_second_and_chunked(tmp_path, caplog):
    """Chunked BEGIN IMMEDIATE batches, each hold << 1s (logged as 'write txn held')."""
    n = 3500  # > WRITE_BATCH_ROWS (3000) => exercises the chunking loop too
    uni = [tok(f"0x{i:04x}", f"S{i}", f"T{i}", "1") for i in range(n)]
    prices = [px(f"0x{i:04x}", "105", "100") for i in range(n)]
    p = OraclePredictor(tmp_path / "t.db", SpyFetcher(uni, prices), CFG)

    with caplog.at_level(logging.INFO, logger="stockgap.oracle"):
        p.snapshot()

    holds = [
        float(m.group(1))
        for m in (
            re.search(r"write txn held ([0-9.]+)s", rec.getMessage())
            for rec in caplog.records
        )
        if m
    ]
    assert len(holds) >= 2, "expected one logged hold per write batch (chunked)"
    assert max(holds) < 1.0, f"write transaction held too long: {max(holds)}s"

    con = sqlite3.connect(tmp_path / "t.db")
    assert con.execute("SELECT COUNT(*) FROM oracle_predictions").fetchone()[0] == n
    con.close()
