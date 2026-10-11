# StockGap — Judge Quickstart (90 seconds)

StockGap measures the gap between a tokenized stock's on-chain price and an **independent** reference close (488 RWA tokens, every 30 s) and sells the signal to agents over a pay-per-call MCP server on BNB Chain. This page is the fastest path to verify that every claim in the submission is real.

## 1. See it live (no setup)

| What | Where |
|---|---|
| Live dashboard (mobile-first) | <https://http--stockgap-app--6h2d2gps9lkr.code.run/> |
| Demo video (2:22) | <https://youtu.be/vQEqXFPtVyE> |
| Agent-facing MCP endpoint | `POST https://http--stockgap-app--6h2d2gps9lkr.code.run/mcp` |

```bash
# tools/list is always free
curl -s -X POST https://http--stockgap-app--6h2d2gps9lkr.code.run/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# weekend oracle: implied Monday open vs last independent close, with a deviation flag per underlying
curl -s -X POST https://http--stockgap-app--6h2d2gps9lkr.code.run/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_oracle_latest","arguments":{}}}'
```

Every MCP response carries an `X-ERC8004-Identity` header (`curl -i` to see it).

## 2. See the pay-per-call gate (402)

The public demo runs with `MCP_PAY_MODE=free` so anyone can call it. To see the x402 gate, run the dashboard locally in pay mode:

```bash
MCP_PAY_MODE=b402 python3 scripts/dashboard.py &
curl -si -X POST http://localhost:8098/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_oracle_latest","arguments":{}}}'
# -> HTTP 402 with "x402Version":1 payment instructions
```

A real mainnet payment through this gate (0.005 BNB, replay rejected) is documented in [`docs/evidence/prueba2/`](./evidence/prueba2/NOTES.md).

## 3. Verify on-chain (BSC mainnet, chain 56)

| What | Link |
|---|---|
| `PriceRegistry` contract (source: [`contracts/PriceRegistry.sol`](../contracts/PriceRegistry.sol)) | [`0x8d2cf4a5…b971`](https://bscscan.com/address/0x8d2cf4a56f754d5770985d95aa179ef4d5b9b971) |
| Deploy tx | [`0x2235be2b…afa1`](https://bscscan.com/tx/0x2235be2b3c516f89f4bdb84f199fb774182c424f024a10d1a35569536948afa1) |
| First oracle publish tx (`OraclePublish` event) | [`0x8055d62e…7cec`](https://bscscan.com/tx/0x8055d62e8667e5564e3ad448a0a5f769285a918ee7f1b05b3ec436ac1dc97cec) |
| First successful live trade (1 wei BNB→NVDAB, one attempt) | [`0xd8ef9e75…a48f`](https://bscscan.com/tx/0xd8ef9e75467ddb27ab97076e3b112fbe4a552488b81dddd09a81bb4cd490a48f) |
| Seller agent (BNB Agent Studio): ERC-8004 identity **#365285**, real paid ERC-8183 job **56937** | [stockgap-seller](https://github.com/Alejdro83/stockgap-seller) · [job evidence](https://github.com/Alejdro83/stockgap-seller/tree/main/docs/evidence/mainnet-job-56937) |

## 4. Binance Web3 API: what was actually called

RWA Data API (`rwa/tokens`, `rwa/price`, `rwa/underlying-market` — the core data layer), Trading API (aggregator quote/swap), Transaction API (simulate/broadcast), Wallet API (balances), and B402 Payments. B402 `supported` was verified live against Binance after Developer Portal approval: [`docs/evidence/b402-supported-live.json`](./evidence/b402-supported-live.json) (reproduce with `scripts/b402_supported_probe.py`).

## 5. Numbers (measured 2026-10-10 on the live collector database)

- 12.1M `spread_ticks` rows; 488 RWA tokens (442 Ondo + 46 BStock) + 40 cross-issuer pairs per 30 s tick.
- `|spread_bps_real|` p25 / p50 / p75 = **4.7 / 12.3 / 29.0 bps** (filter |spread| > 0.5 bps, n ≈ 4.76M), measured against an independent per-share close, not the derived same-endpoint pair (which is structurally ~0).
- Tests: `pip install -r requirements.txt eth-account && python3 -m pytest -q` → **417 passed, 2 skipped** (~2 min).

## 6. Honest limits (read these before scoring)

- Three early demo-trade broadcasts reverted on-chain (an undocumented ~2 s route deadline in the swap calldata); root cause and receipts in [`RECOVERY.md`](../RECOVERY.md). A pre-broadcast deadline gate now prevents it.
- The on-chain oracle writer is real on mainnet but toggled off by default (`ORACLE_PUBLISH=0`).
- Binance's own B402 `verify`/`settle` were **not** exercised live, and the `binance` adapter in `stockgap/b402_gate.py` still targets x402 v1 shapes while Binance's API is x402 V2. The live pay path is our `local_receipt` on-chain verification.
- The `X-ERC8004-Identity` header on the MCP server still shows a placeholder registry id; the real mainnet identity (#365285) belongs to the seller agent.
- The dashboard runs on Northflank fed by a local collector plus a 5-minute shipper, because Binance Web3 blocks Northflank egress (`40304`).
- No xStocks appear in the Binance RWA Data API universe (Ondo and BStock only), so none are claimed.

## 7. Where the evidence lives

[`DX-LOG.md`](../DX-LOG.md) (dated developer-experience notes, including corrections) · [`RECOVERY.md`](../RECOVERY.md) (failures and recovery) · [`FACTORY.md`](../FACTORY.md) (how it was built) · [`StockGap/README.md`](../StockGap/README.md) (architecture deep dive) · [`docs/VIDEO_SCRIPT.md`](./VIDEO_SCRIPT.md)
