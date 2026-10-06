# Round 3 design review: ledger compaction v3

**Verdict: changes requested.** v3 gets the *design* right: the delete-exactly-aggregated invariant, a snapshot read followed by a short `write_txn`, one write transaction per prune batch, rowid prune for hourly bars, schema-first migration, refusal when the index is missing, `PASSIVE` checkpoint, and a JSON counts majority. Most of N1–N18 are addressed in intent.

The problems are in two places:

- **The invariant is not enforced in code.** The DELETE uses an id range taken from the ts-sorted list instead of the aggregated id set. It has no ts predicate and no row-count check.
- **The test file can't go green against the plan's own implementation.** The fixture never builds `idx_spread_ts`, so every behavioural test exits early on the N7 refusal. At least five more tests fail for independent reasons.

Everything below was checked against the real repo:

- `db.write_txn` exists.
- `ship_rows.read_watermark` exists. It is strict, returns `None` when the file is missing, raises `ShipError` when it is corrupt, and requires exactly `APPEND_TABLES` with ints ≥ 0. Its module is `__main__`-guarded, so it is importable.
- `collector.SCHEMA` is a multi-statement script, and the `spread_bps_real` / `reference_*` columns are added by `migrate_schema()`, not by `SCHEMA`.
- Oracle `snapshot_ts` is 20 chars (`…T15:05:01Z`, no millis).
- Prod `ship_meta.json` is in the real format. `spread_ticks_hourly` and the ts indexes don't exist yet. `rotate_data.py` isn't in crontab yet.

## Round-2 items: status

| Item | Status |
|---|---|
| N1 real format | **Partial.** Format is right, but it reimplements the parser instead of importing `read_watermark`. The plan leaves this as a "Nota: if it exists, import it", but it does exist. The copy accepts `True`/negative ints and does not require the exact table set (R1). |
| N2 delete == aggregated | **Not enforced** (R2, R3) |
| N3 atomic merge+delete | Resolved: one `write_txn` around merge and delete |
| N4 txn per prune batch | Resolved |
| N5 hourly prune by rowid | Code resolved; **the test is wrong** (R7) |
| N6 migration | Code resolved. The runbook stops only the collector (R12). |
| N7 refuse without index | Code resolved; **breaks every other test** (R4) |
| N8 short lock | Mostly. The txn is "short" but still deletes ~108k rows plus 3–4 index updates (R11). `ionice -c2 -n7` resolved. |
| N9 tests | **Not resolved** (R4–R10) |
| N10 dry-run | `connect_ro` and guarded counts resolved; **crashes on a fresh DB** (R6) |
| N11 majority | Code resolved (JSON counts); **the test doesn't exercise it** (R9) |
| N12 ref tie / finite | Tie `>=` resolved. The finite rule is resolved, but **the ref reject isn't counted** (R8). |
| N13 None default | Resolved |
| N14 PASSIVE | Resolved |
| N15 injected now | Resolved |
| N16 monkeypatch | Resolved (but see R13 on clamp semantics) |
| N17 walk advance | Resolved (`hi` is computed before `try`, and the format is validated up front) |
| N18 DDL import | **Not resolved.** It is still `DDL = [...]`, which is a literal `Ellipsis`, and it is also the wrong shape (R5). |

## Blockers

**R1. The watermark parser is duplicated and weaker, and the shipper's sanity rule is missing.**
- Implement `load_watermarks` as `read_watermark(meta)` from `scripts/ship_rows.py`. Load it with `importlib`, or move it to `stockgap/` and re-export it from `ship_rows`.
- Catch `ShipError` (and `None`) and treat it as "no watermark".
- Add the shipper's restored-DB sanity check: if `wm[table] > MAX(id)` for a table, treat that watermark as unusable. Otherwise, if the DB is restored from a backup and `sqlite_sequence` rolls back, new rows reuse ids ≤ wm. The shipper refuses to run in that case, but compaction would delete those never-shipped rows as "shipped".
- Remove the "Nota" hedge. The plan has to decide this, not the executor.

**R2. The DELETE doesn't match the aggregated set: `lo`/`hi` come from the ts-ordered list.**
```python
rows = ... ORDER BY ts_utc, id
lo, hi_id = rows[0][0], rows[-1][0]          # id of earliest-ts row, id of latest-ts row
DELETE ... WHERE id BETWEEN lo AND hi_id AND id <= wm AND id < MAX(id)
```
This depends on id order matching ts order, which isn't guaranteed: an NTP step backwards, a backfill or re-ingest, or any hand-inserted row breaks it. A concrete case: an hour holds ids {10 @ :05, 7 @ :50}, so `lo=10, hi=7`. `BETWEEN 10 AND 7` matches nothing. Both rows are merged, none are deleted, and they are merged again every night. That is exactly the N2 double-count, now silent.

The mirror case is just as bad. The BETWEEN has no ts predicate, so a row from a *different* hour whose id falls inside `[lo, hi]` gets deleted without ever being aggregated, which is silent data loss.

Fix (all three parts):
- (a) `lo, hi = min(ids), max(ids)`.
- (b) Repeat the ts predicate in the DELETE: `ts_utc >= ? AND ts_utc < ? AND id BETWEEN ? AND ? AND id <= ?`. You can drop `id < (SELECT MAX(id))`, since `hi < max_id` already holds and AUTOINCREMENT never hands out ids ≤ the old max.
- (c) Inside the txn, `if cur.rowcount != len(rows): raise` so `write_txn` rolls back merge and delete together. That turns the invariant into a checked assertion instead of an assumption.

Also add a test that inserts rows with non-monotonic ts/id inside one hour, plus a foreign-hour row whose id falls inside the range. Run compact twice and assert that the bar is unchanged and the foreign row survives.

**R3. The plan's own straggler and majority tests violate the MAX(id) guard, so they fail.**
- `test_straggler_merges_then_second_run_stable`: the straggler is inserted *after* the keeper, so it **is** `MAX(id)`, gets excluded, and the bar stays `(1,1,10,10,10)`. The assert `(2,2,40,10,30)` fails.
- `test_majority_and_ref_tie_merge`: the same problem. The `'regular'` straggler is `MAX(id)` and never gets aggregated, so `counts == {"closed": 5}`. The assert `{"closed":5,"regular":1}` fails.

Fix: insert a fresh `_keeper(conn)` after every straggler insert.

**R4. No fixture builds `idx_spread_ts`, so every behavioural test returns early with `failures == 1`.**
`compact()` correctly refuses without the index (N7), but `_mk` only creates tables. As a result:
- `test_aggregation_math_exact`, `test_hourly_prune…`, `test_nonfinite…`, `test_cutoff…` and `test_lock_contention…` all fail.
- `test_missing_ship_meta…` and `test_second_run_unchanged…` pass, but **vacuously**: nothing ran.

Fix: `_mk` calls `migrate_indexes(db)`. `test_nightly_refuses_without_index` gets its own bare-table helper.

**R5. The fixture DDL is unusable as specified (N18).**
- `DDL = [...]` is a list holding `Ellipsis`, so `conn.execute(Ellipsis)` raises `TypeError`.
- Even with the real constants, `collector.SCHEMA` and `oracle.SCHEMA` are multi-statement scripts, so `conn.execute` raises "You can only execute one statement at a time".
- They also **lack** `spread_bps_real`, `reference_close_independent`, `reference_source` and `reference_kind`, which `migrate_schema()` adds and which `_row()` inserts. The oracle also adds `reference_source` via ALTER.

Spell it out:
```python
from stockgap.collector import SCHEMA as COLLECTOR_SCHEMA, migrate_schema
from stockgap.oracle import SCHEMA as ORACLE_SCHEMA
conn.executescript(COLLECTOR_SCHEMA); migrate_schema(conn); conn.executescript(ORACLE_SCHEMA)
# + the oracle reference_source ALTER (or call its migrate helper if exported)
db.ensure_wal(conn)          # tests must run in WAL like prod (see R10)
```

**R6. Dry-run crashes on any DB that doesn't have `spread_ticks_hourly` yet.** The dry-run prune loop runs `SELECT COUNT(*) FROM spread_ticks_hourly`, but dry-run never creates the table (correctly, since it's read-only). The result is an uncaught `OperationalError` (it's outside any `try`). `test_dry_run_read_only_and_guarded_plan` hits it, because its DB has never seen a real run.

Prod Task 3 step 4 survives only because migration runs first. A dry-run before migration, which is the natural thing for an operator to try, crashes.
- Fix: in dry-run, skip any table missing from `sqlite_master` (planned = 0), and report `index_present: bool`.
- Without the index, dry-run also does a full 6M-row scan *per hour walked*. Either refuse like the real path does, or warn that it is unindexed.

**R7. `test_hourly_prune_without_id_column` fails: the bar is pruned in the first run.** The row is 40 days old. Run 1 (now=NOW) aggregates it into a bar dated NOW−40d, then the same run's hourly prune (cutoff NOW−30d) deletes it. So run 1 has `hourly_pruned == 1`, and run 2 asserts `hourly_pruned == 1` but gets 0.

Fix: seed the row at −20 days, assert the bar exists after run 1, then run 2 at `NOW + 11d` and assert it's pruned. (In run 2 the keeper is older than raw_cutoff, but it is MAX(id), so it survives. Fine.)

**R8. A non-finite `ref` is still not counted as a reject (N12 is half done).** `_aggregate_rows` counts rejects only for `spread` and `implied`. `ref="inf"` is skipped silently, so `rows_reject == 1` and the test's `>= 2` fails. Add the same `if r is None and ref not in (None, ""): rejects += 1`.

## Correctness / medium

**R9. The majority test doesn't test the merge, and has no ref-tie assertion.** Even after the R3 fix, the `UPDATE … SET market_status='regular' WHERE token_symbol='AAA'` is meaningless, because the aggregated 'closed' rows are gone. Drop it. Also add a real tie case: two rows with the same `ts`, different finite refs, ids ascending. Assert that the higher id's ref wins in `_aggregate_rows` and again via `_merge_bar` on a straggler with equal `ts`.

**R10. The lock test runs in rollback-journal mode, not WAL.** Nobody calls `ensure_wal` in the tests, so the contention test exercises journal-mode RESERVED locks, not the WAL `BUSY_SNAPSHOT` path that N3/N4/N8 exist for. Put `ensure_wal` in the fixture (R5). Also close `holder` in a `finally`.

**R11. "Milliseconds" is false for the per-hour txn.** One hour is about 108k rows. Each delete also maintains `idx_spread_tick`, `idx_spread_symbol` and `idx_spread_ts`, plus 488 bar upserts, so expect roughly 1–3 s under `ionice -c2 -n7`. That's within the collector's 10 s budget, but with no margin on a busy disk.

Merge is additive, so chunk instead: split each hour's id-sorted rows into chunks of about 10k, and give every chunk its own aggregate→merge→delete txn (the R2 rowcount check applies per chunk). Also abort the walk after N consecutive hour failures (e.g. 3). As written, a stuck lock costs 10 s × every hour in the backlog, about 1000 s.

**R12. The migration runbook stops only the collector.** Index builds hold the write lock for tens of seconds. The oracle, alerts, `b402_gate`, `oracle_score` and the ingest router (now mounted in the dashboard) are writers too, and all use `busy_timeout=10000`. Stop or pause *every* writer, or run migration inside a declared maintenance window. Name the exact commands. "`hermes cron pause` / `pkill` según runbook" is not executable.

**R13. Config clamping is the destructive choice.** `num()` returns `lo` for out-of-range input, so `RAW_KEEP_DAYS=0` or `-5` gives **2-day** retention: a typo silently drops 12 days of raw data. An invalid value should fall back to the *default* (14) with a WARNING, and only valid values should be clamped up. Update `test_config_validation` (`0 → 14`, `-5 → 14`).

**R14. `test_aggregation_math_exact` orders ambiguously.** `ORDER BY token_symbol` leaves the two AAA bars in undefined order. Use `ORDER BY token_symbol, ts_hour`. Also, the `_ts(192)` rows in `test_nonfinite` share the same ts, despite the comment "1 s después". That's fine for the `>=` tie, but fix the comment.

**R15. The second-run stability check covers only scenario (b).** N2 asked for a second-run assertion in all three survivor scenarios:
- (a) Missing meta: present, but vacuous until R4 is fixed.
- (c) `test_max_id_keeper_survives` runs only once. Add a second run and assert the bar is unchanged.

## Checked and OK (requested hunts)

- **Hour-walk condition.** `while hour < raw_cutoff` with `hour = MIN(ts)[:13]` is correct. String comparison on `YYYY-MM-DDTHH` is chronological, and `hi` is computed before `try`, so a failure still advances. `HOUR_RE.match` is redundant but harmless.
- **`_merge_bar` when `refts` is None.** `b["ref"]` and `b["refts"]` are always set together, and so are `row[8]` and `row[9]`. So `refts is None` means no stored ref, and taking the new one is correct. The `(b["refts"] or "")` is dead code, not a bug.
- **Dry-run COUNT parity.** The predicate matches the SELECT predicate (`ts` range, `id <= wm`, `id < max_id`). It will diverge from the real DELETE until R2 makes the DELETE mirror it. With R2(b), all three agree.
- **Injected NOW.** `_ts()`, the cutoff and the keeper are all derived from `NOW`, so there's no wall-clock dependence, apart from R7's arithmetic.
- **Oracle `snapshot_ts`** (20 chars, no millis) compares correctly against the `YYYY-MM-DDTHH` cutoff.

## Required for v4

1. Import `read_watermark`, and treat `ShipError` as no watermark. A watermark above `MAX(id)` makes it unusable (R1).
2. DELETE with `min`/`max` ids plus the ts predicate plus `rowcount == len(rows)`, else roll back. Add the non-monotonic test (R2).
3. Fixture: `executescript(SCHEMA)`, `migrate_schema`, the oracle schema and its ALTER, `ensure_wal`, `migrate_indexes`. Keeper after every straggler (R3, R4, R5, R10).
4. Dry-run tolerates a missing hourly table and reports `index_present` (R6).
5. Fix the hourly-prune test arithmetic (R7). Count ref rejects (R8). Add a real majority/tie test (R9).
6. Chunked per-hour txns and a consecutive-failure abort (R11). Stop every writer during migration (R12). Invalid config → default, not the minimum (R13). R14 and R15 test fixes.

VERDICT: changes_requested
