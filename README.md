# StockGap — Tokenized-stocks spread monitor with pay-per-call agent access

[![Python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![SQLite](https://img.shields.io/badge/sqlite-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![MCP](https://img.shields.io/badge/MCP-JSON--RPC%202.0-2545E6)](https://modelcontextprotocol.io/)

Submission for **BNB Hack: Tokenized Stocks Edition** (submissions lock 2026-10-11 12:00 UTC).

> **StockGap watches the gap between on-chain tokenized-stock prices and their independent reference close — and exposes the signals to agents over a pay-per-call JSON-RPC layer.**

Built on BSC mainnet (chain 56) against the Binance Web3 API (OC gateway): an HMAC-signed market-data client feeds a spread engine and SQLite tick store; alerts, a mobile dashboard, a weekend-open oracle scorer, and a simulate-gated demo-trade pipeline sit on top; an MCP server with a b402 pay gate makes the read tools callable by other agents. Built by a three-seat agent factory (see [FACTORY.md](./FACTORY.md)).

## 🎯 What it does

1. **Monitor** — a collector polls the RWA market endpoints (batched, rate-limit aware) and stores price/reference ticks with full provenance per row.
2. **Measure** — computes `spread_bps_real`, the gap between the token's implied price and an **independent** close (the naive derived-pair spread is structurally ≈0 — see below).
3. **Alert** — a threshold-crossing scanner (cron `*/2`) with per-token rate limiting, digest mode, and honest metric labeling; file-log transport today, Telegram pluggable.
4. **Visualize** — a mobile-first dashboard (FastAPI + Jinja2 + HTMX) on `localhost:8098`, SQLite opened read-only.
5. **Score** — a weekend oracle (cron `*/5`) estimating the implied Monday open vs the last independent close, with per-token deviation flags.
6. **Serve agents** — an MCP layer (JSON-RPC 2.0, `POST /mcp`) with three read tools behind a b402 pay-per-call gate (`free` by default, on-chain receipt or Binance settlement), plus a demo-trade pipeline (quote → simulate → sign → broadcast) with a strict simulate-before-broadcast gate.

## ✨ Features

| Feature | Description |
|---------|-------------|
| **Real spread metric** | `spread_bps_real` vs independent close — the derived-pair spread bug (488/488 ≈ 0 bps) found and fixed |
| **Provenance per row** | one consistent price source per metric; `source_endpoint` + `fetched_at` on every tick |
| **Honest history** | rows without an independent close labeled `reference_kind='derived'`, `spread_bps_real=NULL` — no backfill |
| **Crossing alerts** | threshold crossings (not level spam), rate-limited, digest mode, metric-switchable (`ALERT_METRIC=real\|derived`) |
| **Weekend oracle** | implied Monday open vs independent close, `tokenToShareRatio`-normalized per share |
| **Simulate-gated executor** | aborts before sign/broadcast unless a fresh simulate returns SUCCESS; refuses wrong router/chain |
| **MCP pay-per-call** | JSON-RPC 2.0 tools over `POST /mcp`; `initialize`/`tools/list` always free; b402 gate (`local_receipt` or `binance`) with idempotent paid calls |
| **Ops watchdogs** | collector watchdog (cron `*/5`) + stateless alert scans + `flock`-guarded oracle refresh |

## 🏗️ Architecture

```
                     Binance Web3 API (OC gateway, HMAC-signed)
                                   │
                     ┌─────────────▼──────────────┐
                     │ collector (run_collector)  │  writes ticks
                     │ watchdog: ensure_collector │  (cron */5)
                     └─────────────┬──────────────┘
                                   ▼
                            SQLite (data/)
              ┌───────────────┬────┴───────────┬─────────────────┐
              │ mode=ro       │ mode=ro        │ oracle cache    │
   ┌──────────▼──────┐ ┌──────▼──────┐ ┌───────▼────────┐ ┌──────▼──────────┐
   │ dashboard :8098 │ │ MCP /mcp    │ │ oracle (--once)│ │ alerts (*/2)    │
   │ FastAPI+HTMX    │ │ + b402 gate │ │ cron */5 flock │ │ crossings→log/  │
   │ (local only)    │ │ free|b402   │ │ ref cache TTL  │ │ Telegram (plug) │
   └─────────────────┘ └─────────────┘ └────────────────┘ └─────────────────┘
              ▲
              │ demo-trade pipeline (operator-approved, one broadcast per approval)
        executor: quote → simulate → sign (local) → broadcast → receipt check
```

- **Key decision** — SQLite writer discipline: collector writes tick tables; the oracle writes its own tables (`oracle_predictions`/`oracle_reference`, `oracle_reputation`/`oracle_feedback`); every read path opens `mode=ro` (dashboard adds `immutable=1`); alerts/MCP are read-only `mode=ro` except two narrow documented writes — the alert scanner's watermark rows (`alert_state`) and the pay gate's paid-call records (`paid_calls`).
- **Key decision** — one price source per metric (provenance rule) so no series is ambiguous about its frame.
- **Key decision** — label, don't backfill: missing independent closes stay `derived`, never fabricated.

## 🚀 Quick Start

```bash
# Clone
git clone <url>
cd stockgap-agent

# Verify (no Docker — runs directly on Python 3; no Docker build is part of this track)
python3 -m pytest -q
# 199 passed in 5.73s

# Start the dashboard (local only — no public URL)
nohup python3 scripts/dashboard.py >> data/dashboard.log 2>&1 &

# Verify
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8098/
# 200

# Start the tick collector (watchdog: scripts/ensure_collector.sh, crontab */5)
python3 scripts/run_collector.py
```

> Restart note: `pkill -f scripts/dashboard.py` run from a shell command that itself contains that string kills the invoking shell — run the kill and the relaunch as separate steps.

### API Example

```bash
# List the agent-callable tools (always free)
curl -s -X POST http://localhost:8098/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Call a tool (pay-gated when MCP_PAY_MODE=b402; free by default)
curl -s -X POST http://localhost:8098/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_oracle_latest","arguments":{}}}'
# {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"snapshot_ts\": \"2026-10-02T09:15:01Z\", \"rows\": [{\"underlying_ticker\": \"AAON\", ...\"deviation_bps\": 618.08, \"deviation_flag\": true, \"cutoff_ts\": \"2026-10-04T23:59:00Z\"}]}"}]}}
```

## 📊 Stage Progress

Per-module test counts (measured: `grep -c 'def test_' tests/*.py`; no build-order implied):

| Module | Tests | Key features |
|--------|------:|--------------|
| test_signer | 15 | HMAC-SHA256 signing, OpenSSL cross-check vector |
| test_alerts | 28 | crossings, rate limiting, ratio≠1 formatting, actionability |
| test_b402 | 23 | pay gate: local receipt verify + mock `binance` settlement |
| test_collector | 9 | batched collection, provenance columns |
| test_dashboard | 13 | read-only dashboard + fragments |
| test_executor | 16 | tx sanitization, chain/router refusals, simulate gate |
| test_mcp | 14 | JSON-RPC tools, free/dispatch, idempotent paid calls |
| test_oracle | 27 | per-share close normalization, fallbacks, cache TTL |
| test_spread | 30 | spread engine, cross-issuer |
| test_spread_real | 24 | ratio normalization, NFLX regression, derived-vs-real |
| **Total** | **199** | **all passing** |

Recorded suite-size checkpoints in DX-LOG (milestone notes, **not** a build order): 15 → 82 → 98 → 125 → **199**.

**Token universe (verified live):** **488 tokens = 442 Ondo + 46 BStock**. xStocks are documented in the API but are **NOT listed live** — `rwa/search` for `AAPLx`/`NVDAx`/`TSLAx` returns code 40382 (not found). Never assume 1 token = 1 share: `tokenToShareRatio` ranges ≈0.0667–10.03 (228/488 are exactly 1).

## 🧪 Testing

```bash
python3 -m pytest -q
# 199 passed in 5.73s
```

Test coverage includes:
- request signing (incl. an OpenSSL cross-check vector) and wire-exact request paths
- spread math: ratio normalization (10 / 1 / 0.066667), the NFLX +90000 bps regression, derived-vs-independent metrics
- alert crossings, per-token rate limiting, digest mode, metric switch
- executor tx sanitization (nonce injection, chainId 56, checksums) and refusals (wrong router / wrong chain)
- simulate-before-broadcast abort path
- MCP JSON-RPC tools and b402 gate (local receipts + mocked `binance` adapter — no live merchant settlement tested)

## 🏭 Factory

> Roster tag: **operator-attested** (see [FACTORY.md](./FACTORY.md) for provenance rules).

| Seat | Role | Harness | Model |
|------|------|---------|-------|
| L.I.S.A. | strategist / coordinator / verifier | Hermes Agent (Telegram) | `xiaomi-mimo/mimo-v2.6-pro` |
| Claude | builder (dashboard, reviews) | `claude -p` CLI | Claude (Anthropic) |
| MiMo subagent | executor (spread engine, modules) | Hermes `delegate_task` | `xiaomi-mimo/mimo-v2.6-pro` |

**Factory design** (see [FACTORY.md](./FACTORY.md)):
- coordinator-led decomposition; all handoffs through the coordinator (operator-attested)
- parallel builder/executor tracks, serialized before shared-store work (operator-attested)
- every quantitative claim re-derived from a command or file before acceptance
- a wrong diagnosis retracted and corrected (the demo-trade root cause)

## 📡 API Reference

### Dashboard (REST, local only — `http://localhost:8098`)

| Method | Endpoint | Idempotent | Description |
|--------|----------|:----------:|-------------|
| `GET` | `/` | ✅ | mobile dashboard shell (HTML) |
| `GET` | `/frag/gaps` | ✅ | top spread-gap fragment |
| `GET` | `/frag/cross` | ✅ | cross-issuer leaders fragment |
| `GET` | `/frag/oraculo` | ✅ | weekend-oracle fragment |
| `GET` | `/frag/sistema` | ✅ | system/status fragment (suite status from `data/pytest_status.json`) |

### Agent layer (JSON-RPC 2.0, `POST /mcp`)

| Method | Params | Idempotent | Description |
|--------|--------|:----------:|-------------|
| `initialize` | — | ✅ | handshake — always free |
| `tools/list` | — | ✅ | enumerate tools — always free |
| `tools/call` `get_spread` | `token_symbol?` | ✅ | current top spreads (bps); `metric` reports which formula was used |
| `tools/call` `get_cross_issuer` | `underlying?` | ✅ | cheap-vs-expensive leaders across issuers of one underlying |
| `tools/call` `get_oracle_latest` | `underlying?` | ✅ | implied Monday open vs independent close, deviation flags |

Pay gate (`stockgap/b402_gate.py`, `config/mcp.env`): `MCP_PAY_MODE=free|b402` (default `free`), `B402_PROVIDER=local_receipt|binance`. The `binance` adapter (`/build/api/v2/b402/{supported,verify,settle}`) is **mock-tested only** — no live settlement (merchant credentials unavailable). Paid calls are idempotent (paid-call table keyed by tx hash / payload digest).

## 📁 Project Structure

```
stockgap-agent/
├── stockgap/          # signer, client, collector, spread, alerts, oracle, executor,
│   │                  #   balance, dashboard, mcp_server, b402_gate (+ templates/)
├── scripts/           # run_collector, alerts, summary, dashboard launcher, demo driver, fixtures fetchers
├── tests/             # 10 test modules, 199 tests
├── config/            # alerts.env, mcp.env, oracle.env (no live secrets inside)
├── fixtures/          # real OCResult<T> envelopes + MANIFEST.json
├── data/              # SQLite + logs (not committed, ~400 MB)
├── mandates/          # generic seat mandates (strategist, builder, executor)
├── FACTORY.md         # how the multi-agent factory worked
├── RECOVERY.md        # failure & recovery report
├── budgets.json       # per-seat budget record (honest: not tracked)
├── scheduler.json     # how work + crons were scheduled
└── DX-LOG.md          # developer-experience log (source of truth for history)
```

## 📜 Evidence

| Evidence | Description |
|----------|-------------|
| [FACTORY.md](./FACTORY.md) | seats, orchestration, verification strategy, design trade-offs, a wrong result caught |
| [RECOVERY.md](./RECOVERY.md) | failures + recovery actions incl. the retracted diagnosis and the operator-error double-launch (verifiable tx hashes) |
| [budgets.json](./budgets.json) | per-seat budgets (`not tracked` honestly) + measured on-chain gas ≈ 0.0000845 BNB |
| [scheduler.json](./scheduler.json) | seat→model map, coordination mode, the 3 StockGap cron entries verbatim |
| [mandates/](./mandates/) | generic per-seat charters (kept project-agnostic on purpose) |
| [DX-LOG.md](./DX-LOG.md) | full developer-experience log — source of truth for project history |

**Deployment honesty:** everything runs on `localhost:8098` on the build machine — **no public URL**. The three demo-trade broadcasts all reverted on-chain (2-second in-calldata route deadline — proven by `eth_call` replay + calldata decode for `0x9ca3…` and `0x7c33…`; **INFERRED** for `0xfdc9…` (identical pattern, not replayed). Min-return was not the cause of the three broadcast reverts — a real simulate failure `Min return not reached` occurred at 1% slippage on the first demo attempt. Full receipts and root-cause chain in [RECOVERY.md](./RECOVERY.md)); no live b402 `binance` settlement was tested.

## 📜 License

TBD — no `LICENSE` file has been committed yet.

<!-- ENTRY-URLS: TODO(alex): track + per-stage URLs -->

<details>
<summary><b>Signing spec & verified request details (Day-0, verified live)</b></summary>

```
preHash   = timestamp + METHOD(UPPERCASE) + requestPath + body
X-OC-SIGN = Base64(HMAC-SHA256(preHash, secretKey))
```

- `timestamp`: ISO 8601 with ms, UTC (`2026-05-11T10:08:57.715Z`) — NOT epoch ms. Same string in preHash and `X-OC-TIMESTAMP`.
- `requestPath`: full path **including `/build` prefix** and query raw-encoded exactly as sent (spaces `%20`, not `+`; order preserved). Missing `/build` → `40102`.
- `body`: exact raw wire bytes; `""` for GET. Never re-serialize after signing.
- Headers: `X-OC-APIKEY`, `X-OC-TIMESTAMP`, `X-OC-SIGN`; optional `X-OC-RECV-WINDOW` (default 5000, max 60000), `X-OC-NONCE`.
- Success = envelope `code == 0` even at HTTP 200 (business errors are HTTP 200 with `code != 0`). Error codes: 40001 params, 40101 key, 40102 signature, 40103 timestamp/replay, 40104 permissions, 42900 rate limit.
- Credentials come from outside the repo (`~/.hermes/secrets/`); the **stripped** secret variant is the one the gateway accepts (verified live).

**Verified endpoint params (discovered live — NOT in the docs dump):**

| Endpoint | Params |
| -------- | ------ |
| `GET /build/api/v1/dex/market/rwa/tokens` | none required |
| `GET /build/api/v1/dex/market/rwa/price` | `binanceChainId` (e.g. `56`), `tokenContractAddresses` (plural; contract address list) |
| `GET /build/api/v1/dex/market/rwa/underlying-market` | `binanceChainId`, `tokenContractAddress` (singular) |

Note the plural/singular asymmetry between `price` and `underlying-market`.

**Shares multiplier:** the live field name is **`tokenToShareRatio`** (string decimal): 1 token = `tokenToShareRatio` underlying shares. Docs call it `multiplier` / `sharesMultiplier` — wrong names. Normalization: `referencePrice = tokenPrice ÷ tokenToShareRatio`; the independent per-share close is `underlying-market.referencePrice × tokenToShareRatio` (the ×ratio step is the NFLX regression fix).

**Fixtures** (fetched 2026-10-02, all HTTP 200 / code 0 / success true): `fixtures/rwa_tokens.json` (488 tokens with `tokenToShareRatio`, prices, market-hours `statusInfo`), `fixtures/rwa_price.json`, `fixtures/rwa_underlying_market.json`. Envelope `timestamp` is epoch ms (unlike the ISO `X-OC-TIMESTAMP` header).

**Rate limits (verified):** 5 RPS per endpoint, 1200 req/min per IP+key. Batched `rwa/price` (≤50 addresses per request) with 0.25s stagger → a full 488-token tick in ~11 requests / ~3s. OCResult business errors arrive at HTTP 200 — always check `code == 0`.

</details>
