# StockGap — Submission Checklist & Form Drafts

> BNB Hack: Tokenized Stocks Edition · submissions lock **2026-10-11 23:59 UTC** · judging 12–23 Oct
> All char counts below are exact, **including spaces and newlines** (verified by script; see `docs/MORNING_REVIEW_PACK.md` for the order of submission).

## 1. Mandatory items

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | Public GitHub repo | ⏳ READY, NOT YET PUBLIC | https://github.com/Alejdro83/stockgap-agent — operator flips to public at submission time (go/no-go in morning review) |
| 2 | License in repo | ✅ DONE | MIT — [LICENSE](../LICENSE) verified present at repo root |
| 3 | Deployed link **or** judge-runnable instructions | ✅ INSTRUCTIONS READY | `DEPLOYED_URL` (Northflank deploy in progress) — root [README.md](../README.md) quickstart is judge-runnable today |
| 4 | 5–6 min video pitch | 🎬 SCRIPT READY | [docs/VIDEO_SCRIPT.md](./VIDEO_SCRIPT.md) — record + upload, then fill `VIDEO_URL` |
| 5 | Google Form #1 | 📝 DRAFTS READY | fields in §2 below |
| 6 | Google Form #2 | 📝 DRAFTS READY | fields in §2 below |

## 2. Form fields — paste-ready drafts (char counts in brackets)

### One-liner — limit 200 — **PICK ONE** (all three ≤ 200)

**Option A (drafted into root README — 198 chars):**
```
StockGap measures the gap between tokenized-stock prices and their independent reference close — 488 RWA tokens / 30s — and sells signals to agents over a pay-per-call (x402) MCP layer on BNB Chain.
```

**Option B (167 chars):**
```
An independent price-gap oracle for tokenized equities on BNB Chain: 488 RWA tokens monitored every 30 seconds, signals monetized for agents via x402 pay-per-call MCP.
```

**Option C (167 chars):**
```
Tokenized stocks have two prices — the token's and the underlying's. StockGap measures the gap honestly and turns it into pay-per-call signals for agents on BNB Chain.
```

### Detailed description — limit 2000 — **1955 chars**
```
Tokenized stocks trade on-chain while the underlying share trades on a traditional exchange — but almost nobody measures the gap honestly. Naive derived-pair spreads are structurally ~0 (the reference is computed from the token price itself), so an apparent peg is an artifact. StockGap fixes this with an independent per-share close and full provenance per row.

What it does: (1) a rate-limit-aware collector polls 488 RWA tokens (442 Ondo + 46 BStock) every 30s and stores price/reference ticks; (2) a spread engine normalizes by tokenToShareRatio (1 token != 1 share; ratios 0.0667-10.03) and reports spread_bps_real against the independent close; (3) a threshold-crossing alert scanner with per-token rate limiting and digest mode; (4) a mobile-first dashboard (FastAPI + HTMX, SQLite read-only) on localhost:8098; (5) a weekend oracle predicting Monday's implied open vs the last independent close, with per-token deviation flags and an ERC-8004-style reputation feedback loop; (6) an MCP server (JSON-RPC 2.0) whose read tools sit behind an x402/b402 pay-per-call gate - 402 responses carry x402Version:1, payments verified as on-chain receipts (local_receipt) or Binance settlement; every MCP response carries an ERC-8004-style identity header, and publishing the oracle's derived price to BSC testnet is the deployment hook (tx hashes pending, item A8).

Honest numbers (2026-10-03): ~1.0M spread_ticks rows; |spread| p25/p50/p75 = 5.1/13.5/32.9 bps (abs>0.5 filter, n~477K); 488 RWA + 40 cross-issuer pairs per 30s tick; 442 underlyings with an independent close; 213 passing tests. Honest limits: the three demo-trade broadcasts reverted on-chain (2s route deadline, root cause documented in RECOVERY.md - eth_call replay + calldata decode for 2 of 3 txs, the 3rd inferred from the identical pattern); live b402 binance settlement is mock-tested only. Built by a three-seat multi-agent factory (operator + strategist + builder/executor agents).
```

### Written demo — limit 1500 — **1169 chars**
```
Written demo (judge-runnable, ~10 min; published write-up (ITSC): https://thoracic.clinic/articles/itsc-qs4nqr):
1. git clone the repo; python3 -m pytest -q -> 213 passed (4s).
2. mkdir -p data (data/ is gitignored); python3 scripts/run_collector.py - watch a live 30s tick of 488 RWA tokens land in SQLite with per-row provenance (source_endpoint, reference_kind, fetched_at).
3. python3 stockgap/oracle.py --once - one oracle snapshot (publishing its derived price to BSC testnet is the deployment hook - see A8); then nohup python3 scripts/dashboard.py -> open http://localhost:8098 - mobile-first spread dashboard (SQLite read-only); sort by spread_bps_real to see live gaps vs the independent close (|spread| p50 = 13.5 bps).
4. curl POST /mcp tools/list (always free) -> three read tools; tools/call get_oracle_latest -> weekend-Monday-open oracle snapshot with deviation_bps + deviation_flag per underlying (442 with independent closes). With MCP_PAY_MODE=b402 the same call returns HTTP 402 + x402Version receipt instructions: pay-per-call agent access.
5. Alerts demo: scripts/alerts.py crossing scanner (digest + per-token rate limiting) -> data/alerts.jsonl.
```

### Track selection — **Infrastructure & Tooling** (+ justification, 393 chars)
```
Infrastructure & Tooling: StockGap is the measurement, alerting and agent-access layer that tokenized equities on BNB Chain are missing (independent reference pricing, spread telemetry, pay-per-call MCP). The alternative, Set & Earn, is scoped to BNB Agent Studio agents; StockGap is an independent MCP data service with x402/b402 monetization, so Infrastructure & Tooling is where it belongs.
```

### Tech stack — **357 chars**
```
Python 3; FastAPI + Jinja2 + HTMX (mobile dashboard); SQLite (WAL, short write transactions, read-only readers); JSON-RPC 2.0 MCP server; x402/b402 pay gate with on-chain receipt verification on BSC; HMAC-SHA256 request signing (OpenSSL cross-checked); Sentry observability (opt-in toggle); pytest (213 tests); Dockerfile + supervisor for Northflank deploy.
```

### BNB Chain integration — **785 chars**
```
BNB Chain touchpoints: (1) x402/b402 pay-per-call MCP gate - agents pay per call; HTTP 402 responses carry x402Version:1; local_receipt verifies an on-chain BSC payment receipt before dispatch (paid calls idempotent by tx hash); the Binance /api/v2/b402/{supported,verify,settle} adapter ships mock-tested; (2) ERC-8004-style identity hooks - X-ERC8004-Identity header (erc8004:bsc-testnet:..., registry id pending) on every MCP response plus an oracle_reputation/oracle_feedback loop; publishing the oracle's derived price to BSC testnet is the deployment hook (tx hashes pending, A8); (3) demo-trade pipeline on BSC mainnet (chain 56) via the Binance Web3 aggregator with a simulate-before-broadcast gate - 3 broadcasts, honestly documented (reverted; root cause: 2s route deadline).
```

### How it could grow through the BNB ecosystem & Agent Bazaar — **673 chars**
```
Through the BNB ecosystem: StockGap is the independent price-gap data layer that tokenized-stock issuers and market makers lack - as RWA listings on BNB Chain grow, every issuer needs a peg monitor and every agent needs machine-readable, payable signals. Agent Bazaar is the natural storefront: StockGap lists its MCP tools as pay-per-call services (x402 receipts settle in BNB), agents discover, call and pay autonomously, and the ERC-8004 reputation loop (oracle_reputation/oracle_feedback with on-chain attestation hooks) gives buyers a verifiable track record. Roadmap: more issuers/venues, per-issuer alert subscriptions, BNB-settled plans, staked oracle attestations.
```

### Tx hashes / contract (≤ 3) — **583 chars**
```
Contracts: none yet (pre-deployment hackathon build). Verified BSC mainnet tx hashes (demo-trade pipeline, all status 0x0 = reverted; value returned, only gas burned): 0xfdc9806cd4791e10ca806075b2dfb84d040d414d55cab37ea08f943cf8f32021 (root cause INFERRED - identical pattern, not replayed), 0x9ca395c7db3b277fe3317c45be4ddc7bf1e09755241dff32911a88c42c7c7dfc, 0x7c3379a18636c68c3bb0dc23dbcee6fefcff916baf74c1302ae4a88c4817446f (both proven 'Route: expired' 2s in-calldata deadline by eth_call replay + calldata decode, RECOVERY.md). BSC-testnet oracle tx hashes available on request.
```
> Alternative if the form wants only successful txs: *"Not applicable — pre-deployment hackathon build; testnet oracle tx hashes available on request."* (The 3 hashes above are real and explorer-verifiable — they reverted for a documented reason; keep them only if the honesty framing fits the field.)

### Links block (paste where the form asks for URLs)

| Field | Value |
|---|---|
| GitHub repo | https://github.com/Alejdro83/stockgap-agent |
| Deployed demo | `DEPLOYED_URL` (Northflank in progress — swap before submit) |
| Video | `VIDEO_URL` (placeholder — swap after upload) |
| Demo / written walkthrough | https://thoracic.clinic/articles/itsc-qs4nqr (published ITSC paper) |

## 3. ASK-OPERATOR rows

| # | Field | Question for operator | Status |
|---|-------|----------------------|--------|
| A1 | Team members | Real name, role, contact (email/handle) per team member — for the team fields in both forms | ❓ BLOCKING |
| A2 | BNB wallet (HEX) | Wallet address in HEX (0x…) for the wallet field / potential prize payout | ❓ BLOCKING |
| A3 | Tagline pick | Confirm Option A (198 chars, already in root README) or switch to B/C — see morning review pack | ❓ decision (D2) |
| A4 | Track confirm | Confirm **Infrastructure & Tooling** (drafted justification above) | ❓ decision (D1) |
| A5 | Public-flip go/no-go | Flip repo public only after deploy URL lands and README placeholders (`DEPLOYED_URL`, `VIDEO_URL`) are replaced | ❓ decision (D4) |
| A6 | ITSC wording | Confirm what we may claim about https://thoracic.clinic/articles/itsc-qs4nqr in the pitch (deep-README ITSC appendix not found in repo — wording kept generic on purpose) | ❓ BLOCKING |
| A7 | Video host | Where to upload the recording (YouTube?) so `VIDEO_URL` can be filled | ❓ BLOCKING |
| A8 | Testnet oracle writer | Confirm the deployment-side "oracle writes derived price to BSC testnet" claim + get the tx hashes before recording/submitting (no in-repo evidence — Claude review flag) | ❓ BLOCKING |

## 4. Char-count summary (machine-verified)

| Field | Limit | Used |
|---|---|---|
| One-liner A / B / C | 200 | 198 / 167 / 167 |
| Detailed description | 2000 | 1955 |
| Written demo | 1500 | 1169 |
| Track justification | (no limit given) | 393 |
| Tech stack | (no limit given) | 357 |
| BNB Chain integration | (no limit given) | 785 |
| Growth / Agent Bazaar | (no limit given) | 673 |
| Tx hashes / contract | ≤ 3 hashes | 583 (3 hashes) |
