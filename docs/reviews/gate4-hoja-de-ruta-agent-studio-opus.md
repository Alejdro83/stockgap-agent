I verified each round-3 fix against the files it cites and wrote the review below. It is ready to save as `gate4-hoja-de-ruta-agent-studio-opus.md`; I made no changes to any file. **The verdict is changes_requested:** three new MED findings, and three round-3 items are still PARTIAL.

## (a) Round-3 findings
N15r [HIGH] Runtime relabel + 10-08 EOD hard checkpoint: PARTIAL
- The relabel is in D7: "GO (host process; contenedor/NF build PENDIENTE `[G3-N15]`)" (`stockgap.md:129`).
- The hard checkpoint, the NO-GO branch and the explicit "no live seller URL" are at `:129`, repeated at `:135` and in `stockgap-seller/docs/evidence/bloqueadores.md` (row G3-N15).
- **Leftover:** the status lines still say plain "N15 = GO" (`:121`, `:134`).
- **Leftover:** Bloque 0 exit criterion (1) still claims "en contenedor" (`:149`). The evidence only shows a container simulated on the host (`SPIKE-LOG.md:149`). → N33.

N22r [LOW] `:163` captures/toolchain + hackathon page: PARTIAL
- The toolchain is now right: pnpm 10 + corepack / Bun 1.3+ (`:142`, `:163`), matching `toolchain_quickstart.txt:88-91`. The workspace pins `pnpm@10.24.0` (`stockgap-seller/package.json`).
- `:163` cites files that **do not exist**. There is no `bnbdoc_sdk_deployment_*` and no `bnbdoc_sdk_quickstart_*`; the real names are `bnbdoc_developer-kit_bnbchain-studio_deployment_.*` and `bnbdoc_developer-kit_bnbagent-sdk_quickstart-typescript_.*`. The networks file is `bnbdoc_sdk_networks.txt`, not `bnbdoc_sdk_networks_*`. `SPIKE-LOG.md` sits at the repo root, not in `spike/`.
- The hackathon page **is captured**, in `hackathon_page.txt`: `:3` "one track", `:7` "Submissions lock Sun 11 Oct, 12:00", `:41` "four minutes or less". Yet `:163` lists it as "pendiente de capturar". → N30.

N24 [MED] Seller ERC-8004 identity network: CLOSED
- The network is now on every mention: "2572 (BSC TESTNET, chain 97 — toda mención lleva la red)" (`:142`; also `:134`).
- Bloque 2 includes mainnet registration and `update-endpoint` (`:135`, `:142`). The NO-GO case records that the endpoint stays localhost (`:142`). S10 falls back to "2572 (testnet 97)" (`:155`).
- Residual ambiguity about which wallet registers the seller on mainnet → N34.

N25 [MED] Seller repo remote / publish plan: PARTIAL
- `.studio/` is ignored (`stockgap-seller/.gitignore`, the first rule). The repo is scheduled for 10-08 (`:135`), and step 4 now includes it (`:108`).
- **Open:** the links block has no seller-repo entry, not even a placeholder (`:181-186`). D4 in the decisions line is unchanged (`:117`).
- `git remote -v` is empty. That is expected, since the push is scheduled.
- 10 evidence captures are **untracked** (`git status`: `hackathon_page.*`, `nf_sandbox_pricing.*`, `chain56_contracts.*`, `bloqueadores.md`, …). A push would leave them out → N30.

N26 [MED] Bloque 3 funding + chain-56 code check: CLOSED
- A10 plus mainnet buyer funding (allow-list U/USDC/USDT + gas BNB) have deadline 10-08 EOD, owner Alex (`:135`).
- The `eth_getCode` capture is scheduled before 10-09 09:00 (`:136`). It is in fact **already done**: `chain56_contracts.txt` was captured 2026-10-07T02:40:31Z at block 126173663, for all 4 addresses, with an upstream cross-check against `apex-contracts_deployments.txt`.
- The roadmap still calls it pending → N30.

N27 [LOW] Video arithmetic + sweep scope: CLOSED
- The arithmetic adds up (`:39`):
  - 45s saved (−25s and −20s).
  - The seller section (≤60s) **replaces** the BNB section 2:45–3:15.
  - 3:45 − 45s − 30s + 60s = 3:30, which is ≤ 4:00.
- `:155` matches, and the sweep at `:157` now includes `README.md:4,13,31,71` and `VIDEO_SCRIPT.md:18,21,48,50,62`.
- Content side effect of the replacement → N35.

N28 [LOW] Factual slips: PARTIAL
- **Fixed:**
  - Sponsorship wording at `:161`, matching `bnbdoc_sdk_networks.txt:121` ("Gas-sponsored on BSC Testnet and BSC Mainnet").
  - `WALLET_KEYSTORE_JSON` spelled correctly at `:129` and `:160`.
  - Track wording at `:45`, with the re-verify date 10-07 at `:159`.
  - An NF evidence file is cited at `:129`.
- **Open:** `:163` still lists the NF pricing capture as "pendiente", which contradicts `:129`.
- **Open:** the capture shows Sandbox = **2 services AND 2 projects** (`nf_sandbox_pricing.txt:3`), but "usamos 1" is unproven → N29.
- **Open:** the finding-count line is wrong. `:121` says "13 → 10 → 5" while the same line says v2.1 folded 15. Round 3 had 5 new findings + 2 PARTIAL, i.e. 7 to fold → N33.

## (b) New findings
N29 [MED] The NF Sandbox capacity claim ("usamos 1", budget 0 USD) contradicts the wiki
The wiki records a running NF service `agent-bazaar-bot` in project `agent-bazaar`, costing ~$2.70/month (`projects/agent-bazaar/content.md:18`). The NF capture caps Sandbox at 2 services and 2 projects. If the account is Sandbox, the two projects already used are `agent-bazaar` and `stockgap`, and the services may also be full. Then the 10-08 EOD checkpoint fails for a quota reason, not a technical one. If the account is pay-as-you-go, the "0 USD" budget is wrong (nf-compute-10 costs $2.70/month). Either way D7's premise is unverified.
Fix: today, do a read-only listing of the NF account (tier, projects, services) and record the result in D7. If it is full, Alex decides before 10-08 between (a) a paid nf-compute-10 inside project `stockgap` (update the budget) and (b) pausing `agent-bazaar-bot`. Never touch `stockgap-app`.
Refs: `stockgap.md:129`, `nf_sandbox_pricing.txt:3`, `agent-bazaar/content.md:18,30`.

N30 [MED] The evidence block (`:163`) and the Bloque 3 line (`:136`) misstate what exists, and the captures are uncommitted
- `:163` cites 3 filename patterns that don't exist.
- It lists as "pendientes" four captures that do exist: `toolchain_quickstart.*`, `hackathon_page.*`, `nf_sandbox_pricing.*`, `chain56_contracts.*`.
- `:136` schedules work that is already done.
- All of these files are untracked in the seller repo, so the 10-08 push and the public flip would ship without them. These are exactly the citations the forms and the DX Report will rely on.
Fix: rewrite `:163` with the real filenames and mark the four captures DONE, with their capture timestamps. Change `:136` to "HECHO 10-07 02:40Z (`chain56_contracts.*`)". Commit `docs/evidence/` before the push, after a secret grep (no keystore or `.env` material).
Refs: `stockgap.md:136,163`; `stockgap-seller` `git status`; `chain56_contracts.txt:3`.

N31 [MED] Nobody owns several 10-08/10-09 items, and Alex is overloaded
- On 10-08 EOD Alex owns all of: the NF runtime deploy (D7), the funding, and A10 (Alex only). Alex also writes the daily DX diary.
- No owner is given for: the seller repo creation and push, the mainnet ERC-8004 registration plus `update-endpoint`, the Bloque 2 code gate, the 10-09 consistency sweep, and Bloque 1.
- Blockers A1, A2, A7, A9 and A11 have **no date**. The forms on 10-10 00:00–12:00 cannot be filled without A1, A2, A7 and A9.
- On 10-09 Alex has the ≥4h DX slot, the Bloque 3 go/no-go at 09:00, and the freeze.
Fix:
- Add an owner and date to every 10-08/10-09 item. The repo, registration, sweep and Bloque 1 go to LISA or a subagent with a Claude gate; only the key and funding steps stay with Alex.
- Set A1, A2, A7 and A9 to 10-09 EOD (Alex), with a 10-10 fallback.
- Set A11 to "won't-have": the cut is accepted, ERC-8183 only.
Refs: `stockgap.md:115,135-138`.

N32 [LOW] The special-prize amounts and "dry-run" are now on the official page, but the roadmap says they aren't
`hackathon_page.txt:17` shows **$2,000** for each special prize. `:33` says "dry-run every call before you commit real funds" (Transaction API). The roadmap still says "sin importe en la web oficial" and "no citar … sin captura".
Fix: cite `hackathon_page.txt:17,33`, and drop "sin importe" and the "Transaction API dry-run" exclusion.
Refs: `stockgap.md:123,162,163`.

N33 [LOW] Stale version and status labels
- Title `:119` still says "**v2.1**" (it should be v2.2, post-G3).
- `:121` still says "gate ronda 3 (verify-only) corriendo".
- `:121` lists only the gate1/gate2 reviews as verbatim, omitting gate3.
- `:121` gives the count history as 13 → 10 → 5 (see N28).
- "N15 = GO" appears without its qualifier (`:121`, `:134`), and `:149` still claims a container run.
Fix: change the title to v2.2. Make the history line "13 → 15 → 7 (5 new + 2 partial)" and cite `gate3-…opus.md`. Use the D7 qualifier everywhere. Reword `:149` (1) to "host process; contenedor simulado".

N34 [LOW] Wallet behind the mainnet ERC-8004 registration, and the "6m27s" claim
- `:142` names `0x60EA…` (the disposable spike wallet, `:149`) as "wallet vendedor".
- D8 requires a **new** mainnet seller wallet (`:131`), and `:135` lists "wallet D8" as part of Bloque 2.
- So it is unclear which wallet holds the chain-56 ID. If 0x60EA, which the spike also used on the 48h `bnb` trial, signs the mainnet ID, that contradicts D8.
- "verificado 6m27s" (`:129`) has no artifact. `SPIKE-LOG.md:147,259` say "≥5 min" and an etime of 04:16.
Fix: state that the chain-56 registration uses the fresh D8 wallet, and record only its 0x address. Replace "6m27s" with "≥5 min (SPIKE-LOG §5)".

N35 [LOW] The seller segment removes the strongest on-chain shot from the video
The BNB section that gets replaced (`VIDEO_SCRIPT.md:56-62`) carries the x402 gate narration and the S10 PriceRegistry mainnet zoom. That is the confirmed mainnet evidence D5 leads with (`stockgap.md:67-70`). `:155` redefines S10 as the seller's ERC-8004 without moving the PriceRegistry shot anywhere.
Fix: keep ≤10s of the PriceRegistry and the x402 402-response inside the ≤60s seller segment, which still leaves the total at ≤3:30. Update `VIDEO_SCRIPT.md:21,62` in the 10-09 sweep.

## Summary
| Status | Items |
|---|---|
| CLOSED | N24, N26, N27 |
| PARTIAL | N15r (status lines unqualified), N22r (wrong filenames; hackathon capture uncited), N25 (no links entry; evidence untracked), N28 (`:163` vs `:129`; count line; NF claim) |
| STILL-OPEN | none |
| New MED | N29 (NF capacity/budget unverified), N30 (evidence block wrong + uncommitted), N31 (owners/dates on 10-08/10-09; A1/A2/A7/A9 undated) |
| New LOW | N32 (prize amounts), N33 (stale labels), N34 (registration wallet; 6m27s), N35 (video loses PriceRegistry shot) |

No hard-constraint violation found:
- **Forbidden zone:** untouched (`:146`).
- **NF redeploy:** none before 10-11. A second service is not a redeploy of `stockgap-app`.
- **Key policy:** the wiki holds addresses only.

VERDICT: changes_requested
