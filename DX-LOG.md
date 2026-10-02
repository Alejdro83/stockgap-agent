# DX-LOG — Developer experience notes

## Cross-endpoint price-field caveat (2026-10-02, verified live)

Three endpoints expose "price"-like fields for the same token and they are NOT
interchangeable. Fresh live check (same 0.3s window, ACNon, BSC 56):

| source | field | value |
|---|---|---|
| `rwa/tokens` | `tokenPrice` | 226.2735450023987328618691192573 |
| `rwa/tokens` | `referencePrice` | 219.50130637419753 |
| `rwa/price` | `tokenPrice` | 219.501306374197539972 |
| `rwa/price` | `referencePrice` | 212.931757 |
| `rwa/underlying-market` | `marketData.referencePrice` | 206.611742 |
| aggregator quote | `toToken.tokenUnitPrice` | 219.501306374197539972 |

**Conclusion: the discrepancy PERSISTS within a 0.3s fetch window — it is
structural, not different timestamps.**

Findings:

1. Within EITHER `rwa/tokens` or `rwa/price`, `referencePrice =
   tokenPrice / tokenToShareRatio` holds exactly (488/488 in `rwa/tokens`,
   confirmed independently). The two fields are mutually derived — a spread
   computed from one endpoint's own pair is ~0 by construction (residuals are
   field-rounding only).
2. `rwa/price.tokenPrice` is digit-for-digit equal to `rwa/tokens.referencePrice`
   for the same token (ACNon: 219.5013063741975...). Cross-endpoint, one ratio
   division apart. Do NOT mix these fields blindly.
3. `rwa/underlying-market.marketData.referencePrice` (206.61 for ACN) is a third,
   independent feed (traditional-market quote; 2-decimal and 6-decimal
   precisions observed). BStock tokens can return `referencePrice: null` there
   (NVDAB verified) — handle as missing, not zero.
4. The aggregator quote's `toToken.tokenUnitPrice` equals `rwa/price.tokenPrice`
   exactly — the executable price matches the `rwa/price` frame.

## Ratio distribution (never assume 1 token = 1 share)

`tokenToShareRatio` (string decimal): 228/488 tokens are exactly 1, but
NFLXon/PPLTon = 10, PALLon = 5, ENLVon = 0.066667. The engine requires a
positive ratio and raises `ValueError` on missing/zero — it never defaults to 1.
Formula `referencePrice = tokenPrice / tokenToShareRatio` confirmed 488/488 in
`rwa/tokens`.

## Provenance rule (applied in collector)

One consistent price source per metric: every price field in `spread_ticks` /
`cross_issuer_ticks` comes from `/api/v1/dex/market/rwa/price`; ratio is a
static token property from `/api/v1/dex/market/rwa/tokens` (universe cache).
Every stored row carries `source_endpoint` + `fetched_at` (plus the API's own
`tokenPriceUpdatedAt`) so no series is ever ambiguous about its frame.

## Rate limits (verified)

5 RPS per endpoint, 1200 req/min per IP+key. Batched `rwa/price` (≤50
`tokenContractAddresses` CSV per request) with 0.25s stagger keeps a full
488-token tick at ~11 requests / ~3s. OCResult business errors arrive at HTTP
200 — always check `code == 0`.


## Telegram alerts + summary CLI (2026-10-02)

- `scripts/alerts.py` (runner) + `stockgap/alerts.py` (lib): scans only NEW rows
  (watermarks `last_seen_spread_rowid` / `last_seen_cross_rowid` in the
  `alert_state` table of `data/spread_ticks.db`), detects threshold CROSSINGS
  (prev vs current `spread_bps`, so a tick series stuck above threshold alerts
  once, not every scan), rate-limits to `ALERT_RATE_LIMIT_MIN` (default 30) per
  token, optional digest mode (`ALERT_DIGEST_MODE`, `ALERT_DIGEST_INTERVAL_MIN`,
  `--flush-digest`).
- Thresholds/toggles all via env or `config/alerts.env` (env wins):
  `SPREAD_THRESHOLD_BPS` (150), `CROSS_ISSUER_THRESHOLD_BPS` (100),
  `ALERT_SPREAD_ENABLED`, `ALERT_CROSS_ENABLED`, `ALERTS_ENABLED`,
  `TELEGRAM_ALERTS`, `FILE_LOG_ALERTS`, `TELEGRAM_CHAT_ID`, `STOCKGAP_DB`.
- Alert text (Spanish, mobile): ticker, plataforma, spread %, precio token vs
  referencia (con nota `tokenToShareRatio` cuando ratio != 1), estado de
  mercado, y accionabilidad: xStock ejecutable 24/7; ondo/bstock solo horario
  EE.UU. (la API rechaza fuera de horario con 40367/40369).
- Transport: pluggable — `FileLogTransport` (data/alerts.log, always on) +
  `TelegramTransport` (python-requests → api.telegram.org sendMessage) only when
  `TELEGRAM_BOT_TOKEN` is set; token is redacted from all logs. **No bot token
  was found** in `.openclaw/workspace/agent-bazaar` (no .env/.env.local/Northflank
  files there at all), so delivery currently runs file-log-only; drop the token
  into `config/alerts.env` (`TELEGRAM_BOT_TOKEN=`) and Telegram goes live with
  zero code changes (chat 6438086899 already the default `TELEGRAM_CHAT_ID`).
- `scripts/summary.py`: ventana `--hours/--minutes/--since`, texto o `--json` —
  min/mean/max bps por token, top-5 gaps, líderes cross-issuer, conteo de ticks.
- Wiring: crontab `*/2 * * * *` one-shot scan (`scripts/alerts.py`), log in
  `data/alerts-cron.log`; standalone alternative `scripts/alerts.py --loop`.
  Watchdog pair: `scripts/ensure_collector.sh` (*/5) keeps collector alive;
  alerts cron is stateless one-shot so a missed run just delays the alert.
- Tests: `tests/test_alerts.py` — crossings, rate limiting, ratio != 1
  formatting, actionability (platform + market hours). 82 passed total.


## Executor pipeline + first demo trade (2026-10-02)

- **No xStocks exist in the live universe.** `fixtures/rwa_tokens.json` and the
  live `GET /api/v1/dex/market/rwa/tokens` both contain exactly two platforms
  (`ondo`: 442, `bstock`: 46 of 488 tokens on chain 56); `GET /rwa/platforms`
  lists only ondo + bstock. `GET /api/v1/dex/market/rwa/search` with keywords
  `AAPLx` / `NVDAx` / `TSLAx` returns `code=40382` (not found). The xStock
  (type=2) family documented in the API docs is NOT currently listed. Demo
  trade therefore targets the SWAP-routable equity token **NVDAB** (BStock,
  `0x02fca66c1d1afb4e2a7884261eb00f63598a7436`, vendor `LiquidMesh`,
  `executionMode=SWAP`) — the same execution mode xStocks would use.
- **Wallet API pagination envelope**: `GET /balance/all-token-balances-by-address`
  returns `data = [{page, pageSize, tokenAssets: [...]}]`, not a flat token
  list. Empty wallet → `tokenAssets: []` with `code=0` (render as "no token
  holdings", not "?" rows).
- **Native BNB works as `fromTokenAddress`**: `0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE`
  quotes fine (BNB `tokenUnitPrice` is echoed in `fromToken`).
- **`/swap` response `tx` carries more than the signable fields**:
  `minReceiveAmount`, `slippagePercent`, `signatureData`, `computeUnitPrice`,
  `computeUnitLimit` ride alongside `from/to/data/value/gas/gasPrice/
  maxPriorityFeePerGas`. Note `maxPriorityFeePerGas` was returned EQUAL to
  `gasPrice` (50030000 wei both) — don't assume it is a small priority tip.
- **`/pre-transaction/simulate` re-executes calldata against live pool state**:
  the first demo attempt (`autoSlippage=true`, `slippagePercent=1.00`,
  quoteId `dd61a784fd2e41d09d3f878a42fe52b1`) simulated `status=FAILED`,
  `failReason="execution reverted: Min return not reached"` even though the
  quote's `priceImpactPercent` was ~0.0003%. The identical trade rebuilt with
  `slippagePercent=5` (quoteId `3d3e7080e43845be900d8d568e1b322c`) simulated
  `status=SUCCESS` with concrete `balanceChanges` (-0.003 BNB native,
  +0.009924806992173916 NVDAB). Use slippage >= 5 for simulate-verified
  execution on this route.
- **Simulate request shape** (undocumented in the API map): `POST
  /api/v1/dex/pre-transaction/simulate` body
  `{"binanceChainId": "56", "evmTx": {"from","to","data","value","gasLimit","gasPrice"}}`
  with `gasLimit`/`gasPrice` numeric strings taken from `/swap` `tx.gas` /
  `tx.gasPrice`. Response `data`: `{status, failReason, balanceChanges,
  allowanceChanges}`.
- **Broadcast request shape** (for later, key-gated):
  `{"binanceChainId", "address", "signedTransaction", "enableMevProtection"}`.
  RFQ submit: `{"userSignature", "vendor", "quoteId" (= rfq.orderId, NOT the
  /quote route id), "requestId" (UUID, persist before first send — 42901
  ORDER_SUBMIT_IN_PROGRESS means reuse the SAME UUID)}.` 40462 SWAP_QUOTE_MISMATCH
  fires when /swap params drift from the cached /quote params; 40401 TTL is ~30 s.
- **`/swap` requires `userWalletAddress` even in SWAP mode** — omitting it
  (it is only documented as mandatory for RFQ routes) returns `40001 Parameter
  [userWalletAddress] is required`. Always send the signing wallet on both
  `/quote` and `/swap`.

---

## Real demo trade + `_signable_tx` fix (2026-10-02)

- **`_signable_tx` sanitization (root cause: /swap `data.tx` is not signable)**:
  eth_account needs `{nonce, gasPrice, gas, to, value, data, chainId}` (ints,
  checksum `to`); the API returns all-DECIMAL strings + `from` + non-EVM fields
  (`minReceiveAmount`, `slippagePercent`, `signatureData`, `computeUnitPrice`,
  `computeUnitLimit`, `maxPriorityFeePerGas`) and NO `nonce`/`chainId`.
  Sanitizer: drops `from` + non-EVM fields (never mixes `maxFeePerGas`/
  `maxPriorityFeePerGas` with `gasPrice` — legacy type 0 is correct for BSC),
  `to_checksum_address` on `to`, `int(s,16)` for `0x`-strings else `int(s,10)`
  (NOT `int(s,0)` — leading-zero decimals parse as octal), injects nonce from
  `eth_getTransactionCount(addr,'pending')`, ALWAYS sets `chainId=56` (without
  it eth_account signs pre-EIP-155 v=27/28 — cross-chain replayable and BSC
  rejects), `gasPrice = max(api, live eth_gasPrice)` (stale-quote underpriced),
  `gas*12//10` headroom, and REFUSES to sign anything not addressed to router
  `0xB44446b0c8E56988c34f7Ff73Ae904982b5FdDA5` or when `eth_chainId != 0x38`.
  Regression tests pin: no `from`, chainId 56, nonce injection, decimal/hex/
  gasLimit variants, checksumming, router/chain refusals, `signed.v in (147,
  148)` (EIP-155 binding). `execute_swap` now also ABORTS before sign/broadcast
  when the fresh simulate is not SUCCESS. Full suite: 98 passed.
- **Approved demo trade (exactly 1 broadcast attempt, no re-send)**: buy NVDAB
  (`0x02fca66c1d1afb4e2a7884261eb00f63598a7436`) with 0.003 BNB, slippage 5,
  SWAP/LiquidMesh, quoteId `200a4042cad24154ac4192e6531f03fa`. Fresh simulate
  = SUCCESS (+9927196883554222 wei NVDAB, -0.003 BNB). Signed locally, broadcast
  via `/pre-transaction/broadcast-transaction`: orderId
  `902adb1bd6a831af6700f8ef8000b3f2b83e629a`, **txHash
  `0xfdc9806cd4791e10ca806075b2dfb84d040d414d55cab37ea08f943cf8f32021`**.
> ⚠️ SUPERSEDED — see section "Quote-and-swap Flash API: real shapes + TRUE root cause of the NVDAB reverts" below: the true root cause is a 2s in-calldata route deadline ("Route: expired"), NOT min-return. Original text kept for the record.
- **On-chain receipt (eth_getTransactionReceipt via bsc-dataseed):
  `status 0x0` — REVERTED** in block 125205351, gasUsed 519570,
  effectiveGasPrice 53184394 wei (fee 0.00002763 BNB). Min-return race:
  simulate passed against pool state, then the route moved before inclusion
  (the slippage-1.00 attempt had already shown "Min return not reached"
  sensitivity on this route). The 0.003 BNB value was returned by the revert;
  only gas was spent. NOT re-sent per the one-broadcast rule. Post-trade
  balances: 0.011643 BNB native, 0 NVDAB (`stockgap/balance.py`: "no token
  holdings").
- **Lesson**: simulate SUCCESS is point-in-time, not a fill guarantee — a real
  retry needs a NEW approval (new quote, new nonce, new broadcast attempt).

## Weekend oracle quirks (2026-10-02, verified live)

- **`rwa/price.referencePrice` is useless as a "last close"**: it is derived from
  the token (`tokenPrice / tokenToShareRatio`), so deviation vs it is 0 by
  construction (488/488 rows, residual ~1e-20 bps). The oracle therefore uses
  `rwa/underlying-market.marketData.referencePrice` as the independent close.
- **`underlying-market.referencePrice` is divided by `tokenToShareRatio` once
  more than `rwa/price.referencePrice`**: raw deviation was exactly
  `(ratio-1)*10000` bps (NFLXon ratio 10 → +90000 bps, CVNAon ratio 5 → +40000).
  Per-share close = `referencePrice * ratio`; after that median |dev| = 0.0 bps
  across ratios 1 / 0.5 / 4 / 5 / 10 / 1.03 (442 Ondo tokens).
- **That feed tracks the token during the Ondo `overnight` session** (median
  |dev| ≈ 0 bps, even drifts between two calls seconds apart: ACN 206.1459 →
  206.2410), i.e. it is a live overnight quote, not a frozen Friday close. UNVERIFIED
  until the weekend whether it freezes while the underlying is closed — check
  `oracle_reference.reference_close` over Saturday; if it drifts with the token the
  weekend deviation carries no information and an external Friday-close feed
  (`--real-opens`-style) is needed.
- **BStock tokens return `marketData.referencePrice: null`** from
  `underlying-market` (AAOIB, LITEB, …); the oracle falls back to the same
  underlying's Ondo issuer, else records NULL deviation (6/488 today).
- **`underlying-market` is one token per request** (`tokenContractAddress`, no
  batch): a full refresh of ~442 underlyings takes ~4 min at ~0.5 s/call + 0.22 s
  stagger. Cached in SQLite (`oracle_reference`, TTL 30 min, ≤150 calls/tick).
- **No open/OHLC field anywhere in `underlying-market`** (only referencePrice,
  52w hi/lo, volume, market cap…). The real Monday open must come from an
  external feed (`oracle_score.py --real-opens file.csv|json`); `--fetch-opens`
  is a best-effort referencePrice proxy read while `marketStatus` is regular.

## Quote-and-swap Flash API: real shapes + TRUE root cause of the NVDAB reverts (2026-10-02, live)

### Real request shape (probed POST + GET; the task brief's POST method is wrong)
- `POST /api/v1/dex/aggregator/quote-and-swap` → HTTP 200 but NON-OC envelope:
  `{"status":"ERROR","type":"VALID","code":"000002","errorData":"Request method 'POST' not supported","data":null,"subData":null,"params":null}`.
  Note `code` is a STRING here (vs `0` int in OCResult) — handle both types.
  **The endpoint is GET-only** (matches llms-full).
- `GET /api/v1/dex/aggregator/quote-and-swap` works with the full `/quote` params plus
  `slippagePercent` + `vendor=LiquidMesh` (`executionMode=SWAP` sent as extra param is
  accepted/ignored). Response: OCResult with
  `data = {executionMode:"SWAP", rfq:null, routerResult:{...NO quoteId...}, tx:{...same
  shape as /swap's data.tx}}`. `minReceiveAmount` is baked as EXACTLY 0.92000000 × the
  same-response `routerResult.toTokenAmount` for slippagePercent=8 (verified on both live
  txs). No `quoteId` anywhere → the 30s quoteId TTL genuinely cannot bite.

### Root cause of ALL THREE NVDAB reverts is NOT min-return: it is a 2-second route deadline
Driver `scripts/demo_trade_flash.py` (`--probe`, `--live --method GET`) broadcast via the
executor's tested path. **INCIDENT: the live driver was accidentally launched twice in one
harness batch (operator error) — two broadcasts of the same approved 0.003 BNB trade**:

| tx | nonce | block | gasUsed | status | effectiveGasPrice |
|---|---|---|---|---|---|
| `0x9ca395c7db3b277fe3317c45be4ddc7bf1e09755241dff32911a88c42c7c7dfc` | 2 | 125207506 | 519968 | 0x0 | 53750000 |
| `0x7c3379a18636c68c3bb0dc23dbcee6fefcff916baf74c1302ae4a88c4817446f` | 3 | 125207520 | 519967 | 0x0 | 55679437 |

Both value 0.003 BNB returned exactly; only gas burned: 27,948,280,000,000 +
28,951,469,818,579 = **56,899,749,818,579 wei (~0.0000569 BNB)** total (measured balance
delta matches receipts to the wei). Wallet after: 11,585,857,234,590,841 wei =
0.011585857234590841 BNB, NVDAB 0. `/aggregator/history?txHash=` says `status:"failed"`,
`errorMsg:null` — useless for failure reasons; don't rely on it.

**Decoded evidence chain:**
1. `eth_call` replay of the exact tx calldata reverts with
   `execution reverted: Route: expired` (`Error(string)`, selector `0x08c379a0`).
2. The calldata embeds uint64 BE timestamps at byte offsets **1696 = build_time** and
   **3424/3968 = build_time + 2s** (runA: 1790905866 → 1790905868; runB: 1790905873 →
   1790905875). The route payload's `valid-until` is **build + 2 SECONDS**.
3. Inclusion timestamps (`/aggregator/history` `txTime`): runA block @1790905868.8 =
   **+0.8s past deadline**; runB block @1790905875.1 = **+0.1s past deadline**. Both just
   missed it — and 2026-10-01's `0xfdc9806c…` (gasUsed 519570, same execution depth,
   auto-slippage) fits the identical pattern.
   > 📅 DATE DISCREPANCY: this tx is dated 2026-10-02 in the earlier section. Unresolved in the record — block 125205351 is authoritative (verify via block time on a BSC explorer).
4. Consistency: slippage width (1 / 5 / 8 %) changed nothing; simulate returned SUCCESS at
   T+0.5s (deadline still alive) every time; identical gasUsed across two different routes
   = same deterministic `require` at the same depth, not price drift.

**Conclusion:** the "quoteId-TTL min-return race" diagnosis was wrong. The binding
constraint is the **2s in-calldata route deadline**: with `POST /broadcast-transaction` +
public BSC relay, quote→broadcast alone takes ~2s and inclusion lands 3–6s after build —
structurally past `valid-until`. Min-return was never exercised (8% band unreachable at
this size). Fixes for a future attempt (none taken — no further sends): get a route with a
longer deadline; or land inclusion inside 2s (private/low-latency relay); or teach the
executor to detect the deadline word in calldata and abort cheaply BEFORE broadcasting when
`build_age + expected_inclusion > deadline`. Revert-reason extraction recipe: public BSC
endpoints reject `debug_traceTransaction`/`trace_transaction` ("missing trie node" /
unavailable) — use `eth_call` replay of the tx fields instead.

pytest after the run: 125 passed.

## 2026-10-02 — Dashboard móvil (puerto 8098)

FastAPI + Jinja2 + HTMX + Tailwind CDN, SQLite solo lectura (`immutable=1`). Código en
`stockgap/dashboard.py` + `stockgap/templates/`, lanzador `scripts/dashboard.py`, tests en
`tests/test_dashboard.py`. El conteo de pytest que muestra la pestaña Sistema se lee de
`data/pytest_status.json` (actualizar tras correr la suite).

Reinicio (una línea):
`pkill -f scripts/dashboard.py; cd /home/lab-master/stockgap-agent && nohup python3 scripts/dashboard.py >> data/dashboard.log 2>&1 &`

## 2026-10-02 — Capa agente-a-agente de pago por llamada (MCP + b402)

- `stockgap/mcp_server.py`: MCP mínimo sobre HTTP (JSON-RPC 2.0) en `POST /mcp`, montado en la app del
  dashboard (`app.include_router`, puerto 8098; sin puerto extra). Herramientas: `get_spread`,
  `get_cross_issuer`, `get_oracle_latest` (SQLite solo lectura, `mode=ro`). `get_spread` usa
  `spread_bps_real` si la columna existe y lo indica en `metric`. `initialize`/`tools/list` siempre gratis.
- `stockgap/b402_gate.py` + `config/mcp.env`: `MCP_PAY_MODE=free|b402` (defecto free) y
  `B402_PROVIDER=local_receipt|binance`. local_receipt: el comprador paga on-chain y manda el tx hash en
  `X-Payment`; se verifica el recibo por RPC (status 0x1, to==comercio, valor>=precio, bloque reciente).
  binance: adaptador `/build/api/v2/b402/{supported,verify,settle}` (solo v2); verify→claim→settle, y si
  settle falla se libera el claim. NO probado en vivo (faltan credenciales de comercio) — solo tests con mock;
  el esquema de `paymentPayload`/`paymentRequirements` es una suposición x402 estándar.
- Idempotencia: tabla `paid_calls` (PK = tx hash en minúsculas / digest sha256 del payload b402) en la misma DB.
  Única escritura del módulo; solo ocurre tras un pago verificado.
- Header `X-ERC8004-Identity` en toda respuesta; `IdentityStub.record_call()` es el hook (no-op) para la
  futura escritura de reputación. Sin llamadas on-chain, sin firmas, sin broadcast.
- Token ERC-20 soportado en `verify_receipt` (MCP_ASSET=<dirección>) pero sin prueba en cadena real.
- Cuidado: `pkill -f scripts/dashboard.py` desde un comando que contenga esa cadena mata el propio shell.

---

## 2026-10-02T02:45Z — MÉTRICA REAL de spread: spread_bps_real vs cierre INDEPENDIENTE (fix del bug estructural ~0 bps)

- **Bug (488/488 tokens, 122 000 filas):** `rwa/price.referencePrice` es DERIVADA (= tokenPrice/tokenToShareRatio) ⇒ `spread_bps` histórico estructuralmente ≈ 0.0 bps para todos.
- **Métrica real** (`stockgap/spread.py::real_spread`): `implied = tokenPrice/tokenToShareRatio`; `close_independiente = rwa/underlying-market.referencePrice × tokenToShareRatio` (normalización ×ratio en `stockgap/spread.py::independent_share_close`, ÚNICA copia — `stockgap/oracle.py::_reference_closes` ahora la usa: refactor DRY); `spread_bps_real = (implied − close)/close × 10000`. Regresión NFLX (+90000 bps sin ×ratio) fijada en tests.
- **Caché compartida `oracle_reference`** (oracle_reference.reference_close ya normalizado por acción): reutilizada vía `stockgap/oracle.py::load_reference_closes` (max-age 1800 s, env `REFERENCE_MAX_AGE_S`). El collector SOLO LEE la caché (rwa/underlying-market cuesta 1 request/token); el oracle (cron */5) la refresca. Caché fría/entrada vana ⇒ fallback etiquetado `reference_kind='derived'`.
- **Columnas nuevas (solo aditivas — contrato dashboard intacto):** `spread_bps_real`, `reference_close_independent`, `reference_source`, `reference_kind`. Migración `_migrate` etiqueta las filas históricas como `reference_kind='derived'` / `reference_source='rwa/price (derived)'` / `spread_bps_real=NULL` (backfill imposible: el cierre independiente nunca se fetchó — honestidad > relleno).
- **Alertas:** flag `ALERT_METRIC=real|derived` (default `real`, documentado en config/alerts.env). `find_crossings` mide sobre `spread_bps_real`; filas sin métrica real (históricas/caché fría) se saltan — nunca alerta-basura sobre la derivada. Cross-issuer se mantiene en su propio `spread_bps` (issuer-vs-issuer, no depende de la referencia). `format_spread_alert` muestra la métrica que dispara + línea "Ref independiente (cierre subyacente)" o marca "DERIVADA del propio token, spread NO informativo". Verificado end-to-end con dry-run (sin envíos, sin broadcasts).
- **Verificación en vivo (vi 02:37–02:40 UTC, mercado abierto):** collector reiniciado con el mecanismo documentado `scripts/ensure_collector.sh` (pidfile data/collector.pid; crontab intacto). Ticks 251+: 688 filas nuevas con `spread_bps_real` poblado, 390 con |real| > 0.5 bps — con `spread_bps` derivado = 0.00 en las mismas filas. Gaps reales presentes: ABNBon +62.59 bps (ref_indep 161.916 vs token 162.929), VRTVon −353.7, ATGEon −186.7. El resto del universo queda 'derived' mientras el oracle recalienta la caché (150 refresh/tick).
- **Tests:** nuevo `tests/test_spread_real.py` (24 casos: normalización ×ratio 10/1/0.066667, regresión NFLX, derivado-vs-independiente, fallback etiquetado, migración de filas históricas, loader de caché (fresh/stale/sin tabla), switch de métrica de alertas + formato honesto). Suite: 125 → 199 (el delta incluye tests de agentes concurrentes: test_dashboard/test_mcp/test_b402). data/pytest_status.json actualizado (199 passed).
