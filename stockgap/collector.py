"""24/7 spread-tick collector: Binance Web3 ``rwa/price`` → SQLite + JSONL.

Each tick:
1. Universe from ``GET /api/v1/dex/market/rwa/tokens`` (cached, TTL-refreshed;
   provides tokenToShareRatio + market status per token).
2. Batch prices from ``GET /api/v1/dex/market/rwa/price`` —
   ``binanceChainId`` + ``tokenContractAddresses`` (comma-separated), batches
   staggered to respect the 5 RPS / endpoint rate limit.
3. Gross spread per token (tokenPrice / tokenToShareRatio vs referencePrice)
   and cross-issuer spread per underlying ticker → persisted to SQLite and
   appended to JSONL.

Rate limits (verified): 5 RPS per endpoint, 1200 req/min per IP+key — one
batch of ≤ 50 addresses per request keeps a full-universe tick at ~10 requests.
OCResult envelopes report business errors at HTTP 200 — ``code == 0`` is the
success check.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from . import db  # noqa: E402 — shared WAL/busy_timeout connection factory
from .oracle import (  # noqa: E402 — shared reference cache + source labels
    DERIVED_REFERENCE_SOURCE,
    REFERENCE_SOURCE,
    load_reference_closes,
)
from .spread import cross_issuer_spreads, gross_spread, real_spread

__all__ = ["SpreadCollector", "iso_now"]

log = logging.getLogger("stockgap.collector")

# Bounded retry for record_tick: the API data is already fetched, so a locked
# DB costs nothing to retry (1-2 retries with a short sleep).
RECORD_RETRY_DELAYS = (0.5, 1.5)
# Exit non-zero after this many consecutive failed ticks so the watchdog
# restarts a genuinely broken process instead of looping silently forever.
MAX_CONSECUTIVE_FAILURES = 10

# Single price source per metric: all price fields come from this endpoint.
# tokenToShareRatio is a static token property sourced from
# /api/v1/dex/market/rwa/tokens (universe) — see DX-LOG.md for the
# cross-endpoint price-field caveat.
PRICE_ENDPOINT = "/api/v1/dex/market/rwa/price"

SCHEMA = """
CREATE TABLE IF NOT EXISTS spread_ticks (
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
    open_state INTEGER,
    source_endpoint TEXT,
    fetched_at TEXT,
    spread_bps_real TEXT,
    reference_close_independent TEXT,
    reference_source TEXT,
    reference_kind TEXT
);
CREATE TABLE IF NOT EXISTS cross_issuer_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    tick_id INTEGER NOT NULL,
    underlying_ticker TEXT,
    n_issuers INTEGER,
    cheap_symbol TEXT,
    cheap_platform TEXT,
    cheap_implied TEXT,
    rich_symbol TEXT,
    rich_platform TEXT,
    rich_implied TEXT,
    spread_abs TEXT,
    spread_bps TEXT,
    source_endpoint TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS collector_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_spread_tick ON spread_ticks(tick_id);
CREATE INDEX IF NOT EXISTS idx_cross_tick ON cross_issuer_ticks(tick_id);
CREATE INDEX IF NOT EXISTS idx_spread_symbol ON spread_ticks(token_symbol);
CREATE INDEX IF NOT EXISTS idx_cross_underlying ON cross_issuer_ticks(underlying_ticker);
"""


def iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class SpreadCollector:
    """Records spread ticks 24/7 into SQLite + JSONL."""

    def __init__(
        self,
        db_path: Any,
        jsonl_path: Any,
        client: Any = None,
        tick_interval: float = 30.0,
        batch_size: int = 50,
        stagger_s: float = 0.25,
        universe_ttl_s: float = 600.0,
        reference_max_age_s: float = 1800.0,
        use_reference_cache: bool = True,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ):
        self.db_path = str(db_path)
        self.jsonl_path = Path(jsonl_path)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.tick_interval = tick_interval
        self.batch_size = batch_size
        self.stagger_s = stagger_s
        self.universe_ttl_s = universe_ttl_s
        # Independent-close cache (oracle_reference) freshness for the REAL
        # spread metric. Older/missing entries = cache-cold → labelled 'derived'.
        self.reference_max_age_s = reference_max_age_s
        self.use_reference_cache = use_reference_cache
        self.max_consecutive_failures = int(max_consecutive_failures)

        # WAL + busy_timeout(10s) via the shared factory; assert WAL engaged.
        self._con = db.connect(self.db_path)
        db.ensure_wal(self._con)
        self._con.executescript(SCHEMA)
        self._migrate()
        self._con.commit()

        row = self._con.execute("SELECT MAX(tick_id) FROM spread_ticks").fetchone()
        self._tick_id = row[0] or 0

        self._universe: Optional[List[dict]] = None
        self._universe_at = 0.0

    # ---------------- universe + prices ----------------

    def load_universe(self, force: bool = False) -> List[dict]:
        """Token universe from ``rwa/tokens`` (cached). Falls back to cache."""
        now = time.time()
        if (
            not force
            and self._universe
            and now - self._universe_at < self.universe_ttl_s
        ):
            return self._universe
        try:
            status, parsed, _ = self.client.request("GET", "/api/v1/dex/market/rwa/tokens")
            if status == 200 and parsed.get("code") == 0 and isinstance(parsed.get("data"), list):
                fresh: List[dict] = parsed["data"]
                self._universe = fresh
                self._universe_at = now
                self._meta_set("universe_count", str(len(fresh)))
                return fresh
            log.warning("universe fetch failed: http=%s code=%s", status, parsed.get("code"))
        except Exception as exc:  # noqa: BLE001 — keep the collector alive
            log.warning("universe fetch error: %s", exc)
        if self._universe:
            log.warning("sticking with cached universe (%d tokens)", len(self._universe))
            return self._universe
        raise RuntimeError("no universe available and rwa/tokens failed")

    def fetch_price_rows(self, contracts: Sequence[str]) -> List[dict]:
        """Batch ``rwa/price`` (binanceChainId + tokenContractAddresses CSV)."""
        rows: List[dict] = []
        batches = [
            list(contracts)[i : i + self.batch_size]
            for i in range(0, len(contracts), self.batch_size)
        ]
        for i, batch in enumerate(batches):
            if i and self.stagger_s:
                time.sleep(self.stagger_s)  # stay well under 5 RPS/endpoint
            params = {"binanceChainId": "56", "tokenContractAddresses": ",".join(batch)}
            try:
                status, parsed, _ = self.client.request(
                    "GET", "/api/v1/dex/market/rwa/price", params=params
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("price batch %d error: %s", i, exc)
                continue
            if status == 200 and parsed.get("code") == 0 and isinstance(parsed.get("data"), list):
                rows.extend(parsed["data"])
            else:
                log.warning(
                    "price batch %d failed: http=%s code=%s msg=%s",
                    i, status, parsed.get("code"), parsed.get("msg"),
                )
        return rows

    # ---------------- tick building ----------------

    def build_tick_rows(
        self, universe: Sequence[Mapping[str, Any]], price_rows: Sequence[Mapping[str, Any]],
        tick_id: int, fetched_at: Optional[str] = None,
        reference_by_ticker: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[List[dict], List[dict]]:
        """Join live prices with universe metadata → (spread_rows, cross_rows).

        One consistent price source per metric: every price field
        (tokenPrice, referencePrice) comes from the SAME ``rwa/price``
        response; ``tokenToShareRatio`` is a static token property from
        ``rwa/tokens`` (universe). Each row records ``source_endpoint`` and
        ``fetched_at`` — never mix price fields across endpoints blindly.
        String-valued for exact round-trip storage. Unpriced/unknown rows are
        skipped rather than recorded as garbage.

        REAL metric (2026-10-02): ``rwa/price.referencePrice`` is DERIVED from
        the token itself (= tokenPrice/ratio), so ``spread_bps`` against it is
        ~0 by construction. ``reference_by_ticker`` carries the INDEPENDENT
        per-share underlying close (``oracle_reference`` cache, normalized with
        ``stockgap.spread.independent_share_close`` at fetch time). When a
        ticker has one, ``spread_bps_real`` is computed against it and the row
        is labelled ``reference_kind='independent'``; when the cache is cold
        the row falls back to the derived reference and is HONESTLY labelled
        ``reference_kind='derived'`` with ``spread_bps_real`` NULL.
        """
        fetched_at = fetched_at or iso_now()
        meta_by_contract = {
            str(u.get("tokenContractAddress", "")).lower(): u for u in universe
        }
        joined = []
        spread_rows: List[dict] = []
        for row in price_rows:
            addr = str(row.get("tokenContractAddress", "")).lower()
            meta = meta_by_contract.get(addr)
            if meta is None:
                continue
            ratio = meta.get("tokenToShareRatio")
            token_price = row.get("tokenPrice")
            reference = row.get("referencePrice")
            try:
                g = gross_spread(token_price, ratio, reference)
            except ValueError:
                continue
            status_info = meta.get("statusInfo") or {}
            spread_rows.append(
                {
                    "tick_id": tick_id,
                    "underlying_ticker": meta.get("underlyingTicker"),
                    "token_symbol": meta.get("tokenSymbol"),
                    "token_contract": row.get("tokenContractAddress"),
                    "platform_id": meta.get("platformId") or row.get("platformId"),
                    "token_price": str(token_price),
                    "token_to_share_ratio": str(ratio) if ratio is not None else None,
                    "reference_price": str(reference),
                    "token_price_updated_at": row.get("tokenPriceUpdatedAt"),
                    "implied_share_price": str(g["implied_share_price"]),
                    "spread_abs": str(g["spread_abs"]),
                    "spread_bps": str(g["spread_bps"]),
                    "spread_pct": str(g["spread_pct"]),
                    "market_status": status_info.get("marketStatus"),
                    "open_state": 1 if status_info.get("openState") else 0,
                    "source_endpoint": PRICE_ENDPOINT,
                    "fetched_at": fetched_at,
                    # Real metric defaults: derived fallback, honestly labelled.
                    "spread_bps_real": None,
                    "reference_close_independent": None,
                    "reference_source": DERIVED_REFERENCE_SOURCE,
                    "reference_kind": "derived",
                }
            )
            indep_close = (reference_by_ticker or {}).get(meta.get("underlyingTicker"))
            if indep_close not in (None, ""):
                try:
                    rs = real_spread(token_price, ratio, indep_close)
                except ValueError:  # unusable close — keep the labelled fallback
                    rs = None
                if rs is not None:
                    spread_rows[-1].update(
                        {
                            "spread_bps_real": str(rs["spread_bps_real"]),
                            "reference_close_independent": str(
                                rs["reference_close_independent"]
                            ),
                            "reference_source": REFERENCE_SOURCE,
                            "reference_kind": "independent",
                        }
                    )
            joined.append(
                {
                    "underlyingTicker": meta.get("underlyingTicker"),
                    "tokenSymbol": meta.get("tokenSymbol"),
                    "platformId": meta.get("platformId") or row.get("platformId"),
                    "tokenPrice": token_price,
                    "tokenToShareRatio": ratio,
                }
            )

        cross_rows: List[dict] = []
        for ticker, c in sorted(cross_issuer_spreads(joined).items()):
            cross_rows.append(
                {
                    "tick_id": tick_id,
                    "underlying_ticker": ticker,
                    "n_issuers": c["n_issuers"],
                    "cheap_symbol": c["cheap_symbol"],
                    "cheap_platform": c["cheap_platform"],
                    "cheap_implied": str(c["cheap_implied"]),
                    "rich_symbol": c["rich_symbol"],
                    "rich_platform": c["rich_platform"],
                    "rich_implied": str(c["rich_implied"]),
                    "spread_abs": str(c["spread_abs"]),
                    "spread_bps": str(c["spread_bps"]),
                    "source_endpoint": PRICE_ENDPOINT,
                    "fetched_at": fetched_at,
                }
            )
        return spread_rows, cross_rows

    # ---------------- storage ----------------

    def record_tick(
        self, ts_utc: str, tick_id: int,
        spread_rows: Sequence[Mapping[str, Any]], cross_rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Write one tick in ONE short SQLite transaction, then the JSONL mirror.

        The JSONL append is a non-fatal mirror (SQLite is the source of truth):
        a failure there is logged at WARNING and does not fail the tick — so a
        raised error from this method ALWAYS means the SQLite transaction
        rolled back and the caller may safely reclaim the tick_id.
        """
        with db.write_txn(self._con) as con:
            con.executemany(
                """INSERT INTO spread_ticks (
                    ts_utc, tick_id, underlying_ticker, token_symbol, token_contract,
                    platform_id, token_price, token_to_share_ratio, reference_price,
                    token_price_updated_at, implied_share_price, spread_abs, spread_bps,
                    spread_pct, market_status, open_state, spread_bps_real,
                    reference_close_independent, reference_source, reference_kind
                ) VALUES (
                    :ts_utc, :tick_id, :underlying_ticker, :token_symbol, :token_contract,
                    :platform_id, :token_price, :token_to_share_ratio, :reference_price,
                    :token_price_updated_at, :implied_share_price, :spread_abs, :spread_bps,
                    :spread_pct, :market_status, :open_state, :spread_bps_real,
                    :reference_close_independent, :reference_source, :reference_kind
                )""",
                [
                    dict(
                        r,
                        ts_utc=ts_utc,
                        spread_bps_real=r.get("spread_bps_real"),
                        reference_close_independent=r.get("reference_close_independent"),
                        reference_source=r.get("reference_source", DERIVED_REFERENCE_SOURCE),
                        reference_kind=r.get("reference_kind", "derived"),
                    )
                    for r in spread_rows
                ],
            )
            con.executemany(
                """INSERT INTO cross_issuer_ticks (
                    ts_utc, tick_id, underlying_ticker, n_issuers, cheap_symbol,
                    cheap_platform, cheap_implied, rich_symbol, rich_platform,
                    rich_implied, spread_abs, spread_bps
                ) VALUES (
                    :ts_utc, :tick_id, :underlying_ticker, :n_issuers, :cheap_symbol,
                    :cheap_platform, :cheap_implied, :rich_symbol, :rich_platform,
                    :rich_implied, :spread_abs, :spread_bps
                )""",
                [dict(r, ts_utc=ts_utc) for r in cross_rows],
            )

        try:
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                for r in spread_rows:
                    fh.write(json.dumps({"type": "spread_tick", "ts_utc": ts_utc, **r},
                                        ensure_ascii=False) + "\n")
                for r in cross_rows:
                    fh.write(json.dumps({"type": "cross_issuer_tick", "ts_utc": ts_utc, **r},
                                        ensure_ascii=False) + "\n")
        except OSError as exc:  # non-fatal mirror — SQLite already committed
            log.warning("jsonl append failed for tick %s (SQLite committed): %s",
                        tick_id, exc)

    # ---------------- loop ----------------

    def run_once(self) -> dict:
        """One full tick: universe → batched prices → spreads → storage.

        tick_id policy: the id is allocated BEFORE the write and RECLAIMED
        (decremented) when the tick fails to persist — ``record_tick`` is
        atomic (one SQLite transaction; its JSONL mirror is non-fatal), so a
        failure means nothing was committed and the id is safe to reuse.
        tick_ids therefore stay contiguous across failed ticks and restarts.
        """
        ts = iso_now()
        self._tick_id += 1
        tick_id = self._tick_id
        try:
            universe = self.load_universe()
            contracts = [
                str(u["tokenContractAddress"])
                for u in universe
                if u.get("tokenContractAddress")
            ]
            price_rows = self.fetch_price_rows(contracts)
            # REAL metric: independent per-share closes from the shared
            # oracle_reference cache (populated by stockgap/oracle.py). Cache-cold
            # tickers fall back to the derived reference, honestly labelled.
            reference: dict = {}
            if self.use_reference_cache:
                try:
                    reference = load_reference_closes(
                        self._con, max_age_s=self.reference_max_age_s
                    )
                except Exception as exc:  # noqa: BLE001 — never kill the tick
                    log.warning("reference cache read failed: %s", exc)
                    reference = {}
            spreads, cross = self.build_tick_rows(
                universe, price_rows, tick_id, reference_by_ticker=reference
            )
            self._record_tick_with_retry(ts, tick_id, spreads, cross)
        except Exception:
            # Nothing for tick_id was committed (see docstring) — reclaim it.
            self._tick_id -= 1
            raise
        self._meta_set("last_tick_ts", ts)  # guarded bookkeeping: never fails the tick
        meta = {
            "tick_id": tick_id, "ts_utc": ts,
            "n_spreads": len(spreads), "n_cross": len(cross),
            "n_contracts": len(contracts),
            "n_reference_independent": sum(
                1 for r in spreads if r.get("reference_kind") == "independent"
            ),
        }
        log.info("tick %s: %d spreads, %d cross-issuer", tick_id, len(spreads), len(cross))
        return meta

    def _record_tick_with_retry(
        self, ts: str, tick_id: int,
        spread_rows: Sequence[Mapping[str, Any]], cross_rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """record_tick with bounded retry (the API data is already in memory).

        Each failed attempt rolls back before sleeping so no half-open write
        transaction survives into the retry. Re-raises the last error when all
        attempts are exhausted.
        """
        last_exc: Optional[Exception] = None
        attempts = 1 + len(RECORD_RETRY_DELAYS)
        for attempt in range(attempts):
            try:
                self.record_tick(ts, tick_id, spread_rows, cross_rows)
                return
            except Exception as exc:  # noqa: BLE001 — retry any storage error
                last_exc = exc
                log.warning(
                    "record_tick %s attempt %d/%d failed: %s",
                    tick_id, attempt + 1, attempts, exc,
                )
                try:
                    self._con.rollback()  # drop the failed batch's transaction
                except Exception as exc2:  # noqa: BLE001
                    log.warning("rollback after failed tick %s: %s", tick_id, exc2)
                if attempt < len(RECORD_RETRY_DELAYS):
                    time.sleep(RECORD_RETRY_DELAYS[attempt])
        assert last_exc is not None
        raise last_exc

    def run_forever(self, max_ticks: Optional[int] = None) -> None:
        """Tick every ``tick_interval`` seconds; never die on a bad tick.

        Resilience contract (regression fix for the 176x 'database is locked'
        daemon deaths):

        - a failed tick is logged + rolled back and NEVER kills the loop;
        - the failure bookkeeping (``_meta_set``) is guarded — its own
          'database is locked' used to escape and kill the daemon;
        - after ``max_consecutive_failures`` consecutive failed ticks the
          process EXITS NON-ZERO so the watchdog restarts a genuinely broken
          process instead of looping silently forever.

        ``max_ticks`` bounds the loop (tests); None = run forever.
        """
        self._meta_set("started_at", iso_now())
        log.info("collector started: interval=%ss db=%s jsonl=%s",
                 self.tick_interval, self.db_path, self.jsonl_path)
        consecutive = 0
        ticks = 0
        while True:
            started = time.time()
            ok = True
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 — 24/7 survival
                ok = False
                log.exception("tick failed: %s", exc)
                try:
                    self._con.rollback()  # no half-open txn survives into the next tick
                except Exception as exc2:  # noqa: BLE001
                    log.warning("rollback after failed tick: %s", exc2)
                self._meta_set("last_error", f"{iso_now()}: {exc}")  # guarded, never escapes
            if ok:
                consecutive = 0
            else:
                consecutive += 1
                if consecutive >= self.max_consecutive_failures:
                    log.error(
                        "%d consecutive failed ticks — exiting non-zero so the "
                        "watchdog restarts a healthy process", consecutive,
                    )
                    raise SystemExit(2)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return
            elapsed = time.time() - started
            time.sleep(max(1.0, self.tick_interval - elapsed))

    def _migrate(self) -> None:
        """Add provenance columns to DBs created before source tracking existed."""
        for table in ("spread_ticks", "cross_issuer_ticks"):
            cols = {r[1] for r in self._con.execute(f"PRAGMA table_info({table})")}
            if "source_endpoint" not in cols:
                self._con.execute(f"ALTER TABLE {table} ADD COLUMN source_endpoint TEXT")
            if "fetched_at" not in cols:
                self._con.execute(f"ALTER TABLE {table} ADD COLUMN fetched_at TEXT")

        # Real-metric columns (2026-10-02): spread vs the INDEPENDENT underlying
        # close. Historical rows cannot be backfilled (the independent close was
        # never fetched) — label them honestly as 'derived' instead of leaving
        # them ambiguous. NEVER drop/rename existing columns (dashboard relies
        # on the old schema).
        cols = {r[1] for r in self._con.execute("PRAGMA table_info(spread_ticks)")}
        for col in (
            "spread_bps_real",
            "reference_close_independent",
            "reference_source",
            "reference_kind",
        ):
            if col not in cols:
                self._con.execute(f"ALTER TABLE spread_ticks ADD COLUMN {col} TEXT")
        self._con.execute(
            "UPDATE spread_ticks SET reference_kind = 'derived',"
            " reference_source = ? WHERE reference_kind IS NULL",
            (DERIVED_REFERENCE_SOURCE,),
        )

    def _meta_set(self, key: str, value: Any) -> None:
        """Collector_meta bookkeeping: GUARDED, never raises.

        This handler used to be the daemon killer: when the DB was locked its
        own 'database is locked' escaped the tick-failure handler and took the
        process down (root cause #1 of the 176x lock outage). It now rolls
        back and logs at WARNING.
        """
        try:
            self._con.execute(
                "INSERT INTO collector_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self._con.commit()
        except Exception as exc:  # noqa: BLE001 — bookkeeping must never kill the loop
            log.warning("meta_set(%s) failed (ignored): %s", key, exc)
            try:
                self._con.rollback()
            except Exception:  # noqa: BLE001
                pass
