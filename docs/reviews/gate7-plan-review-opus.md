Round 7 gate: Plan v7, ledger compaction

The v6 → v7 diff. I ran diff on the two plans. It contains only the five fixes, plus title, header, label renames ([N1]/[N5] → [G5-N1]/[G5-N5]) and one new gate-history line. The only other change is import pytest at :71, which the new parametrize decorator needs. Nothing else in the design or code changed.

N1 (medium, running test_deploy_contract.py): fixed.
- :1030 now reads pytest tests/ -q --ignore=tests/test_deploy_contract.py.
- :1032–1034 add a warning: "NEVER run tests/test_deploy_contract.py on b-max: it TERMs run_collector.py…".
- The checklist at :1091–1092 has the same --ignore and warning.
- I grepped every pytest mention. None runs the deploy-contract file; the only other command is scoped to tests/test_compact_ledger.py (:570).

N2 (low, runbook robustness): fixed.
- Self-match: the two pkill patterns are anchored to the start of the command line, ^python3 scripts/[r]un_collector\.py and ^python3 scripts/[d]ashboard\.py (:1056–1057). The live command lines are exactly python3 scripts/run_collector.py and python3 scripts/dashboard.py, so they match and an executor's prompt can't.
- One-shot stop check: it is now a 60 × 1 s pgrep loop that prints all writers stopped (:1058). Its pattern ^(/usr/bin/)?python3 … covers the crontab's /usr/bin/python3 scripts/alerts.py and /usr/bin/python3 stockgap/oracle.py. The rule not to migrate unless that message printed is spelled out at :1068–1069.
- Racy curl: it is now a 15 × 1 s retry loop that prints the final status code (:1063).

N3 (low, query-plan test coverage): fixed. The test at :553–567 is now parametrized over WALK_SELECT, WALK_COUNT and WALK_DELETE. Each gets its own argument count, matching the placeholders: 6, 4 and 5. Each case also asserts that the string "INDEXED BY idx_spread_ts" is present. One correction to your summary: the round-6 gap was that the test covered only WALK_SELECT, not "only WALK_COUNT/WALK_DELETE". The fix covers all three, so the point doesn't matter.

N4 (low, stale header and labels): fixed. The title says v7 and the header cites gate6 with [G6-N#] (:1–4). The gate line says round 7 (:6). Round-5 tags are prefixed [G5-N#] and round-6 tags [G6-N#]. Round 6 is in the gate history (:1105).

N5 (low, writing before refusing): fixed. _has_index now runs at :848, and a run without the index returns at :855 before anything is written. The HOUR_SCHEMA write_txn moved to after the check (:857–859). The only statement before the check is ensure_wal, which writes nothing on a database already in WAL mode.

Tests. /tmp/g11 has no compaction.py or tests/test_compact_ledger.py, so I couldn't use it. Earlier in this session I checked /tmp/g10 instead: its code and test file are byte-identical to the plan's §2b and §2a blocks, and pytest tests/test_compact_ledger.py passed 26/26. docs/evidence/g7-pytest.log shows 402 passed, which matches the plan.

Minor comments (not blocking):
1. Pidfile hint on the wrong line. At :1057, the kill -TERM "$(cat data/collector.pid)" hint sits on the dashboard pkill line, but that pidfile belongs to the collector. An executor taking that "alternative" would leave the dashboard running. The stop loop would then never print its message, so the mistake fails safe. Still, the hint belongs on :1056.
2. 50k prune batches (carried over from v6, not a v7 regression). In my previous reply I marked this changes_requested; it was there in v6 too, and per this round's scope I'm downgrading it to a follow-up. PRUNE_BATCH = 50_000 in _guarded_delete breaks the plan's own 10k-row transaction rule [R11]. The live data adds about 106k rows a day to cross_issuer_ticks and about 140k to oracle_predictions. At the plan's stated 1–3 s per 10k rows, each batch holds the write lock for 5–15 s, which can exceed the writers' 10 s busy_timeout. With the 180-day defaults this won't fire for months, but it fires on the first night if CROSS_KEEP_DAYS or ORACLE_KEEP_DAYS is set low. It should be fixed before the prune first runs.
3. No test for the N5 fix. test_nightly_refuses_without_index doesn't check that spread_ticks_hourly stays absent, so moving the table creation back in front of the check would still pass the suite.

All five round-6 fixes are correctly applied and nothing else changed.

VERDICT: approve_with_minor_comments
