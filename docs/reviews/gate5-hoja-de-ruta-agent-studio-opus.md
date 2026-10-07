I could only partly verify the round-4 fixes. **Verdict: changes_requested.** N29 (MED) is still PARTIAL, and three LOW items (N33, N34, N35) were not applied, although the text says they were. All reads were read-only; I changed nothing.

## (a) Round-4 findings
N29 [MED] NF Sandbox capacity/budget vs wiki: PARTIAL
- D7 now records a listing: "verificada por API 10-07: cuenta StockGap = 1 proyecto + 1 servicio (`stockgap-app`, `nf-compute-20` …)" (`stockgap.md:129`). **No file backs it up.** There is no NF account-listing capture in `stockgap-seller/docs/evidence/` or `stockgap-agent/docs/evidence/`. As it stands, it is a self-report.
- The account tier is still unknown. `:129` admits: "El tier exacto NO se ve vía API". So "presupuesto 0 USD solo si … Sandbox" is still a premise that nobody has checked.
- The contingency contradicts itself. `:129` says "`agent-bazaar` es de OTRA cuenta, no toca esta cuota". The fallback in the same sentence is "(b) pausar `agent-bazaar-bot`", which would free no quota on this account. The wiki entry for agent-bazaar names no NF account (`projects/agent-bazaar/content.md:18,30`), so "OTRA cuenta" is also unverified.
- The condition is inverted: "Si cupiera por cuota" should be "Si NO cupiera".
- "jamás tocar `stockgap-app`" is kept. That part is good.

N30 [MED] Evidence block, Bloque 3 line, uncommitted captures: PARTIAL (MED part closed; LOW remainder)
- Every file cited at `:163` exists:
  - `bnbdoc_developer-kit_bnbchain-studio_deployment_.*`
  - `bnbdoc_developer-kit_bnbagent-sdk_quickstart-typescript_.*`
  - `bnbdoc_sdk_networks.txt`
  - `toolchain_quickstart.*`, `hackathon_page.*`, `nf_sandbox_pricing.*`
  - `chain56_contracts.*`, `apex-contracts_deployments.*`
  - `spike/get_spread_calls.jsonl`, `spike/negotiate_envelope.json`
  - `SPIKE-LOG.md`, at the repo root
- `:136` now reads "HECHO 10-07 02:40Z (`chain56_contracts.*`, block 126173663, 4/4)". That is correct.
- `git status` confirms the files are still untracked: 10 captures plus `bloqueadores.md`. The tracked set is only `bnbdoc_*` and `spike/*`.
- The commit is scheduled for "al inicio de Bloque 2", i.e. 10-08 before the push (`:163`). That is acceptable.
- **Remainder:** only `chain56_contracts.*` has a capture timestamp. The other three are just "DONE 10-07", and the brief asked for timestamps.

N31 [MED] Owners and dates on 10-08/10-09: CLOSED
- A1, A2, A7 and A9 now carry "deadline 10-09 EOD (Alex), fallback 10-10 00:00–12:00 UTC" (`:115`).
- A11 is "WON'T-HAVE (cut aceptada)", ERC-8183 only (`:115`).
- `:135` now splits the owners:
  - LISA or a subagent with a Claude gate: repo, mainnet registration, deploy, 10-09 sweep.
  - Alex: keys, funds, forms.
- Bloque 1 is already done: commit `e98ad1e` exists in `stockgap-agent` and touches only `README.md` and `SUBMISSION_CHECKLIST.md`, with `[skip ci]`.
- Alex's 10-09 load (09:00 go/no-go, A1/A2/A7/A9, ≥4h DX slot) is heavy but feasible.
- The new text introduces owner contradictions → N37.

N32 [LOW] Prize amounts and "dry-run": PARTIAL
- `:163` cites "2.000 USD ×2 `:17`, dry-run citable `:33`". This is verified against `hackathon_page.txt:17` ($2,000 ×2) and `:33` ("dry-run every call before you commit real funds").
- **Not fixed at `:123`:** it still says "sin importe … las cifras «~2K» vienen de cobertura secundaria: **no citar importes sin captura**".
- **Not fixed at `:162`:** it still says "(**sin importe en la web oficial**)".
- So the document now contradicts itself, and the stale line is the one most likely to be pasted into the forms.

N33 [LOW] Stale version and status labels: PARTIAL
- **Fixed:**
  - The title is v2.3 (`:119`).
  - `:121` cites `gate{1..4}` (all four files exist) and has the qualifier "N15 = GO (host; contenedor/NF pendiente)".
  - `:129` is qualified.
- **Not fixed:**
  - `:134` still says plain "N15 = GO, N19 = GO".
  - `:149` (1) still says "proceso `node`/`bun` plano **en contenedor**". `SPIKE-LOG.md:149` says the container was only *simulated* on the host.
- **Stale:**
  - `:134` still lists "Bloque 1 en paralelo", while `:121` says it is HECHO.
  - The `:121` history lists G4 as "7 (3 MED + 4 LOW)". It leaves out G4's 4 PARTIAL items, which is the same miscount N28 flagged for G3.

N34 [LOW] Mainnet ERC-8004 registration wallet and the "6m27s" claim: STILL-OPEN
- `:129` still says "verificado 6m27s sin `bag dev`". `SPIKE-LOG.md:147` shows an etime of 04:16, and `:259` says "long-lived ≥5 min". Nothing records 6m27s.
- `:142` still says "wallet vendedor `0x60EA…`". The text on the "registro en mainnet (chain 56) en Bloque 2" does not say which wallet signs it. `:135` says the fresh D8 wallets are generated, but it does not tie the chain-56 ID to the fresh D8 wallet.

N35 [LOW] Video loses the PriceRegistry shot: STILL-OPEN
- `:155` is unchanged. The ≤60s seller segment is "petición → informe → tx en explorador", and S10 is the seller's ERC-8004. Neither the PriceRegistry mainnet zoom nor the x402 402-response is kept.
- `:157` lists `VIDEO_SCRIPT.md:18,21,48,50,62`, but that was already there in round 3. The sweep has no instruction to keep the shot.
- The arithmetic is still 3:45 − 45s − 30s + 60s = 3:30 ≤ 4:00. That is fine, and there is room for the shot.

## (b) New findings
N36 [LOW] "2 proyectos" is misquoted from the NF capture
`nf_sandbox_pricing.txt` describes Sandbox as "2× free services, 1× free database, 2× free cron jobs". The capture does not contain "2 projects". Both `:129` and `:163` say "2 servicios + 2 proyectos". My round-4 N28/N29 introduced this error. The quota reasoning in D7 rests on it, and a DX Report citation would carry it.
Fix: quote the capture verbatim at `:129` and `:163`: "2× free services, 1× free DB, 2× free cron jobs". Drop "2 proyectos" unless a separate capture shows it.
Refs: `stockgap.md:129,163`; `nf_sandbox_pricing.txt` (Tiers block).

N37 [LOW] Owners contradict each other after the [G4-N31] edit, and one 10-08 slot depends on itself
- `:129` says D7 hosting "owner: Alex (operador); verificación: LISA".
- `:135` says deploy = "LISA/subagente con Claude-gate".
- `:165` says "LISA solo diseña y verifica". That contradicts LISA being an executor at `:135`.
- Within the 10-08 EOD slot, Alex must "reponer la wallet compradora mainnet". The subagent creates that wallet in the same slot, so funding is blocked until the address exists.
- A10 is still labelled "HOY" at `:115` while it sits under 10-08 at `:135`.

Fix:
- Use one owner table: deploy and registration = subagent (executor) + LISA (verify) + Claude (gate); NF secret-group entry and funding = Alex.
- Make D7's owner line match.
- Have the subagent generate the D8 wallets as the *first* 10-08 task, with Alex's funding window after it.
- Change A10's "HOY" to an absolute date.

Refs: `stockgap.md:115,129,135,165`.

N38 [LOW] The planned "`eth_getCode` of the D8 wallet" capture proves nothing
An EOA returns `0x` from `eth_getCode`, so that capture cannot show the chain-56 registration (`:163` "Pendiente").
Fix: capture `bag erc8004 show/resolve` on chain 56, plus the registry `ownerOf(agentId)`, or the registration tx/receipt if one exists. Record only the 0x address.
Refs: `stockgap.md:163`.

N39 [LOW] A stray untracked log in the seller repo root can leak at push time
- `stockgap-seller/youtube_uploader_mcp.log` is untracked and not covered by `.gitignore`.
- It has 2 lines matching key/token patterns. I did not print the values.
- The planned secret-grep only covers `docs/evidence/` (`:163`). A broad `git add` before the 10-08 push or the D4 public flip would publish it.

Fix: move the file out of the repo or add it to `.gitignore`, and run the secret-grep over the whole tree before the first push.
Refs: `stockgap.md:135,163`; `stockgap-seller` `git status`.

## Summary
| Status | Items |
|---|---|
| CLOSED | N31 |
| PARTIAL | N29 (MED: listing has no artifact, tier unknown, contingency (b) contradicts "OTRA cuenta", "Si cupiera" inverted), N30 (timestamps missing on 3 of 4 captures), N32 (`:123`, `:162` still say "sin importe"), N33 (`:134` unqualified, `:149` "en contenedor", `:134` Bloque 1 stale, G4 history miscount) |
| STILL-OPEN | N34 ("6m27s"; registration wallet not stated), N35 (PriceRegistry and 402 shot not kept) |
| New LOW | N36 ("2 proyectos" misquote), N37 (owner contradictions; 10-08 wallet/funding order), N38 (`eth_getCode` on an EOA), N39 (stray log could be pushed) |

I found no hard-constraint violation:
- **Forbidden zone:** untouched (`:146`, `:165`).
- **NF redeploy:** none before 10-11. Creating a second service is not a redeploy of `stockgap-app`, and fallback (b) never touches it.
- **Key policy:** the wiki holds 0x addresses only, and `.studio/` is gitignored.

Open MED: N29 is still PARTIAL, so the verdict rule requires changes_requested.

VERDICT: changes_requested
