# Round 5 design review: ledger compaction v5

**Verdict: changes requested.** All of the round-4 fixes landed and the tests pass. One narrow problem remains: the new keyset read (F10) uses the wrong index on the real database. I measured it, the fix is about two lines, and it should be in the plan before Sonnet executes it.

## What I ran

I copied `HEAD` into `/tmp/g6` and pulled the three code blocks out of `docs/plans/2026-10-06-ledger-rotation-compaction-v5.md`, which matches the plan on stdin:
- tests: lines 68–547
- `compaction.py`: lines 555–956
- CLI: lines 962–998

Running `python3 -m pytest tests/test_compact_ledger.py -q` gave **23 passed, 0 failed**, twice. This is the first round in which the plan's own tests pass against the plan's own code. Following the gate convention, I did not run the full suite, so I can't confirm the claimed total of 415.

I also checked the plan against the real system:
- **Crontab (`crontab -l`):** it has exactly the four writer lines the runbook filters out (`ensure_collector.sh`, `alerts.py`, `oracle.py --once`, `ship_rows.py`). The runbook's `grep -v` pattern matches all four and nothing else.
- **Running processes:** `run_collector.py` (pid 979455) and `dashboard.py` (pid 1876787) are live.
- **Ingest router:** it is mounted in `stockgap/dashboard.py:61-62`.
- **Shipper:** `read_watermark` (ship_rows.py:329) guarantees `set(tables) == set(APPEND_TABLES)`, so `wm["spread_ticks"]` can't raise `KeyError`. Its guard at ship_rows.py:702 matches the plan's R1 guard.

Then I took a consistent copy of the live ledger with SQLite's backup API: 2.27 GB, 6,097,760 rows, all timestamps 24 characters long, covering 2026-10-02 to 2026-10-06.
- `migrate_indexes` took **2.7 s** in total (`idx_spread_ts` alone took 2.3 s).
- A real `compact()` with `now=2026-10-05` deleted **961,272 rows across 24 hours** in 25.8 s.
- The dry-run planned exactly 961,272 rows and 24 hours, so parity holds on real data.
- There were 111 transactions, the longest 0.79 s and the median 0.10 s.

The real watermark (`ship_meta.json`: spread 6,096,296 against MAX(id) 6,097,760) passes the R1 guard.

## F1–F17: status

| # | Status | Evidence (v5 plan file lines) |
|---|---|---|
| F1 | **Resolved** | `_wm(db, spread=None, cross=0, oracle=0)` uses the real MAX(id) (:122). The restored-DB test sets only `spread=keeper+1` and asserts the raw rows are untouched (:233). |
| F2 | **Resolved** | `max_id` is read once before the walk (:835). The walk SELECT has `AND id < ?` (:867). The keeper test passes. |
| F3 | **Resolved** | The oracle ALTER is gone. `oracle.py:119` already declares `reference_source`. |
| F4 | **Resolved** | `a` is at 191.2 h, in the same hour as `b` with a later ts and a lower id; FRG is at h=10 with an id between them (:179-180). |
| F5 | **Resolved** | The tie rows and the straggler use `_ts(192)` (:332). |
| F6 | **Resolved, with runbook defects** | Accepted as deviation (a), below. It does have three execution bugs: N2, N3 and N4. |
| F7 | **Resolved** | The parity test exists (:284). The `hi` line is final (:843), with no hedging left. |
| F8 | **Resolved** | Chunk tie (:398), partial failure (:419) and 3-failure abort (:451) tests exist. |
| F9 | **Resolved** | `InvariantError` is re-raised (:892), caught once at the walk boundary (:909), and the CLI exits 1 (:994). Tested at :468. |
| F10 | **Partial** | It is structurally correct: reads, merge, delete and check all happen inside each `write_txn` (:867). The planner then picks the wrong access path (N1). |
| F11 | **Resolved** | `HOUR_SCHEMA` (:607) and `INDEXES` (:627) are inlined, and the INSERT names its columns (:751). |
| F12 | **Resolved** | `== {ids[1], keeper}` (:213). |
| F13 | **Resolved** | After an empty hour the walk jumps to the next `MIN(ts_utc)` (:903). The test (:495) fixes the statement count. On the real copy the jump query uses the covering index `idx_spread_ts`. |
| F14 | **Resolved** | The dead `elif` is gone. |
| F15 | **Resolved** | `ensure_wal` is called in both `migrate_indexes` (:697) and `compact` (:812). |
| F16 | **Resolved** | The README text now says the `.db` is byte-identical but `-wal`/`-shm` files may be created (:1032). |
| F17 | **Resolved** | A pre-migration dry-run step was added (:1006). |

## Deviations

- **(a) F6 dashboard start command: accepted.** README.md:38 really does launch the dashboard with `nohup python3 scripts/dashboard.py >> data/dashboard.log 2>&1 &`. There is no systemd unit, and StockGap/README.md:86 has the restart note. Filling in my own placeholder this way is correct. The caveat about running steps separately is necessary but not enough on its own (N2).
- **(b) Catching `InvariantError` at the walk boundary: accepted.** It meets the intent of F9:
  - no further deletes after the violation;
  - the transaction with the violation rolls back;
  - exit 1;
  - the report survives for the log, which is strictly better than a traceback.

  Earlier hours that already committed are correctly counted in `rows_deleted`.
- **(c) Retention prunes still run after a 3-failure abort: accepted.** The prunes use their own predicates and tables. Each batch is its own `write_txn`, and the cross/oracle prunes keep the watermark and `id < MAX(id)` guards. A run of lock timeouts on the walk isn't evidence that those predicates are wrong; only an invariant break is. The CLI still exits 1 through `aborted`.

## Findings

**N1 (high): the walk SELECT scans by rowid instead of `idx_spread_ts`, so every hour's last chunk scans the rest of the table while holding the write lock.**
- **Plan on the real copy, after migration:**
  - `EXPLAIN QUERY PLAN` for the :867 SELECT shows `SEARCH spread_ticks USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)`.
  - The DELETE and the dry-run COUNT do use `idx_spread_ts`.
- **Why this is expensive:** a chunk with fewer than `CHUNK_ROWS` rows (every hour's last chunk, plus any hour with nothing eligible) only finishes when it reaches `min(wm, max_id)`. So once per hour it scans every newer row, inside `BEGIN IMMEDIATE`.
- **Measured on 10-03T12, warm cache:**

  | Chunk | rowid scan (as written) | `INDEXED BY idx_spread_ts` |
  |---|---|---|
  | first chunk | 0.183 s | 0.006 s |
  | last (empty-tail) chunk | 0.516 s | 0.002 s |

- **At steady state:**
  - With `RAW_KEEP_DAYS=14` the table holds about 36M rows (about 13 GB), against 6M rows today. That makes the tail scan roughly 3–5 s with a warm cache, about 24 times a night.
  - With a cold cache it is much longer, and it runs under `ionice -c2 -n7`. That is a priority inversion: a low-I/O-priority process holding the write lock.
  - This can exceed the collector's 10 s `busy_timeout`, which is the `database is locked` failure that db.py's docstring was written to prevent.
  - It also breaks the plan's own R11 and F10 promise of short transactions.
- **Fix:**
  - Write `FROM spread_ticks INDEXED BY idx_spread_ts WHERE …` in the walk SELECT. Use the same hint in the dry-run COUNT and the DELETE so the plan is deterministic.
  - `INDEXED BY` fails loudly if the index is missing, which is consistent with the R4 refuse-without-index rule. Each chunk then becomes an index range over one hour plus a sort of at most about 108k rowids.
  - Add a test that runs `EXPLAIN QUERY PLAN` on the walk SQL against the fixture and asserts `idx_spread_ts` appears. Put the SQL in a module constant so the test and the code share it.

**N2 (medium): the runbook's `pkill` and `pgrep` lines match their own invoking shell when an agent runs them, and the executor is an agent (Sonnet).**
- I saw this live during this review: `pgrep -af 'run_collector|scripts/dashboard'` listed its own `/bin/bash -c … pgrep …` wrapper (pid 3713233).
- As a result:
  - :1020's `pkill -TERM -f 'scripts/dashboard.py'` kills the invoking `bash -c` shell. That is the README:86 hazard, and "run as separate steps" doesn't avoid it, because the pattern is in the step itself.
  - :1021's check can never print "all writers stopped", so an agent will either loop or conclude that something is still running.
- The last line (:1023) ends in `&`, which puts the whole `crontab … && ensure_collector.sh && nohup …` chain in the background, so its failures go unseen.
- **Fix:** use the bracket trick and split the restart:
  ```
  pkill -TERM -f 'scripts/[r]un_collector.py'
  pkill -TERM -f 'scripts/[d]ashboard.py'
  pgrep -af 'scripts/[r]un_collector|scripts/[d]ashboard\.py|scripts/[a]lerts\.py|stockgap/[o]racle\.py|scripts/[s]hip_rows' || echo "all writers stopped"
  ```
  Then, as separate steps:
  - `crontab data/crontab.pre-migration.bak && crontab -l | diff - data/crontab.pre-migration.bak`
  - `scripts/ensure_collector.sh`
  - `nohup python3 scripts/dashboard.py >> data/dashboard.log 2>&1 &`
  - `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8098/` → `200`

**N3 (low): re-running the backup step loses crontab entries.** If step 1 at :1018 runs a second time, for example on a retry after the filter step, it overwrites the backup with the already-filtered crontab. The restore then silently drops the four writer lines. Fix: `test -e data/crontab.pre-migration.bak || crontab -l > data/crontab.pre-migration.bak`, or use a timestamped filename, and restore from that exact file.

**N4 (low): wrong collector log path.** Step 3 (:1027) watches `data/logs/collector.log`. The collector writes to `data/collector.log` (run_collector.py:37 `FileHandler(DATA / "collector.log")`, and the redirect in ensure_collector.sh). Fix the path.

**N5 (low): the report is inconsistent when an hour partly commits and then fails.** Chunk 1's rows count toward `rows_deleted`, but `hours_with_rows` only goes up on success (:889). That is visible in the F8b test: `rows_deleted == 2` with `hours_with_rows == 0`. Fix: also increment `hours_with_rows` in the `except Exception` path when `n_hour > 0`.

## New code since round 4: what I checked and found sound

- **Keyset loop:** a `break` inside `write_txn` on an empty chunk commits an empty transaction, which is harmless. Chunks are contiguous id slices within the hour, so the rowcount invariant is exact. A chunk that fails rolls back on its own and the earlier chunks stay. Covered by F8b.
- **`InvariantError` path:** the `write_txn` rolls back and the error propagates. It skips the generic handler (:892) and is caught once (:909). Covered by the F9 test, including a clean second run.
- **Empty-hour jump:** `hour` strictly increases on every iteration (`nxt >= h1s`), so the loop terminates. When a first chunk fails with `n_hour == 0`, the jump moves on rather than spinning. An unreadable next ts lands in the `strptime` `ValueError` branch and breaks out.
- **Real-data invariants:** no rowcount mismatch across 961k deletes. Dry-run and real counts match.

N1 is the only blocker. Once it is fixed and the test that checks the query plan passes, I'd expect to approve. N2–N5 are runbook and report fixes that can go in the same revision.

Scratch copy: `/tmp/g6` (code plus a 2.3 GB copy of the live ledger, already compacted by my real-run test).

VERDICT: changes_requested
