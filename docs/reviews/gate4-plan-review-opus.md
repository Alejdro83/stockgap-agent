# Round 4 design review: ledger compaction v4

**Verdict: changes requested.** I assembled v4's `compaction.py` and test file exactly as written, filling the two `...` placeholders with v3's `HOUR_SCHEMA` and `INDEXES`, and ran them in a scratch copy of the repo (`/tmp/g4`). Results:

- **As written:** every test errors in the fixture, at `ALTER TABLE oracle_predictions ADD COLUMN reference_source`.
- **With that ALTER removed:** 11 of 17 tests fail.
- **With the fixture's watermark helper (`_wm`) also fixed:** 5 still fail. One is a real code regression; the other four are test-arithmetic errors.

The design itself is now sound, and I verified that the chunked rowcount invariant holds. But the plan's tests still don't pass against its own implementation, for the fourth round running.

## Round-3 items: status

| Item | Status |
|---|---|
| R1 import `read_watermark`, `ShipError` handling, guard against a watermark above MAX(id) | **Code resolved.** The parser is reused correctly and the guard uses the shipper's own direction (`MAX < wm` means refuse). **But it breaks every test** (F1). |
| R2 DELETE uses min/max ids, a ts predicate and a rowcount check | **Code resolved**, and I checked the chunk interaction (see "Verified OK"). **The test doesn't test it** (F4). The fix also lost the MAX(id) keeper (F2). |
| R3 keeper after every straggler | Resolved. |
| R4 fixture builds the indexes | Resolved. |
| R5 fixture schema | **Partial.** `executescript`, `migrate_schema` and `ensure_wal` are right. The oracle ALTER is wrong, and that was **my own R5 error**: `oracle.SCHEMA` already declares `reference_source` (oracle.py:119), and the ALTER at :393 only runs for old DBs. The result is `duplicate column name` in every test (F3). |
| R6 dry-run tolerates missing tables, reports `index_present` | Resolved. I probed a fresh pre-migration DB: no crash, `index_present: False`. |
| R7 hourly-prune arithmetic | Partial. The bar is right, but run 2 then compacts the keeper and the final count assert fails (F2). |
| R8 ref rejects counted | Resolved. `rows_reject == 2` passes once `_wm` is fixed. |
| R9 ref tie and majority counts | **Code resolved** (it passes once the hour is corrected). **The test seeds the wrong hour** (F5). |
| R10 WAL + `finally` | Resolved. |
| R11 chunks + abort after 3 failures | Resolved in code. **No test covers either** (F8). |
| R12 runbook stops every writer | **Not resolved.** The commands are still placeholders and point at the wrong scheduler (F6). |
| R13 invalid config → default, valid clamped up | Resolved. The test passes. |
| R14 ordering / comment | Resolved. |
| R15 second-run checks in all three scenarios | Present. Scenario (c) now fails on the regression (F2). |

## Blockers

**F1. The default `_wm()` makes every behavioural test fail against v4's own R1 guard.**
- `_wm(db)` defaults all three tables to `10**9`. `load_watermarks` correctly sees `10**9 > MAX(id)`, decides the DB was restored, and returns `None`. Nothing is aggregated or deleted.
- `test_second_run_unchanged…` passes `spread=ids[0]` but leaves cross and oracle at `10**9` on **empty** tables (MAX = 0). The guard rejects it for the same reason.
- `test_restored_db_watermark_rejected` and `test_missing_ship_meta…` pass, but for the wrong reason: the cross/oracle watermark trips the guard. So they don't isolate what they claim to test.
- Observed: `assert (0 …`, `planned_rows_deleted 0 == 1`, `rows_reject 0 == 2`, and so on, across 11 tests.

Fix: `_wm(db, spread=None, cross=0, oracle=0)`, where `spread=None` means the real `SELECT COALESCE(MAX(id),0) FROM spread_ticks`. Then the restored-DB test sets only `spread=MAX+1` and also asserts that the raw rows are untouched.

**F2. Regression: the walk no longer excludes the MAX(id) row, which can wedge the shipper.**
- In v3 the SELECT had `id < max_id`. R2(b) said the DELETE could drop it "since `hi < max_id` already holds", which assumed the SELECT kept it. v4 removed it from **both**: the SELECT is now `ts in hour AND id <= wm ORDER BY id`.
- Once the shipper has caught up (`wm == MAX(id)`) and the newest row's ts is older than `raw_cutoff`, the walk aggregates and deletes the MAX(id) row. That happens after a backfill or re-ingest of old ts, or after the collector has been down for 14+ days.
- The shipper then sees `local_max < wm` (ship_rows.py:700) and exits `EXIT_WATERMARK`. Compaction's own R1 guard also disables itself the next night. Both stay stuck until a new row arrives.
- Probe (the R7 scenario, run 2 at NOW+11d): `keeper id 2 → MAX(id) now None`, plus a `KPR` bar.
- Fallout:
  - `test_max_id_keeper_survives_and_stable` fails: `[(2, 109.0)] != [(1, 10.0)]`.
  - `test_hourly_prune…` fails on the final assert (`1 == 0`, the keeper's new bar).
  - The docstring ("Filas elegidas con el mismo predicado…") and the dry-run (which still has `id < max_id`) are now both wrong about the real path.

Fix: read `max_id` once before the walk and add `AND id < ?` to the walk SELECT. Leave the DELETE as it is; the rowcount check covers it.

**F3. The fixture's oracle ALTER fails in every test** (`duplicate column name: reference_source`). Delete that line. `oracle.SCHEMA` already has the column. If a migration path for older DBs is wanted, gate the ALTER on `PRAGMA table_info`. I also checked the fixture order, and it is correct: `migrate_schema` ends in an `UPDATE` that opens an implicit transaction, and the next `executescript` commits it, so `ensure_wal` runs outside a transaction.

**F4. `test_nonmonotonic_ids_and_foreign_row` doesn't build the scenario it names.**
- `a` is at `_ts(191.0)` and `b` at `_ts(192.0)`. Those are **different hours** (…T13 and …T12), so there's no non-monotonic pair inside one hour. Observed: `[(1, 20.0), (1, 10.0)] != [(2, 30.0)]`.
- The foreign row is at h=50, which is older than `raw_cutoff` (48h). It gets compacted into its own bar, so `"FRG" in left` fails. Even when it passes, that assert would not prove the ts predicate does anything.
- Fix: put `a` at `_ts(191.2)` (same hour as `b`, later ts, lower id). Put FRG at `h_ago=10` (newer than the cutoff, so never compacted, with an id between `a` and `b`).
  - If the DELETE loses its ts predicate, rowcount becomes 3 ≠ 2, the transaction rolls back and the bar assert fails.
  - If the DELETE loses both the ts predicate and the rowcount check, FRG is deleted and the survivor assert fails.
- I ran the corrected hours: the bar part passes and the FRG part fails only because of the h=50 placement.

**F5. `test_ref_tie_and_majority_counts` seeds the tie rows in a different hour, outside the window.**
- `ts="2026-10-04T12:00:00.000Z"` is the **exact** `raw_cutoff` hour (`NOW − 2d`). Since `hour < raw_cutoff` is false, those rows are never compacted. The closed rows live at …09-28T12.
- So `fetchone()` reads the closed bar: `1.0 != 20.0`. The counts `{closed:5, regular:2}` can never share a bar.
- Fix: use `_ts(192)` (`2026-09-28T12:00:00.000Z`) for the tie rows and the straggler. With that change the test passes, so the R9 tie logic itself is correct, both in `_aggregate_rows` and in `_merge_bar`.

**F6. The R12 runbook still isn't executable and names the wrong scheduler.**
- None of these writers is a hermes job. They are plain **user crontab** entries:
  - `ensure_collector.sh` every 5 min
  - `alerts.py` every 2 min
  - `oracle.py --once` every 5 min
  - `ship_rows.py` every 5 min
- Long-running processes: `scripts/run_collector.py` and `scripts/dashboard.py` (the dashboard hosts the ingest router; `b402_gate` and `oracle_score` are imported there and in `stockgap/ingest.py`).
- Critically, `ensure_collector.sh` **restarts the collector within 5 minutes** if you `pkill` it, which can land mid-index-build. The plan has to say this exactly:
  ```
  crontab -l > data/crontab.pre-migration.bak
  crontab -l | grep -v -E 'ensure_collector\.sh|scripts/alerts\.py|stockgap/oracle\.py|ship_rows\.py' | crontab -
  pkill -TERM -f 'scripts/run_collector.py'; pkill -TERM -f 'scripts/dashboard.py'
  pgrep -af 'run_collector|dashboard.py|alerts.py|oracle.py|ship_rows' || echo "all writers stopped"
  python3 scripts/compact_ledger.py --migrate-indexes
  crontab data/crontab.pre-migration.bak && scripts/ensure_collector.sh && <exact dashboard start command>
  ```
- Also state how the dashboard is normally started; I couldn't find a unit for it.

## Correctness / medium

**F7. Dry-run doesn't match the real path.** Dry-run counts with `id < max_id`; the real walk doesn't. Probe with the MAX(id) row old: `planned 1 real 2`. Fixing F2 restores parity. Add a test that runs dry-run and then the real path on the same fixture and asserts `planned_rows_deleted == rows_deleted` and `planned_hours_with_rows == hours_with_rows`.

Also, the dry-run `hi` expression (`… if False else …`) is still in the "complete" code, followed by a note telling the executor to simplify it. That is the same hedge pattern R1 rejected: write the final line in the plan.

**F8. Chunking and the consecutive-failure abort have zero test coverage.** `CHUNK_ROWS=10_000` and every fixture has fewer than 10 rows. Add:
- (a) `monkeypatch.setattr(compaction, "CHUNK_ROWS", 2)` with 5 rows in one hour, including a same-ts ref tie split across a chunk boundary. Assert one bar with `n_ticks == 5` and the higher-id ref. I traced this path and it is correct: id-ascending chunks plus the `>=` tie in `_merge_bar`. It needs a test to stay correct.
- (b) Make chunk 2 fail (for example, a monkeypatched `_merge_bar` that raises on the second call). Assert that chunk 1 is committed, the chunk-2 rows survive, and a second run merges them with no double count.
- (c) Make every hour fail, and assert `failures == 3` and that the walk stopped.

**F9. A rowcount mismatch is handled like a lock timeout.** `RuntimeError` falls into the same `except Exception` as a lock timeout, so the walk logs it and carries on to the next hour. A mismatch means the invariant is broken (a concurrent deleter, a predicate bug or schema drift), so the safe response is to **abort the walk** and exit non-zero, not to keep deleting. Raise a dedicated `InvariantError`, re-raise it out of the walk, set `rep["aborted"]`, and let the CLI exit 1. Also log at ERROR and set `rep["aborted"] = True` when the 3-failure abort fires; it currently stops silently.

**F10. The aggregated rows are read outside the transaction that deletes them.**
- The whole hour (~108k rows) is SELECTed in autocommit, then merged and deleted in later per-chunk transactions.
- The rowcount check catches concurrent **deletes**. It cannot catch an UPDATE that lands between the snapshot and the transaction: the bar records stale values. `migrate_schema`'s `UPDATE … reference_kind` on collector start is one real UPDATE path, though it touches only NULL rows.
- Cheaper and stricter: keyset-paginate **inside** each `write_txn`: `… AND id > :last AND id < :max_id ORDER BY id LIMIT 10000`, then aggregate, merge, delete and check in the same transaction. That removes the staleness window and the 108k-row memory spike, and the transaction is still bounded by `CHUNK_ROWS`.

**F11. `HOUR_SCHEMA = """...(idéntica a v3…)"""` and `INDEXES = (...)  # idéntico a v4` are still placeholders.** That's N18 again, and the v4 note refers to itself. `for name, stmt in INDEXES` on `(...)` raises `TypeError`. Inline both. The `_merge_bar` 15-column positional INSERT depends on the exact column order of `HOUR_SCHEMA`, so name the columns in the INSERT as well.

**F12. `test_second_run_unchanged…` asserts the wrong survivor set.** `{ids[1]}`, but the keeper also survives: observed `{2, 3} != {2}`. Assert `== {ids[1], keeper}`.

## Low

- **F13.** The walk starts at `MIN(ts_utc)` and steps one hour at a time. A single bad-clock row (`1970-…`) means about 500k indexed SELECTs every night, and an old unshipped survivor keeps the walk anchored. After an empty hour, jump ahead with `SELECT MIN(ts_utc) FROM spread_ticks WHERE ts_utc >= ?`.
- **F14.** The `elif row and row[0] and not dry_run and wm is None: pass` branch is unreachable, because it is the `elif` of `if row and row[0]`. Delete it.
- **F15.** `compact()` opens with `sdb.connect` but never calls `ensure_wal`. db.py's contract is that every writer calls it, and it asserts the DB really is in WAL mode, so call it.
- **F16.** `connect_ro` on a cold WAL DB **creates** `-wal` and `-shm` (verified). `db.read_bytes() == before` still holds, but the README shouldn't call dry-run "touches nothing".
- **F17.** Task 3 step 4 runs the dry-run *after* migration only. Also run it before migration to record the pre-migration report: `index_present: False` and the walk skipped. I verified that this works.

## Verified OK (requested hunts)

- **Chunk boundary × rowcount check.** Rows are id-sorted and in-hour. Each chunk is a contiguous slice, so `ts∈hour ∧ id∈[lo,hi] ∧ id≤wm` matches exactly that slice:
  - New inserts get `id > MAX ≥ wm`, so they are excluded.
  - A concurrent delete gives rowcount < len, so the chunk rolls back.
  - A mid-hour failure leaves the earlier chunks committed; the remaining rows come back the next night as stragglers, with no double count.
- **`_aggregate_rows` tie under chunking.** With `ORDER BY id`, the `>=` rule gives "latest ts, tie goes to the higher id" within a chunk. Across chunks, `_merge_bar` uses `>=` with new (higher-id) data, so it stays consistent. Confirmed empirically once F5 is fixed.
- **R13 config.** `0→14`, `-5→14`, `garbage→180`, `1→2`, hourly floor `raw+1`.
- **R6 pre-migration dry-run** works, and the R1 guard matches the shipper's own check (ship_rows.py:700).

## Required for v5

1. Fix `_wm()`: real MAX(id) for spread, `0` for cross and oracle. Make the restored-DB test isolate spread only (F1).
2. Restore `id < max_id` in the walk SELECT, and add the dry-run/real parity test (F2, F7).
3. Drop the oracle ALTER from the fixture (F3).
4. Fix the nonmonotonic test (same hour; FRG at h=10) and the tie test (`_ts(192)`) (F4, F5).
5. Write a concrete crontab/pkill runbook that handles the `ensure_collector` watchdog (F6).
6. Add chunk, partial-failure and abort tests (F8). Make an invariant violation abort the run (F9).
7. Prefer keyset reads inside each `write_txn` (F10). Inline `HOUR_SCHEMA` and `INDEXES` (F11). Fix the survivor-set assert (F12).

Before resubmitting, LISA should materialise both code blocks into a scratch repo and run `pytest`. Four rounds of failures that only show up when the tests actually run means the plan's tests are not being executed before the gate.

VERDICT: changes_requested
