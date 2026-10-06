# Round 2 design review: ledger compaction v2

**Verdict: changes requested.** v2 fixes the round-1 *structure*: it uses `db.connect`, merges instead of overwriting, checks the watermark and MAX(id), makes dry-run read-only, walks hours instead of scanning, clamps the config, and defers the cron. But on the real repo it has three new problems:

- **Compaction never deletes anything in production.** The watermark loader expects the wrong file format.
- **Merge semantics now double-count.** Every row that is aggregated but not deleted gets merged again the next night.
- **The planned order of operations crashes.** The index migration runs before the hourly table exists.

At least 6 of the 11 tests fail against the plan's own implementation, so the TDD step is still broken.

I checked everything below against the real code and DB. Python is 3.11.15, so `sqlite3` uses the legacy implicit-BEGIN mode. `data/ship_meta.json` is `{"version":1,"tables":{…}}`. `spread_ticks` has ids 1..5,987,472, and its oldest row is `2026-10-02T00:21`. No `ts` indexes exist yet.

## Blockers (new, or round-1 items that aren't really resolved)

**N1. The watermark loader can never succeed, so compaction never deletes.** `ship_rows.py:362` writes `{"version": 1, "tables": {...}}`, but `load_watermarks` checks `data.get("spread_ticks")` at the top level. On the real file that is always `None`, the assert fails, and the function returns `None` every run. The tests write a flat dict, so they pass while production silently compacts nothing. It's also the trigger for N2.
- Fix: read `data["tables"]` and check `version == 1`, ideally by reusing `ship_rows.read_watermark`'s strict parser. Don't use `assert` for validation, because it's stripped under `-O`.
- Write all fixtures in the real format.

**N2. Rows that are aggregated but not deleted are merged again every night (B3 fix turned into a double-count bug).** `_aggregate_hour` merges *every* raw row in the hour, but the DELETE only removes rows with `id <= wm AND id < MAX(id)`. Every survivor is still raw and older than the cutoff, so the next run merges it into the bar again. That happens for:
- (a) every row when the watermark is missing or unusable, which is always in production because of N1;
- (b) unshipped rows while the shipper is behind;
- (c) the MAX(id) row once the collector has stopped.

`n_ticks`, the sums and sumsq grow without bound. `test_missing_ship_meta_blocks_all_deletes` asserts this behaviour as correct.

Rule to enforce: **aggregate exactly the set of ids you delete, in the same transaction.** Select with the same predicate as the DELETE (`ts in hour AND id <= wm AND id < max_id`), merge those rows, delete those rows, then COMMIT. With no watermark, skip aggregation entirely. Add a test that runs `compact()` twice in each of the three scenarios and asserts the bar is unchanged after the second run.

**N3. Aggregation and delete aren't atomic, which N2 needs.** `_aggregate_hour` ends with `conn.commit()`, which ends the `BEGIN IMMEDIATE` transaction. The DELETE then runs in a new implicit transaction, which is deferred, not IMMEDIATE, so B6 isn't met here. If that DELETE fails (busy, snapshot error), `rollback()` undoes only the delete. The merged bar is already committed, so the next run merges again. Remove the inner commit and use `db.write_txn()` (it already exists in `stockgap/db.py`) around merge plus delete.

**N4. The prune batches after the first one aren't IMMEDIATE (B6 only partly fixed).** `_guarded_delete` commits after every batch. From batch 2 on, each `DELETE … WHERE id IN (SELECT …)` starts with an implicit deferred BEGIN: it reads first, then upgrades to a write, which can hit `SQLITE_BUSY_SNAPSHOT` with no busy wait while the collector writes about 30 rows/s. Wrap each batch in `write_txn()`. Re-read `MAX(id)` inside each batch transaction instead of once before the loop.

**N5. The hourly prune fails every night.** `_guarded_delete` runs `SELECT MAX(id) FROM spread_ticks_hourly`, but that table has no `id` column (its PK is composite). The result is an OperationalError, so `failures += 1`, `main()` exits 1 every night, and hourly bars are never pruned. No test covers it, because no test has a bar old enough to prune.
- Fix: give `_guarded_delete` a `guard_ids: bool`. The hourly table isn't shipped, so prune it with a batched `DELETE … WHERE ts_hour < ?` keyed on `rowid`.
- Add a test that seeds a bar older than `hourly_keep_days`.

**N6. `migrate_indexes` crashes on the documented order and blocks the collector.**
- Task 3 runs `--migrate-indexes` before any non-dry `compact()`, so `spread_ticks_hourly` doesn't exist yet. `idx_hourly_ts` then fails with `no such table`. That happens after the roughly 6M-row `idx_spread_ts` build inside the same transaction, so the exception and `close()` roll the whole migration back. The migration can never succeed.
- It also uses a bare `sqlite3.connect` (round-1 B6 said all writers go through `db.connect`) and holds one `BEGIN IMMEDIATE` across all four builds. That means tens of seconds of write lock against the collector's 10 s budget.

Fix:
- create `HOUR_SCHEMA` inside `migrate_indexes`;
- build one index per transaction;
- use `db.connect`;
- tell the runbook to **stop the collector** during the migration, not just "watch the log".

**N7. Nightly `compact()` still builds indexes itself (B9 isn't really resolved).** The non-dry path runs every `INDEXES` statement and swallows `OperationalError`. If the operator skips or botches the migration, the 04:25 cron builds the multi-million-row index and holds the write lock for the duration, which is exactly what B9 forbade. When the build hits a busy lock, the error is swallowed and the job goes on to do per-hour range scans without an index.

Fix: the nightly path runs **no** index DDL. It checks `sqlite_master` for `idx_spread_ts` and refuses with ERROR and exit 1 if it's missing. Only `HOUR_SCHEMA`'s `CREATE TABLE IF NOT EXISTS` may stay, because it's cheap.

**N8. The write lock is held while Python processes about 100k rows, and `ionice -c3` turns that into priority inversion.** Each hour holds `BEGIN IMMEDIATE` across fetching and aggregating about 108k rows (30 rows/s × 3600) in Python. Under `ionice -c3`, the idle IO class, a busy disk can starve the compactor's IO *while it holds the write lock*, and the collector times out at 10 s.

Fix: in this order:
1. Aggregate the hour from a read snapshot, capturing the exact id range.
2. `BEGIN IMMEDIATE`.
3. Re-check the bar and merge.
4. `DELETE … WHERE id BETWEEN lo AND hi AND <same guards>`.
5. COMMIT.

This keeps the lock to milliseconds and also gives N2's "delete exactly what you aggregated" rule. Use `ionice -c2 -n7`, not `-c3`.

**N9. Six of the eleven tests fail against the v2 implementation.**

| Test | Problem |
|---|---|
| `test_aggregation_math_exact` | The hour walk goes from hour −192 to the cutoff at −48, so `hours_done == 144`, not 2 (empty hours count as "done"). `COUNT(*) spread_ticks == 0` contradicts B5: the MAX(id) row (BBB @ −191) survives. |
| `test_dry_run_is_read_only` | The dry-run path never touches `rows_deleted`, so `rows_deleted >= 1` fails. |
| `test_cutoff_boundary_and_month_boundary` | The month-boundary row is inserted last, so it holds MAX(id) and is kept. `all(t >= cutoff)` then fails on `2026-09-30T23…`. |
| `test_lock_contention_waits` | `holder` was created on the main thread, so `threading.Timer(..., holder.commit)` raises `ProgrammingError` (check_same_thread). The lock is never released, and `compact()`'s first write (`HOUR_SCHEMA`) raises "database is locked" after 10 s, *uncaught*. Use `check_same_thread=False`, assert elapsed ≥ 0.7 s, and assert the rows were actually deleted. |
| `test_never_deletes_unshipped_rows` | Passes, but it pins nothing about N2: no second run. |
| `test_missing_ship_meta_blocks_all_deletes` | Passes, but asserts the N2 bug as correct behaviour. |

Fix:
- Count `hours_done` only for hours that had rows, or assert `hours_with_rows`.
- Seed an extra, newer "keeper" row in every fixture so MAX(id) protection doesn't interfere with what's being tested.
- Report planned work in dry-run under `planned_*` keys, using the same guards (watermark, MAX(id)) the real run uses.

**N10. Dry-run reports numbers the real run won't produce.** The prune counts are bare `COUNT(*) WHERE col < cutoff`, with no watermark or MAX(id) guards. `hours_done` counts every hour walked. The `SELECT 1 … LIMIT 1` probe result is thrown away. The PR-description report from Task 3.2 would be misleading.
- Compute the same guarded counts the real run would use.
- Use `db.connect_ro`, which exists. The "if present" hedge is unnecessary.

## Correctness (medium)

**N11. Majority merge is wrong.** `_majority([old_majority, new_majority])` compares two labels with count 1 each, and the tie-break is the lexicographic max. A 54k-tick `"closed"` bar plus one `"regular"` straggler becomes `"regular"`. Either store per-status counts (or `n_regular`/`n_open`), or keep the old majority unless the new batch is larger than `n_ticks`.

**N12. Reference-close edge cases.**
- With equal `ts`, the strict `>` keeps the *first* id, contradicting C1's "last by `ORDER BY ts_utc, id`". Use `>=`.
- A non-finite or garbage `ref` sets `refts` but stores `ref=NULL`. On merge, a newer NULL then overwrites a valid older close, and the reject isn't counted.

Fix: run `_finite(ref)` before choosing a candidate, and only advance `refts` when the value is finite. Apply the same rule in `_merge_bar`.

**N13. The prune guard falls back to unbounded.** `(wm or {}).get(table, wm and 10**9 or None)`: if a watermark dict exists but lacks the table, the guard becomes `10**9`, i.e. no watermark at all. It's unreachable today only because of the `assert`. The default must be `None`.

**N14. TRUNCATE checkpoint can stall the collector (my round-1 B7 advice was wrong).** `wal_checkpoint(TRUNCATE)` waits on the busy handler for readers (dashboard, MCP) and blocks new writers while pending. That's up to 10 s of collector stall. It also returns a busy row instead of raising, so the `except` does nothing. Use `PRAGMA wal_checkpoint(PASSIVE)`: `journal_size_limit=64MiB` already trims the WAL. Log the returned row.

**N15. Clock-sensitive tests.** Fixtures call `datetime.now()` separately from `compact()`. A run that crosses an hour boundary moves `onedge` into compaction, and flaky tests result. Add `now: datetime | None = None` to `compact()` and inject it.

**N16. `test_config_validation` leaks global env.** It sets `X=y`, never cleans it up, and leaks everything if an assert fails. Use `monkeypatch.setenv`. Also add garbage `HOURLY_KEEP_DAYS` and negative-value cases.

**N17. The walk advances outside the per-hour `try`.** The `hour = strptime(...)` advance sits outside the `try`. A malformed `MIN(ts_utc)` crashes the whole run instead of counting a failure. Validate the format of `MIN(ts_utc)` up front.

**N18. The fixture DDL is still a placeholder.** `DDL = [ ... verbatim ... ]` isn't "complete", and copying it creates drift from `collector.py`/`oracle.py`. Item 1 asked for reuse: import the DDL constants, or snapshot them from `sqlite_master` once in conftest.

## Round-1 items: status

| Item | Status |
|---|---|
| B1, C3, C4, C5, C6 (`main()` exit code), C7 (walk/format), O1, O2 (except `-c3`, see N8), O3, O4, O5 | Resolved |
| B2 | Partly: data fixed, assertions fail (N9) |
| B3 | Regressed into double-counting (N2, N3) |
| B4 | Broken in production (N1, N13) |
| B5 | Resolved for `spread_ticks`/cross/oracle; crashes on hourly (N5); conflicts with tests (N9) |
| B6 | Partly (N3, N4, N6) |
| B7 | Resolved, but TRUNCATE needs changing (N14) |
| B8 | Read-only, yes; report is wrong (N10) |
| B9 | Not really (N6, N7) |
| C1 | Off by tie (N12) |
| C2 | Resolved |
| C6 | Batching OK; batches aren't IMMEDIATE (N4) |

## Required for v3

1. Parse `ship_meta.json` in its real `{"version", "tables"}` format, strictly, reusing the shipper's parser (N1).
2. Aggregate exactly the ids being deleted, in a single short `write_txn` per hour (aggregate from a snapshot first); with no watermark, skip aggregation. Add a test that a second run leaves the bar unchanged in the three survivor scenarios (N2, N3, N8).
3. Run every prune batch in its own `write_txn` (N4).
4. Prune hourly bars without the `id` guard, and test it (N5).
5. In `migrate_indexes`: create the hourly table first, one transaction per index, `db.connect`, collector stopped during the run (N6).
6. Nightly path does no index DDL and fails loudly if the index is missing (N7).
7. Fix the six tests: keeper row, `hours_with_rows`, `check_same_thread=False` plus an elapsed assertion, injected `now`, monkeypatch (N9, N15, N16).
8. Dry-run uses `connect_ro` and reports guarded `planned_*` counts (N10).
9. Fix the majority merge, ref tie and non-finite ref handling, the `None` guard default, `PASSIVE` checkpoint, the walk-advance `try`, and import the DDL instead of copying it (N11–N14, N17, N18).
10. Change the cron from `ionice -c3` to `-c2 -n7` (N8).

VERDICT: changes_requested
