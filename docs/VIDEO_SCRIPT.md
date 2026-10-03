# StockGap — Video Pitch Script (≤4:00)

> Official rule: **demo video of four minutes or less**. Target: **3:45** (hard max 4:00). Language: English narration. ES = Spanish speaker notes for the operator.
> Shot list = what the screen recording shows. Record with `scripts/dashboard.py` + collector already running so the demo is live.

---

## Shot list (record in this order, then edit to the timings below)

| # | Shot | What to capture |
|---|------|-----------------|
| S1 | Title card | Repo README hero (tagline) — 3 s |
| S2 | The gap, visually | Dashboard table sorted by `spread_bps_real` desc; cursor on a big positive and a big negative spread |
| S3 | Live tick | Terminal: `python3 scripts/run_collector.py` — one 30 s tick landing (488 rows; show ~10 s) |
| S4 | Dashboard tour | `localhost:8098` on a phone-sized viewport: token row → provenance fields (`reference_kind`, `source_endpoint`) |
| S5 | MCP as an agent | Terminal: `tools/list` curl → `tools/call get_oracle_latest` curl → JSON response with `deviation_bps` |
| S6 | The pay gate | Same call with `MCP_PAY_MODE=b402` → HTTP 402 + `x402Version:1` receipt instructions |
| S7 | Tests | `python3 -m pytest -q` → `248 passed` |
| S8 | Numbers | Dashboard or `scripts/summary.py` output: ≈1M rows, p25/50/75 = 5.1/13.5/32.9 bps |
| S9 | Rigor evidence | `RECOVERY.md` scroll: the three reverted tx hashes + `eth_call` replay section, then BscScan of the successful trade tx (honesty shot) |
| S10 | BNB + ERC-8004 | MCP response headers showing `X-ERC8004-Identity: erc8004:bsc-mainnet:…` + BscScan of the `PriceRegistry` contract (registry id still a placeholder — swap in the real id if it lands before recording) |
| S11 | Close | README links row (repo, DEPLOYED_URL) |

---

## 0:00–0:35 — The problem

**EN:** "Tokenized stocks trade on-chain, the underlying share trades on a traditional exchange — and almost nobody measures the gap honestly. If your reference price is computed from the token price itself, the spread is structurally zero: a fake peg. StockGap starts from the uncomfortable question — what is the tokenized stock actually worth versus the real share?"

**ES:** *Los valores tokenizados cotizan on-chain y la acción subyacente en el mercado tradicional — y casi nadie mide la brecha de forma honesta. Si el precio de referencia se calcula desde el propio precio del token, el spread es cero estructural: un peg falso. StockGap parte de la pregunta incómoda: ¿cuánto vale realmente el tokenizado frente a la acción real?*

Shots: S1 → S2.

---

## 0:35–1:50 — Live demo

**EN:** "This is live, not a mockup. The collector polls 488 tokenized stocks — 442 Ondo and 46 BStock — every 30 seconds, rate-limit aware, and every row carries full provenance: source endpoint, fetch time, and whether the reference close is independent or derived. Here's a tick landing. In the dashboard — mobile-first, FastAPI and HTMX, SQLite opened strictly read-only — we sort by the real spread: the gap between the token's implied share price and the independent per-share close, normalized by tokenToShareRatio, because one token is often not one share. Now, as an agent would use it: JSON-RPC to the MCP endpoint — tools/list is always free, get_oracle_latest returns the weekend-Monday-open oracle with a deviation flag per underlying. And here is the business model switch: with the b402 gate on, the same call returns HTTP 402 with x402Version-1 receipt instructions. Pay per call, on BNB Chain."

**ES:** *Esto está en vivo, no es un mockup. El colector sondea 488 valores tokenizados cada 30 segundos y cada fila guarda procedencia completa. En el dashboard — mobile-first, SQLite estrictamente de solo lectura — ordenamos por el spread real: la brecha entre el precio implícito y el cierre independiente por acción, normalizado por tokenToShareRatio, porque un token casi nunca es una acción. Como lo usaría un agente: JSON-RPC al endpoint MCP — tools/list siempre es gratis — y con el gate b402 activo la misma llamada devuelve HTTP 402 con instrucciones de recibo x402Version-1. Pago por llamada, en BNB Chain.*

Shots: S3 → S4 → S5 → S6. (Keep each curl unedited; the JSON is the credibility.)

---

## 1:50–2:45 — Honest numbers + rigor

**EN:** "Numbers we measured, not estimated: over a million spread ticks and growing with the live collector — re-check the exact count on recording day. The absolute spread distribution: 5.1 basis points at p25, 13.5 at the median, 32.9 at p75. Per tick: 488 RWA pairs plus 40 cross-issuer pairs; the oracle covers 442 underlyings with a genuinely independent close — never a derived one, labeled per row, one price source per metric. 248 passing tests. And the honest part: our first three demo-trade broadcasts reverted on-chain — a 2-second deadline baked into the route calldata, proven by calldata decode and eth_call replay. We root-caused it, built a pre-broadcast deadline gate — and the next trade landed on-chain: first attempt, one broadcast, a one-wei dust swap. When we get a diagnosis wrong we retract it — the receipts and the correction are in the repo."

**ES:** *Números medidos, no estimados: más de un millón de ticks y creciendo con el colector en vivo — revisar la cifra exacta el día de la grabación. Distribución del spread absoluto: 5,1 puntos básicos en p25, 13,5 en la mediana, 32,9 en p75. Por tick: 488 pares RWA más 40 cross-issuer; el oráculo cubre 442 subyacentes con cierre realmente independiente — nunca derivado, etiquetado por fila, una fuente de precio por métrica. 248 tests pasando. Y la parte honesta: las tres primeras transacciones de demo revirtieron on-chain — un plazo de 2 segundos en el calldata de la ruta, probado con decodificación y replay eth_call. Encontramos la causa, construimos una compuerta de plazo antes de transmitir — y la siguiente transacción aterrizó on-chain: al primer intento, una transmisión, un swap de polvo de un wei. Cuando nos equivocamos de diagnóstico, lo retractamos — los recibos y la corrección están en el repo.*

Shots: S8 → S7 → S9 (the revert receipts and the successful trade are both features of the pitch, not secrets).

---

## 2:45–3:15 — BNB Chain integration

**EN:** "This is built on BNB Chain. The pay gate is x402-style over BSC: the 402 response carries x402Version-1, and local receipt verification checks an on-chain BSC payment before dispatch — paid calls are idempotent by transaction hash. Our oracle now publishes its derived price on-chain to a PriceRegistry contract on BSC mainnet — behind a toggle — and every MCP response already carries an ERC-8004-style identity header: the hook for agent identity and reputation. The trade pipeline executes on BSC mainnet through the Binance Web3 aggregator with simulate-before-broadcast and deadline gates."

**ES:** *Está construido sobre BNB Chain. El muro de pago es estilo x402 sobre BSC: la respuesta 402 lleva x402Version-1 y la verificación de recibo on-chain revisa el pago en BSC antes de despachar — las llamadas pagas son idempotentes por hash de transacción. Nuestro oráculo ya publica su precio derivado on-chain en un contrato PriceRegistry en BSC mainnet — detrás de un toggle — y cada respuesta MCP ya lleva una cabecera de identidad estilo ERC-8004: el gancho para identidad y reputación de agentes. Y el pipeline de trading ejecuta en BSC mainnet con compuertas de simular antes de transmitir y de plazo.*

Shot: S10 (zoom on the `X-ERC8004-Identity` header, then the `PriceRegistry` contract on BscScan).

---

## 3:15–3:45 — Roadmap + close (Agent Bazaar)

**EN:** "Where this goes on BNB: as tokenized equities grow, every issuer needs a peg monitor and every agent needs payable, machine-readable signals. Agent Bazaar is the natural storefront — StockGap lists its MCP tools as pay-per-call services settled in BNB, agents discover and pay autonomously, and the ERC-8004 reputation loop gives buyers a verifiable track record. Next: more issuers and venues, per-issuer alert subscriptions, BNB-settled plans, staked oracle attestations. Repo, live demo and the run notes are linked in the submission. Thank you."

**ES:** *Hacia dónde va en BNB: a medida que crezcan los valores tokenizados, cada emisor necesita un monitor del peg y cada agente necesita señales pagables y legibles por máquina. Agent Bazaar es el escaparate natural — StockGap publica sus herramientas MCP como servicios de pago por llamada liquidados en BNB, y el bucle de reputación ERC-8004 da a los compradores un historial verificable. Próximos pasos: más emisores y mercados, suscripciones de alertas por emisor, planes liquidados en BNB, atestaciones de oráculo con stake. Repo, demo en vivo y notas de ejecución están en la submission. Gracias.*

Shot: S11.

---

**Total: 3:45 (hard max 4:00).** If over 4:00, cut S3's tick to 5 s and shorten the roadmap list.
