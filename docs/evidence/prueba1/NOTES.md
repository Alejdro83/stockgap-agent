# Prueba 1 — NOTAS de cierre (pasos 8-11)

## Desviaciones documentadas
- **El pago NO está atado al pagador.** `verify_receipt` (`b402_gate.py:130-160`) comprueba status, `to==merchant`, `value>=price` y edad de bloque, pero no `payer` ni la petición concreta: cualquier transferencia entrante a `0xb471…ae64` con valor ≥ precio dentro de la ventana canjearía un informe, y el hash (público en cadena entre pago y canje) podría ser canjeado por otro cliente primero `[G1-N3]`.
- `MCP_CHAIN_ID` es **informativo**: la cadena real la fija `MCP_RPC_URL` (`GateConfig` no tiene ese campo y `load_config` no lo lee; solo aparece en `KEYS`, `b402_gate.py:34-35,77-87`) `[G1-N7]`.
- `paid_calls` de Prueba 1 vive en la **copia** de `data/spread_ticks.db` (`STOCKGAP_DB=<copia>`), no en la BD viva `[G1-N9]`. Motivo: `claim` quema el hash ANTES de servir (`mcp_server.py:212-214`, `b402_gate.py:279`) y un fallo de informe consumiría el pago igualmente — de ahí el pre-check E4.
- El informe se sirve sobre un **snapshot** de la BD (copiada con `VACUUM INTO`), no sobre datos en vivo: el `tick_ts` del informe es el del snapshot `[G2-N1]`.
- Precio por LLAMADA, no por hora.

## Narración del video — prohibiciones
- **No** narrar «solo el comprador puede canjear» (el pago no está atado al pagador) `[G1-N3]`.
- **No** decir «datos en vivo»: decir «snapshot de <ts>» (con el `MAX(ts_utc)` de E4) `[G2-N1]`.
- **No** afirmar cifras USD sin citar fuente y hora en la descripción `[G1-N6]`.
- **No** mostrar historial de shell ni variables de entorno. El plano del seller usa `WALLET_KEYSTORE_FILE` sin `cat`; `.studio/.env.local` (WALLET_PASSWORD), `~/.vercel-token` y `config/alerts.env` fuera de plano. La cabecera `X-ERC8004-Identity` se recorta de las capturas si muestra placeholder, o se pasa `MCP_ERC8004_ID` de testnet explícito `[G1-N10]`.

## Estado del cierre (pasos 8-11) — CANJEADO Y CERRADO
- **Falso inicio (nota):** el primer pago del humano (hash `0x921831ac0a4f7cdade9d497b028a5094dc8eae3020b0fd841479aaa12441e1bc`) fue a BSC **MAINNET** por error (0,005 BNB reales a la misma wallet del comercio — fondos propios, sin daño) y **NO forma parte de Prueba 1**; el pago válido de Prueba 1 es `0x58e1b19f611c907ada63bae3ad46379c0dbf0907e008e65947615aeaf102474a`.
- Hash del pago válido (literal, sin modificar): `0x58e1b19f611c907ada63bae3ad46379c0dbf0907e008e65947615aeaf102474a`.
- Paso 8a (verificación on-chain previa al canje): `eth_getTransactionByHash` y `eth_getTransactionReceipt` sobre el RPC de testnet (`https://data-seed-prebsc-1-s1.binance.org:8545`) → status `0x1`, `to` = `0xb471f1aB267144CF4CFff4800beA3ebC0E08ae64`, value = 5000000000000000 (5e15), bloque 135495382, edad del bloque ≈ 280 s ≤ 1800 s ✓ (E8: `08_payment_tx.json`, `08b_payment_receipt.json` — sobrescritos con las respuestas reales del hash válido).
- Paso 8b (canje): `POST /mcp tools/call get_spread {"token_symbol":"NVDAon"}` con `X-Payment: <hash>` → **HTTP 200 + GapReport** al primer intento (sin rechazos transitorios), `tick_ts` del snapshot = `2026-10-07T18:56:03.349Z` (E9: `09_200_report.txt`; cabecera `X-ERC8004-Identity` con placeholder recortada `[G1-N10]`).
- Paso 10 (beat anti-replay): reenvío del mismo hash → «Este tx hash ya fue usado para una llamada anterior» — texto exacto ✓ (E10: `10_replay_rejected.txt`).
- Paso 11 (cierre): `paid_calls` en la copia de BD → exactamente 1 fila para el hash (E11: `11_paid_calls_row.txt`); `SHA256SUMS` regenerado sobre todas las capturas del directorio y verificado con `sha256sum -c` (rc=0). No hubo reinicio de la instancia → no aplica `02c`.
- Instancia del demo: sigue VIVA sin reiniciar (tmux `prueba1-demo`, pid 2436466, `127.0.0.1:8198`); `:8098` (pid 1695012) intacto.

## Explorador (referencia, no criterio de cierre) `[G2-N6]`
- URL del pago válido: `https://testnet.bscscan.com/tx/0x58e1b19f611c907ada63bae3ad46379c0dbf0907e008e65947615aeaf102474a`
- Resultado: HTTP 403 (muro anti-bots) → plano omitido según runbook («si no carga, se omite el plano sin más»). Sin `09b_explorer_tx.html`. La prueba canónica del pago son los JSON del RPC (E8).
