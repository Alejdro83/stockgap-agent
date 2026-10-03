# StockGap — Video Pitch Script (5–6 min)

> Target: **5:30–5:45** (hard limits: 5:00 min / 6:00 max; if a cut lands under 5:00, expand the honest-numbers or ITSC section). Language: English narration. ES = Spanish speaker notes for the operator.
> Shot list = what the screen recording shows. Record with `scripts/dashboard.py` + collector already running so the demo is live.

---

## Shot list (record in this order, then edit to the timings below)

| # | Shot | What to capture |
|---|------|-----------------|
| S1 | Title card | Repo README hero (tagline) — 3 s |
| S2 | The gap, visually | Dashboard table sorted by `spread_bps_real` desc; cursor on a big positive and a big negative spread |
| S3 | Live tick | Terminal: `python3 scripts/run_collector.py` — one 30 s tick landing (488 rows) |
| S4 | Dashboard tour | `localhost:8098` on a phone-sized viewport: token row → provenance fields (`reference_kind`, `source_endpoint`) |
| S5 | MCP as an agent | Terminal: `tools/list` curl → `tools/call get_oracle_latest` curl → JSON response with `deviation_bps` |
| S6 | The pay gate | Same call with `MCP_PAY_MODE=b402` → HTTP 402 + `x402Version:1` receipt instructions |
| S7 | Tests | `python3 -m pytest -q` → `213 passed` |
| S8 | Architecture | Static diagram from `StockGap/README.md` (collector → spread → SQLite → alerts/dashboard/oracle → MCP + b402) |
| S9 | Numbers | Dashboard or `scripts/summary.py` output: ≈1M rows, p25/50/75 = 5.1/13.5/32.9 bps |
| S10 | Rigor evidence | `RECOVERY.md` scroll: the three reverted tx hashes + `eth_call` replay section (honesty shot) |
| S11 | BNB + ERC-8004 | MCP response headers showing `X-ERC8004-Identity: erc8004:bsc-testnet:…` (registry id pending — swap in the real id if A8 lands before recording) |
| S12 | Close | README links row (repo, DEPLOYED_URL, ITSC paper) |

---

## 0:00–0:40 — The problem

**EN:** "Tokenized stocks trade on-chain, the underlying share trades on a traditional exchange — and almost nobody measures the gap between them honestly. If your reference price is computed from the token price itself, the spread is structurally zero: a fake peg. StockGap starts from the uncomfortable question — what is the tokenized stock actually worth versus the real share?"

**ES:** *Los valores tokenizados cotizan on-chain y la acción subyacente en el mercado tradicional — y casi nadie mide la brecha de forma honesta. Si el precio de referencia se calcula desde el propio precio del token, el spread es cero estructural: un peg falso. StockGap parte de la pregunta incómoda: ¿cuánto vale realmente el tokenizado frente a la acción real?*

Shots: S1 → S2.

---

## 0:40–2:10 — Live demo

**EN:** "This is live, not a mockup. The collector polls 488 tokenized stocks — 442 Ondo and 46 BStock — every 30 seconds, rate-limit aware, and every row carries full provenance: source endpoint, fetch time, and whether the reference close is independent or derived. Here's a tick landing. In the dashboard — built mobile-first, FastAPI and HTMX, SQLite opened strictly read-only — we sort by the real spread: the gap between the token's implied share price and the independent per-share close, normalized by tokenToShareRatio, because one token is often not one share. Now, as an agent would use it: JSON-RPC to the MCP endpoint — tools/list is always free, get_oracle_latest returns the weekend-Monday-open oracle with a deviation flag per underlying. And here is the business model switch: with the b402 gate on, the same call returns HTTP 402 with x402Version-1 receipt instructions. Pay per call, on BNB Chain."

**ES:** *Esto está en vivo, no es un mockup. El colector sondea 488 valores tokenizados cada 30 segundos y cada fila guarda procedencia completa. En el dashboard — mobile-first, SQLite estrictamente de solo lectura — ordenamos por el spread real: la brecha entre el precio implícito y el cierre independiente por acción, normalizado por tokenToShareRatio, porque un token casi nunca es una acción. Como lo usaría un agente: JSON-RPC al endpoint MCP — tools/list siempre es gratis — y con el gate b402 activo la misma llamada devuelve HTTP 402 con instrucciones de recibo x402Version-1. Pago por llamada, en BNB Chain.*

Shots: S3 → S4 → S5 → S6. (Keep each curl unedited; the JSON is the credibility.)

---

## 2:10–2:40 — Architecture (30 seconds)

**EN:** "Architecture in 30 seconds: one HMAC-signed client feeds a spread engine and a SQLite tick store with WAL and short write transactions. On top: an alerts scanner, a mobile dashboard, a weekend oracle with a reputation feedback loop, and an MCP server with the pay gate. One price source per metric — provenance by rule — and when data is missing we label it, we never backfill."

**ES:** *Arquitectura en 30 segundos: un cliente firmado con HMAC alimenta un motor de spreads y un almacén SQLite con WAL y transacciones de escritura cortas. Encima: alertas, dashboard móvil, un oráculo de fin de semana con bucle de reputación, y un servidor MCP con el muro de pago. Una fuente de precio por métrica; si falta dato, se etiqueta — nunca se rellena.*

Shot: S8.

---

## 2:40–3:30 — Honest numbers

**EN:** "Numbers we measured, not estimated: over a million spread ticks and growing with the live collector — re-check the exact count on recording day. The absolute spread distribution: 5.1 basis points at p25, 13.5 at the median, 32.9 at p75. Per tick: 488 RWA pairs plus 40 cross-issuer pairs, and the oracle covers 442 underlyings with a genuinely independent close. 213 passing tests. And the honest part: our three demo-trade broadcasts reverted on-chain. We root-caused it — a 2-second deadline baked into the route calldata, proven by calldata decode and eth_call replay for two of three transactions (the third inferred from the identical pattern) — value returned, only gas burned, about 0.0000845 BNB total. The receipts are in the repo."

**ES:** *Números medidos, no estimados: más de un millón de ticks y creciendo con el colector en vivo — revisar la cifra exacta el día de la grabación. Distribución del spread absoluto: 5,1 puntos básicos en p25, 13,5 en la mediana, 32,9 en p75. Por tick: 488 pares RWA más 40 cross-issuer; el oráculo cubre 442 subyacentes con cierre realmente independiente. 213 tests pasando. Y la parte honesta: nuestras tres transacciones de demo revirtieron on-chain. Encontramos la causa — un plazo de 2 segundos en el calldata de la ruta, probado con decodificación y replay eth_call en dos de tres (la tercera, inferida por el patrón idéntico) — el valor se devolvió, solo se quemó gas. Los recibos están en el repo.*

Shots: S9 → S7 → S10 (the revert receipts are a feature of the pitch, not a secret).

---

## 3:30–4:15 — ITSC rigor

**EN:** "Why trust the measurement? The methodology is published — the ITSC write-up is linked in our README and in the submission. Concretely in the code: an independent reference close, never a derived one, labeled per row; ratio normalization regression-tested against the NFLX case where a 10x token ratio produced a 90,000-basis-point phantom spread; and one price source per metric so no series is ambiguous about its frame. When we got a diagnosis wrong — we first blamed a quote-TTL min-return race for the reverts — we retracted it publicly in our recovery report and published the real root cause."

**ES:** *¿Por qué confiar en la medición? La metodología está publicada — el documento ITSC está enlazado en el README y en la submission. En el código: cierre de referencia independiente, nunca derivado, etiquetado por fila; normalización por ratio con test de regresión (el caso NFLX: un ratio 10x generaba un fantasma de 90.000 puntos básicos); y una fuente de precio por métrica. Y cuando nos equivocamos de diagnóstico — culpamos primero a un min-return por TTL — lo retractamos públicamente y publicamos la causa real.*

Shot: S10 + ITSC link on screen (`https://thoracic.clinic/articles/itsc-qs4nqr`).
> ⚠️ ASK-OPERATOR (A6): confirm exact claims we may make about the ITSC paper before recording this section.

---

## 4:15–5:00 — BNB Chain integration

**EN:** "This is built on BNB Chain. The pay gate is x402-style over BSC: the 402 response carries x402Version-1, and local receipt verification checks an on-chain BSC payment before dispatch — paid calls are idempotent by transaction hash. Our oracle is wired to publish its derived price to BSC testnet — that on-chain hook is next — and every MCP response already carries an ERC-8004-style identity header: the hook for agent identity and reputation, where our feedback tables already implement the ERC-8004-style loop, with on-chain attestation as the next step. And the trade pipeline executes on BSC mainnet through the Binance Web3 aggregator with a strict simulate-before-broadcast gate."

**ES:** *Está construido sobre BNB Chain. El muro de pago es estilo x402 sobre BSC: la respuesta 402 lleva x402Version-1 y la verificación de recibo on-chain revisa el pago en BSC antes de despachar — las llamadas pagas son idempotentes por hash de transacción. El oráculo está preparado para publicar su precio derivado en BSC testnet — ese gancho on-chain es lo siguiente — y cada respuesta MCP ya lleva una cabecera de identidad estilo ERC-8004: el gancho para identidad y reputación de agentes. Y el pipeline de trading ejecuta en BSC mainnet con una compuerta estricta de simular antes de transmitir.*

Shot: S11 (zoom on the `X-ERC8004-Identity` header).

---

## 5:00–5:45 — Roadmap + close (Agent Bazaar)

**EN:** "Where this goes on BNB: as tokenized equities grow, every issuer needs a peg monitor and every agent needs payable, machine-readable signals. Agent Bazaar is the natural storefront — StockGap lists its MCP tools as pay-per-call services settled in BNB, agents discover and pay autonomously, and the ERC-8004 reputation loop gives buyers a verifiable track record. Next: more issuers and venues, per-issuer alert subscriptions, BNB-settled plans, staked oracle attestations. Repo, live demo and the published methodology are linked in the submission. Thank you."

**ES:** *Hacia dónde va en BNB: a medida que crezcan los valores tokenizados, cada emisor necesita un monitor del peg y cada agente necesita señales pagables y legibles por máquina. Agent Bazaar es el escaparate natural — StockGap publica sus herramientas MCP como servicios de pago por llamada liquidados en BNB, y el bucle de reputación ERC-8004 da a los compradores un historial verificable. Próximos pasos: más emisores y mercados, suscripciones de alertas por emisor, planes liquidados en BNB, atestaciones de oráculo con stake. Repo, demo en vivo y metodología publicada están en la submission. Gracias.*

Shot: S12.

---

**Total: 5:30–5:45.** If over 6:00, cut S3's full 30 s tick to 10 s and shorten 3:30–4:15 (drop the NFLX anecdote).
