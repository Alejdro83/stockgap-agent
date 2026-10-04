# Morning Review Pack — 1-page agenda (2026-10-11 morning, deadline 12:00 UTC)

## 1. Overnight status (fill after deploy lands)

| TODO | Status | Fill in |
|------|--------|---------|
| Northflank deploy healthy (`DEPLOYED_URL`) | ⏳ overnight | paste URL: `______________` |
| Smoke-test deployed: `GET /` = 200 + `POST /mcp tools/list` works there | ⏳ | done at: `____:____` |
| Swap `DEPLOYED_URL` placeholder in root README + SUBMISSION_CHECKLIST links block | ⏳ | commit: `__________` |
| Record video (script: docs/VIDEO_SCRIPT.md) + upload → `VIDEO_URL` | ⏳ | URL: `______________` |
| Swap `VIDEO_URL` placeholder in root README + checklist | ⏳ | |
| Final `python3 -m pytest -q` = 272 passed | ⏳ | |

## 2. ASK-OPERATOR (blocking — forms cannot be filled without these)

| # | Item |
|---|------|
| A1 | Team members: real name, role, contact per person |
| A2 | BNB wallet address (HEX, 0x…) |
| A7 | Video host for upload (YouTube?) |
| A8 | ~~Testnet oracle writer~~ — ✅ RESOLVED 2026-10-03: real BSC **mainnet** oracle publish (PriceRegistry `0x8d2cf4a5…b971`, deploy tx `0x2235be2b…afa1`, publish tx `0x8055d62e…7cec`); ERC-8004 registry id still pending |

## 3. Decision points (say go / no-go / pick)

| # | Decision | Options | Recommendation |
|---|----------|---------|----------------|
| D1 | **Track confirm** | Infrastructure & Tooling **vs** Set & Earn | **Infrastructure & Tooling** — Set & Earn bonus only counts for BNB Agent Studio agents (justification drafted: checklist §2) |
| D2 | **Tagline pick** | A: "StockGap measures the gap between tokenized-stock prices and their independent reference close — 488 RWA tokens / 30s — and sells signals to agents over a pay-per-call (x402) MCP layer on BNB Chain." (198) · B: "An independent price-gap oracle for tokenized equities on BNB Chain: 488 RWA tokens monitored every 30 seconds, signals monetized for agents via x402 pay-per-call MCP." (167) · C: "Tokenized stocks have two prices — the token's and the underlying's. StockGap measures the gap honestly and turns it into pay-per-call signals for agents on BNB Chain." (167) | **A** (already in root README; most concrete) |
| D3 | **License confirm** | MIT LICENSE exists at repo root — keep as-is? | **Keep MIT** (mandatory item already satisfied) |
| D4 | **Public-flip go/no-go** | Flip https://github.com/Alejdro83/stockgap-agent public | Only after D2 lands + `DEPLOYED_URL`/`VIDEO_URL` swapped — repo is judged on first click |
| D5 | Tx-hashes field framing | 3 successful BSC txs + the `PriceRegistry` contract (the 3 reverted history txs stay in the repo's RECOVERY.md) | **Successful hashes + contract** (checklist §2 draft; all 3 hashes status 0x1) |

## 4. Exact submission order (do not reorder)

1. **Morning review** — close items in §2 (operator answers) and decisions in §3.
2. Swap placeholders: `DEPLOYED_URL`, `VIDEO_URL` in root `README.md` + `docs/SUBMISSION_CHECKLIST.md`; commit + (allowed now) push.
3. Record + upload video; verify playback at `VIDEO_URL`.
4. **D4 go** → flip repo public → verify incognito view shows README + LICENSE.
5. Smoke-test public repo + deployed URL one last time (`pytest` still 272).
6. **Google Form #1** — paste drafts from `docs/SUBMISSION_CHECKLIST.md` §2 (one-liner, detailed description, track, team A1, wallet A2).
7. **Google Form #2** — written demo, tech stack, BNB Chain integration, growth/Agent Bazaar, tx hashes/contract (D5), links block.
8. Re-read both forms once (char limits are respected per the counts in §4 of the checklist; re-check the root README tagline char count after the placeholder swap), submit **before 12:00 UTC**.
9. Save confirmation receipts; done.

> Everything above is prep only until step 6 — nothing has been submitted, no repo is public.
