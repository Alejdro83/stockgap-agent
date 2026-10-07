# Gate G1, round 2: StockGap Agent Studio roadmap v2

v2 fixes 7 of the 13 round-1 findings cleanly. The other 6 are only partly fixed. Checking v2 against the live Northflank (NF) service turned up a new HIGH problem: every push to `master` automatically rebuilds and redeploys the NF app. That breaks v2's own "no NF redeploy" rule.

All `stockgap.md` line numbers refer to `/home/lab-master/.hermes/wiki/projects/stockgap.md`.

## (a) Round-1 findings

**#1 [HIGH] Critical path / cut order: STILL-OPEN (partial).**
- Fixed: D9 now has dated milestones (`stockgap.md:133-139`).
- Still missing:
  - Bloque 1 and Bloque 3 have no date.
  - Bloque 4's "resurrect on 10-10 before 09:00" (`:150`) contradicts the 10-09 EOD code freeze (`:136`).
  - The cut order (`:139`) only cuts Bloque 4. The other two items listed are no-ops: cloud-mainnet is already "covered by D7" and Fase 2 is already out.
- Details in N18.

**#2 [HIGH] Bloque 3 hosting / 48h trial URL: STILL-OPEN (partial).**
- Fixed: "Never put a trial URL in the forms", and the evidence that lasts through judging is now the video plus the repo (`:129`).
- Not fixed: round 1 asked for an owner, a budget and a go/no-go time on 10-08. None is in D7. B1 also rests on an unverified claim that the Studio runtime can be self-hosted. See N15.

**#3 [HIGH] Mainnet signing key policy: CLOSED** (`:131`).
- The policy covers a new seller wallet, a ≤10 USD cap, executor key ≠ `MCP_MERCHANT` ≠ seller wallet, and subagents never get the executor key.
- One gap: the policy doesn't cover the buyer wallet. See N19.

**#4 [HIGH] Bloque 2 interface: CLOSED** (`:141-143`).
- v2 pins the seller to `POST /mcp tools/call` using `get_spread` / `get_cross_issuer` / `get_oracle_latest`. All three exist (`stockgap/mcp_server.py:65,91,113`).
- The forbidden zone is listed, and `MCP_PAY_MODE` gets recorded before coding. Locally it is `free` (`config/mcp.env:5`); what the NF instance runs is still unknown.
- The "no NF redeploy" rule can't currently be enforced. See N14.

**#5 [MED] Fase 2 on the ingest app: CLOSED** (`:117`, `:127`).

**#6 [MED] Track rule / Bloque 1 deliverable: CLOSED.**
- A9 added (`:115`), D1 gated on A9 (`:117`), Bloque 1 is now a ≤1h task with xStocks dropped (`:147`).
- Leftover: `README.md:48` still has the heading `## Track: Infrastructure & Tooling`. See N23.

**#7 [MED] Toolchain: CLOSED as a plan** (`:146`).
- The host is unchanged so far: `pnpm` is 12.9.1, and `bun` and `bag` are not installed. That's expected because Bloque 0 hasn't run.

**#8 [MED] Stale premise on mainnet evidence: STILL-OPEN (partial).**
- Fixed: Límites honestos (`:59`), the confirmed-evidence block (`:67-70`) and D5 (`:117`).
- Not fixed: A8 (`:115`) and BNB touchpoint 2 (`:169`) were reconciled in the wrong direction. They still say the oracle publish is on testnet and its tx hashes are pending, but the repo shows it is on mainnet. See N16.

**#9 [MED] DX Report under-resourced: CLOSED** (`:151`, `:136`, `:139`).
- There's a separate diary file written only by Alex, a ≥4h writing slot, and DX ranks above Bloques 3/4 in the cut order.
- The diary file doesn't exist yet. See N21.

**#10 [MED] Factual claims with no source: CLOSED** (`:123`, `:156-160`), with LOW leftovers in N22.

**#11 [MED] Video plan: STILL-OPEN.**
- Fixed: the list of artifacts to capture (`:148`).
- Not fixed:
  - Round 1 asked for the storyboard now. v2 pushes it to 10-09 EOD (`:136`).
  - Its "≥45s for the seller" doesn't fit, because the script is already 3:45, not 5–6 min. See N20.

**#13 [LOW] Inconsistent facts: STILL-OPEN.**
- Fixed: the "213" test count in the tech stack line (`:174`).
- Not fixed:
  - The count v2 pins, **392**, is itself stale. See N17.
  - The deploy-status mismatch is still in the checklist (`docs/SUBMISSION_CHECKLIST.md:12` "INSTRUCTIONS READY", `:86` "Northflank in progress").

**#12 [LOW] Bloque 4 undefined: CLOSED** (`:150`: 2h timebox, drop-if-late, cut by default). Leftover in N23.

## (b) New findings

**N14 [HIGH] Any push to `master` redeploys NF, which breaks the "no NF redeploy before 10-11" rule (`:143`).**

What the live service shows (`northflank get service stockgap-app`):
- `vcsData.projectBranch: 'master'`, `disabledCI: false`, `buildSHA: 'latest'`, `ciIgnoreFlagsEnabled: false`.
- `deployedSHA: 1182690`, which equals the current `origin/master`.
- The last build was 2026-10-06T19:56, right after the ledger-compaction merge.

Many steps in v2 push to `master`:
- placeholder swaps (`:106`)
- Bloque 1 README wording (`:147`)
- the consistency sweep (`:154`)
- `docs/DX-DIARY-alex.md` if it's committed
- the review files currently untracked in `docs/reviews/`

Each push rebuilds and restarts the ingest target that the `*/5` shipper writes to. If the seller lives in a *dir* inside this repo (`:143` allows "repo/dir propio"), it also rebuilds NF and may get picked up by the Dockerfile build.

**Fix:**
1. Before any push, `PATCH` the service build settings to `ciIgnoreFlagsEnabled: true` with `ciIgnoreFlags: ["docs/**", "**/*.md", "README.md"]`. Alternatively set `disabledCD` until 10-11.
2. Confirm the PATCH itself doesn't trigger a redeploy.
3. Change `:143` to "seller in **its own repo**", not a subdirectory.
4. Add a one-line check after each push: `deployedSHA` should be unchanged.

**N15 [HIGH] D7 B1 assumes the Studio runtime can be self-hosted, with no evidence or go/no-go.**
- The only source quoted (`:157`) lists exactly three deploy targets: `bnb`, `aws` and `azure`. Self-hosting is not one of them.
- "One runtime — the same process that holds the key" describes the deploy model. It does not say the runtime can run outside those targets or `bag dev`.
- B1 also implies a second NF service (plan/cost unknown, a new GitHub build link, and a NF secret holding the seller key). D8 says "our env" without saying whether that includes NF secrets.
- The testnet fallback for Bloque 3 (`:139`) needs this runtime alive after the 48h trial ends around 10-09.

**Fix:**
1. Add a go/no-go to Bloque 0 (deadline 10-07 EOD): the scaffold builds and runs as a plain `node`/`bun` process in a container, outside `bag dev`, with the keystore read from an env var or secret, and serves one request.
2. If it fails: drop "runtime a NF" from Bloque 2 (`:135`). Run the seller locally for recording; lasting evidence is the video plus the repo. Accept that judges will have no live seller URL.
3. If it passes: name the NF plan and cost, and state "seller key in its own NF secret group, never linked to `stockgap-app`".

**N16 [MED] Touchpoint 2 and A8 call the oracle publish testnet; the repo says it is mainnet.**

The wiki says:
- `:169`: `X-ERC8004-Identity` header is `erc8004:bsc-testnet:…`, and the oracle price publish is on "BSC testnet (tx hashes pendientes, A8)".
- `:115`: A8 asks to confirm a testnet publish.

The repo says:
- `README.md:55`, `DEPLOY.md:53` and `docs/SUBMISSION_CHECKLIST.md:50,77`: the publish goes to PriceRegistry `0x8d2c…b971` on **BSC mainnet**.
- `DX-LOG.md:327`: `0x8055…` is that publish tx, status 0x1.
- `config/mcp.env:23` and `stockgap/b402_gate.py:48`: the header is `erc8004:bsc-mainnet:REGISTRY_PLACEHOLDER#0`.

A8 is chasing evidence that isn't needed, and touchpoint 2 would put "pending testnet" text into the forms.

**Fix:**
1. Rewrite touchpoint 2: mainnet PriceRegistry `0x8d2c…b971`, publish tx `0x8055…` (0x1), deploy tx `0x2235…` (0x1).
2. Close A8 as moot.
3. Say plainly that the registry ID in the app header stays a placeholder until after 10-11. Changing `MCP_ERC8004_ID` means an NF env change and a restart, which is forbidden. The seller's own ERC-8004 ID from Bloque 2 gets shown separately.

**N17 [MED] The pinned test count is wrong: 418, not 392.**
- `pytest -q` on `master@1182690` today: **418 passed in 133.21s**. The ledger-compaction merge added 26 tests after the "392" sweep.
- v2 hard-codes 392 at `:44`, `:57`, `:109`, `:152`, `:154` and `:174`. The [G1-N13] sweep itself tells everyone to paste the stale number.

**Fix:** in all six places, replace the literal with "N = output of `pytest -q` on the code-freeze SHA (10-09)". Record the SHA and N together.

**N18 [MED] The D9 schedule contradicts itself.**
- Bloque 3 has no date. Its only time anchor is "if it gets hard on 10-09" (`:139`), which is the same day as the freeze, the ≥4h DX slot and the storyboard. Alex is the bottleneck for all three.
- Bloque 4 "resurrects on 10-10 before 09:00" (`:150`). That violates the freeze and eats into the 10-10 morning, when the video still has to be recorded and uploaded and the forms filed by 12:00.
- Bloque 1 has no date.

**Fix:**
- Bloque 1 on 10-07, in parallel with the spike (it's docs only).
- Bloque 3 done by 10-09 12:00 UTC, with a go/no-go at 10-09 09:00 that falls back to N15/N19's path.
- Bloque 4 runs before the freeze or not at all.
- 10-10 00:00–12:00 UTC is record, upload and forms only.

**N19 [MED] Bloque 3 has no buyer wallet and no evidence that BSC mainnet is supported.**
- "Buyer→seller payment on mainnet" (`:149`) needs a funded buyer key. D8 (`:131`) only covers the seller.
- The only chain claim cited for the SDK (`:158`) is gas-free registration on testnet. Nothing shows that the x402/ERC-8183 contracts the SDK uses exist on chain 56.

**Fix:**
1. Extend D8 to a second fresh buyer wallet (≤5 USD), never the executor key or `MCP_MERCHANT`. Record its address only.
2. Make "ERC-8183/x402 contract addresses on chain 56, with a doc capture" a Bloque 0 exit check. If they're absent, Bloque 3 becomes testnet immediately, which depends on N15 passing.

**N20 [MED] The video budget is infeasible as written.**
- `docs/VIDEO_SCRIPT.md:3,76` already targets 3:45, with ≈530 English narration words.
- The wiki's "script is 5–6 min, cut it" (`:39`, `:152`) is stale.
- Adding ≥45s for the seller gives about 4:30, over the official 4:00 limit.
- S10 (`VIDEO_SCRIPT.md:21`) zooms on a header that shows `REGISTRY_PLACEHOLDER`.

**Fix:**
1. Fix `:39` and `:152`.
2. Name the cuts now (≥50s): trim "Honest numbers" 1:50–2:45 to about 30s and "Roadmap" 3:15–3:45 to about 10s, then move the seller into the 2:45–3:15 BNB slot.
3. Replace the S10 header shot with the seller's real ERC-8004 ID.

**N21 [LOW] The DX diary doesn't exist yet.** `ls docs/DX-DIARY*` finds no file, and D9 says the first entry is due today (`:134`). Committing it under `docs/` triggers N14 until the ignore flags are set. **Fix:** create it today and keep it uncommitted until N14 is fixed.

**N22 [LOW] Leftovers from #10.**
- `docs/evidence/` has no captures of the Studio docs or hackathon page.
- The toolchain requirements in `:146` (pnpm 10, Bun ≥1.3) still have no source.

**Fix:** save the three sources from `:157-159` as captures and cite the toolchain requirement.

**N23 [LOW] Stale or unclear text.**
- `:103` still says "no reordenar" and `:112` still says forms go in "before 10-11 12:00". Both conflict with D9; `:162` patches this over, but the lines should be removed.
- The frontmatter still says `updated: 2026-10-06` (`:4`).
- D8's "más de 10 USD de gas histórico = rotar" is unclear. Replace it with "balance cap ≤10 USD; only the operator refills".
- Bloque 4's "deterministic signing" (`:150`) is not a named BNB Wallet Skill. Name one or leave it cut.
- Bloque 1 must explicitly include `README.md:48` and `docs/SUBMISSION_CHECKLIST.md:55,57,97`.

## Summary

| Status | Findings |
|---|---|
| CLOSED | #3, #4, #5, #6, #7, #9, #10, #12 |
| STILL-OPEN | #1, #2, #8, #11, #13 |
| New HIGH | N14, N15 |
| New MED | N16, N17, N18, N19, N20 |
| New LOW | N21, N22, N23 |

What blocks execution: fix N14 (the NF auto-redeploy on push) before the next push to `master`, and add N15's go/no-go to today's Bloque 0 spike.

VERDICT: changes_requested
