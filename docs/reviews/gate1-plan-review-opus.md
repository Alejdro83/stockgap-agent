# Design review: ledger compaction and local log rotation plan

**Verdict: changes requested.** The plan can't pass its own TDD step, and on the production ledger it would lose data, block the collector, and crash the shipper.

I checked it against the real code and `data/spread_ticks.db`. The DB is 2.2 GB in WAL mode with 5,979,664 rows. Every `ts_utc` value is 24 characters and ends in `Z`. 1,034,755 rows (17%) have an empty `spread_bps_real`. No rows have a NULL `token_symbol` or `platform_id`. The shipper's watermark for `spread_ticks` is 5,976,248.

## Blockers

**B1. The test fixture can't run.** `_seed()` calls `ensure_schema()`, which only creates `spread_ticks_hourly`. It never creates `spread_ticks`, `cross_issuer_ticks` or `oracle_predictions`, so the first INSERT fails with `no such table: spread_ticks`. The prune loop has no `try` around it, so it would also crash on the missing tables. The suite fails for the wrong reason before and after the implementation, so the TDD step is meaningless.

**B2. The test data can't produce the asserted numbers.**
- The `spread` loop variable (`10.0`, `20.0`, `-5.0`) is never written. Every row gets `spread_bps="5.0"`, and `spread_bps_real` (the column being aggregated) is never set, so it is NULL. The average is `None`, not 15.0.
- `hours_ago=[24*8, 24*8, 2]` seeds the same hour twice, so AAA has `n_ticks=4`, not 2. The comment "two old hours' worth" is wrong.
- `assert report["hours_aggregated"] >= 1` passes even when every hour fails, because the value is `len(hours)` counted before any work. `hours_aggregated_done` is computed but never reported.

**B3. `test_idempotent_second_run` doesn't test idempotency.** After the first run the raw rows are gone, so the second run does nothing. The interesting case is a late raw row arriving for an hour that is already compacted. For that hour, `DELETE FROM spread_ticks_hourly WHERE ts_hour=?` followed by re-aggregating only the new raw rows overwrites a bar built from about 54k ticks with a bar built from one. That is silent loss of scientific data. Fix it one of two ways:
- merge instead of replace: store `n`, `sum`, `min`, `max`, plus `first`/`last` with their `ts`, and combine on conflict; or
- refuse and log loudly when the hour already exists.

Then add a test that inserts a straggler into an already-compacted hour and checks the merged bar.

**B4. Compaction can delete rows the shipper hasn't sent yet.** `scripts/ship_rows.py` sends rows `WHERE id > watermark`. If the shipper or Northflank is down longer than `RAW_KEEP_DAYS`, the compactor deletes unshipped raw rows and they never reach Northflank. Required guard: never delete `spread_ticks` rows with `id > watermark['spread_ticks']` from `data/ship_meta.json`. The same applies to the prune of `cross_issuer_ticks` and `oracle_predictions`. If the watermark file is missing or corrupt, don't delete anything.

**B5. Emptying a table kills the shipper.** `ship_rows.py:699-705` exits loudly when `local MAX(id) < watermark`. If a prune or compaction ever empties a table (for example, the collector was paused for more than 14 days), `MAX(id)` becomes 0. The shipper then refuses every 5 minutes, forever. The compactor must never delete the row holding a table's `MAX(id)`. Add a test for this.

**B6. Locking ignores the repo's own write rules.** `stockgap/db.py` says every writer must use `db.connect()`, which sets a 10 s busy timeout, WAL and a 64 MiB journal size limit. It must also use `BEGIN IMMEDIATE`, because a deferred read-to-write upgrade ignores `busy_timeout`. The collector has a history of 176 "database is locked" failures (`collector.py:525`). The plan instead uses a bare `sqlite3.connect()` with a 5 s timeout and deferred transactions, doing a SELECT and then a DELETE inside each hour. Use `db.connect()` with an explicit `BEGIN IMMEDIATE` per hour, and add a test that runs with a concurrent writer holding the lock.

**B7. `VACUUM` on every run is unacceptable.** On a 2.2 GB live WAL database (growing toward about 15 GB at steady state), VACUUM:
- holds the write lock for minutes, far past the collector's 10 s budget, so ticks are lost;
- pushes the whole DB through the WAL, overriding the 64 MiB journal size limit.

Freed pages are reused anyway. Remove VACUUM. If it's wanted at all, make it an opt-in `--vacuum` flag the operator runs by hand while the collector is stopped. Run `PRAGMA wal_checkpoint(TRUNCATE)` at the end instead.

**B8. Dry-run writes to the production ledger.** `compact()` calls `ensure_schema()` before checking `dry_run`, so a dry run:
- creates `spread_ticks_hourly`;
- builds `idx_spread_ts` on 6M rows, holding the write lock for tens of seconds (collector stall, see B6);
- builds indexes on `cross_issuer_ticks` and `oracle_predictions`.

Task 3.2 runs exactly this against production, and Task 3's checklist claims "no writes". A dry run must open the DB through `db.connect_ro()` and do no DDL. Add a test that checks the file's bytes/mtime and `sqlite_master` are unchanged after a dry run.

**B9. Index creation needs its own migration step.** Building `idx_spread_ts` on the live DB inside a nightly job is a migration. Make it an explicit one-time `--migrate-indexes` step, timed and documented. Or avoid the index altogether: ids increase with time (the R5 assumption `ship_rows` already relies on), so a binary search over `id` finds the cutoff, and you can then work in `id` ranges.

Separately, `SELECT DISTINCT substr(ts_utc,1,13) … WHERE substr(…) < ?` can never use an index. It scans the full table (heading toward 40M rows) every night. Iterate hour ranges from `MIN(ts_utc)` to the cutoff instead.

## Correctness

**C1. "Last" reference close isn't deterministic.** `refs[-1]` comes from a SELECT without `ORDER BY`. Add `ORDER BY ts_utc, id`.

**C2. Averages can't be re-aggregated.** `n_ticks` counts every row, but the average uses only rows with a non-empty `spread_bps_real` (about 17% of rows are empty). Anyone re-aggregating to daily means from `n_ticks × avg` gets the wrong answer. Store:
- `n_spread`, `spread_bps_real_sum` and `spread_bps_real_sumsq` (for stddev);
- `n_implied` and `implied_share_price_sum`.

Also check which columns the 2026-10-06 backtest actually used. "Exactly covers it" is asserted, not shown; if it also used `spread_bps` or `token_price`, keep those too.

**C3. The bucket key and the primary key disagree.** Buckets group by `(underlying, token, platform)`, but the PK is `(ts_hour, token_symbol, platform_id)`. If they ever diverge, `INSERT OR REPLACE` silently drops a bucket. The data is currently 1:1, but put `underlying_ticker` in the PK or bucket by the PK. Also mark the PK columns `NOT NULL`: SQLite allows duplicate NULLs in a non-integer PK.

**C4. Bad numbers can poison an hour.** `float()` on a non-numeric value fails the whole hour every night, and the raw rows are kept with only a warning. `float("inf")` or `"nan"` gets into the averages. Parse with an `isfinite` filter (the same idiom as `rotate_data._env_*`), count rejects, and add a test.

**C5. Retention settings aren't validated.** `load_cfg()` does a bare `int()`: bad input crashes, and `0` or negative values delete aggressively. If `HOURLY_KEEP_DAYS < RAW_KEEP_DAYS`, hourly bars are pruned the moment they're built. Do the following:
- clamp `RAW_KEEP_DAYS` to at least 2;
- require `HOURLY_KEEP_DAYS >= RAW_KEEP_DAYS + 1`;
- fall back to the default on garbage, like `rotate_data`;
- add tests for all three.

The test fixtures use 7 days but production defaults to 14; the README must state 14.

**C6. Prune and failure handling.**
- The prune DELETEs run unguarded and unbatched. Wrap each table in `try`, and batch (for example `DELETE … WHERE id IN (SELECT id … LIMIT 50000)`) so lock windows stay short.
- `main()` returns 0 even when hours failed. Return non-zero, or at least log at ERROR with a count, so cron and the logs show it.

**C7. Time edges are fine and need a pin.** All computation is in UTC, the cutoff is truncated to the hour, and every stored timestamp has the same 24-character `Z` format, so string comparison, month/day boundaries and the incomplete current hour all work. With `RAW_KEEP_DAYS ≥ 1`, the current partial hour can never be compacted. Add a test that seeds rows at `cutoff_hour - 1µs` and at exactly the `cutoff_hour` boundary to pin this. Also seed across a month boundary (e.g. `…-09-30T23` / `…-10-01T00`), because `hi` is computed through `strptime`.

## Operational

**O1. The cron line runs unreviewed code.** Task 3 installs the `compact_ledger.py` cron while the code only lives on the unmerged `ledger-compaction` branch, and cron runs whatever is checked out. Install the compaction cron only after the operator merges. Before enabling deletes, run the dry run first, then one supervised real run with the collector log tailed.

**O2. Match the deploy entrypoint's cron conventions.** Use `flock -n -E 0` and `nice -n 19` (and `ionice -c3` for compaction), as `entrypoint.sh:278` does. Without `-E 0`, lock contention exits 1 and looks like a failure.

**O3. Northflank will keep growing.** Northflank's ledger is never compacted, so it grows without bound. That's out of scope, but note it as follow-up work.

**O4. Pin the test import and naming conventions.** `scripts/` has no `__init__.py`. `from scripts.compact_ledger import …` only works as a namespace package. Either put the logic in `stockgap/compaction.py`, with the script as a thin wrapper (the `rotation.py`/`rotate_data.py` pattern), or say explicitly that the namespace import is intended. The logger name should follow the repo convention (`rotate_data` uses `getLogger("rotate_data")`).

**O5. The rotate cron (Task 1) is fine.** One small check: confirm `data/alerts-cron.log` and `data/oracle-cron.log` rotate under `data/*.log` with copytruncate. They should.

## Required changes

1. Make the fixture create the real collector and oracle schema (reuse `stockgap.collector` and `stockgap.oracle` DDL), and seed `spread_bps_real` with the intended values.
2. Correct the fixture hours and assertions (distinct old hours; exact `n_ticks`, `n_spread`, avg/min/max per bucket), and assert on a "done" counter, not the hours found.
3. Make re-compacting an hour merge with the existing bar (or refuse), and add a test for a late row arriving in an already-compacted hour.
4. Never delete rows with `id > ship watermark`, and delete nothing if `ship_meta.json` is missing or corrupt. Add a test.
5. Never empty an append table or delete its `MAX(id)` row. Add a test that runs `ship_rows`'s sanity check after compaction.
6. Use `stockgap.db.connect()` and `BEGIN IMMEDIATE` per hour and per prune batch. Add a lock-contention test.
7. Remove the unconditional `VACUUM`; end with `wal_checkpoint(TRUNCATE)`, and make VACUUM opt-in only if wanted.
8. Make dry-run read-only (`connect_ro`, no DDL), and add a test that the DB is unchanged after a dry run.
9. Move index creation into an explicit migration step, or use id-range binary search. Drop the DISTINCT-substr full scan.
10. Add `ORDER BY ts_utc, id` for the last reference close.
11. Add the count/sum/sumsq columns, and confirm the backtest's column needs.
12. Align the primary key with the bucket key and make its columns `NOT NULL`.
13. Parse numbers with an `isfinite` filter, count rejects, and test it.
14. Validate and clamp the env knobs, enforce hourly retention ≥ raw retention + 1, and test this.
15. Batch prune and guard it per table; return non-zero from `main()` on failures.
16. Add tests for the cutoff boundary and a month boundary.
17. Install the compaction cron only after merge, with `flock -n -E 0 nice -n 19 ionice -c3`; do a supervised first real run.
18. Follow the `rotation.py`/`rotate_data.py` module split, or justify the `scripts.` namespace import.

VERDICT: changes_requested
