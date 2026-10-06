# Round 6 review: ledger compaction plan v6

**Verdict: changes requested, but only for one line of executor instructions.** The design, the code and the plan's own tests are now sound, and the round-5 blocker is fixed: I measured it on a fresh copy of the live ledger. What's left is one line in the plan's instructions to Sonnet. If Sonnet follows it literally, it will run `tests/test_deploy_contract.py` and take down the production collector. That's a text-only fix. A v7 that changes only N1 below (and optionally N2–N5) should be approved without re-reviewing the code.

## What I ran

I made a fresh `git archive HEAD` copy in `/tmp/g9` and extracted the three code blocks verbatim from the v6 plan:
- tests: lines 68–558
- `compaction.py`: lines 566–975
- CLI: lines 981–1017

**Plan suite:** `python3 -m pytest tests/test_compact_ledger.py -q` gave **24 passed** in 6.2 s, which matches `docs/evidence/g6-pytest.log`. Following the convention, I did not run `test_deploy_contract.py` or the full suite.

**Live ledger copy:** I took a consistent copy with the SQLite backup API (`/tmp/g9/probe`): 6,122,648 rows, 2026-10-02 to 2026-10-06.

**Before migration (no index):**
- `--dry-run`: `index_present False`, walk skipped, `failures 0`, no crash.
- Real run: refuses with `failures 1`.

**Migration:** `migrate_indexes` took 2.8 s.

**`EXPLAIN QUERY PLAN` after migration:**
- `WALK_SELECT`: `SEARCH spread_ticks USING INDEX idx_spread_ts (ts_utc>? AND ts_utc<?)` plus `USE TEMP B-TREE FOR ORDER BY`
- `WALK_COUNT`: `SEARCH … USING COVERING INDEX idx_spread_ts (…)`
- `WALK_DELETE`: `SEARCH … USING INDEX idx_spread_ts (…)`

**Real `compact()` with `now=2026-10-05`, compared with round 5:**

| | Round 5 (v5) | Round 6 (v6) |
|---|---|---|
| Rows deleted | 961,272 | 961,272 |
| Hours | 24 | 24 |
| Total time | 25.8 s | **10.0 s** |
| Write transactions | 111 | 111 |
| Longest transaction | 0.79 s | **0.109 s** |
| Median / p95 transaction | 0.10 s / — | 0.102 s / 0.107 s |

- The dry-run planned exactly 961,272 rows and 24 hours, so dry-run and real counts still match.
- `failures 0`, and there were no rowcount mismatches.
- Each chunk now reads only that hour's slice of the index, so transaction length no longer grows with table size. That settles the round-5 worry about the 36M-row steady state.

**Real system checks:**
- **Crontab (`crontab -l`):** still exactly four writer lines: `ensure_collector.sh` (*/5), `alerts.py` (*/2), `flock … stockgap/oracle.py --once` (*/5) and `ship_rows.py` (*/5).
  - The runbook's `grep -v` pattern removes those four and leaves the comfyui and omniroute lines alone.
- **Running processes:** `python3 scripts/run_collector.py` (pid 4009020) and `python3 scripts/dashboard.py` (pid 1876787) are live.
- **`ensure_collector.sh`:** it uses a pidfile with `kill -0`, so the pidfile goes stale after the `pkill`. The manual `scripts/ensure_collector.sh` step then restarts the collector correctly, and it logs to `data/collector.log`.
- **Dashboard port:** `scripts/dashboard.py:2` confirms 0.0.0.0:8098.

## Round-5 findings N1–N5: status

| # | Status | Evidence in the v6 plan |
|---|---|---|
| N1 (INDEXED BY) | **Resolved** | Module constants `WALK_SELECT`, `WALK_COUNT` and `WALK_DELETE` at :609–617, all with `INDEXED BY idx_spread_ts`. The code uses them at :879 (dry-run COUNT), :887 (SELECT) and :896 (DELETE). The query-plan test at :551–558 asserts `idx_spread_ts`. Measured above. |
| N2 (bracket trick, split restart) | **Resolved** | The `pkill`/`pgrep` lines at :1039–1041 use `[r]`/`[d]`/`[a]`/`[o]`/`[s]`. I ran the patterns live: they match only the two real processes, not the invoking shell. The restart is split into separate steps at :1043–1046 (crontab restore + diff, `ensure_collector.sh`, `nohup` dashboard, `curl` :8098). |
| N3 (idempotent backup) | **Resolved** | :1037 is `test -e … \|\| crontab -l > data/crontab.pre-migration.bak`, and :1043 restores from that exact file and diffs against it. |
| N4 (log path) | **Resolved** | :1050 now watches `data/collector.log`. |
| N5 (partial-hour count) | **Resolved** | :913–914 increments `hours_with_rows` in the `except` path when `n_hour > 0`. The test at :439 asserts `hours_with_rows == 1` on the partial failure, and the second run asserts `== 1` too. |

- **Gate history:** the round-5 line is present (:1097).
- **Title:** line 1 says "Plan v6". The header metadata is stale (see N4 below).

## Checking what changed since v5

**`INDEXED BY` and the DELETE/rowcount invariant**
- `INDEXED BY` only changes the access path, not which rows match, so `cur.rowcount` keeps the same meaning.
- Each chunk is still a contiguous id slice within the hour, filtered by the same hour, `id <= wm` and `id BETWEEN lo AND hi`, so `rowcount == len(rows)` is exact.
- Confirmed empirically: no mismatches across 961k deletes. The F9 test (a destructive merge) still triggers the abort and rollback.

**Failing loudly without the index**
- A missing index raises `OperationalError: no such index: …` at prepare time (verified).
- In the normal path it can't be reached, because `_has_index` refuses first (the R4 rule).
- If the index were dropped mid-run, every hour would raise, the generic handler would count it, the walk would abort after 3 failures with `aborted = True`, and the CLI would exit 1. That is loud enough.

**Empty-hour jump query** (`SELECT MIN(ts_utc) … WHERE ts_utc >= ?`): it has no hint, but the planner already uses the covering index (verified in round 5). I accept that.

**The query-plan test only half pins the plan.** Because the hint is in the SQL string, the test mostly checks that:
- the hint is still in `WALK_SELECT`;
- the statement still prepares against the real fixture schema.

That is what N1 asked for. It does not cover `WALK_COUNT` or `WALK_DELETE` (see N3 below).

**N5 report change:** there is no double count. The success-path increment (:906) and the exception-path increment are mutually exclusive, and nothing after :906 inside the `try` can raise.

## Findings

**N1 (medium): the verification step tells the executor to run the full suite, including `tests/test_deploy_contract.py`, which kills processes on this host.**
- **Where:** the heading at :1020 says "`python3 -m pytest tests/ -q` → verde (416 …)", and the first checklist item says "`python3 -m pytest tests/ -q` → 0 fallos".
- **What happens if Sonnet follows it:**
  - `test_deploy_contract.py` runs `scripts/deploy/entrypoint.sh`. Its `/proc` scans TERM processes matching `run_collector` (entrypoint.sh:125–187), which kills the production collector (it has already done this today).
  - The `timeout`/`kill(0)` process-group signal can kill the non-interactive executor CLI mid-run.
  - The "0 fallos" target can't be met here anyway: there are 3 known environmental failures in that file.
- **The claimed total isn't backed by the evidence:** `docs/evidence/g6-pytest.log` shows 24 + 400 with that file excluded. 416 is arithmetic, not a run.
- **Fix:** replace both lines with:

  `python3 -m pytest tests/ -q --ignore=tests/test_deploy_contract.py` → 400 passed, 0 failed

  Add explicitly: "NEVER run `tests/test_deploy_contract.py` on b-max: it TERMs `run_collector.py` processes and can kill the executor's process group. Its 3 failures here are environmental and already known." Change the claim at :1020 to "24 new + 400 existing (deploy contract excluded)".

**N2 (low): runbook robustness: `pkill` self-match through the executor's argv, a one-shot stop check, and a racy `curl`.**
- **Self-match:** the bracket patterns stop the invoking shell from matching. But if Sonnet is launched as `claude -p "<prompt>"` and that prompt contains the literal `scripts/run_collector.py` or `scripts/dashboard.py`, `pkill -f` TERMs the executor itself. This invocation shows prompts do go in argv (pid 4013092).
- **Fix:** anchor the patterns to the real cmdlines, which I verified as `python3 scripts/run_collector.py` and `python3 scripts/dashboard.py`:
  - `pkill -TERM -f '^python3 scripts/[r]un_collector\.py'`
  - `pkill -TERM -f '^python3 scripts/[d]ashboard\.py'`
  - Or kill the collector by pidfile: `kill -TERM "$(cat data/collector.pid)"`.
- **One-shot stop check (:1041):** the collector may still be flushing after TERM, and cron jobs started before the crontab edit may still be running. A single `pgrep` will then show live writers, and the plan doesn't say what to do.
- **Fix:** poll instead:

  `for i in $(seq 60); do pgrep -f '^(/usr/bin/)?python3 (scripts/(run_collector|dashboard|alerts|ship_rows)|stockgap/oracle)\.py' >/dev/null || { echo "all writers stopped"; break; }; sleep 1; done`

  Do not migrate unless it printed "all writers stopped".
- **`curl` race (:1046):** it runs immediately after `nohup` and can return `000` before the dashboard binds.
- **Fix:** `for i in $(seq 15); do c=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8098/); [ "$c" = 200 ] && break; sleep 1; done; echo $c`

**N3 (low): the query-plan test covers only `WALK_SELECT`.** A later edit that removed the hint from `WALK_COUNT` or `WALK_DELETE` would pass the suite. On SQLite builds without the hint, the DELETE planner can prefer the rowid range for `id BETWEEN`.
- **Fix:** parametrize the test at :551 over all three constants, with arguments of matching arity:
  - `WALK_COUNT`: `(h0, h1, 1, 2)`
  - `WALK_DELETE`: `(h0, h1, 1, 2, 2)`

  Assert `idx_spread_ts` is in the plan for each. Optionally also assert `"INDEXED BY idx_spread_ts" in sql`, so the intent is pinned even if a future SQLite planner would pick the index on its own.

**N4 (low): stale header and colliding finding labels.**
- **Header:** line 2 still says "v5 = tras gate Opus ronda 4 … correcciones de la ronda 4 marcadas [F#]". Line 5 says "Gate: Opus (round 5)".
- **Labels:** the new tags `[N1]`/`[N5]` reuse the round-2 N-series, which still appears in code comments: `[N4]` in `_guarded_delete`, and `[N11]`, `[N14]`, `[N17]`. So `[N4]` now means two different things.
- **Fix:**
  - Header: "v6 = tras gate Opus ronda 5 (`gate5-plan-review-opus.md`, N1–N5); correcciones marcadas [G5-N#]"; "Gate: Opus (round 6)".
  - Retag the round-5 references as `[G5-N1]` and `[G5-N5]`.

**N5 (low): a real run without the index writes before refusing.** `compact(dry_run=False)` runs `CREATE TABLE IF NOT EXISTS spread_ticks_hourly` in a `write_txn` before checking `idx_spread_ts`. I confirmed this on the probe copy: the table is created, then the run returns `failures 1`.
- It is harmless: the DDL is idempotent, and `--migrate-indexes` creates the table anyway.
- But it contradicts "refuse without index", and it takes the write lock once while writers are live (for example, if the cron line were ever installed before the migration).
- **Fix:** move the `_has_index` check ahead of the `HOUR_SCHEMA` `write_txn` in `compact()`.

## Summary

- **Design:** sound. The invariant, watermark guard, keeper row, chunked transactions, abort semantics and empty-hour jump are all verified in tests and on real data. `INDEXED BY` cut the longest transaction 7× (0.79 s → 0.11 s) and total time 2.6× (25.8 s → 10.0 s), and holds up as the table grows.
- **Why changes are still requested:** N1 is an instruction that, followed literally, kills the production collector and possibly the executor. The gate verdict is what hands the plan to Sonnet, so it can't ship as written.
- **Next round:** a v7 that fixes N1 (and ideally N2–N5) should be approved on a text diff alone, with no code re-review.

Scratch copy: `/tmp/g9`, holding the code extracted from v6 and a 2.4 GB copy of the ledger, already migrated and compacted by my test run.

VERDICT: changes_requested
