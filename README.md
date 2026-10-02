# StockGap — Day 0 vertical slice

Binance Web3 API (OC gateway) HMAC signer + credential verification + real
response fixtures for the tokenized-stocks spread monitor.

## Layout

- `stockgap/signer.py` — HMAC-SHA256 request signer (see spec below)
- `stockgap/client.py` — stdlib `http.client` client (request path bytes are
  exactly what was signed — no re-encoding between signature and wire)
- `tests/test_signer.py` — 15 unit tests, incl. an OpenSSL cross-check vector
- `scripts/fetch_fixtures.py` — live credential verification, secret-variant
  probe, param discovery, fixture capture
- `scripts/fetch_underlying_market.py` — `rwa/underlying-market` fixture capture
- `fixtures/` — real `OCResult<T>` envelopes + `MANIFEST.json` + `ops_log.json`

## Signing spec (verified live)

```
preHash   = timestamp + METHOD(UPPERCASE) + requestPath + body
X-OC-SIGN = Base64(HMAC-SHA256(preHash, secretKey))
```

- `timestamp`: ISO 8601 with ms, UTC (`2026-05-11T10:08:57.715Z`) — NOT epoch ms.
  Same string in preHash and `X-OC-TIMESTAMP`.
- `requestPath`: full path **including `/build` prefix** and query raw-encoded
  exactly as sent (spaces `%20`, not `+`; order preserved). Missing `/build` →
  `40102`.
- `body`: exact raw wire bytes; `""` for GET. Never re-serialize after signing.
- Headers: `X-OC-APIKEY`, `X-OC-TIMESTAMP`, `X-OC-SIGN`; optional
  `X-OC-RECV-WINDOW` (default 5000, max 60000), `X-OC-NONCE`.
- Success = envelope `code == 0` even at HTTP 200 (business errors are HTTP 200
  with `code != 0`). Error codes: 40001 params, 40101 key, 40102 signature,
  40103 timestamp/replay, 40104 permissions, 42900 rate limit.

Credentials come from `~/.hermes/secrets/binance_web3_api_key` and
`~/.hermes/secrets/binance_web3_secret_key` (secret file has a trailing newline;
the **stripped** variant is the one the gateway accepts — verified live).

## Verified endpoint params (discovered live — NOT in the docs dump)

| Endpoint | Params |
| -------- | ------ |
| `GET /build/api/v1/dex/market/rwa/tokens` | none required |
| `GET /build/api/v1/dex/market/rwa/price` | `binanceChainId` (e.g. `56`), `tokenContractAddresses` (plural; contract address list) |
| `GET /build/api/v1/dex/market/rwa/underlying-market` | `binanceChainId`, `tokenContractAddress` (singular) |

Note the plural/singular asymmetry between `price` and `underlying-market`.

## Shares multiplier

The live field name is **`tokenToShareRatio`** (string decimal, e.g. `"1"`,
`"5"`, `"1.030852839739623902"`): 1 token = `tokenToShareRatio` underlying
shares. Docs call it `multiplier` / `sharesMultiplier` — wrong names. Spread
normalization: `referencePrice = tokenPrice ÷ tokenToShareRatio` (verified:
ACNon 219.9736… ÷ 1.03085… ≈ 213.39 = `referencePrice`).

## Fixtures (fetched 2026-10-02, all HTTP 200 / code 0 / success true)

- `fixtures/rwa_tokens.json` — 488 tokens (Ondo/xStock/BStock), envelope
  `{code, msg, data, timestamp, success}`; per-token
  `tokenToShareRatio`, `referencePrice`, `tokenPrice`, `statusInfo` (market
  hours: `openState`, `marketStatus`, `reasonCode`, `nextOpenTime`,
  `nextCloseTime`).
- `fixtures/rwa_price.json` — on-chain `tokenPrice` + `referencePrice` +
  `tokenPriceUpdatedAt` (epoch ms).
- `fixtures/rwa_underlying_market.json` — underlying `marketData`
  (referencePrice, 52W high/low, volumeShares24H, totalShares, marketCap, …).

Envelope `timestamp` is epoch ms (unlike the ISO `X-OC-TIMESTAMP` request header).

## Run

```bash
python3 -m unittest discover -s tests -t .   # 15 tests
python3 scripts/fetch_fixtures.py            # live probe + tokens + price
python3 scripts/fetch_underlying_market.py   # live underlying-market
```

Rate limits respected: 5 RPS/endpoint (0.25 s sleep between calls); budgets are
1200/60s per IP and per key.
