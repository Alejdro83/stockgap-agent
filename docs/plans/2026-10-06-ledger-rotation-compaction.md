# Plan: Rotación de logs local + compactación/retención del ledger SQLite

**Date:** 2026-10-06 · **Author:** LISA (strategist) · **Executor:** Sonnet · **Review gate:** Opus (this document)

## Context (read once, implementer)

Repo: `/home/lab-master/stockgap-agent` (Python 3, stdlib-heavy, pytest suite `tests/`).
The SQLite ledger `data/spread_ticks.db` grows ~1.3M `spread_ticks` rows/day (5.8M today,
projected ~40M/month). `scripts/rotate_data.py` (JSONL/log rotation) already exists and is
wired on Northflank via `scripts/deploy/entrypoint.sh` every 10 min under flock, but nothing
runs it locally, and **nothing** compacts the SQLite ledger.

Goal: (1) run `rotate_data.py` locally on the same cadence as the deploy, (2) add
`scripts/compact_ledger.py` that aggregates raw `spread_ticks` into hourly bars and prunes
by age, with env-configurable retention, dry-run, idempotency and tests. Scientific value is
preserved: the 2026-10-06 signal backtest worked on per-(ticker,hour) means — the hourly bars
exactly cover it.

Constraints (from repo conventions):
- stdlib only (`sqlite3`, `argparse`, `logging`, `datetime`, `pathlib`) — no pandas.
- Tests at `tests/` module level, light deps, no test DB writes outside tmp paths.
- Idempotent and best-effort: failures logged and skipped, never fatal (same style as
  `scripts/rotate_data.py`).
- Never log token/credentials (no secrets involved here anyway).

## Task 1 — cron local para `rotate_data.py` (5 min)

1. Run it once by hand and confirm it exits 0:
   `cd /home/lab-master/stockgap-agent && /usr/bin/flock -n data/rotate.lock /usr/bin/python3 scripts/rotate_data.py`
2. Install this crontab line (via `crontab -l | … | crontab -`, replace nothing, only append;
   verify exactly one `rotate_data.py` line exists afterwards):
   ```
   */10 * * * * cd /home/lab-master/stockgap-agent && /usr/bin/flock -n data/rotate.lock /usr/bin/python3 scripts/rotate_data.py >> data/logs/rotate.log 2>&1
   ```
3. Verify: `crontab -l | grep -c 'rotate_data.py'` prints `1`.

## Task 2 — `scripts/compact_ledger.py` (TDD, 60–90 min)

### 2a. Write the failing test FIRST — `tests/test_compact_ledger.py`

```python
"""Tests for scripts/compact_ledger.py — aggregation, pruning, idempotency, dry-run."""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.compact_ledger import CompactConfig, compact, ensure_schema  # noqa: E402


def _seed(db: Path, hours_ago: list[int]) -> None:
    conn = sqlite3.connect(db)
    ensure_schema(conn)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = []
    for h in hours_ago:
        ts = (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for token, platform, spread in (("AAA", "bstock", "10.0"),
                                        ("AAA", "bstock", "20.0"),
                                        ("BBB", "ondo", "-5.0")):
            rows.append((ts, "AAA" if token == "AAA" else "BBB", token, platform,
                         "100.0", "1.0", "100.0", "95.0", "0.5", "5.0", "0.5",
                         "regular", 1))
    conn.executemany(
        "INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
        " token_contract, platform_id, token_price, token_to_share_ratio,"
        " reference_price, implied_share_price, spread_abs, spread_bps, spread_pct,"
        " market_status, open_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r[0], i, r[1], r[2], "0x1", r[3], r[4], r[5], r[6], r[7], r[8], r[9],
          r[10], r[11], r[12]) for i, r in enumerate(rows)])
    conn.commit()
    conn.close()


def test_aggregation_math_and_prune(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, hours_ago=[24 * 8, 24 * 8, 2])  # two old hours' worth + one fresh
    cfg = CompactConfig(raw_keep_days=7, hourly_keep_days=180,
                        cross_keep_days=180, oracle_keep_days=180)
    report = compact(db, cfg, dry_run=False)
    conn = sqlite3.connect(db)
    old_raw = conn.execute(
        "SELECT COUNT(*) FROM spread_ticks WHERE ts_utc < ?",
        ((datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H"),),
    ).fetchone()[0]
    assert old_raw == 0, "raw rows older than cutoff must be aggregated away"
    fresh = conn.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0]
    assert fresh > 0, "recent raw rows must survive"
    agg = conn.execute(
        "SELECT n_ticks, spread_bps_real_avg, spread_bps_real_min, spread_bps_real_max"
        " FROM spread_ticks_hourly WHERE token_symbol='AAA'"
    ).fetchone()
    assert agg is not None and agg[0] == 2 and agg[1] == 15.0 and agg[2] == 10.0 and agg[3] == 20.0
    assert report["hours_aggregated"] >= 1


def test_idempotent_second_run(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, hours_ago=[24 * 8])
    cfg = CompactConfig(raw_keep_days=7, hourly_keep_days=180,
                        cross_keep_days=180, oracle_keep_days=180)
    compact(db, cfg, dry_run=False)
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0]
    compact(db, cfg, dry_run=False)
    after = conn.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0]
    assert before == after == 1


def test_dry_run_touches_nothing(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, hours_ago=[24 * 8])
    cfg = CompactConfig(raw_keep_days=7, hourly_keep_days=180,
                        cross_keep_days=180, oracle_keep_days=180)
    compact(db, cfg, dry_run=True)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0] == 3


def test_hourly_prune(tmp_path):
    db = tmp_path / "t.db"
    _seed(db, hours_ago=[24 * 40])
    cfg = CompactConfig(raw_keep_days=7, hourly_keep_days=30,
                        cross_keep_days=180, oracle_keep_days=180)
    compact(db, cfg, dry_run=False)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] == 0
```

Run `python3 -m pytest tests/test_compact_ledger.py -x` → must FAIL (module does not exist).

### 2b. Implement `scripts/compact_ledger.py`

```python
#!/usr/bin/env python3
"""SQLite ledger compaction & retention.

Aggregates raw spread_ticks rows older than RAW_KEEP_DAYS into spread_ticks_hourly
(1 row per (hour, underlying, token, platform): avg/min/max of spread_bps_real,
avg implied_share_price, last reference_close_independent, majority market_status),
then deletes the aggregated raw rows hour by hour. Finally prunes hourly,
cross_issuer_ticks and oracle_predictions rows past their KEEP_DAYS.

Idempotent (INSERT OR REPLACE by primary key recomputes each hour fully),
best-effort (errors are logged, one hour at a time), dry-run capable. The cron
wraps it in ``flock -n data/compact.lock``.

    python3 scripts/compact_ledger.py [--data DIR] [--dry-run]

Env (all optional): RAW_KEEP_DAYS=14, HOURLY_KEEP_DAYS=180,
CROSS_KEEP_DAYS=180, ORACLE_KEEP_DAYS=180.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("stockgap.compact")

HOUR_SCHEMA = """
CREATE TABLE IF NOT EXISTS spread_ticks_hourly (
    ts_hour TEXT NOT NULL,
    underlying_ticker TEXT,
    token_symbol TEXT,
    platform_id TEXT,
    n_ticks INTEGER NOT NULL,
    spread_bps_real_avg REAL,
    spread_bps_real_min REAL,
    spread_bps_real_max REAL,
    implied_share_price_avg REAL,
    reference_close_independent_last REAL,
    market_status_majority TEXT,
    PRIMARY KEY (ts_hour, token_symbol, platform_id)
)"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_spread_ts ON spread_ticks(ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_hourly_ts ON spread_ticks_hourly(ts_hour)",
    "CREATE INDEX IF NOT EXISTS idx_cross_ts ON cross_issuer_ticks(ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_oracle_ts ON oracle_predictions(snapshot_ts)",
)


@dataclass
class CompactConfig:
    raw_keep_days: int = 14
    hourly_keep_days: int = 180
    cross_keep_days: int = 180
    oracle_keep_days: int = 180


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(HOUR_SCHEMA)
    for stmt in INDEXES:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:  # table may not exist in tiny fixtures
            pass
    conn.commit()


def _majority(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if counts else ""


def compact(db_path: Path, cfg: CompactConfig, dry_run: bool = False) -> dict:
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    raw_cutoff = (now - timedelta(days=cfg.raw_keep_days)).strftime("%Y-%m-%dT%H")
    report = {"hours_aggregated": 0, "rows_deleted": 0, "hourly_pruned": 0,
              "cross_pruned": 0, "oracle_pruned": 0}

    hours = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(ts_utc,1,13) FROM spread_ticks WHERE substr(ts_utc,1,13) < ?",
        (raw_cutoff,)).fetchall()]
    report["hours_aggregated"] = len(hours)
    if dry_run:
        report["rows_deleted"] = conn.execute(
            "SELECT COUNT(*) FROM spread_ticks WHERE substr(ts_utc,1,13) < ?",
            (raw_cutoff,)).fetchone()[0]
    else:
        for hour in sorted(hours):
            try:
                hi = (datetime.strptime(hour, "%Y-%m-%dT%H") + timedelta(hours=1)
                      ).strftime("%Y-%m-%dT%H")
                rows = conn.execute(
                    "SELECT underlying_ticker, token_symbol, platform_id,"
                    " spread_bps_real, implied_share_price, reference_close_independent,"
                    " market_status FROM spread_ticks WHERE ts_utc >= ? AND ts_utc < ?",
                    (hour + ":00", hi + ":00")).fetchall()
                buckets: dict[tuple, list] = {}
                for r in rows:
                    buckets.setdefault((r[0], r[1], r[2]), []).append(r)
                agg = []
                for (und, tok, plat), items in buckets.items():
                    spr = [float(x[3]) for x in items if x[3] not in (None, "")]
                    imp = [float(x[4]) for x in items if x[4] not in (None, "")]
                    refs = [x[5] for x in items if x[5] not in (None, "")]
                    agg.append((hour, und, tok, plat, len(items),
                                sum(spr) / len(spr) if spr else None,
                                min(spr) if spr else None,
                                max(spr) if spr else None,
                                sum(imp) / len(imp) if imp else None,
                                refs[-1] if refs else None,
                                _majority([x[6] or "" for x in items])))
                conn.execute("DELETE FROM spread_ticks_hourly WHERE ts_hour=?", (hour,))
                conn.executemany(
                    "INSERT OR REPLACE INTO spread_ticks_hourly VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    agg)
                cur = conn.execute(
                    "DELETE FROM spread_ticks WHERE ts_utc >= ? AND ts_utc < ?",
                    (hour + ":00", hi + ":00"))
                report["rows_deleted"] += cur.rowcount
                report["hours_aggregated_done"] = report.get("hours_aggregated_done", 0) + 1
                conn.commit()
            except Exception as exc:  # best-effort, one hour at a time
                log.warning("hour %s failed: %s", hour, exc)
                conn.rollback()

    for table, col, days, key in (("spread_ticks_hourly", "ts_hour", cfg.hourly_keep_days,
                                   "hourly_pruned"),
                                  ("cross_issuer_ticks", "ts_utc", cfg.cross_keep_days,
                                   "cross_pruned"),
                                  ("oracle_predictions", "snapshot_ts", cfg.oracle_keep_days,
                                   "oracle_pruned")):
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H")
        if dry_run:
            report[key] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} < ?", (cutoff,)).fetchone()[0]
        else:
            report[key] = conn.execute(
                f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)).rowcount
            conn.commit()

    if not dry_run:
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            log.info("VACUUM skipped: %s", exc)
    conn.close()
    return report


def load_cfg() -> CompactConfig:
    return CompactConfig(
        raw_keep_days=int(os.getenv("RAW_KEEP_DAYS", "14")),
        hourly_keep_days=int(os.getenv("HOURLY_KEEP_DAYS", "180")),
        cross_keep_days=int(os.getenv("CROSS_KEEP_DAYS", "180")),
        oracle_keep_days=int(os.getenv("ORACLE_KEEP_DAYS", "180")))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    default_data = Path(__file__).resolve().parents[1] / "data"
    ap.add_argument("--data", default=str(default_data))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = Path(args.data) / "spread_ticks.db"
    if not db.exists():
        log.error("ledger not found: %s", db)
        return 1
    rep = compact(db, load_cfg(), dry_run=args.dry_run)
    mode = "DRY-RUN" if args.dry_run else "OK"
    log.info("%s hours=%s rows_deleted=%s hourly_pruned=%s cross_pruned=%s oracle_pruned=%s",
             mode, rep["hours_aggregated"], rep["rows_deleted"], rep["hourly_pruned"],
             rep["cross_pruned"], rep["oracle_pruned"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run `python3 -m pytest tests/test_compact_ledger.py tests/ -x -q` → ALL green.

## Task 3 — wire-up + docs (15 min)

1. Crontab append (verify exactly one `compact_ledger.py` line afterwards):
   ```
   25 4 * * * cd /home/lab-master/stockgap-agent && /usr/bin/flock -n data/compact.lock /usr/bin/python3 scripts/compact_ledger.py >> data/logs/compact.log 2>&1
   ```
2. Dry-run against the real ledger and paste output into the PR description:
   `python3 scripts/compact_ledger.py --dry-run`
3. README «Maintenance» note (3 lines): what rotates (`rotate_data.py` every 10 min), what
   compacts (`compact_ledger.py` daily 04:25), and the four `*_KEEP_DAYS` env knobs.
4. Commit on a branch `ledger-compaction`, do NOT merge to `master` (repo is submission-frozen;
   merge is the operator's call).

## Verification checklist (executor runs all, pastes outputs)

- `python3 -m pytest tests/ -q` → 0 failures
- `python3 scripts/compact_ledger.py --dry-run` → report printed, no writes
- `crontab -l | grep -c 'rotate_data.py\|compact_ledger.py'` → `2`
- `git status` clean on branch `ledger-compaction`
