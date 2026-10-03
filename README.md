# StockGap

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-213%20passing-brightgreen)](./tests)
[![Python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![BNB Chain](https://img.shields.io/badge/BNB%20Chain-BSC%2056-F0B90B)](https://www.bnbchain.org/)

> **StockGap measures the gap between tokenized-stock prices and their independent reference close — 488 RWA tokens / 30s — and sells signals to agents over a pay-per-call (x402) MCP layer on BNB Chain.**

Prepared for **BNB Hack: Tokenized Stocks Edition** (submissions lock 2026-10-11 23:59 UTC).

- **Repo:** https://github.com/Alejdro83/stockgap-agent (public at submission time)
- **Deployed demo:** `DEPLOYED_URL` (placeholder — swap when the Northflank deploy lands; judge-runnable instructions below)
- **Video pitch (5–6 min):** `VIDEO_URL` (placeholder — swap after upload; script in [docs/VIDEO_SCRIPT.md](./docs/VIDEO_SCRIPT.md))
- **Published methodology paper (ITSC) / written demo:** https://thoracic.clinic/articles/itsc-qs4nqr
- **Deep-dive README (architecture, verified API details, evidence):** [StockGap/README.md](./StockGap/README.md)

## What it does

1. **Monitor** — a rate-limit-aware collector polls 488 RWA tokenized stocks (442 Ondo + 46 BStock) every 30 s and stores price/reference ticks with per-row provenance.
2. **Measure** — a spread engine normalizes by `tokenToShareRatio` (1 token ≠ 1 share) and reports `spread_bps_real`: the gap between the token's implied price and an **independent** per-share close. The naive derived-pair spread is structurally ≈0 — StockGap measures the gap that actually exists.
3. **Alert** — threshold-crossing scanner (cron `*/2`) with per-token rate limiting and digest mode.
4. **Visualize** — mobile-first dashboard (FastAPI + Jinja2 + HTMX) on `:8098`, SQLite opened read-only.
5. **Score** — weekend oracle predicting the implied Monday open vs the last independent close, with per-token deviation flags and an ERC-8004-style reputation feedback loop.
6. **Serve agents** — an MCP server (JSON-RPC 2.0, `POST /mcp`) with read tools behind an **x402/b402 pay-per-call gate**, plus a simulate-gated demo-trade pipeline (quote → simulate → sign → broadcast).

## Quickstart (judge-runnable)

```bash
git clone https://github.com/Alejdro83/stockgap-agent && cd stockgap-agent
python3 -m pytest -q          # 213 passed (~4s)
mkdir -p data                 # data/ is gitignored

# terminal 1: live collector — 30s ticks of 488 RWA tokens (rows land after the first tick)
python3 scripts/run_collector.py

# terminal 2: mobile dashboard
nohup python3 scripts/dashboard.py >> data/dashboard.log 2>&1 &
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8098/    # 200

# Agent-facing MCP (tools/list is always free)
curl -s -X POST http://localhost:8098/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

With `MCP_PAY_MODE=b402`, calls return HTTP 402 + `x402Version:1` receipt instructions — pay-per-call agent access. Full run notes: [StockGap/README.md](./StockGap/README.md).

## Track: Infrastructure & Tooling

StockGap is not a consumer DeFi product — it is the measurement, alerting and agent-access layer that tokenized equities on BNB Chain are missing: independent reference pricing, spread telemetry, and a pay-per-call MCP interface. "Set & Earn" is tempting because our x402/b402 gate literally monetizes agent calls, but the Set & Earn bonus counts only for BNB Agent Studio agents; StockGap is an independent MCP data service. Infrastructure & Tooling is the honest home for it.

## BNB Chain integration

- **x402/b402 pay-per-call MCP gate** — HTTP 402 responses carry `x402Version:1`; `local_receipt` verifies an **on-chain BSC payment receipt** before dispatch (paid calls are idempotent by tx hash); Binance's `/api/v2/b402/{supported,verify,settle}` adapter ships mock-tested.
- **Oracle publish** — the oracle writes its predictions/reputation to SQLite; publishing its derived price to **BSC testnet** is the deployment hook (testnet tx hashes pending confirmation — [docs/SUBMISSION_CHECKLIST.md](./docs/SUBMISSION_CHECKLIST.md) A8).
- **ERC-8004 hooks** — every MCP response carries an `X-ERC8004-Identity` header (`erc8004:bsc-testnet:…`, registry id pending); `oracle_reputation`/`oracle_feedback` implement the ERC-8004-style reputation/feedback interface with an on-chain attestation stub.
- **Demo-trade pipeline** — BSC mainnet (chain 56) via the Binance Web3 aggregator with a strict simulate-before-broadcast gate.

## Growth through the BNB ecosystem & Agent Bazaar

StockGap is the independent price-gap data layer that tokenized-stock issuers and market makers lack — as RWA listings on BNB Chain grow, every issuer needs a peg monitor and every agent needs machine-readable, payable signals. Agent Bazaar is the natural storefront: StockGap lists its MCP tools as pay-per-call services (x402 receipts settle in BNB), agents discover, call and pay autonomously, and the ERC-8004 reputation loop gives buyers a verifiable track record. Roadmap: more issuers/venues, per-issuer alert subscriptions, BNB-settled plans, staked oracle attestations.

## Honest numbers (2026-10-03)

| Metric | Value |
|---|---|
| spread_ticks rows | ≈1.0M (999,336 at check time, live collector) |
| \|spread\| p25 / p50 / p75 | 5.1 / 13.5 / 32.9 bps (abs > 0.5 filter, n ≈ 477K) |
| Per 30s tick | 488 RWA + 40 cross-issuer pairs |
| Oracle coverage | 442 underlyings with an independent close |
| Tests | 213 passing |

Honest limits: the three demo-trade broadcasts reverted on-chain (2-second in-calldata route deadline — root-caused via `eth_call` replay + calldata decode, [RECOVERY.md](./RECOVERY.md)); live b402 `binance` settlement is mock-tested only.

## Evidence

[StockGap/README.md](./StockGap/README.md) (deep dive) · [FACTORY.md](./FACTORY.md) · [RECOVERY.md](./RECOVERY.md) · [DX-LOG.md](./DX-LOG.md) · [docs/](./docs/) (submission pack)

## License

MIT — see [LICENSE](./LICENSE).
