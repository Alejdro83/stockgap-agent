# StockGap

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-416%20passing-brightgreen)](./tests)
[![Python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![BNB Chain](https://img.shields.io/badge/BNB%20Chain-BSC%2056-F0B90B)](https://www.bnbchain.org/)

> **StockGap measures the gap between tokenized-stock prices and their independent reference close — 488 RWA tokens / 30s — and sells signals to agents over a pay-per-call (x402) MCP layer on BNB Chain.**

Prepared for **BNB Hack: Tokenized Stocks Edition** (submissions lock 2026-10-11 12:00 UTC).

- **Repo:** https://github.com/Alejdro83/stockgap-agent (public at submission time)
- **Deployed demo:** <https://http--stockgap-app--6h2d2gps9lkr.code.run/> (Northflank, live; `POST /mcp` `tools/list` is free) — judge-runnable instructions below
- **Video pitch (≤4 min):** `VIDEO_URL` (placeholder — swap after upload; script in [docs/VIDEO_SCRIPT.md](./docs/VIDEO_SCRIPT.md))
- **Written demo (judge-runnable, ~10 min):** the [Quickstart](#quickstart-judge-runnable) below runs everything locally; full run notes in [StockGap/README.md](./StockGap/README.md)
- **Deep-dive README (architecture, verified API details, evidence):** [StockGap/README.md](./StockGap/README.md)

## What it does

1. **Monitor** — a rate-limit-aware collector polls 488 RWA tokenized stocks (442 Ondo + 46 BStock) every 30 s through the **Binance Web3 RWA Data API** (`rwa/tokens`, `rwa/price`, `rwa/underlying-market` — the track's tokenized-stocks data layer) and stores price/reference ticks with per-row provenance.
2. **Measure** — a spread engine normalizes by `tokenToShareRatio` (1 token ≠ 1 share) and reports `spread_bps_real`: the gap between the token's implied price and an **independent** per-share close. The naive derived-pair spread is structurally ≈0 — StockGap measures the gap that actually exists.
3. **Alert** — threshold-crossing scanner (cron `*/2`) with per-token rate limiting and digest mode.
4. **Visualize** — mobile-first dashboard (FastAPI + Jinja2 + HTMX) on `:8098`, SQLite opened read-only.
5. **Score** — weekend oracle predicting the implied Monday open vs the last independent close, with per-token deviation flags and an ERC-8004-style reputation feedback loop.
6. **Serve agents** — an MCP server (JSON-RPC 2.0, `POST /mcp`) with read tools behind an **x402/b402 pay-per-call gate**, plus a simulate-gated demo-trade pipeline (quote → simulate → sign → broadcast).

## Quickstart (judge-runnable)

```bash
git clone https://github.com/Alejdro83/stockgap-agent && cd stockgap-agent
python3 -m pip install -r requirements.txt eth-account   # eth-account: needed by the on-chain writer tests
python3 -m pytest -q          # 416 passed, 2 skipped (~2 min wall, measured 2026-10-08 @ d6de0a5)
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

## Track: Tokenized Stocks Products & Agents (main track)

StockGap submits to the hackathon's single track — **Tokenized Stocks Products & Agents** (verified on the official event page 2026-10-07; the "Infrastructure & Tooling" / "Set & Earn" options belong to an older form edition and are not claimed). The fit is direct: the track rule asks for bStocks, Ondo or xStocks to be central — StockGap's live universe is **442 Ondo + 46 BStock** (xStocks appear only in tests and are not claimed) — and the track's idea list names an "on-chain vs reference price monitor" on the **RWA Data API**, which is exactly what StockGap is: independent reference pricing, spread telemetry (`spread_bps_real`), and a pay-per-call MCP interface for agents. The BNB Agent Studio / Agentic Wallet special prizes are optional add-ons tied to bonus judging, not separate tracks.

## BNB Chain integration

- **x402/b402 pay-per-call MCP gate** — HTTP 402 responses carry `x402Version:1`; `local_receipt` verifies an **on-chain BSC payment receipt** before dispatch (paid calls are idempotent by tx hash); Binance's `/api/v2/b402/{supported,verify,settle}` adapter ships mock-tested.
- **Oracle publish** — `stockgap/onchain_writer.py` publishes the oracle's derived price to a `PriceRegistry` contract on **BSC mainnet** (chain 56): [`0x8d2cf4a56f754d5770985d95aa179ef4d5b9b971`](https://bscscan.com/address/0x8d2cf4a56f754d5770985d95aa179ef4d5b9b971) (deploy tx [`0x2235be2b…afa1`](https://bscscan.com/tx/0x2235be2b3c516f89f4bdb84f199fb774182c424f024a10d1a35569536948afa1)); first publish tx [`0x8055d62e…7cec`](https://bscscan.com/tx/0x8055d62e8667e5564e3ad448a0a5f769285a918ee7f1b05b3ec436ac1dc97cec) (event `OraclePublish`, snapshot ACN priceE8=19962666700). The writer is real but **toggled**: `ORACLE_PUBLISH=0|1` (default 0 = mock DB ledger).
- **ERC-8004 hooks** — every MCP response carries an `X-ERC8004-Identity` header (`erc8004:bsc-mainnet:REGISTRY_PLACEHOLDER#0` — the header id stays a placeholder until after the lock because changing it means redeploying the live service; the seller agent's real **BSC mainnet ERC-8004 identity is agent #365285**, see [stockgap-seller](https://github.com/Alejdro83/stockgap-seller)); `oracle_reputation`/`oracle_feedback` implement the ERC-8004-style reputation/feedback interface with an on-chain attestation stub.
- **Demo-trade pipeline** — BSC mainnet (chain 56) via the Binance Web3 aggregator with a strict simulate-before-broadcast gate, a **pre-broadcast calldata deadline gate** (the vendor route deadline is fixed at build+~2 s — no API parameter; probe evidence in `docs/evidence/flash_deadline_probe.json`) and a unified `Executor.quote_and_swap()` path. First successful live trade: [`0xd8ef9e75…a48f`](https://bscscan.com/tx/0xd8ef9e75467ddb27ab97076e3b112fbe4a552488b81dddd09a81bb4cd490a48f) (1 wei BNB→NVDAB dust demo, one attempt).

- **Live mainnet pay-per-call (Prueba 2, 2026-10-08)** — a real 0.005 BNB payment on BSC mainnet unlocked a `get_spread` report through the x402 gate: tx [`0x656e0477…957d`](https://bscscan.com/tx/0x656e0477b5fa3cb0f470c56ef6ec7eb9709047163ae303efb9bcc0a66645957d) (status 0x1), replay of the same hash rejected. Evidence: [docs/evidence/prueba2/](./docs/evidence/prueba2/NOTES.md). The earlier [prueba1](./docs/evidence/prueba1/) run is a **BSC testnet dry-run**, not mainnet evidence.

## Growth through the BNB ecosystem & Agent Bazaar

StockGap is the independent price-gap data layer that tokenized-stock issuers and market makers lack — as RWA listings on BNB Chain grow, every issuer needs a peg monitor and every agent needs machine-readable, payable signals. Agent Bazaar is the natural storefront: StockGap lists its MCP tools as pay-per-call services (x402 receipts settle in BNB), agents discover, call and pay autonomously, and the ERC-8004 reputation loop gives buyers a verifiable track record. Roadmap: more issuers/venues, per-issuer alert subscriptions, BNB-settled plans, staked oracle attestations.

## Honest numbers (2026-10-10)

| Metric | Value |
|---|---|
| spread_ticks rows | ≈12.1M (12,076,686 at check time, live collector) |
| \|spread\| p25 / p50 / p75 | 4.7 / 12.3 / 29.0 bps (abs > 0.5 filter, n ≈ 4.76M) |
| Per 30s tick | 488 RWA + 40 cross-issuer pairs |
| Oracle coverage | 442 underlyings with an independent close |
| Tests | 416 passed, 2 skipped (`pytest -q` @ `d6de0a5`) |

Honest limits: three early demo-trade broadcasts reverted on-chain (2-second in-calldata route deadline — root-caused via `eth_call` replay + calldata decode, [RECOVERY.md](./RECOVERY.md) documents them unchanged). The pipeline now ships a **pre-broadcast deadline gate**, and tx [`0xd8ef9e75…a48f`](https://bscscan.com/tx/0xd8ef9e75467ddb27ab97076e3b112fbe4a552488b81dddd09a81bb4cd490a48f) is the **first successful live trade** through the hardened pipeline (1 wei BNB→NVDAB dust demo, one attempt). The oracle's on-chain writer is real on BSC mainnet ([`PriceRegistry 0x8d2cf4a5…b971`](https://bscscan.com/address/0x8d2cf4a56f754d5770985d95aa179ef4d5b9b971)) but **toggled off by default** (`ORACLE_PUBLISH=0` = mock DB ledger). b402: `local_receipt` verification works live against BSC RPC (wrong-recipient receipts correctly rejected), but live Binance `binance` settlement is mock-tested only and the API account has no b402 merchant provisioned — `/api/v2/b402/{supported,verify}` answer HTTP 200 with `code=100001008` ("business entity empty"); `settle` is unexercised. The ERC-8004 registry id is still a placeholder.

## Evidence

[StockGap/README.md](./StockGap/README.md) (deep dive) · [FACTORY.md](./FACTORY.md) · [RECOVERY.md](./RECOVERY.md) · [DX-LOG.md](./DX-LOG.md) · [docs/](./docs/) (submission pack)

## Maintenance

- `scripts/rotate_data.py` runs every 10 minutes from crontab (`flock -n` + `nice`): gzips JSONL archives and copytruncates `data/*.log` / `data/logs/*.log` past `LOG_MAX_MB`.
- `scripts/compact_ledger.py` runs daily at 04:25 (`flock -n` + `ionice` + `nice`): folds raw `spread_ticks` older than `RAW_KEEP_DAYS` into `spread_ticks_hourly` bars and prunes old bars / `cross_issuer_ticks` / `oracle_predictions`. It only touches rows the shipper has already shipped (`ship_meta.json` watermark) and never the newest row; with no valid watermark it aggregates and deletes nothing.
- Knobs: `RAW_KEEP_DAYS=14`, `HOURLY_KEEP_DAYS=180`, `CROSS_KEEP_DAYS=180`, `ORACLE_KEEP_DAYS=180`. An invalid value falls back to the default; a valid but too-low value is clamped up (raw ≥ 2 days, hourly > raw).
- `--dry-run` writes no data (`data/spread_ticks.db` stays byte-identical) but may create `spread_ticks.db-wal` / `-shm` when it opens the read connection — it is not "touches nothing".
- `--migrate-indexes` is a one-time step that needs every writer stopped (collector, dashboard, alerts, oracle, shipper); the nightly job refuses to run without `idx_spread_ts`.
- There is no automatic `VACUUM`.

## License

MIT — see [LICENSE](./LICENSE).
