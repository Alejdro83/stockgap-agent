## (a) Round-2 findings

#1 [HIGH] Critical path / cut order: CLOSED
- The schedule now has dates for every item: Bloque 1 on 10-07 (`stockgap.md:134`), Bloque 2 on 10-08 EOD (`:135`), Bloque 3 by 10-09 12:00 UTC with a go/no-go at 09:00 (`:136`), the freeze on 10-09 EOD (`:137`), and 10-10 00:00–12:00 reserved for recording and forms (`:138`).
- The cut order now names one real cut, Bloque 4. The two no-op cuts are labelled as such, and DX and the forms are protected (`:140`).

#2 [HIGH] Bloque 3 hosting / 48h trial URL: CLOSED
- D7 now names an owner (Alex) and a verifier (LISA). The budget is 0 USD for NF plus ≤5 USD for the buyer wallet. The go/no-go was moved earlier, to 10-07 EOD (`:129`).
- Both branches are written out. GO means a separate NF secret group that is never linked to `stockgap-app`. NO-GO means the seller stays local, the video plus repo are the evidence, and there is no live URL. "Nunca una URL de trial en los forms" is still there (`:129`).
- One leftover: the claim that the NF Sandbox gives 2 always-on services has no evidence. See N28.

#8 [MED] Stale premise on mainnet evidence: CLOSED
- The Límites honestos section and the confirmed-evidence block are reconciled (`:59`, `:67-70`). A8 is marked MOOT (`:115`). Touchpoint 2 now says mainnet (`:172`).
- This matches the repo: `README.md:55`, `docs/SUBMISSION_CHECKLIST.md:100` and `config/mcp.env:23` (`erc8004:bsc-mainnet:REGISTRY_PLACEHOLDER#0`).

#11 [MED] Video plan: CLOSED
- The storyboard is fixed today (`:137`, `:155`). The cuts are named, the seller goes into the 2:45–3:15 slot, and S10 uses the seller's real ERC-8004 ID. The list of artifacts to capture is kept (`:151`).
- The time arithmetic is off. See N27.

#13 [LOW] Inconsistent facts: CLOSED (as a scheduled sweep)
- Every literal test count is replaced by "N = `pytest -q` on the freeze SHA" (`:157`).
- The deploy-status mismatch is called out with correct line refs. `docs/SUBMISSION_CHECKLIST.md:12` still says "INSTRUCTIONS READY" and `:86` still says "Northflank in progress", as cited.
- The sweep misses some places. See N27.

N14 [HIGH] Any push to `master` redeploys NF: CLOSED
- The seller is in its own repo (`:146`). It exists as a separate git repo, `/home/lab-master/stockgap-seller` (HEAD `b3e84a7`).
- `pathIgnoreRules` are set to `docs/**`, `**/*.md`, `README.md`, the `[skip ci]` flags are on, the post-push `deployedSHA` check is in place, and the doc says `deployedSHA 1182690…` was untouched with zero rebuilds (`:146`, `:121`).
- I could not check the NF service state myself in this read-only, no-network session. I'm accepting that part on the doc's word.

N15 [HIGH] Studio runtime self-hosting with no evidence or go/no-go: PARTIAL
- The go/no-go is defined and was run inside the spike (`:129`, `:134`, `:149`). The NF plan and cost are named, and the seller key goes in its own secret group (`:129`).
- Evidence supports most of my criterion. A plain `node dist/mcpMain.js` with no `bag dev` (`stockgap-seller/run-plain.sh`) ran for ≥5 min and served real requests. The keystore came from `WALLET_KEYSTORE_JSON`/`WALLET_PASSWORD` (`stockgap-seller/SPIKE-LOG.md:141-151`).
- **Not proven:** "in a container". The spike only *simulated* a container on the host by moving `.studio/wallets/` (`SPIKE-LOG.md:149`). NF building a pnpm-10 workspace with Node 22 has never been tried.
- Because N15 is already marked GO, the NO-GO fallback at `:129` can no longer trigger if the NF deploy fails on 10-08.
- **Remaining fix:** relabel it "GO (host process; container/NF build pending)". Add a checkpoint: if the second NF service is not serving `get_spread` by 10-08 EOD, apply the NO-GO branch at `:129`.

N16 [MED] Touchpoint 2 and A8 called the oracle publish testnet: CLOSED
- A8 is MOOT, with the mainnet PriceRegistry, publish tx and deploy tx (`:115`).
- Touchpoint 2 is rewritten. It also states that the header stays a placeholder until after 10-11 and that the seller's ERC-8004 ID is shown separately (`:172`).

N17 [MED] The pinned test count was wrong (418, not 392): CLOSED
- The literal is replaced with "N = `pytest -q` on the freeze SHA, recorded with the SHA" at `:44`, `:57`, `:109`, `:155`, `:157` and `:177`.
- The repo text still says 392: `README.md:4,31,71`, `docs/SUBMISSION_CHECKLIST.md:42,48,62` and `docs/VIDEO_SCRIPT.md:18,48,50`. That's expected until the sweep runs. The sweep's explicit scope is in N27.

N18 [MED] The D9 schedule contradicted itself: CLOSED
- All four parts of my fix are in: Bloque 1 on 10-07 in parallel with the spike (`:134`, `:150`), and Bloque 3 by 10-09 12:00 with a 09:00 go/no-go (`:136`).
- Bloque 4 now runs before the freeze or not at all (`:153`), and 10-10 is record, upload and forms only (`:138`).
- The leftover "no reordenar" is gone (`:103`). Step 8 now follows D9 (`:112`).

N19 [MED] No buyer wallet, and no evidence that chain 56 is supported: CLOSED
- D8 adds a fresh buyer wallet capped at ≤5 USD. It must never be the executor key or `MCP_MERCHANT`, and only its address is recorded (`:131`).
- The exit check ran (`:149`, `:142`). The four chain-56 addresses match `SPIKE-LOG.md:171-177`.
- The doc capture `stockgap-seller/docs/evidence/bnbdoc_sdk_networks.txt` lists BSC Mainnet 56 and names the upstream address source. It also says addresses are "not listed in this documentation", so the addresses come from SDK presets.
- Nobody has checked on-chain that contracts actually exist at those addresses. See N26.

N20 [MED] The video budget was infeasible: CLOSED
- `:39` and `:155` now say 3:45, which matches `docs/VIDEO_SCRIPT.md:3,76`.
- The cuts are named, the seller goes into the 2:45–3:15 slot, and S10 uses the seller's real ERC-8004 ID (`:39`, `:155`).
- `:39` claims "≥50s ganados", but the cuts actually save 45s. See N27.

N21 [LOW] The DX diary didn't exist: CLOSED
- `docs/DX-DIARY-alex.md` exists. It is a template only, with no entries yet (`:8-9`, `:34`), and it is untracked, as the fix required (`:154`).
- Today's first entry is still Alex's to write.

N22 [LOW] Leftovers from #10 (captures, toolchain source): PARTIAL
- Studio and SDK docs are now captured, but in `stockgap-seller/docs/evidence/bnbdoc_*` (deployment, quickstarts, networks), not in `stockgap-agent/docs/evidence/`. The roadmap never points to them.
- The hackathon page (single track, lock at 12:00 UTC, video ≤4 min) is still not captured anywhere. A grep of both evidence dirs finds nothing.
- `:163` is stale and wrong: it says there is no capture yet and gives the toolchain as "pnpm 12". The capture says "Corepack and pnpm 10" and "Bun 1.3+" (`bnbdoc_developer-kit_bnbchain-studio_quickstart_.txt:106-111`).
- **Remaining fix:** rewrite `:163` to cite the seller-repo captures and say pnpm 10. Capture the hackathon page before the forms are filled.

N23 [LOW] Stale or unclear text: CLOSED
- Frontmatter now reads `updated: 2026-10-07` (`:4`). "No reordenar" is gone (`:103`), and `:112` follows D9.
- D8 now says "balance cap ≤10 USD; solo el operador repone" (`:131`). Bloque 4 stays cut, with a stated reason (`:153`).
- Bloque 1's scope lists `README.md:48` and the checklist lines `:55,57,97` (`:150`). Those lines are already edited in the uncommitted working tree.

## (b) New findings

N24 [MED] The seller's ERC-8004 identity is testnet-only, but the plan presents it next to mainnet claims
- agent_id 2572 is registered on chain 97, and its endpoint is `http://localhost:8000/mcp` (`SPIKE-LOG.md:85-101`, `stockgap.md:142`).
- Meanwhile Bloque 3 targets a mainnet payment (`:152`). S10 shows "el ERC-8004 real del vendedor" inside the narration about BSC mainnet (`:39`, `:155`, `VIDEO_SCRIPT.md:58-62`). The forms cite "agent_id + `bag erc8004 show/resolve`" without saying which network (`:142`).
- No step registers the seller on chain 56. Gas sponsorship covers mainnet too, per `bnbdoc_sdk_networks.txt`: "Gas-sponsored on BSC Testnet and BSC Mainnet". No step fixes the localhost endpoint if N15 falls back to local.
- **Fix:** in Bloque 2 (by 10-08 EOD), either:
  - register the seller on chain 56 and use that ID in S10 and the forms, or
  - label every mention as "agent_id 2572 (BSC testnet, chain 97)".

  Either way, run `update-endpoint` to the NF URL, or record that it stays localhost under NO-GO.

N25 [MED] The seller repo has no remote and is missing from the publish plan
- `SPIKE-LOG.md:3` says "sin push a ningún remote".
- D7's GO branch puts the seller on a second NF service (`:129`), and NF builds from a VCS link.
- The NO-GO branch relies on "video + repo con instrucciones" (`:129`). But D4, the public flip, and Bloque 6 only cover `stockgap-agent` (`:108`, `:155`). The links block has no seller repo (`:181-185`).
- Judges evaluating the Studio special prize would have no access to the seller code.
- **Fix:** in Bloque 2 (10-08):
  - Create a GitHub repo for the seller and push it after checking `.studio/` is ignored (the launcher sources `.studio/.env.local`).
  - Link it to the NF service.
  - Add "flip the seller repo public" to D4/step 4, and add its URL to the links block.

N26 [MED] Funding for Bloque 3 is on the critical path with no date or owner
- The testnet path needs A10, which only Alex can do (manual faucets; bots can't pass Turnstile) (`:115`).
- The mainnet path needs a funded buyer wallet: a stablecoin from the seller's allow-list (U/USDC/USDT) plus BNB for gas (`SPIKE-LOG.md:184-186`, `:131`).
- Neither has a deadline before the 10-09 09:00 go/no-go (`:136`). On 10-09 Alex also has the ≥4h DX slot (`:137`).
- The chain-56 contract addresses come from SDK presets and have not been checked on-chain (N19 above).
- **Fix:**
  - Give A10 and the mainnet buyer funding the deadline 10-08 EOD, owner Alex.
  - Before the 10-09 09:00 go/no-go, capture `eth_getCode`/BscScan for the 4 addresses at `:142`.

N27 [LOW] The video arithmetic is wrong and the sweep scope is incomplete
- The cuts save 25s ("Honest numbers" 55s → 30s) plus 20s ("Roadmap" 30s → 10s), which is 45s. `:39` claims "≥50s ganados".
- The seller's slot length is never stated, so the total isn't checked against 4:00.
- `VIDEO_SCRIPT.md` itself is unchanged: "392" appears on screen and in the narration (`:18`, `:48`, `:50`), and the S10 placeholder is at `:21`.
- The sweep at `:157` lists only the checklist. It misses `README.md:4` (the 392 badge), `README.md:13` ("swap when the Northflank deploy lands", which repeats the #13 mismatch) and `README.md:31,71`.
- **Fix:**
  - Write "45s saved; seller ≤60s; total ≤4:00", and say whether the seller adds to the BNB section or replaces part of it.
  - Add `VIDEO_SCRIPT.md:18,21,48,50,62` and `README.md:4,13,31,71` to the 10-09 sweep, so they are updated before recording on 10-10.

N28 [LOW] Factual slips introduced by the v2.1 edits
- `:161` says "gas-free" holds "solo para testnet". The captured networks page says mainnet is sponsored too. This matters for DX Report accuracy.
- `:129` and `:160` spell the variable `WALLET_KEYORESTORE_JSON` inside a quote marked as verbatim. The doc says `WALLET_KEYSTORE_JSON` (`bnbdoc_…deployment_.txt:146`).
- "NF Sandbox = 2 servicios always-on … verificado 2026-10-07" (`:129`) has no evidence file.
- `:45` still says the track is "Infrastructure & Tooling (decisión D1)", while `README.md:48-50` already claims the single track. `README.md:50` dates that check 2026-10-07; the wiki's source list (`:162`) says 2026-10-06.
- `:121` says v2.1 folds "10 findings"; the actual count is 15 (10 new plus 5 open from round 1).
- **Fix:** correct each of these in place, and save the NF plan page as evidence.

## Summary

| Status | Items |
|---|---|
| CLOSED | #1, #2, #8, #11, #13, N14, N16, N17, N18, N19, N20, N21, N23 |
| PARTIAL | N15 (container/NF leg not proven; no 10-08 fallback trigger), N22 (hackathon page not captured; `:163` stale and says pnpm 12) |
| STILL-OPEN | none |
| New MED | N24 (seller ID is testnet-only next to mainnet claims), N25 (seller repo has no remote and isn't in the publish plan), N26 (Bloque 3 funding undated) |
| New LOW | N27 (video arithmetic and sweep scope), N28 (factual slips) |

Each remaining fix is about a line long and can go into Bloque 2's 10-08 scope. Nothing here reopens decisions that rounds 1–2 settled. The MED findings and the N15 PARTIAL touch content, so the verdict rule gives changes_requested.

VERDICT: changes_requested
