**Round 8: v7→v8 fold check**

All three folds are correct, and the v7→v8 diff touches nothing else. This plan is **APPROVED** and can go to Sonnet for execution.

The full diff has six hunks:

1. **Title and header (:1–4, :6):** v7 → v8, the gate7 pointer, the `[G7-#]` legend and "round 8". Text only.
2. **[G7-3] (:551–553):** a `sqlite_master` table-name assertion `"spread_ticks_hourly" not in names`, added to `test_nightly_refuses_without_index` right after the `failures == 1 / index_present is False` check (:550).
   - It pins the right behaviour. The no-index refusal returns at :858, before the only `HOUR_SCHEMA` creation in `compact()` (:861–862).
   - The fixture uses `with_indexes=False`, so `migrate_indexes` (the other place that creates the table) never runs. If someone moves the table creation above the refusal, this test now fails.
3. **[G7-2] (:614):** `PRUNE_BATCH = 50_000` → `10_000`, now matching `CHUNK_ROWS = 10_000` (:615) and the 10k-row write-transaction rule in [R11]. `_guarded_delete` (:829–837) reads it for both the `LIMIT` and the exit check `rowcount < PRUNE_BATCH`, so changing the constant is enough.
4. **[G7-1] (:1059–1060):** the `kill -TERM "$(cat data/collector.pid)"` hint moved from the dashboard `pkill` line to the `run_collector` line. That matches `scripts/ensure_collector.sh`, which writes `data/collector.pid` for the collector only. The prose at :1069–1070 already said the same.
5. **Gate history (:1109):** the Round 7 line was added.

**Scoped check:** I put v8's 2b code and 2a test file into a scratch copy of the `/tmp/g10` tree and ran `python3 -m pytest tests/test_compact_ledger.py -q`: **26 passed**. That includes the new [G7-3] assertion and runs with `PRUNE_BATCH = 10_000`. I did not run `tests/test_deploy_contract.py`, and I didn't need `/tmp/g13`.

As you asked, I'm not flagging the stale evidence pointer at :1037–1038.

I'm also not raising anything new. Last turn I reported four low-severity points against v8:
- the crontab-restore `diff` can never show a difference;
- the pre-migration dry-run runs `MIN(ts_utc)` before checking for the index, so it scans the whole table;
- the evidence comes from the `/tmp/g10` copy, which you've accepted;
- nothing in the script stops the migration if writers are still running.

They all predate the folds, and none is a blocker.

VERDICT: approve