"""StockGap weekend oracle — prediction recorder.

The core product insight: tokenized stocks trade 24/7 on-chain while the US
underlying market is closed. From Friday's close (16:00 New York = 20:00 UTC)
onward, the on-chain token price IS the market's implicit prediction of
Monday's open. This module records that prediction continuously.

Every ORACLE_INTERVAL_MIN minutes, for every token in the universe:

    implied_open_price  = tokenPrice / tokenToShareRatio   (predicted Monday open)
    last_reference_close = referencePrice                  (last underlying close)
    deviation_bps        = (implied_open_price - last_reference_close)
                           / last_reference_close * 10000

...together with snapshot_ts and the scoring cutoff this snapshot feeds
(the next Sunday 23:59 UTC), into the ``oracle_predictions`` table of the
shared collector DB (data/spread_ticks.db).

Data reuse: universe + prices come from the SAME Binance Web3 endpoints the
collector uses (``rwa/tokens`` + ``rwa/price`` via stockgap.client) through the
``load_universe`` / ``fetch_price_rows`` fetcher interface — a live
SpreadCollector instance satisfies it (scripts/run_oracle.py wires exactly
that); tests inject a fake fetcher with synthetic prices and timestamps.

Everything is configurable (interval, cutoff weekday/time, thresholds) via
env vars or config/oracle.env — nothing hardcoded. Money math is Decimal;
invalid rows are skipped, never recorded as garbage.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

if __package__ in (None, ""):  # direct run: python3 stockgap/oracle.py
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "stockgap"

from .spread import implied_share_price, independent_share_close  # noqa: E402

__all__ = [
    "OraclePredictor",
    "build_prediction_rows",
    "cutoff_for",
    "LiveFetcher",
    "is_enabled",
    "iso_now",
    "load_config",
    "load_reference_closes",
    "main",
    "next_cutoff",
]

log = logging.getLogger("stockgap.oracle")

PRICE_ENDPOINT = "/api/v1/dex/market/rwa/price"
UNDERLYING_ENDPOINT = "/api/v1/dex/market/rwa/underlying-market"
REFERENCE_SOURCE = "rwa/underlying-market"
DERIVED_REFERENCE_SOURCE = PRICE_ENDPOINT + " (derived)"
BPS = Decimal("10000")

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = REPO / "config" / "oracle.env"

# Precedence everywhere: real environment > config file > these defaults.
DEFAULTS: Mapping[str, str] = {
    "ORACLE_ENABLED": "1",               # 0 = recorder is a no-op (cron-safe)
    "ORACLE_INTERVAL_MIN": "5",          # snapshot cadence
    "ORACLE_CUTOFF_WEEKDAY": "sunday",   # scoring cutoff day (UTC)
    "ORACLE_CUTOFF_HOUR": "23",          # cutoff hour (UTC)
    "ORACLE_CUTOFF_MINUTE": "59",        # cutoff minute (UTC)
    "ORACLE_DEVIATION_FLAG_BPS": "100",  # |deviation| above this is flagged
    "ORACLE_UNIVERSE_TTL_S": "600",      # rwa/tokens cache TTL
    "ORACLE_BATCH_SIZE": "50",           # addresses per rwa/price batch
    "ORACLE_STAGGER_S": "0.25",          # pause between price batches (5 RPS)
    "ORACLE_REF_TTL_S": "1800",          # independent close cache TTL
    "ORACLE_REF_MAX_PER_TICK": "150",    # underlying-market calls per tick (warm-up cap)
    "ORACLE_REF_STAGGER_S": "0.22",      # pause between underlying-market calls (5 RPS)
    "STOCKGAP_DB": str(REPO / "data" / "spread_ticks.db"),
}

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS oracle_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    snapshot_ts TEXT NOT NULL,
    cutoff_ts TEXT NOT NULL,
    underlying_ticker TEXT,
    token_symbol TEXT,
    token_contract TEXT,
    platform_id TEXT,
    token_price TEXT,
    token_to_share_ratio TEXT,
    implied_open_price TEXT NOT NULL,
    last_reference_close TEXT,
    deviation_bps TEXT,
    deviation_flag INTEGER NOT NULL DEFAULT 0,
    market_status TEXT,
    source_endpoint TEXT,
    fetched_at TEXT,
    reference_source TEXT
);
CREATE TABLE IF NOT EXISTS oracle_reference (
    underlying_ticker TEXT PRIMARY KEY,
    reference_close TEXT NOT NULL,
    source_endpoint TEXT,
    fetched_epoch REAL NOT NULL,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_oracle_pred_cutoff
    ON oracle_predictions (cutoff_ts, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_oracle_pred_ticker
    ON oracle_predictions (underlying_ticker, snapshot_ts);
"""


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_reference_closes(
    con: sqlite3.Connection,
    *,
    now_epoch: Optional[float] = None,
    max_age_s: Optional[float] = 1800.0,
) -> dict:
    """``{ticker: per-share close}`` from the shared ``oracle_reference`` cache.

    Values are INDEPENDENT underlying closes (``rwa/underlying-market``'s
    ``referencePrice`` normalized with :func:`stockgap.spread.independent_share_close`
    at fetch time — see :meth:`OraclePredictor._reference_closes`). Consumers
    (the collector's real spread metric) must fall back to the derived
    reference and LABEL the row when a ticker is missing here.
    ``max_age_s`` filters out stale entries (None = take everything);
    entries older than it count as cache-cold.
    """
    now_epoch = time.time() if now_epoch is None else float(now_epoch)
    out: dict = {}
    try:
        fetched = con.execute(
            "SELECT underlying_ticker, reference_close, fetched_epoch"
            " FROM oracle_reference"
        ).fetchall()
    except sqlite3.OperationalError:  # table absent (oracle never ran here)
        return out
    for ticker, close, fetched_epoch in fetched:
        if max_age_s is not None and (now_epoch - float(fetched_epoch)) >= float(max_age_s):
            continue  # stale = cache-cold for this ticker
        out[str(ticker)] = str(close)
    return out


# ---------------- configuration ----------------

def load_config(
    config_file: Optional[Any] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Oracle config: real env > config file > defaults. All values strings.

    ``config_file`` defaults to config/oracle.env (or $STOCKGAP_ORACLE_CONFIG).
    """
    env = os.environ if env is None else env
    path = Path(
        config_file
        or env.get("STOCKGAP_ORACLE_CONFIG")
        or DEFAULT_CONFIG_FILE
    )
    values = dict(DEFAULTS)
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    for key in DEFAULTS:
        if key in env and env[key] != "":
            values[key] = env[key]
    return values


def is_enabled(config: Mapping[str, str]) -> bool:
    return str(config.get("ORACLE_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


def _weekday_number(name: str) -> int:
    try:
        return _WEEKDAYS[name.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"ORACLE_CUTOFF_WEEKDAY: unknown weekday {name!r}") from exc


# ---------------- cutoff selection ----------------

def next_cutoff(
    now: datetime,
    weekday: str = "sunday",
    hour: int = 23,
    minute: int = 59,
) -> datetime:
    """First cutoff timestamp STRICTLY AFTER ``now`` (UTC, tz-aware ok).

    The cutoff is when the weekend prediction is frozen for scoring: by
    default Sunday 23:59 UTC — the last snapshot before US pre-market.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    target = _weekday_number(weekday)
    days_ahead = (target - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    while candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def cutoff_for(
    snapshot_ts: datetime,
    weekday: str = "sunday",
    hour: int = 23,
    minute: int = 59,
) -> datetime:
    """The scoring cutoff a snapshot feeds: the first cutoff at-or-after it.

    Inclusive: a snapshot taken exactly at the cutoff still counts for it.
    """
    return next_cutoff(snapshot_ts - timedelta(microseconds=1), weekday, hour, minute)


# ---------------- prediction math ----------------

def build_prediction_rows(
    universe: Sequence[Mapping[str, Any]],
    price_rows: Sequence[Mapping[str, Any]],
    snapshot_id: int,
    snapshot_ts: str,
    cutoff_ts: str,
    flag_bps: Decimal = Decimal("100"),
    fetched_at: Optional[str] = None,
    reference_by_ticker: Optional[Mapping[str, str]] = None,
) -> List[dict]:
    """Join rwa/price rows with rwa/tokens metadata → oracle prediction rows.

    Pure function (fake prices/timestamps in tests). One consistent price
    source per metric, like the collector: tokenPrice + referencePrice from
    the SAME ``rwa/price`` response; tokenToShareRatio from ``rwa/tokens``.
    Rows with missing/invalid prices are skipped, never recorded as garbage.

    ``implied_open_price`` = tokenPrice / tokenToShareRatio — the market's
    implied prediction of the underlying's next open (per share).
    ``deviation_bps`` is signed vs. ``last_reference_close``: positive = token
    predicts a RICH (gap-up) open.

    ``reference_by_ticker`` carries the INDEPENDENT underlying close
    (rwa/underlying-market). ``rwa/price.referencePrice`` is derived from the
    token itself (= tokenPrice / ratio), so a deviation against it is 0 by
    construction; when ``reference_by_ticker`` is given it is the only
    reference used and a missing ticker yields NULL deviation. When it is
    ``None`` (legacy/tests) the row's own referencePrice is used.
    """
    fetched_at = fetched_at or snapshot_ts
    meta_by_contract = {
        str(u.get("tokenContractAddress", "")).lower(): u for u in universe
    }
    rows: List[dict] = []
    for row in price_rows:
        addr = str(row.get("tokenContractAddress", "")).lower()
        meta = meta_by_contract.get(addr)
        if meta is None:
            continue
        ratio = meta.get("tokenToShareRatio")
        token_price = row.get("tokenPrice")
        if reference_by_ticker is None:
            reference = row.get("referencePrice")
            ref_source = PRICE_ENDPOINT + " (derived)" if reference is not None else None
        else:
            reference = reference_by_ticker.get(meta.get("underlyingTicker"))
            ref_source = REFERENCE_SOURCE if reference is not None else None
        try:
            implied = implied_share_price(token_price, ratio)
        except ValueError:
            continue
        deviation_bps: Optional[Decimal] = None
        if reference is not None and str(reference).strip():
            try:
                ref = Decimal(str(reference).strip())
            except Exception:  # noqa: BLE001 — malformed reference → no deviation
                ref = Decimal(0)
            if ref > 0:
                deviation_bps = (implied - ref) / ref * BPS
        flag = 0
        if deviation_bps is not None and abs(deviation_bps) >= flag_bps:
            flag = 1
        status_info = meta.get("statusInfo") or {}
        rows.append(
            {
                "snapshot_id": snapshot_id,
                "snapshot_ts": snapshot_ts,
                "cutoff_ts": cutoff_ts,
                "underlying_ticker": meta.get("underlyingTicker"),
                "token_symbol": meta.get("tokenSymbol"),
                "token_contract": row.get("tokenContractAddress"),
                "platform_id": meta.get("platformId") or row.get("platformId"),
                "token_price": str(token_price),
                "token_to_share_ratio": str(ratio) if ratio is not None else None,
                "implied_open_price": str(implied),
                "last_reference_close": str(reference) if reference is not None else None,
                "deviation_bps": str(deviation_bps) if deviation_bps is not None else None,
                "deviation_flag": flag,
                "market_status": status_info.get("marketStatus"),
                "source_endpoint": PRICE_ENDPOINT,
                "fetched_at": fetched_at,
                "reference_source": ref_source,
            }
        )
    return rows


# ---------------- recorder ----------------

class OraclePredictor:
    """Records rolling implied-open predictions into ``oracle_predictions``.

    ``fetcher`` is anything with ``load_universe()`` and
    ``fetch_price_rows(contracts)`` — a SpreadCollector in production, a fake
    in tests. ``clock`` returns a tz-aware datetime (injectable for tests).
    """

    INSERT_SQL = """INSERT INTO oracle_predictions (
        snapshot_id, snapshot_ts, cutoff_ts, underlying_ticker, token_symbol,
        token_contract, platform_id, token_price, token_to_share_ratio,
        implied_open_price, last_reference_close, deviation_bps, deviation_flag,
        market_status, source_endpoint, fetched_at, reference_source
    ) VALUES (
        :snapshot_id, :snapshot_ts, :cutoff_ts, :underlying_ticker, :token_symbol,
        :token_contract, :platform_id, :token_price, :token_to_share_ratio,
        :implied_open_price, :last_reference_close, :deviation_bps, :deviation_flag,
        :market_status, :source_endpoint, :fetched_at, :reference_source
    )"""

    def __init__(
        self,
        db_path: Any,
        fetcher: Any,
        config: Optional[Mapping[str, str]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.db_path = str(db_path)
        self.fetcher = fetcher
        self.config = {**DEFAULTS, **(config if config is not None else load_config())}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.interval_s = float(self.config["ORACLE_INTERVAL_MIN"]) * 60.0
        self.flag_bps = Decimal(self.config["ORACLE_DEVIATION_FLAG_BPS"])
        self.cutoff_weekday = self.config["ORACLE_CUTOFF_WEEKDAY"]
        self.cutoff_hour = int(self.config["ORACLE_CUTOFF_HOUR"])
        self.cutoff_minute = int(self.config["ORACLE_CUTOFF_MINUTE"])

        self._con = sqlite3.connect(self.db_path)
        self._con.execute("PRAGMA busy_timeout=10000")
        self._con.executescript(SCHEMA)  # CREATE ... IF NOT EXISTS: idempotent
        self._migrate()
        self._con.commit()
        self.ref_ttl_s = float(self.config["ORACLE_REF_TTL_S"])
        self.ref_max_per_tick = int(self.config["ORACLE_REF_MAX_PER_TICK"])
        self.ref_stagger_s = float(self.config["ORACLE_REF_STAGGER_S"])
        row = self._con.execute("SELECT MAX(snapshot_id) FROM oracle_predictions").fetchone()
        self._snapshot_id = row[0] or 0

    def _migrate(self) -> None:
        cols = {r[1] for r in self._con.execute("PRAGMA table_info(oracle_predictions)")}
        if "reference_source" not in cols:
            self._con.execute("ALTER TABLE oracle_predictions ADD COLUMN reference_source TEXT")

    # ---------------- independent reference close ----------------

    def _reference_closes(self, universe: Sequence[Mapping[str, Any]], now_epoch: float):
        """{ticker: close} from the SQLite cache, refreshing stale tickers.

        One ``rwa/underlying-market`` call per underlying (first issuer with a
        non-null price wins — BStock tokens return null). The endpoint's
        ``referencePrice`` is divided by tokenToShareRatio once more than
        ``tokenPrice`` is, so the cached close is ``referencePrice * ratio``
        (empirically matches tokenPrice/ratio to ~0 bps median, all ratios). At most
        ``ref_max_per_tick`` refreshes per tick spreads the ~450-call warm-up
        over several ticks and keeps well under 5 RPS. Returns ``None`` when
        the fetcher has no independent source (legacy mode).
        """
        if not hasattr(self.fetcher, "fetch_underlying_reference"):
            return None
        contracts_by_ticker: dict = {}
        for u in universe:
            ticker, addr = u.get("underlyingTicker"), u.get("tokenContractAddress")
            if ticker and addr:
                contracts_by_ticker.setdefault(ticker, []).append(str(addr).lower())
        ratio_by_contract = {
            str(u.get("tokenContractAddress", "")).lower(): u.get("tokenToShareRatio")
            for u in universe
        }
        cached = {
            r[0]: (r[1], r[2]) for r in self._con.execute(
                "SELECT underlying_ticker, reference_close, fetched_epoch FROM oracle_reference")
        }
        stale = sorted(
            (t for t in contracts_by_ticker
             if t not in cached or now_epoch - cached[t][1] >= self.ref_ttl_s),
            key=lambda t: cached[t][1] if t in cached else 0.0,  # never-fetched first
        )
        fetched_at = datetime.fromtimestamp(now_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for n, ticker in enumerate(stale[: self.ref_max_per_tick]):
            if n and self.ref_stagger_s:
                time.sleep(self.ref_stagger_s)
            value = None
            for contract in contracts_by_ticker[ticker]:
                try:
                    value = self.fetcher.fetch_underlying_reference(contract)
                except Exception as exc:  # noqa: BLE001 — one bad call must not kill the tick
                    log.warning("underlying-market %s failed: %s", contract, exc)
                    value = None
                if value:
                    try:  # per-SHARE close: underlying-market divides by the ratio again
                        value = str(independent_share_close(value, ratio_by_contract[contract]))
                    except Exception:  # noqa: BLE001 — bad/missing ratio: unusable, try next issuer
                        value = None
                        continue
                    break
            if value:
                self._con.execute(
                    "INSERT INTO oracle_reference (underlying_ticker, reference_close,"
                    " source_endpoint, fetched_epoch, fetched_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(underlying_ticker) DO UPDATE SET"
                    " reference_close=excluded.reference_close,"
                    " source_endpoint=excluded.source_endpoint,"
                    " fetched_epoch=excluded.fetched_epoch, fetched_at=excluded.fetched_at",
                    (ticker, str(value), UNDERLYING_ENDPOINT, now_epoch, fetched_at),
                )
                cached[ticker] = (str(value), now_epoch)
        self._con.commit()
        return {t: v[0] for t, v in cached.items() if t in contracts_by_ticker}

    # ---------------- one snapshot ----------------

    def snapshot(self, now: Optional[datetime] = None) -> Tuple[int, int]:
        """Record one rolling snapshot. Returns (snapshot_id, rows_written)."""
        now = now or self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        snapshot_ts = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cutoff = cutoff_for(
            now, self.cutoff_weekday, self.cutoff_hour, self.cutoff_minute
        )
        cutoff_ts = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        universe = self.fetcher.load_universe()
        contracts = [
            str(u.get("tokenContractAddress", "")).lower()
            for u in universe
            if u.get("tokenContractAddress")
        ]
        price_rows = self.fetcher.fetch_price_rows(contracts)
        reference = self._reference_closes(universe, now.timestamp())
        self._snapshot_id += 1
        rows = build_prediction_rows(
            universe,
            price_rows,
            snapshot_id=self._snapshot_id,
            snapshot_ts=snapshot_ts,
            cutoff_ts=cutoff_ts,
            flag_bps=self.flag_bps,
            reference_by_ticker=reference,
        )
        if rows:
            self._con.executemany(self.INSERT_SQL, rows)
            self._con.commit()
        log.info(
            "oracle snapshot #%d ts=%s cutoff=%s rows=%d/%d priced",
            self._snapshot_id, snapshot_ts, cutoff_ts, len(rows), len(universe),
        )
        return self._snapshot_id, len(rows)

    # ---------------- loop ----------------

    def run_forever(self) -> None:
        """Snapshot every ORACLE_INTERVAL_MIN minutes until killed."""
        log.info("oracle loop: interval=%ss db=%s", self.interval_s, self.db_path)
        while True:
            started = time.time()
            try:
                self.snapshot()
            except Exception as exc:  # noqa: BLE001 — keep the oracle alive
                log.warning("snapshot failed: %s", exc)
            elapsed = time.time() - started
            time.sleep(max(5.0, self.interval_s - elapsed))


# ---------------- live fetcher ----------------

class LiveFetcher:
    """SpreadCollector (universe + batched prices) + underlying-market reference."""

    def __init__(self, collector: Any):
        self._collector = collector

    def load_universe(self) -> List[dict]:
        return self._collector.load_universe()

    def fetch_price_rows(self, contracts: Sequence[str]) -> List[dict]:
        return self._collector.fetch_price_rows(contracts)

    def fetch_underlying_reference(self, contract: str) -> Optional[str]:
        """Independent underlying reference price, or None (e.g. BStock null)."""
        status, parsed, _ = self._collector.client.request(
            "GET", UNDERLYING_ENDPOINT,
            params={"binanceChainId": "56", "tokenContractAddress": contract},
        )
        if status != 200 or parsed.get("code") != 0:
            log.warning("underlying-market %s: http=%s code=%s", contract, status, parsed.get("code"))
            return None
        value = ((parsed.get("data") or {}).get("marketData") or {}).get("referencePrice")
        return str(value).strip() if value not in (None, "") else None


# ---------------- CLI ----------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 stockgap/oracle.py --once`` (cron) or no flag for a loop."""
    parser = argparse.ArgumentParser(description="StockGap weekend-oracle recorder")
    parser.add_argument("--once", action="store_true",
                        help="record one snapshot and exit (cron-friendly)")
    parser.add_argument("--db", default=None, help="SQLite path (default: STOCKGAP_DB)")
    args = parser.parse_args(argv)

    data = REPO / "data"
    data.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(data / "oracle.log")],
    )
    cfg = load_config()
    if not is_enabled(cfg):
        log.info("ORACLE_ENABLED=0 — recorder omitido")
        return 0

    from .client import BinanceWeb3Client, load_credentials
    from .collector import SpreadCollector

    db_path = args.db or cfg["STOCKGAP_DB"]
    api_key, secret, _, _ = load_credentials()
    fetcher = SpreadCollector(  # reused only for load_universe/fetch_price_rows
        db_path=db_path,
        jsonl_path=data / "ticks.jsonl",
        client=BinanceWeb3Client(api_key, secret),
        batch_size=int(cfg["ORACLE_BATCH_SIZE"]),
        stagger_s=float(cfg["ORACLE_STAGGER_S"]),
        universe_ttl_s=float(cfg["ORACLE_UNIVERSE_TTL_S"]),
    )
    predictor = OraclePredictor(db_path, LiveFetcher(fetcher), cfg)
    if args.once:
        snapshot_id, n = predictor.snapshot()
        print(f"oracle snapshot #{snapshot_id}: {n} predicciones registradas")
        return 0 if n else 1
    predictor.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
