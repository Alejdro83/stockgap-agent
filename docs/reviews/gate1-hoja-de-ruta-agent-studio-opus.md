NEEDS_FIXES

#1 [HIGH] Seven blocks can't fit before the deadline, and nothing says what gets cut
About 4–5 days remain before the 2026-10-11 12:00 UTC lock. The roadmap orders seven blocks with "no reordenar" and gives none of them a time box, an owner/day, or a cut order. Submission (Bloque 6) is last, even though most of its pieces already exist: forms are drafted, the deploy is live, and the video script exists. The two biggest unknowns sit on the critical path before submission: Bloque 0 (the spike) and Bloque 3 (the mainnet demo, which per the asterisk needs a new AWS AgentCore/Azure account). Each round-trip through the subagent + Claude gate (Nota) also adds hours that aren't budgeted. If Bloque 3 slips, Bloque 5 and Bloque 6 slip with it. Fix: add a dated critical path. Bloque 0 ≤ 4h, finished 10-07. Bloque 2 by 10-08 EOD. Freeze the code on 10-09 EOD. Record the video and file the forms by 10-10 12:00 UTC (24h buffer). Submit a "safe" version of the forms early and edit later if the form allows it. Explicit cut order: Bloque 4 → Bloque 3 cloud-mainnet deploy (keep the existing mainnet evidence instead, see #8) → Python Fase 2. Refs: stockgap.md:114-140, :47, :98-108.

#2 [HIGH] Bloque 3 "mainnet only" has no deploy path, and the 48h testnet agent will be dead during judging
The asterisk says Studio's free deploy is a 48h testnet trial and that mainnet needs AWS AgentCore/Azure. Bloque 3 still says "BSC mainnet solo" without naming where the Bloque 2 agent runs on mainnet, who pays, or which account. Bloque 0 starts the 48h trial on 10-06/07, so it expires around 10-09. Judging runs 12–23 Oct, so any Studio link in the forms points to a dead deployment. Fix: decide now, as D7: (a) host the seller on AgentCore/Azure for mainnet, with an account owner, budget, and a go/no-go time on 10-08; or (b) host the seller ourselves next to the existing NF app, and present Studio as dev-time/testnet only. Never put a 48h-trial URL in the forms. Use a short demo video plus repo instructions as the evidence that lasts through judging. Refs: stockgap.md:126, :129, :135, :47.

#3 [HIGH] No rule for the mainnet signing key
The roadmap only says the `bnb`-provider wallet is disposable and never reused on mainnet. It doesn't say which key signs the Bloque 3 mainnet demo or the Bloque 4 wallet skill. The repo already has a funded mainnet executor key (env vault, used by `stockgap/executor.py`/`signer.py`) and a merchant address (`MCP_MERCHANT` in config/mcp.env). With nothing stated, a subagent will reuse whatever key is already in the environment, mixing the TS seller with the executor wallet or the payee. Fix: add a key policy. Create a NEW mainnet hot wallet just for the seller, funded with a cap (e.g. ≤ $10 equivalent). Its key lives only in our env (never in the Studio `bnb` provider). It must not equal the executor key or `MCP_MERCHANT`. Record its address (not the key) in the roadmap. Subagents never receive the executor key's env var. Refs: stockgap.md:129-130, :136; stockgap/signer.py; config/mcp.env (`MCP_MERCHANT`).

#4 [HIGH] "Consumes StockGap over HTTP" has no defined interface, and adding one would touch the forbidden zone
The repo has no JSON REST signal endpoint. The public surface is HTML/HTMX (`/`, `/frag/*`) and `POST /mcp` (stockgap/dashboard.py:53,289-309). `/ingest/*` lives on the same app (dashboard.py:62). The shipper writes to that app every 5 min, with a fail-closed preflight. A "small new endpoint" for the seller means editing `stockgap/` and redeploying the NF service. That restarts the ingest target the shipper depends on, which indirectly violates D6 option A. Fix: pin Bloque 2 to the existing `POST /mcp` `tools/call` on the deployed NF URL, using `get_spread`, `get_cross_issuer` and `get_oracle_latest` (mcp_server.py:65-135). Add these explicit rules:
- No changes to `stockgap/**`, `scripts/ship_rows.py`, crontab, `config/ingest.env`.
- No NF redeploy until after 10-11.
- The seller lives in its own repo/dir.
- Record which `MCP_PAY_MODE` the NF instance runs (free vs b402), because the seller must handle the 402.

Refs: stockgap.md:123, :128, :140; stockgap/dashboard.py:53,62; stockgap/mcp_server.py; crontab (ship_rows */5).

#5 [MED] Fase 2 mounts on the ingest app and contradicts option A
D6 says `create_erc8183_app` could be "montar como sub-app sobre el FastAPI existente … dentro del ingest". That is the app that serves `/ingest/rows`, so it directly contradicts "el pipeline híbrido NO se toca". Fix: drop Fase 2 before 10-11, or state that any Python payment layer runs as a separate process/port. Refs: stockgap.md:122-123.

#6 [MED] Track rule unresolved, and Bloque 1 has no deliverable
Bloque 1 cites a "regla dura del track" while D1 is still unresolved. The checklist still drafts "Infrastructure & Tooling" (docs/SUBMISSION_CHECKLIST.md:55,97), but the official site shows a single track. Bloque 1 also has no concrete output. The code already treats Binance Web3 `rwa/tokens`/`rwa/price`/`rwa/underlying-market` as central (stockgap/oracle.py:66-68, collector.py:1-4). The live DB's latest ticks contain only `ondo` (442) and `bstock` (46). xStocks appear only in tests, so naming "bStocks/Ondo/xStocks" overclaims. Fix: resolve D1 against the live form today. Turn Bloque 1 into a ≤1h framing task (README/form wording that shows RWA data is central) and drop xStocks from the claims. Refs: stockgap.md:127, :45, :138.

#7 [MED] Toolchain not met on this host
Node is v22.22.3 (OK). `pnpm` resolves through corepack to **12.9.1**, not the required 10. Bun is **not installed**. The `bag` CLI isn't installed either. Fix: Bloque 0 step 0 should pin `pnpm@10` per project (corepack `packageManager` field) and install Bun 1.3+ in user space. Don't touch the system Node used by other services. Log each friction point with a timestamp for the DX Report. Refs: stockgap.md:121, :126.

#8 [MED] Bloque 3's premises ignore existing mainnet evidence
The wiki says all three demo-trade broadcasts reverted and A8 has "no evidence". DX-LOG (2026-10-03) records:
- a successful mainnet dust swap, tx `0xd8ef…a48f`, status 0x1
- live mainnet publish/deploy receipts `0x8055…`/`0x2235…`, status 0x1, verified via `local_receipt`
- evidence in `docs/evidence/demo_trade_flash_run.json`

Bloque 3 and D5 are planned on a stale premise, which risks redoing work or under-claiming in the forms. Fix: reconcile "Límites honestos", A8 and D5 with DX-LOG:324-340. Narrow Bloque 3 to what is actually new (seller-agent payment on mainnet). Refs: stockgap.md:59-65, :110, :129; DX-LOG.md:324-340.

#9 [MED] DX Report (25%) is under-resourced, and its raw material is agent-written
The report is Bloque 5, after the mainnet work, so it is the most likely to get squeezed. Its stated raw material, `DX-LOG.md`, is a factory/agent-written log, and its last entry is 2026-10-04. There is no human diary entry for 10-06, even though the plan says "desde hoy". Fix:
- Create a separate human-only file (e.g. `docs/DX-DIARY-alex.md`). Add timestamped entries daily from 10-07: install times, doc errors, the testnet/mainnet contradiction.
- Block a fixed writing slot (≥4h on 10-09).
- Treat DX-LOG only as a fact reference, never copied text.
- Move the DX slot ahead of Bloque 3/4 in the cut order.

Refs: stockgap.md:131, :42; DX-LOG.md (tail).

#10 [MED] Factual claims with no source
These are stated as facts with no URL or capture: prize amounts "~2K c/u", ERC-8004 registration "gas-free en testnet", the "Transaction API" dry-run, "unos dólares bastan", the Studio toolchain requirements, and "rechazan informes IA". Fix: add the official URLs with a 2026-10-06 date to the roadmap, and save captures under `docs/evidence/`. Mark anything unverified as such. Refs: stockgap.md:116-121, :129, :131.

#11 [MED] No plan for showing Bloque 2 in a ≤4 min video
The script already runs 5–6 min and must be cut. The Studio seller (the special-prize evidence) has no assigned time in the video and no named demo artifact (tx hash, agent ID, ERC-8004 registry ID). Fix: storyboard now. Give the seller agent ≥45s (request → gap report → payment tx on explorer) and list the artifacts to capture during Bloque 2. Refs: stockgap.md:132, :39; docs/VIDEO_SCRIPT.md.

#12 [LOW] Bloque 4 (Agentic Wallet) is undefined
It names no skill, wallet, or success criterion, and it overlaps with the key policy in #3. Fix: name the skill, reuse the #3 seller wallet, and define a 2h time box with drop-if-late. Refs: stockgap.md:130.

#13 [LOW] Inconsistent facts that could leak into the forms
- Tech stack says "pytest (213 tests)", but elsewhere it is 392.
- Estado row 3 says "DEPLOYED & VERIFIED", while the checklist says "INSTRUCTIONS READY" / "Northflank in progress".

Fix: sweep these before pasting into the forms. Refs: stockgap.md:38, :44, :152; docs/SUBMISSION_CHECKLIST.md:12,86.
