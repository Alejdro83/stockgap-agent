# Plan v2: Rotación local + compactación/retención del ledger SQLite
*(v2 = revisión tras gate Opus #1 — `docs/reviews/gate1-plan-review-opus.md`, VERDICT: changes_requested. Los 18 puntos requeridos se marcan [B#]/[C#]/[O#])*

**Date:** 2026-10-06 · **Author:** LISA · **Executor:** Sonnet · **Gate:** Opus (round 2)

## Context

Repo `/home/lab-master/stockgap-agent`, stdlib+pytest. Ledger `data/spread_ticks.db`: 2.2 GB WAL,
~6M rows, ts format `YYYY-MM-DDTHH:MM:SS.sssZ` (24 chars, all UTC `Z`), ~17% rows have empty
`spread_bps_real`. Shipper `scripts/ship_rows.py` streams by `id > watermark` per table with
watermarks in `data/ship_meta.json`; `ship_rows.py:699-705` exits non-zero when `local MAX(id) <
watermark` (a table emptied = shipper bricked [B5]). All writers must use `stockgap.db.connect()`
(10 s busy timeout, WAL, 64 MiB journal limit) and write transactions must use `BEGIN IMMEDIATE`
(deferred read→write upgrades ignore busy_timeout) [B6] — `collector.py:525` has 176 historical
"database is locked" events. Module split follows the `stockgap/rotation.py` +
`scripts/rotate_data.py` pattern [O4]; logger via `logging.getLogger("compact_ledger")` [O4].

Real target schema (authoritative, from the live DB) — fixtures recreate exactly this [B1]:

```sql
CREATE TABLE IF NOT EXISTS spread_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, tick_id INTEGER NOT NULL,
    underlying_ticker TEXT, token_symbol TEXT, token_contract TEXT, platform_id TEXT,
    token_price TEXT, token_to_share_ratio TEXT, reference_price TEXT,
    token_price_updated_at INTEGER, implied_share_price TEXT, spread_abs TEXT,
    spread_bps TEXT, spread_pct TEXT, market_status TEXT, open_state INTEGER,
    source_endpoint TEXT, fetched_at TEXT, spread_bps_real TEXT,
    reference_close_independent TEXT, reference_source TEXT, reference_kind TEXT);
CREATE TABLE IF NOT EXISTS cross_issuer_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, tick_id INTEGER NOT NULL,
    underlying_ticker TEXT, n_issuers INTEGER, cheap_symbol TEXT, cheap_platform TEXT,
    cheap_implied TEXT, rich_symbol TEXT, rich_platform TEXT, rich_implied TEXT);
CREATE TABLE IF NOT EXISTS oracle_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id INTEGER NOT NULL,
    snapshot_ts TEXT NOT NULL, cutoff_ts TEXT NOT NULL, underlying_ticker TEXT,
    token_symbol TEXT, token_contract TEXT, platform_id TEXT, token_price TEXT,
    token_to_share_ratio TEXT, implied_open_price TEXT NOT NULL);
```

Backtest column needs (verified 2026-10-06): `spread_bps_real` per row hour-mixed, and
`rich_implied`/`cheap_implied` for cross gaps → the hourly bar must keep spread stats AND
implied sum [C2].

## Task 1 — cron local para `rotate_data.py` [O5]

1. Manual run must exit 0: `cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/rotate.lock /usr/bin/nice -n 19 /usr/bin/python3 scripts/rotate_data.py`
2. Verify `data/alerts-cron.log` and `data/oracle-cron.log` are covered by the `data/*.log`
   copytruncate rotation (read `rotate_data.py` config; if a suffix/pattern misses them, note it
   in the PR instead of editing rotation code — out of scope).
3. Append crontab (exactly one `rotate_data.py` line afterwards; `crontab -l | grep -c` prints 1):
   ```
   */10 * * * * cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/rotate.lock /usr/bin/nice -n 19 /usr/bin/python3 scripts/rotate_data.py >> data/logs/rotate.log 2>&1
   ```

## Task 2 — `stockgap/compaction.py` + `scripts/compact_ledger.py` (TDD) [O4]

### 2a. FAILING TESTS FIRST — `tests/test_compact_ledger.py` (complete)

Fixtures create the REAL schema above [B1], seed `spread_bps_real` (the aggregated column!) with
distinct old hours and exact per-bucket values [B2], and always write `data/ship_meta.json` unless
testing its absence [B4].

```python
"""Tests for stockgap/compaction.py — merge semantics, watermarks, locking, dry-run."""
import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stockgap.compaction import CompactConfig, compact, migrate_indexes  # noqa: E402

DDL = [ ... the three CREATE TABLE statements from the plan's Context section, verbatim ... ]


def _mk(db: Path):
    conn = sqlite3.connect(db)
    for stmt in DDL:
        conn.execute(stmt)
    conn.commit()
    return conn


def _ts(hours_ago: int) -> str:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _seed_rows(conn, hours_ago, spreads_by_token=None):
    """Insert one AAA/bstock + one BBB/ondo row per given hour. Returns inserted ids."""
    spreads_by_token = spreads_by_token or {"AAA": "10.0", "BBB": "-5.0"}
    ids = []
    for h in hours_ago:
        for token, spread in spreads_by_token.items():
            cur = conn.execute(
                "INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
                " token_contract, platform_id, token_price, token_to_share_ratio, reference_price,"
                " token_price_updated_at, implied_share_price, spread_abs, spread_bps, spread_pct,"
                " market_status, open_state, source_endpoint, fetched_at, spread_bps_real,"
                " reference_close_independent, reference_source, reference_kind)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_ts(h), 1, token, token, "0x1", "bstock" if token == "AAA" else "ondo",
                 "100.0", "1.0", "100.0", 0, "95.0", "5.0", "5.0", "0.5", "regular", 1,
                 "endpoint", "f", spread, "95.0", "coingecko", "independent"))
            ids.append(cur.lastrowid)
    conn.commit()
    return ids


def _wm(db: Path, values: dict):
    (db.parent / "ship_meta.json").write_text(json.dumps(values))


CFG = CompactConfig(raw_keep_days=2, hourly_keep_days=30,
                    cross_keep_days=30, oracle_keep_days=30)


def test_aggregation_math_exact(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _seed_rows(conn, [192, 191])          # two DISTINCT old hours [B2]
    conn.close()
    _wm(db, {"spread_ticks": 10**9, "cross_issuer_ticks": 10**9, "oracle_predictions": 10**9})
    rep = compact(db, CFG, dry_run=False)
    c = sqlite3.connect(db)
    assert rep["hours_done"] == 2 and rep["failures"] == 0        # done, not found [B2]
    row = c.execute("SELECT n_ticks, n_spread, spread_bps_real_sum, spread_bps_real_sumsq,"
                    " spread_bps_real_min, spread_bps_real_max, implied_share_price_sum,"
                    " n_implied FROM spread_ticks_hourly WHERE token_symbol='AAA'").fetchone()
    assert row == (1, 1, 10.0, 100.0, 10.0, 10.0, 95.0, 1)        # sums/counts [C2]
    assert c.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0] == 0


def test_straggler_merges_into_existing_bar(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _seed_rows(conn, [192])
    conn.close()
    _wm(db, {"spread_ticks": 10**9, "cross_issuer_ticks": 10**9, "oracle_predictions": 10**9})
    compact(db, CFG, dry_run=False)
    conn = sqlite3.connect(db)
    _seed_rows(conn, [192], spreads_by_token={"AAA": "30.0"})     # late row [B3]
    conn.close()
    compact(db, CFG, dry_run=False)
    c = sqlite3.connect(db)
    row = c.execute("SELECT n_ticks, n_spread, spread_bps_real_sum, spread_bps_real_min,"
                    " spread_bps_real_max FROM spread_ticks_hourly WHERE token_symbol='AAA'"
                    " AND underlying_ticker='AAA'").fetchone()
    assert row == (2, 2, 40.0, 10.0, 30.0)                        # merged, not overwritten


def test_never_deletes_unshipped_rows(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    ids = _seed_rows(conn, [192, 191])                            # 4 rows
    conn.close()
    _wm(db, {"spread_ticks": ids[1], "cross_issuer_ticks": 10**9, "oracle_predictions": 10**9})
    compact(db, CFG, dry_run=False)
    c = sqlite3.connect(db)
    left = [r[0] for r in c.execute("SELECT id FROM spread_ticks ORDER BY id")]
    assert set(left) == {i for i in ids if i > ids[1]}            # unshipped survive [B4]


def test_missing_ship_meta_blocks_all_deletes(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _seed_rows(conn, [192, 191])
    conn.close()                                                  # no ship_meta.json
    compact(db, CFG, dry_run=False)
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0] == 4   # nothing deleted
    assert c.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] >= 1  # agg ok


def test_never_deletes_max_id_row(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    ids = _seed_rows(conn, [192])
    conn.close()
    _wm(db, {"spread_ticks": 10**9, "cross_issuer_ticks": 10**9, "oracle_predictions": 10**9})
    compact(db, CFG, dry_run=False)
    c = sqlite3.connect(db)
    assert c.execute("SELECT MAX(id) FROM spread_ticks").fetchone()[0] == max(ids)  # [B5]


def test_dry_run_is_read_only(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _seed_rows(conn, [192])
    conn.close()
    before = db.read_bytes()
    rep = compact(db, CFG, dry_run=True)
    assert db.read_bytes() == before                              # no DDL, no writes [B8]
    assert rep["rows_deleted"] >= 1                               # still reports planned work


def test_nonfinite_values_rejected(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _seed_rows(conn, [192], spreads_by_token={"AAA": "nan", "BBB": "abc"})
    conn.close()
    _wm(db, {"spread_ticks": 10**9, "cross_issuer_ticks": 10**9, "oracle_predictions": 10**9})
    rep = compact(db, CFG, dry_run=False)
    assert rep["rows_reject"] == 2                                 # counted [C4]
    c = sqlite3.connect(db)
    row = c.execute("SELECT n_spread, spread_bps_real_sum FROM spread_ticks_hourly"
                    " WHERE token_symbol='AAA'").fetchone()
    assert row == (0, 0.0)


def test_cutoff_boundary_and_month_boundary(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    cutoff = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) \
        - timedelta(days=CFG.raw_keep_days)
    inside = cutoff - timedelta(seconds=1)                        # must compact [C7]
    onedge = cutoff                                              # must NOT compact
    for when, spread in ((inside, "10.0"), (onedge, "20.0")):
        conn.execute(
            "INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
            " token_contract, platform_id, token_price, token_to_share_ratio, reference_price,"
            " token_price_updated_at, implied_share_price, spread_abs, spread_bps, spread_pct,"
            " market_status, open_state, source_endpoint, fetched_at, spread_bps_real,"
            " reference_close_independent, reference_source, reference_kind)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (when.strftime("%Y-%m-%dT%H:%M:%S.000Z"), 1, "AAA", "AAA", "0x1", "bstock",
             "1", "1", "1", 0, "1", "1", "1", "1", "regular", 1, "e", "f", spread,
             "1", "c", "independent"))
    conn.execute(
        "INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
        " token_contract, platform_id, token_price, token_to_share_ratio, reference_price,"
        " token_price_updated_at, implied_share_price, spread_abs, spread_bps, spread_pct,"
        " market_status, open_state, source_endpoint, fetched_at, spread_bps_real,"
        " reference_close_independent, reference_source, reference_kind)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-09-30T23:00:00.000Z", 1, "AAA", "AAA", "0x1", "bstock",  # month boundary [C7]
         "1", "1", "1", 0, "1", "1", "1", "1", "regular", 1, "e", "f", "30.0",
         "1", "c", "independent"))
    conn.commit()
    conn.close()
    _wm(db, {"spread_ticks": 10**9, "cross_issuer_ticks": 10**9, "oracle_predictions": 10**9})
    compact(db, CFG, dry_run=False)
    c = sqlite3.connect(db)
    left = [r[0] for r in c.execute("SELECT ts_utc FROM spread_ticks")]
    assert len(left) == 2 and all(t >= cutoff.strftime("%Y-%m-%dT%H") for t in left)


def test_config_validation():
    from stockgap.compaction import load_cfg
    import os
    os.environ["RAW_KEEP_DAYS"] = "0"; os.environ["HOURLY_KEEP_DAYS"] = "1"; os.environ["X"] = "y"
    cfg = load_cfg()
    assert cfg.raw_keep_days >= 2                                 # clamped [C5]
    assert cfg.hourly_keep_days >= cfg.raw_keep_days + 1
    os.environ["RAW_KEEP_DAYS"] = "garbage"
    cfg2 = load_cfg()
    assert cfg2.raw_keep_days == 14                               # default on garbage [C5]
    for k in ("RAW_KEEP_DAYS", "HOURLY_KEEP_DAYS"):
        os.environ.pop(k, None)


def test_lock_contention_waits(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _seed_rows(conn, [192])
    ids = [r[0] for r in conn.execute("SELECT id FROM spread_ticks")]
    conn.close()
    _wm(db, {"spread_ticks": 10**9, "cross_issuer_ticks": 10**9, "oracle_predictions": 10**9})
    holder = sqlite3.connect(db)
    holder.execute("BEGIN IMMEDIATE")                             # hold the write lock [B6]
    holder.execute("UPDATE spread_ticks SET tick_id=2")
    t = threading.Timer(0.7, holder.commit)
    t.start()
    rep = compact(db, CFG, dry_run=False)                         # must wait via busy_timeout
    t.join()
    assert rep["failures"] == 0
```

Run `python3 -m pytest tests/test_compact_ledger.py -x` → all FAIL (module missing) [B1].

### 2b. IMPLEMENT — `stockgap/compaction.py` (complete)

Design decisions per review: merge-not-replace aggregation storing `n/sum/sumsq/min/max` [B3][C2];
deletions bounded by `id <= watermark AND id < (SELECT MAX(id) ...)` [B4][B5]; writes via
`stockgap.db.connect()` + `BEGIN IMMEDIATE` per hour and per prune batch [B6]; NO VACUUM — end
with `PRAGMA wal_checkpoint(TRUNCATE)` [B7]; dry-run opens read-only (or `connect_ro` if present)
and executes zero DDL [B8]; hour iteration walks hour strings from `MIN(ts_utc)` to the cutoff —
no `DISTINCT substr()` full scans [B9]; index creation only via explicit `--migrate-indexes`
[B9]; `ORDER BY ts_utc, id` for the last reference close [C1]; `_finite()` parser with reject
counting [C4]; `load_cfg()` clamps/validates [C5]; prunes batched (50 000 ids) and guarded per
table, `main()` exits non-zero when `failures > 0` [C6]; PK = `(ts_hour, underlying_ticker,
token_symbol, platform_id)` all `NOT NULL` = the bucket key [C3].

```python
"""SQLite ledger compaction & retention (see scripts/compact_ledger.py CLI).

Aggregates raw spread_ticks rows older than RAW_KEEP_DAYS into spread_ticks_hourly with
MERGE semantics (late rows combine into existing bars), then deletes only rows that the
shipper has already sent (id <= watermark per data/ship_meta.json) and never a table's
MAX(id) row. Prunes hourly/cross/oracle past their KEEP_DAYS with the same guards.
Dry-run is strictly read-only. Idempotent, best-effort per hour with a failures counter.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("compact_ledger")

HOUR_SCHEMA = """
CREATE TABLE IF NOT EXISTS spread_ticks_hourly (
    ts_hour TEXT NOT NULL,
    underlying_ticker TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    n_ticks INTEGER NOT NULL,
    n_spread INTEGER NOT NULL,
    spread_bps_real_sum REAL NOT NULL,
    spread_bps_real_sumsq REAL NOT NULL,
    spread_bps_real_min REAL,
    spread_bps_real_max REAL,
    n_implied INTEGER NOT NULL,
    implied_share_price_sum REAL NOT NULL,
    reference_close_independent_last REAL,
    reference_close_ts_last TEXT,
    market_status_majority TEXT,
    PRIMARY KEY (ts_hour, underlying_ticker, token_symbol, platform_id)
)"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_spread_ts ON spread_ticks(ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_hourly_ts ON spread_ticks_hourly(ts_hour)",
    "CREATE INDEX IF NOT EXISTS idx_cross_ts ON cross_issuer_ticks(ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_oracle_ts ON oracle_predictions(snapshot_ts)",
)

PRUNE_BATCH = 50_000


@dataclass
class CompactConfig:
    raw_keep_days: int = 14
    hourly_keep_days: int = 180
    cross_keep_days: int = 180
    oracle_keep_days: int = 180


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _majority(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if counts else ""


def load_cfg() -> CompactConfig:  # [C5]
    def num(name, default, lo):
        try:
            v = int(os.getenv(name, str(default)))
        except ValueError:
            return default
        return max(v, lo)

    raw = num("RAW_KEEP_DAYS", 14, 2)
    hourly = num("HOURLY_KEEP_DAYS", 180, raw + 1)
    return CompactConfig(raw, hourly, num("CROSS_KEEP_DAYS", 180, 1),
                         num("ORACLE_KEEP_DAYS", 180, 1))


def load_watermarks(db_path: Path) -> dict | None:  # [B4]
    meta = db_path.parent / "ship_meta.json"
    try:
        data = json.loads(meta.read_text())
        assert all(isinstance(data.get(t), int) for t in
                   ("spread_ticks", "cross_issuer_ticks", "oracle_predictions"))
        return data
    except Exception as exc:
        log.warning("ship_meta.json unusable (%s) — deletes disabled this run", exc)
        return None


def migrate_indexes(db_path: Path) -> None:  # [B9] explicit one-time migration
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("BEGIN IMMEDIATE")
    for stmt in INDEXES:
        t0 = datetime.now(timezone.utc)
        conn.execute(stmt)
        log.info("index done in %.1fs: %s", (datetime.now(timezone.utc) - t0).total_seconds(),
                 stmt[:60])
    conn.execute("COMMIT")
    conn.close()


def _aggregate_hour(conn, hour: str, hi: str) -> tuple[int, int]:  # returns (buckets, rejects)
    rows = conn.execute(
        "SELECT id, ts_utc, underlying_ticker, token_symbol, platform_id, spread_bps_real,"
        " implied_share_price, reference_close_independent, market_status FROM spread_ticks"
        " WHERE ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc, id", (hour + ":00", hi + ":00")
    ).fetchall()
    buckets: dict[tuple, dict] = {}
    rejects = 0
    for rid, ts, und, tok, plat, spread, implied, ref, mkt in rows:
        key = (hour, und or "", tok or "", plat or "")
        b = buckets.setdefault(key, {"n": 0, "ns": 0, "ssum": 0.0, "ssumsq": 0.0,
                                     "smin": None, "smax": None, "ni": 0, "isum": 0.0,
                                     "ref": None, "refts": None, "mk": []})
        b["n"] += 1
        b["mk"].append(mkt or "")
        s = _finite(spread)
        if s is None and spread not in (None, ""):
            rejects += 1
        if s is not None:
            b["ns"] += 1; b["ssum"] += s; b["ssumsq"] += s * s
            b["smin"] = s if b["smin"] is None else min(b["smin"], s)
            b["smax"] = s if b["smax"] is None else max(b["smax"], s)
        i = _finite(implied)
        if i is None and implied not in (None, ""):
            rejects += 1
        if i is not None:
            b["ni"] += 1; b["isum"] += i
        if ref not in (None, "") and (b["refts"] is None or ts > b["refts"]):  # [C1]
            b["ref"], b["refts"] = ref, ts
    for (h, und, tok, plat), b in buckets.items():
        _merge_bar(conn, (h, und, tok, plat, b["n"], b["ns"], b["ssum"], b["ssumsq"],
                          b["smin"], b["smax"], b["ni"], b["isum"],
                          _finite(b["ref"]), b["refts"], _majority(b["mk"])))
    conn.commit()
    return len(buckets), rejects


def _merge_bar(conn, new):  # [B3] merge, never overwrite
    h, und, tok, plat = new[0:4]
    row = conn.execute(
        "SELECT n_ticks, n_spread, spread_bps_real_sum, spread_bps_real_sumsq,"
        " spread_bps_real_min, spread_bps_real_max, n_implied, implied_share_price_sum,"
        " reference_close_independent_last, reference_close_ts_last, market_status_majority"
        " FROM spread_ticks_hourly WHERE ts_hour=? AND underlying_ticker=? AND token_symbol=?"
        " AND platform_id=?", (h, und, tok, plat)).fetchone()
    if row is None:
        conn.execute("INSERT INTO spread_ticks_hourly VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     new)
        return
    n = row[0] + new[4]; ns = row[1] + new[5]; ssum = row[2] + new[6]; ssumsq = row[3] + new[7]
    smin = min([x for x in (row[4], new[8]) if x is not None], default=None)
    smax = max([x for x in (row[5], new[9]) if x is not None], default=None)
    ni = row[6] + new[10]; isum = row[7] + new[11]
    ref, refts = row[8], row[9]
    if new[13] is not None and (refts is None or new[13] > refts):
        ref, refts = new[12], new[13]
    conn.execute(
        "UPDATE spread_ticks_hourly SET n_ticks=?, n_spread=?, spread_bps_real_sum=?,"
        " spread_bps_real_sumsq=?, spread_bps_real_min=?, spread_bps_real_max=?, n_implied=?,"
        " implied_share_price_sum=?, reference_close_independent_last=?,"
        " reference_close_ts_last=?, market_status_majority=? WHERE ts_hour=? AND"
        " underlying_ticker=? AND token_symbol=? AND platform_id=?",
        (n, ns, ssum, ssumsq, smin, smax, ni, isum, ref, refts,
         _majority([row[10] or "", new[14] or ""]), h, und, tok, plat))


def _guarded_delete(conn, table: str, col: str, cutoff: str, wm: int | None) -> int:  # [B4][B5][C6]
    if wm is None:
        return 0
    max_id = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
    if max_id is None:
        return 0
    deleted = 0
    while True:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {col} < ?"
            f" AND id <= ? AND id < ? LIMIT {PRUNE_BATCH})",
            (cutoff, wm, max_id))
        conn.commit()
        deleted += cur.rowcount
        if cur.rowcount < PRUNE_BATCH:
            return deleted


def compact(db_path: Path, cfg: CompactConfig, dry_run: bool = False) -> dict:
    rep = {"hours_scanned": 0, "hours_done": 0, "rows_deleted": 0, "hourly_pruned": 0,
           "cross_pruned": 0, "oracle_pruned": 0, "rows_reject": 0, "failures": 0}
    if dry_run:  # [B8] strictly read-only, no DDL
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    else:
        from stockgap import db as sdb
        conn = sdb.connect(db_path)
        conn.execute(HOUR_SCHEMA)
        for stmt in INDEXES:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        conn.commit()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    raw_cutoff = (now - timedelta(days=cfg.raw_keep_days)).strftime("%Y-%m-%dT%H")

    row = conn.execute("SELECT MIN(ts_utc) FROM spread_ticks").fetchone()
    if row and row[0]:
        hour = row[0][:13]                                        # [B9] walk hours, no scans
        while hour < raw_cutoff:
            rep["hours_scanned"] += 1
            if not dry_run:
                hi = (datetime.strptime(hour, "%Y-%m-%dT%H") + timedelta(hours=1)
                      ).strftime("%Y-%m-%dT%H")
                try:
                    conn.execute("BEGIN IMMEDIATE")               # [B6]
                    n, rej = _aggregate_hour(conn, hour, hi)
                    rep["rows_reject"] += rej
                    if n:
                        wm = load_watermarks(db_path)             # fresh each hour [B4]
                        cur = conn.execute(
                            "DELETE FROM spread_ticks WHERE ts_utc >= ? AND ts_utc < ?"
                            " AND id <= ? AND id < (SELECT MAX(id) FROM spread_ticks)",
                            (hour + ":00", hi + ":00",
                             (wm or {}).get("spread_ticks", -1)))
                        rep["rows_deleted"] += cur.rowcount
                    rep["hours_done"] += 1
                    conn.commit()
                except Exception as exc:
                    rep["failures"] += 1
                    log.warning("hour %s failed: %s", hour, exc)
                    conn.rollback()
            else:
                conn.execute("SELECT 1 FROM spread_ticks WHERE ts_utc >= ? AND ts_utc < ? LIMIT 1",
                             (hour + ":00", (datetime.strptime(hour, "%Y-%m-%dT%H")
                                             + timedelta(hours=1)).strftime("%Y-%m-%dT%H") + ":00"))
                rep["hours_done"] += 1
            hour = (datetime.strptime(hour, "%Y-%m-%dT%H") + timedelta(hours=1)
                    ).strftime("%Y-%m-%dT%H")

    wm = load_watermarks(db_path)
    for table, col, days, key in (("spread_ticks_hourly", "ts_hour", cfg.hourly_keep_days,
                                   "hourly_pruned"),
                                  ("cross_issuer_ticks", "ts_utc", cfg.cross_keep_days,
                                   "cross_pruned"),
                                  ("oracle_predictions", "snapshot_ts", cfg.oracle_keep_days,
                                   "oracle_pruned")):
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H")
        if dry_run:
            rep[key] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} < ?",
                                    (cutoff,)).fetchone()[0]
        else:
            try:
                conn.execute("BEGIN IMMEDIATE")
                rep[key] = _guarded_delete(conn, table, col, cutoff,
                                           (wm or {}).get(table, wm and 10**9 or None)
                                           if table != "spread_ticks_hourly" else 10**9)
            except Exception as exc:
                rep["failures"] += 1
                log.warning("prune %s failed: %s", table, exc)
                conn.rollback()
    if not dry_run:                                               # [B7] no VACUUM
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError as exc:
            log.info("checkpoint skipped: %s", exc)
    conn.close()
    return rep
```

*(Note for reviewer: the hourly prune is content, not ship stream — its guard is `10**9` i.e.
unbounded but still MAX(id)-protected inside `_guarded_delete`; cross/oracle prunes use their
watermarks. `_guarded_delete` recomputes `MAX(id)` per call so the keeper row moves forward as
the collector inserts.)*

### 2c. CLI wrapper — `scripts/compact_ledger.py` (thin, mirrors `rotate_data.py`)

```python
#!/usr/bin/env python3
"""CLI wrapper for stockgap.compaction. Run nightly under flock -n -E 0, nice/ionice.

    python3 scripts/compact_ledger.py [--data DIR] [--dry-run] [--migrate-indexes]
Env: RAW_KEEP_DAYS=14, HOURLY_KEEP_DAYS=180, CROSS_KEEP_DAYS=180, ORACLE_KEEP_DAYS=180.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stockgap.compaction import compact, load_cfg, migrate_indexes  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("compact_ledger")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=str(Path(__file__).resolve().parents[1] / "data"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--migrate-indexes", action="store_true")
    args = ap.parse_args()
    db = Path(args.data) / "spread_ticks.db"
    if not db.exists():
        log.error("ledger not found: %s", db)
        return 1
    if args.migrate_indexes:
        migrate_indexes(db)
        return 0
    rep = compact(db, load_cfg(), dry_run=args.dry_run)
    log.info("%s %s", "DRY-RUN" if args.dry_run else "OK", rep)
    return 1 if rep["failures"] else 0                           # [C6]


if __name__ == "__main__":
    sys.exit(main())
```

Run `python3 -m pytest tests/ -q` → ALL green (existing 392 + new).

## Task 3 — migration, docs, deferred cron [O1][O2]

1. **One-time supervised index migration** on the real ledger (timed, while watching
   `data/logs/collector.log`): `python3 scripts/compact_ledger.py --migrate-indexes`
2. Dry-run against the real ledger (`--dry-run`) → paste report into PR description.
3. README «Maintenance» note: `rotate_data.py` every 10 min; `compact_ledger.py` daily 04:25;
   the four `*_KEEP_DAYS` knobs (defaults 14/180/180/180); `--vacuum` intentionally absent —
   manual `VACUUM` only with collector stopped.
4. Branch `ledger-compaction`, do NOT merge (operator merges; repo is submission-frozen).
5. **Cron install DEFERRED to the operator after merge** [O1] — document the line in the PR:
   ```
   25 4 * * * cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/compact.lock /usr/bin/ionice -c3 /usr/bin/nice -n 19 /usr/bin/python3 scripts/compact_ledger.py >> data/logs/compact.log 2>&1
   ```
   First real run: supervised, with `data/logs/collector.log` tailed. [O3 follow-up noted in PR:
   the Northflank ledger is never compacted — future work.]
6. Task 1's `rotate_data.py` cron IS installed now (approved code, live on NF already).

## Verification checklist (executor pastes outputs)

- `python3 -m pytest tests/ -q` → 0 failures
- `python3 scripts/compact_ledger.py --migrate-indexes` → 4 indexes, timed
- `python3 scripts/compact_ledger.py --dry-run` → report, DB byte-identical
- `crontab -l | grep -c 'rotate_data.py'` → `1`
- `git status` clean on `ledger-compaction`
