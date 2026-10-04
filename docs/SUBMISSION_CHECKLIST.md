# StockGap — Submission Checklist & Form Drafts

> BNB Hack: Tokenized Stocks Edition · submissions lock **2026-10-11 12:00 UTC** · judging 12–23 Oct
> All char counts below are exact, **including spaces and newlines** (verified by script; see `docs/MORNING_REVIEW_PACK.md` for the order of submission).

## 1. Mandatory items

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | Public GitHub repo | ⏳ READY, NOT YET PUBLIC | https://github.com/Alejdro83/stockgap-agent — operator flips to public at submission time (go/no-go in morning review) |
| 2 | License in repo | ✅ DONE | MIT — [LICENSE](../LICENSE) verified present at repo root |
| 3 | Deployed link **or** judge-runnable instructions | ✅ INSTRUCTIONS READY | `DEPLOYED_URL` (Northflank deploy in progress) — root [README.md](../README.md) quickstart is judge-runnable today |
| 4 | Demo video (official rule: four minutes or less) | 🎬 SCRIPT READY | [docs/VIDEO_SCRIPT.md](./VIDEO_SCRIPT.md) (target 3:45) — record + upload, then fill `VIDEO_URL` |
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

### Detailed description — limit 2000 — **1991 chars** (truth pass 2026-10-03: testnet/pending claims replaced with verified mainnet facts; text was over the limit and was trimmed to fit)
```
Tokenized stocks trade on-chain; the underlying share trades on a traditional exchange - and almost nobody measures the gap honestly. Naive derived-pair spreads are structurally ~0 (the reference is computed from the token price itself) - a fake peg. StockGap fixes this with an independent per-share close and full provenance per row.

What it does: (1) a rate-limit-aware collector polls 488 RWA tokens (442 Ondo + 46 BStock) every 30s and stores price/reference ticks; (2) a spread engine normalizes by tokenToShareRatio (1 token != 1 share; ratios 0.0667-10.03) and reports spread_bps_real against the independent close; (3) a threshold-crossing alert scanner with rate limiting and digest mode; (4) a mobile-first dashboard (FastAPI + HTMX, SQLite read-only) on localhost:8098; (5) a weekend oracle predicting Monday's implied open vs the last independent close, with deviation flags and an ERC-8004-style reputation loop; (6) an MCP server (JSON-RPC 2.0) whose read tools sit behind an x402/b402 pay-per-call gate - 402 responses carry x402Version:1, payments verified as on-chain receipts (local_receipt) or Binance settlement; every MCP response carries an ERC-8004-style identity header, and the oracle publishes its derived price to an on-chain PriceRegistry on BSC mainnet (ORACLE_PUBLISH toggle).

Honest numbers (2026-10-03): ~1.0M spread_ticks rows; |spread| p25/p50/p75 = 5.1/13.5/32.9 bps (abs>0.5 filter, n~477K); 488 RWA + 40 cross-issuer pairs per 30s tick; 442 underlyings with an independent close; 383 passing tests. Honest limits: three early demo-trade broadcasts reverted (2s route deadline, root cause in RECOVERY.md) - the pipeline now has a pre-broadcast deadline gate and the first live trade succeeded (1 wei dust demo); the on-chain oracle writer is real on BSC mainnet but toggled off by default (mock DB ledger); live b402 binance settlement is mock-tested only and the API account has no b402 merchant provisioned. Built by a three-seat multi-agent factory.
```

### Written demo — limit 1500 — **1152 chars**
```
Written demo (judge-runnable, ~10 min; full run notes: StockGap/README.md):
1. git clone the repo; python3 -m pytest -q -> 383 passed (~123s).
2. mkdir -p data (data/ is gitignored); python3 scripts/run_collector.py - watch a live 30s tick of 488 RWA tokens land in SQLite with per-row provenance (source_endpoint, reference_kind, fetched_at).
3. python3 stockgap/oracle.py --once - one oracle snapshot (its derived price publishes to a PriceRegistry contract on BSC mainnet when ORACLE_PUBLISH=1); then nohup python3 scripts/dashboard.py -> open http://localhost:8098 - mobile-first spread dashboard (SQLite read-only); sort by spread_bps_real to see live gaps vs the independent close (|spread| p50 = 13.5 bps).
4. curl POST /mcp tools/list (always free) -> three read tools; tools/call get_oracle_latest -> weekend-Monday-open oracle snapshot with deviation_bps + deviation_flag per underlying (442 with independent closes). With MCP_PAY_MODE=b402 the same call returns HTTP 402 + x402Version receipt instructions: pay-per-call agent access.
5. Alerts demo: scripts/alerts.py crossing scanner (digest + per-token rate limiting) -> data/alerts.jsonl.
```

### Track selection — **Infrastructure & Tooling** (+ justification, 393 chars)
```
Infrastructure & Tooling: StockGap is the measurement, alerting and agent-access layer that tokenized equities on BNB Chain are missing (independent reference pricing, spread telemetry, pay-per-call MCP). The alternative, Set & Earn, is scoped to BNB Agent Studio agents; StockGap is an independent MCP data service with x402/b402 monetization, so Infrastructure & Tooling is where it belongs.
```

### Tech stack — **357 chars**
```
Python 3; FastAPI + Jinja2 + HTMX (mobile dashboard); SQLite (WAL, short write transactions, read-only readers); JSON-RPC 2.0 MCP server; x402/b402 pay gate with on-chain receipt verification on BSC; HMAC-SHA256 request signing (OpenSSL cross-checked); Sentry observability (opt-in toggle); pytest (383 tests); Dockerfile + supervisor for Northflank deploy.
```

### BNB Chain integration — **1229 chars**
```
BNB Chain touchpoints: (1) x402/b402 pay-per-call MCP gate - agents pay per call; HTTP 402 responses carry x402Version:1; local_receipt verifies an on-chain BSC payment receipt before dispatch (paid calls idempotent by tx hash); the Binance /api/v2/b402/{supported,verify,settle} adapter ships mock-tested (the live API account has no b402 merchant provisioned: code 100001008); (2) ERC-8004-style identity hooks - X-ERC8004-Identity header (erc8004:bsc-mainnet:REGISTRY_PLACEHOLDER#0, registry id pending) on every MCP response plus an oracle_reputation/oracle_feedback loop; the oracle publishes its derived price to PriceRegistry 0x8d2cf4a56f754d5770985d95aa179ef4d5b9b971 on BSC mainnet (deploy tx 0x2235be2b3c516f89f4bdb84f199fb774182c424f024a10d1a35569536948afa1, publish tx 0x8055d62e8667e5564e3ad448a0a5f769285a918ee7f1b05b3ec436ac1dc97cec) behind the ORACLE_PUBLISH toggle; (3) demo-trade pipeline on BSC mainnet (chain 56) via the Binance Web3 aggregator with a simulate-before-broadcast gate plus a pre-broadcast calldata deadline gate - 3 early broadcasts reverted (2s route deadline, root-caused in RECOVERY.md) and the first live trade succeeded (0xd8ef9e75467ddb27ab97076e3b112fbe4a552488b81dddd09a81bb4cd490a48f).
```

### How it could grow through the BNB ecosystem & Agent Bazaar — **673 chars**
```
Through the BNB ecosystem: StockGap is the independent price-gap data layer that tokenized-stock issuers and market makers lack - as RWA listings on BNB Chain grow, every issuer needs a peg monitor and every agent needs machine-readable, payable signals. Agent Bazaar is the natural storefront: StockGap lists its MCP tools as pay-per-call services (x402 receipts settle in BNB), agents discover, call and pay autonomously, and the ERC-8004 reputation loop (oracle_reputation/oracle_feedback with on-chain attestation hooks) gives buyers a verifiable track record. Roadmap: more issuers/venues, per-issuer alert subscriptions, BNB-settled plans, staked oracle attestations.
```

### Tx hashes / contract (≤ 3) — **610 chars** (3 hashes, all status 0x1)
```
Contract (BSC mainnet): PriceRegistry 0x8d2cf4a56f754d5770985d95aa179ef4d5b9b971. Deploy tx 0x2235be2b3c516f89f4bdb84f199fb774182c424f024a10d1a35569536948afa1 (status 0x1, block 125562365). Oracle publish tx 0x8055d62e8667e5564e3ad448a0a5f769285a918ee7f1b05b3ec436ac1dc97cec (status 0x1, block 125562760, event OraclePublish). First successful live trade through the gated pipeline: 0xd8ef9e75467ddb27ab97076e3b112fbe4a552488b81dddd09a81bb4cd490a48f (status 0x1, block 125566635, 1 wei BNB->NVDAB dust demo). Three earlier reverted demo-trade txs are documented in RECOVERY.md (2s route deadline, root-caused).
```
> The 3 hashes above are all successful (status 0x1) and explorer-verifiable on BscScan. The 3 earlier reverted demo-trade hashes stay documented in [RECOVERY.md](../RECOVERY.md) (repo honesty) but are not needed in the form field.

### Links block (paste where the form asks for URLs)

| Field | Value |
|---|---|
| GitHub repo | https://github.com/Alejdro83/stockgap-agent |
| Deployed demo | `DEPLOYED_URL` (Northflank in progress — swap before submit) |
| Video | `VIDEO_URL` (placeholder — swap after upload) |
| Demo / written walkthrough | Judge-runnable quickstart in the root [README.md](../README.md) (~10 min) + deep-dive [StockGap/README.md](../StockGap/README.md) |

## 3. ASK-OPERATOR rows

| # | Field | Question for operator | Status |
|---|-------|----------------------|--------|
| A1 | Team members | Real name, role, contact (email/handle) per team member — for the team fields in both forms | ❓ BLOCKING |
| A2 | BNB wallet (HEX) | Wallet address in HEX (0x…) for the wallet field / potential prize payout | ❓ BLOCKING |
| A3 | Tagline pick | Confirm Option A (198 chars, already in root README) or switch to B/C — see morning review pack | ❓ decision (D2) |
| A4 | Track confirm | Confirm **Infrastructure & Tooling** (drafted justification above) | ❓ decision (D1) |
| A5 | Public-flip go/no-go | Flip repo public only after deploy URL lands and README placeholders (`DEPLOYED_URL`, `VIDEO_URL`) are replaced | ❓ decision (D4) |
| A7 | Video host | Where to upload the recording (YouTube?) so `VIDEO_URL` can be filled | ❓ BLOCKING |
| A8 | Oracle on-chain writer | ✅ RESOLVED (2026-10-03): real BSC **mainnet** publish — `PriceRegistry` [`0x8d2cf4a56f754d5770985d95aa179ef4d5b9b971`](https://bscscan.com/address/0x8d2cf4a56f754d5770985d95aa179ef4d5b9b971), deploy tx [`0x2235be2b…afa1`](https://bscscan.com/tx/0x2235be2b3c516f89f4bdb84f199fb774182c424f024a10d1a35569536948afa1) (block 125562365), publish tx [`0x8055d62e…7cec`](https://bscscan.com/tx/0x8055d62e8667e5564e3ad448a0a5f769285a918ee7f1b05b3ec436ac1dc97cec) (block 125562760, event `OraclePublish`); writer behind `ORACLE_PUBLISH=0\|1` toggle (default 0). ERC-8004 registry id still `REGISTRY_PLACEHOLDER` | ✅ RESOLVED |

## 4. Char-count summary (machine-verified)

| Field | Limit | Used |
|---|---|---|
| One-liner A / B / C | 200 | 198 / 167 / 167 |
| Detailed description | 2000 | 1991 (truth-passed; trimmed from an over-limit draft) |
| Written demo | 1500 | 1152 |
| Track justification | (no limit given) | 393 |
| Tech stack | (no limit given) | 357 |
| BNB Chain integration | (no limit given) | 1229 |
| Growth / Agent Bazaar | (no limit given) | 673 |
| Tx hashes / contract | ≤ 3 hashes | 610 (3 hashes, all status 0x1) |
