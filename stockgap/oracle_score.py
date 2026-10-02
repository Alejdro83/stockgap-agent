"""StockGap weekend oracle — Monday scoring + reputation (ERC-8004 style).

On Monday, when the US market opens, the oracle's weekend predictions meet
reality: for each underlying, fetch the REAL opening price and score the
prediction frozen at the cutoff snapshot (default: latest snapshot at or
before Sunday 23:59 UTC).

Per prediction row (each tokenized issuer) and per underlying (mean across
issuers) this computes:

    abs_error       = |predicted_open - actual_open|
    abs_pct_error   = abs_error / actual_open * 100          (percent)
    signed_error_bps= (predicted_open - actual_open) / actual_open * 10000
    direction_*     = sign(price - last_reference_close)     (1 up, -1 down, 0 flat)
    direction_hit   = 1 iff predicted direction == actual direction
    oracle_score    = max(0, 100 - abs_pct_error)            (0..100)

Results land in ``oracle_reputation`` and are submitted as verifiable
feedback through a FeedbackWriter — an ERC-8004-style interface. The shipped
writer is DB-backed (mock ledger); the on-chain writer is future work and
stubbed behind the same interface. No signing/broadcasting in this module.

Scoring input for actual opens: a JSON file mapping ticker -> real open price
(e.g. from scripts/fetch_underlying_market.py output). ``--self-test`` proves
the scoring math on synthetic data (same fixtures as tests/test_oracle.py).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from . import db  # noqa: E402 — shared WAL/busy_timeout connection factory

__all__ = [
    "DbFeedbackWriter",
    "fetch_real_opens",
    "load_real_opens",
    "write_reputation",
    "OnchainFeedbackWriter",
    "ScoreOracle",
    "score_prediction",
    "self_test",
]

log = logging.getLogger("stockgap.oracle_score")

BPS = Decimal("10000")

REPO = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS oracle_reputation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    cutoff_ts TEXT NOT NULL,
    snapshot_ts TEXT NOT NULL,
    row_kind TEXT NOT NULL DEFAULT 'token',
    underlying_ticker TEXT,
    token_symbol TEXT,
    token_contract TEXT,
    platform_id TEXT,
    predicted_open TEXT NOT NULL,
    actual_open TEXT NOT NULL,
    last_reference_close TEXT,
    signed_error TEXT,
    signed_error_bps TEXT,
    abs_error TEXT,
    abs_pct_error TEXT,
    direction_predicted INTEGER,
    direction_actual INTEGER,
    direction_hit INTEGER,
    oracle_score TEXT,
    feedback_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_oracle_rep_run ON oracle_reputation (run_id);
CREATE INDEX IF NOT EXISTS idx_oracle_rep_cutoff ON oracle_reputation (cutoff_ts);

CREATE TABLE IF NOT EXISTS oracle_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_ref TEXT NOT NULL,
    run_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _dec(value: Any, name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        value = repr(value)
    try:
        result = Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{name}: not a number: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{name}: not finite: {value!r}")
    return result


def _direction(price: Decimal, reference: Optional[Decimal]) -> int:
    """+1 gap-up vs reference, -1 gap-down, 0 flat/unknown reference."""
    if reference is None or reference <= 0 or price == reference:
        return 0
    return 1 if price > reference else -1


def score_prediction(
    predicted_open: Any,
    actual_open: Any,
    last_reference_close: Any = None,
) -> dict:
    """Pure scoring math for one prediction vs. one real open.

    Raises ValueError on missing/non-positive actual or predicted prices.
    ``direction_hit`` is 1 only when the predicted and actual move directions
    relative to ``last_reference_close`` match (flat==flat counts as a hit).
    """
    predicted = _dec(predicted_open, "predicted_open")
    actual = _dec(actual_open, "actual_open")
    if predicted <= 0:
        raise ValueError(f"predicted_open: must be positive, got {predicted}")
    if actual <= 0:
        raise ValueError(f"actual_open: must be positive, got {actual}")

    reference: Optional[Decimal] = None
    if last_reference_close is not None and str(last_reference_close).strip():
        reference = _dec(last_reference_close, "last_reference_close")

    signed_error = predicted - actual
    abs_error = abs(signed_error)
    abs_pct_error = abs_error / actual * Decimal("100")
    signed_error_bps = signed_error / actual * BPS
    dir_pred = _direction(predicted, reference)
    dir_actual = _direction(actual, reference)
    score = max(Decimal("0"), Decimal("100") - abs_pct_error)
    return {
        "predicted_open": predicted,
        "actual_open": actual,
        "last_reference_close": reference,
        "signed_error": signed_error,
        "signed_error_bps": signed_error_bps,
        "abs_error": abs_error,
        "abs_pct_error": abs_pct_error,
        "direction_predicted": dir_pred,
        "direction_actual": dir_actual,
        "direction_hit": 1 if dir_pred == dir_actual else 0,
        "oracle_score": score,
    }


_REPUTATION_COLS = (
    "run_id", "scored_at", "cutoff_ts", "snapshot_ts", "row_kind",
    "underlying_ticker", "token_symbol", "token_contract", "platform_id",
    "predicted_open", "actual_open", "last_reference_close", "signed_error",
    "signed_error_bps", "abs_error", "abs_pct_error", "direction_predicted",
    "direction_actual", "direction_hit", "oracle_score", "feedback_ref",
)


def write_reputation(record: Mapping[str, Any], con: sqlite3.Connection) -> int:
    """Persist ONE reputation record; returns its row id.

    This is the single seam for reputation storage. Today: local SQLite
    (``oracle_reputation``). Future ERC-8004 on-chain reputation write plugs in
    here (or via OnchainFeedbackWriter) — callers never change. No chain calls.
    """
    params = {c: record.get(c) for c in _REPUTATION_COLS}
    for required in ("run_id", "scored_at", "cutoff_ts", "snapshot_ts", "predicted_open", "actual_open"):
        if params[required] is None:
            raise ValueError(f"write_reputation: missing {required}")
    params["row_kind"] = params["row_kind"] or "token"
    cols = ", ".join(_REPUTATION_COLS)
    marks = ", ".join(f":{c}" for c in _REPUTATION_COLS)
    cur = con.execute(f"INSERT INTO oracle_reputation ({cols}) VALUES ({marks})", params)
    return int(cur.lastrowid)


# ---------------- real-open inputs ----------------

def load_real_opens(path: Any) -> dict:
    """Read real opens {ticker: price-string} from a .json or .csv file.

    JSON: ``{"AAPL": "103.5"}``. CSV: ``ticker,open`` rows (header optional).
    Values stay strings — Decimal conversion happens in the scorer, never float.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("real-opens JSON must be an object {ticker: price}")
        return {str(k).strip(): str(v).strip() for k, v in raw.items()}
    if suffix == ".csv":
        out: dict = {}
        for row in csv.reader(path.read_text().splitlines()):
            if len(row) < 2 or not row[0].strip():
                continue
            ticker, price = row[0].strip(), row[1].strip()
            try:
                Decimal(price)
            except Exception:  # noqa: BLE001 — header row or junk
                continue
            out[ticker] = price
        return out
    raise ValueError(f"unsupported real-opens file type {suffix!r} (use .json or .csv)")


def fetch_real_opens(
    fetcher: Any,
    regular_statuses: Sequence[str] = ("regular", "open", "trading"),
) -> dict:
    """BEST-EFFORT live fetch of the underlying's open: {ticker: referencePrice}.

    ``rwa/underlying-market`` exposes only ``referencePrice`` (no OHLC/open
    field — see DX-LOG.md), so the proxy for "the Monday open" is the
    ``referencePrice`` from ``rwa/price`` read while the underlying session is
    regular; tickers still in overnight/paused/closed are omitted rather than
    guessed. Run it right after 13:30 UTC (09:30 NY) Monday. For exact opens
    use --real-opens with an external feed.
    """
    universe = fetcher.load_universe()
    open_contracts: dict = {}
    for u in universe:
        status = str((u.get("statusInfo") or {}).get("marketStatus") or "").lower()
        if status in regular_statuses and u.get("tokenContractAddress"):
            open_contracts[str(u["tokenContractAddress"]).lower()] = u.get("underlyingTicker")
    if not open_contracts:
        return {}
    opens: dict = {}
    for row in fetcher.fetch_price_rows(list(open_contracts)):
        ticker = open_contracts.get(str(row.get("tokenContractAddress", "")).lower())
        ref = row.get("referencePrice")
        if ticker and ref is not None and str(ref).strip() and ticker not in opens:
            opens[ticker] = str(ref).strip()
    return opens


# ---------------- feedback writers (ERC-8004-style) ----------------

class DbFeedbackWriter:
    """Mock feedback ledger: persists feedback payloads in ``oracle_feedback``.

    Same interface the future on-chain writer will implement; the returned
    ``feedback_ref`` is the receipt callers persist in oracle_reputation.
    """

    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def submit(self, run_id: str, payload: Mapping[str, Any]) -> str:
        ref = f"mock:{uuid.uuid4()}"
        self._con.execute(
            "INSERT INTO oracle_feedback (feedback_ref, run_id, payload, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                ref,
                run_id,
                json.dumps(payload, default=str, sort_keys=True),
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
        self._con.commit()
        return ref


class OnchainFeedbackWriter:
    """Future work: submit feedback as ERC-8004 on-chain attestation.

    Deliberately NOT implemented here (no signing/broadcasting in scope);
    kept so the scorer's interface is final. Implement ``submit`` to sign and
    broadcast an ERC-8004 feedback tx and return its receipt as feedback_ref.
    """

    def submit(self, run_id: str, payload: Mapping[str, Any]) -> str:
        raise NotImplementedError(
            "ERC-8004 on-chain feedback write is future work — use DbFeedbackWriter"
        )


# ---------------- scorer ----------------

class ScoreOracle:
    """Scores the cutoff snapshot against real Monday opens → reputation rows.

    ``clock`` is injectable for tests (returns tz-aware datetime).
    """

    def __init__(
        self,
        db_path: Any,
        feedback_writer: Any = None,
        clock: Optional[Any] = None,
    ):
        self.db_path = str(db_path)
        self._con = db.connect(self.db_path)
        db.ensure_wal(self._con)  # busy_timeout already set by db.connect
        self._con.executescript(SCHEMA)
        self._con.commit()
        self.feedback_writer = feedback_writer or DbFeedbackWriter(self._con)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------------- cutoff snapshot ----------------

    def cutoff_snapshot(self, cutoff_ts: str) -> List[dict]:
        """Latest prediction per token at or before ``cutoff_ts``.

        Only rows tagged with this cutoff (the oracle tags every snapshot with
        the next Sunday cutoff it feeds) participate — the frozen weekend
        prediction. Multiple snapshots over the weekend collapse to the last
        one per token, which is exactly "the snapshot at the cutoff".
        """
        con = self._con
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT * FROM (
                SELECT p.*, ROW_NUMBER() OVER (
                    PARTITION BY token_contract ORDER BY snapshot_ts DESC, id DESC
                ) AS rn
                FROM oracle_predictions p
                WHERE cutoff_ts = ? AND snapshot_ts <= ?
            ) WHERE rn = 1
            """,
            (cutoff_ts, cutoff_ts),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- scoring ----------------

    def score(
        self,
        cutoff_ts: str,
        actual_opens: Mapping[str, Any],
        run_id: Optional[str] = None,
    ) -> List[dict]:
        """Score the cutoff snapshot against real opens {ticker: price}.

        Writes per-token rows (row_kind='token') and per-underlying aggregate
        rows (row_kind='underlying', mean predicted across issuers) to
        oracle_reputation, each with a feedback receipt from the writer.
        Returns the written row dicts.
        """
        run_id = run_id or f"score-{uuid.uuid4().hex[:12]}"
        scored_at = self.clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snapshot_rows = self.cutoff_snapshot(cutoff_ts)

        by_ticker: dict = {}
        written: List[dict] = []
        for row in snapshot_rows:
            ticker = row.get("underlying_ticker")
            if ticker not in actual_opens:
                log.info("no actual open for %s — skipping %s", ticker, row.get("token_symbol"))
                continue
            try:
                s = score_prediction(
                    row["implied_open_price"],
                    actual_opens[ticker],
                    row.get("last_reference_close"),
                )
            except ValueError as exc:
                log.warning("skip %s: %s", ticker, exc)
                continue
            written.append(self._write_row(run_id, scored_at, cutoff_ts, row, "token", s))
            by_ticker.setdefault(ticker, []).append((row, s))

        # Per-underlying aggregate: mean predicted open across issuers.
        for ticker, entries in sorted(by_ticker.items()):
            predicted_mean = sum(e[1]["predicted_open"] for e in entries) / len(entries)
            ref = entries[0][1]["last_reference_close"]
            s = score_prediction(predicted_mean, actual_opens[ticker], ref)
            proto = dict(entries[0][0])
            proto["token_symbol"] = f"{ticker}:AGGREGATE({len(entries)})"
            proto["token_contract"] = f"aggregate:{ticker}"
            proto["implied_open_price"] = str(predicted_mean)
            written.append(self._write_row(run_id, scored_at, cutoff_ts, proto, "underlying", s))

        agg = self._aggregate(written)
        for w in written:
            ref = self.feedback_writer.submit(
                run_id,
                {
                    "type": "oracle_prediction_score",
                    "standard": "ERC-8004-style-feedback(mock)",
                    "run_id": run_id,
                    "cutoff_ts": cutoff_ts,
                    "underlying_ticker": w["underlying_ticker"],
                    "row_kind": w["row_kind"],
                    "predicted_open": w["predicted_open"],
                    "actual_open": w["actual_open"],
                    "abs_pct_error": w["abs_pct_error"],
                    "direction_hit": w["direction_hit"],
                    "oracle_score": w["oracle_score"],
                    "agent": "stockgap-weekend-oracle",
                },
            )
            w["feedback_ref"] = ref
            self._con.execute(
                "UPDATE oracle_reputation SET feedback_ref = ? WHERE id = ?",
                (ref, w["id"]),
            )
        self._con.commit()
        log.info(
            "scored run=%s cutoff=%s rows=%d mean_abs_pct_error=%s direction_hit_rate=%s",
            run_id, cutoff_ts, len(written),
            agg.get("mean_abs_pct_error"), agg.get("direction_hit_rate"),
        )
        return written

    def _write_row(
        self, run_id: str, scored_at: str, cutoff_ts: str,
        pred_row: Mapping[str, Any], row_kind: str, s: Mapping[str, Any],
    ) -> dict:
        record = {
            "run_id": run_id,
            "scored_at": scored_at,
            "cutoff_ts": cutoff_ts,
            "snapshot_ts": pred_row.get("snapshot_ts"),
            "row_kind": row_kind,
            "underlying_ticker": pred_row.get("underlying_ticker"),
            "token_symbol": pred_row.get("token_symbol"),
            "token_contract": pred_row.get("token_contract"),
            "platform_id": pred_row.get("platform_id"),
            "predicted_open": str(s["predicted_open"]),
            "actual_open": str(s["actual_open"]),
            "last_reference_close": str(s["last_reference_close"]) if s["last_reference_close"] is not None else None,
            "signed_error": str(s["signed_error"]),
            "signed_error_bps": str(s["signed_error_bps"]),
            "abs_error": str(s["abs_error"]),
            "abs_pct_error": str(s["abs_pct_error"]),
            "direction_predicted": s["direction_predicted"],
            "direction_actual": s["direction_actual"],
            "direction_hit": s["direction_hit"],
            "oracle_score": str(s["oracle_score"]),
            "feedback_ref": None,
        }
        record["id"] = write_reputation(record, self._con)
        return record

    def _aggregate(self, rows: Sequence[Mapping[str, Any]]) -> dict:
        token_rows = [r for r in rows if r["row_kind"] == "token"]
        if not token_rows:
            return {}
        mean_err = sum(Decimal(r["abs_pct_error"]) for r in token_rows) / len(token_rows)
        hit_rate = sum(r["direction_hit"] for r in token_rows) / len(token_rows)
        return {
            "n": len(token_rows),
            "mean_abs_pct_error": mean_err,
            "direction_hit_rate": hit_rate,
        }

    def summary(self, run_id: str) -> dict:
        """Aggregate reputation stats for one scoring run (token rows)."""
        rows = self._con.execute(
            "SELECT abs_pct_error, direction_hit FROM oracle_reputation"
            " WHERE run_id = ? AND row_kind = 'token'",
            (run_id,),
        ).fetchall()
        return self._aggregate(
            [{"abs_pct_error": r[0], "direction_hit": r[1], "row_kind": "token"} for r in rows]
        )


# ---------------- self-test ----------------

# Synthetic fixture shared with tests/test_oracle.py: fixed prices/timestamps.
SELF_TEST_CUTOFF = "2026-10-04T23:59:00Z"  # a Sunday 23:59 UTC
SELF_TEST_UNIVERSE = [
    {"tokenContractAddress": "0xaaa", "tokenSymbol": "AAPLon", "underlyingTicker": "AAPL",
     "platformId": "x", "tokenToShareRatio": "1", "statusInfo": {"marketStatus": "CLOSED"}},
    {"tokenContractAddress": "0xbbb", "tokenSymbol": "TSLAon", "underlyingTicker": "TSLA",
     "platformId": "x", "tokenToShareRatio": "0.5", "statusInfo": {"marketStatus": "CLOSED"}},
    {"tokenContractAddress": "0xccc", "tokenSymbol": "NFLXon", "underlyingTicker": "NFLX",
     "platformId": "x", "tokenToShareRatio": "10", "statusInfo": {"marketStatus": "CLOSED"}},
]
SELF_TEST_PRICES = [
    # AAPL: predicted 105 vs close 100 → +500 bps, gap-up.
    {"tokenContractAddress": "0xaaa", "tokenPrice": "105", "referencePrice": "100"},
    # TSLA: tokenPrice 48 / ratio 0.5 → predicted 96 vs close 100 → -400 bps, gap-down.
    {"tokenContractAddress": "0xbbb", "tokenPrice": "48", "referencePrice": "100"},
    # NFLX: 5020/10 → predicted 502 vs close 500 → +40 bps, gap-up.
    {"tokenContractAddress": "0xccc", "tokenPrice": "5020", "referencePrice": "500"},
]
# Monday's REAL opens (synthetic):
SELF_TEST_ACTUALS = {"AAPL": "103", "TSLA": "98", "NFLX": "500"}
# Expected, hand-computed:
#   AAPL: pred 105 vs actual 103 → abs_pct_error = 2/103*100 ≈ 1.9417%, dirs up==up → hit
#   TSLA: pred  96 vs actual  98 → abs_pct_error = 2/98*100  ≈ 2.0408%, dirs down==down → hit
#   NFLX: pred 502 vs actual 500 → abs_pct_error = 2/500*100 = 0.4%, up vs flat(500==ref) → miss


def self_test(db_path: Any = None) -> dict:
    """Prove the scoring math on synthetic data. Raises AssertionError on any
    mismatch; returns the score rows for inspection. Wired to ``--self-test``.
    """
    import tempfile

    from .oracle import OraclePredictor  # local import

    if db_path is None:  # ":memory:" would give predictor and scorer separate DBs
        db_path = Path(tempfile.mkdtemp(prefix="oracle-selftest-")) / "selftest.db"

    class _FakeFetcher:
        def load_universe(self):
            return SELF_TEST_UNIVERSE

        def fetch_price_rows(self, contracts):
            return SELF_TEST_PRICES

    fixed_now = datetime(2026, 10, 2, 21, 0, 0, tzinfo=timezone.utc)  # Friday 21:00 UTC
    predictor = OraclePredictor(
        db_path,
        _FakeFetcher(),
        config={"ORACLE_INTERVAL_MIN": "5", "ORACLE_CUTOFF_WEEKDAY": "sunday",
                "ORACLE_CUTOFF_HOUR": "23", "ORACLE_CUTOFF_MINUTE": "59",
                "ORACLE_DEVIATION_FLAG_BPS": "100"},
        clock=lambda: fixed_now,
    )
    snapshot_id, n = predictor.snapshot()
    assert n == 3, f"expected 3 prediction rows, got {n}"

    scorer = ScoreOracle(db_path, clock=lambda: fixed_now)
    rows = scorer.score(SELF_TEST_CUTOFF, SELF_TEST_ACTUALS, run_id="self-test")
    token_rows = {r["underlying_ticker"]: r for r in rows if r["row_kind"] == "token"}

    assert set(token_rows) == {"AAPL", "TSLA", "NFLX"}, token_rows.keys()
    # AAPL: 105 predicted, 103 actual
    a = token_rows["AAPL"]
    assert Decimal(a["predicted_open"]) == Decimal("105"), a
    assert Decimal(a["abs_pct_error"]) == Decimal("2") / Decimal("103") * 100, a
    assert a["direction_hit"] == 1, a
    # TSLA: ratio 0.5 → 48/0.5 = 96 predicted vs 98 actual, down vs down
    t = token_rows["TSLA"]
    assert Decimal(t["predicted_open"]) == Decimal("96"), t
    assert t["direction_predicted"] == -1 and t["direction_actual"] == -1, t
    assert t["direction_hit"] == 1, t
    # NFLX: 502 predicted vs 500 actual, ref 500 → predicted up, actual flat → miss
    nf = token_rows["NFLX"]
    assert Decimal(nf["abs_pct_error"]) == Decimal("0.4"), nf
    assert nf["direction_predicted"] == 1 and nf["direction_actual"] == 0, nf
    assert nf["direction_hit"] == 0, nf
    # Aggregate rows exist per underlying and every row got a feedback receipt
    assert sum(1 for r in rows if r["row_kind"] == "underlying") == 3, rows
    assert all(r.get("feedback_ref", "").startswith("mock:") for r in rows), rows
    summary = scorer.summary("self-test")
    assert summary["n"] == 3, summary
    return {"rows": rows, "summary": summary}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score weekend-oracle predictions vs real Monday opens")
    parser.add_argument("--self-test", action="store_true",
                        help="prove the scoring math on synthetic data and exit")
    parser.add_argument("--cutoff-ts", default=None,
                        help="cutoff snapshot ISO timestamp (default: latest cutoff in DB)")
    parser.add_argument("--real-opens", "--actuals", dest="real_opens", default=None,
                        help="real opens file: .json {ticker: price} or .csv ticker,open")
    parser.add_argument("--fetch-opens", action="store_true",
                        help="best-effort: fetch live referencePrice for tickers in regular session")
    parser.add_argument("--db", default=os.environ.get("STOCKGAP_DB", str(REPO / "data" / "spread_ticks.db")))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.self_test:
        result = self_test()
        print(json.dumps({
            "self_test": "PASS",
            "scores": [
                {k: str(v) for k, v in r.items() if k in (
                    "underlying_ticker", "row_kind", "predicted_open", "actual_open",
                    "abs_pct_error", "direction_hit", "oracle_score")}
                for r in result["rows"]
            ],
            "summary": {k: str(v) for k, v in result["summary"].items()},
        }, indent=2))
        return 0

    if args.real_opens:
        actuals = load_real_opens(args.real_opens)
    elif args.fetch_opens:
        from .collector import SpreadCollector  # local import: needs credentials
        from .client import BinanceWeb3Client, load_credentials
        api_key, secret, _, _ = load_credentials()
        actuals = fetch_real_opens(SpreadCollector(
            args.db, REPO / "data" / "ticks.jsonl", BinanceWeb3Client(api_key, secret)))
        print(f"fetched {len(actuals)} opens (best-effort referencePrice)")
    else:
        parser.error("--real-opens FILE or --fetch-opens required (or use --self-test)")
    if not actuals:
        parser.error("no real opens available — nothing to score")

    con = db.connect(args.db)
    db.ensure_wal(con)  # write path (oracle_reputation/oracle_feedback)
    cutoff_ts = args.cutoff_ts
    if cutoff_ts is None:
        row = con.execute("SELECT MAX(cutoff_ts) FROM oracle_predictions").fetchone()
        cutoff_ts = row[0] if row else None
        if cutoff_ts is None:
            parser.error("no predictions in DB and no --cutoff-ts given")

    scorer = ScoreOracle(args.db)
    rows = scorer.score(cutoff_ts, actuals)
    print(json.dumps({
        "cutoff_ts": cutoff_ts,
        "rows_scored": len(rows),
        "summary": {k: str(v) for k, v in scorer.summary(rows[0]["run_id"]).items()} if rows else {},
    }, indent=2))
    return 0


if __name__ == "__main__":
    if __package__ in (None, ""):  # direct run: python3 stockgap/oracle_score.py
        import sys
        sys.path.insert(0, str(REPO))
        __package__ = "stockgap"
    raise SystemExit(main())
