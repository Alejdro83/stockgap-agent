NEEDS_FIXES
#1 [CRITIC] El puerto 8098 ya está ocupado por el dashboard en producción
Evidencia: `ss -ltnp` muestra 0.0.0.0:8098 ocupado por pid 1695012 (`python3 scripts/dashboard.py`, arrancado el mar 2026-10-06 19:34:32, sin variables MCP_* en su entorno, así que está en modo free). El paso 3 del runplan (prueba1-runplan.md:32) pide arrancar en 127.0.0.1:8098, lo que choca con ese proceso. Además, `stockgap/dashboard.py:327` fija host 0.0.0.0, así que `main()` nunca escucha en 127.0.0.1. Si alguien mata el proceso para liberar el puerto, deja a los jueces sin el endpoint free.
Fix: arrancar una instancia aparte con `MCP_*=... uvicorn stockgap.dashboard:app --host 127.0.0.1 --port 8198` (o cualquier puerto libre que se compruebe antes con `ss`). Todos los curl de los pasos 3, 4, 5, 7 y 9 van a :8198. Añadir el invariante «no se toca el pid de :8098».

#2 [CRITIC] `get_spread {token_symbol: NVDA}` devuelve un informe vacío y aun así consume el pago
Evidencia: `mcp_server.py:76-78` filtra con `UPPER(token_symbol) = UPPER(?)` por coincidencia exacta. En `data/spread_ticks.db`, en el último tick, `token_symbol='NVDA'` da 0 filas; los símbolos reales son `NVDAB` y `NVDAon` (underlying_ticker=NVDA). El paso 7 (runplan:36) cobraría 0,005 tBNB, marcaría el hash como usado (`b402_gate.py:279`) y serviría `rows: []`. Eso no es un plano válido y no se puede repetir porque el hash ya está quemado.
Fix: usar `{token_symbol: "NVDAon"}` o `get_cross_issuer {underlying: "NVDA"}`. En el paso 1, ejecutar exactamente la misma llamada contra la instancia en modo free y exigir `rows` no vacío antes de dar la señal «YA».

#3 [MAYOR] Cualquier transferencia entrante a 0xb471 dentro de la ventana es una prueba válida, porque no se comprueba quién paga
Evidencia: `verify_receipt` (`b402_gate.py:130-160`) solo comprueba status, `to==merchant`, `value>=price` y la edad del bloque. No ata el pago al comprador `0x069a…` ni a la petición concreta. Las dos cuentas tienen exactamente 0x429d069189e0000 = 3e17 wei (0,3 tBNB), consultado vía RPC a las 17:12 UTC, lo que apunta a un fondeo reciente. La transacción de fondeo de 0xb471 (0,3 ≥ 0,005) canjearía un informe si cae dentro de MCP_MAX_BLOCK_AGE_S. La mitigación de «subir la ventana por env» (runplan:41) amplía ese agujero. Además, el hash es público en cadena entre el pago y el canje, así que otro cliente puede adelantarse.
Fix: en el paso 1, listar las transacciones entrantes a 0xb471 en la testnet (bscscan) y fijar `MCP_MAX_BLOCK_AGE_S` menor que la edad de la última entrada que no sea del demo; no subirla nunca por encima de eso. Documentar en la nota de desviaciones que el pago no está atado al pagador (`payer` no verificado), y no narrar en el video «solo el comprador puede canjear».

#4 [MAYOR] El smoke negativo del paso 4 no dice qué hash usar
Evidencia: runplan:33 no indica de dónde sale «un tx hash con destinatario equivocado». Si se usara una transacción hacia 0xb471 (por ejemplo, su fondeo), pasaría la verificación y quedaría consumida en `paid_calls`, que es lo contrario de un smoke negativo. Un hash inventado solo prueba la rama «no encontrada» (`b402_gate.py:139-140`), no la de destinatario (`b402_gate.py:152-153`).
Fix: fijar en el plan un hash concreto de testnet cuyo `to` sea distinto de 0xb471 (por ejemplo, el fondeo de 0x069a), comprobado antes con `eth_getTransactionByHash`. El criterio de éxito es la cadena exacta «Pago rechazado: destinatario distinto del comercio» y 0 filas nuevas en `paid_calls`.

#5 [MAYOR] La regla «solo una capa cobra» depende de un valor por defecto del seller que apunta al gate
Evidencia: `stockgap-seller/run-plain.sh:30` y `app/agent/src/sellerCore.ts:49-50` usan por defecto `STOCKGAP_MCP_URL=http://localhost:8098/mcp`. Si el plano opcional del seller (runplan:43) se arranca sin exportar la URL, o si el gate acaba en 8098 (ver #1), el seller consulta un upstream que cobra: doble capa y `payment_required` (`sellerCore.ts:446`).
Fix: en el plano del seller, exportar siempre `STOCKGAP_MCP_URL=https://http--stockgap-app--6h2d2gps9lkr.code.run/mcp` de forma explícita. Antes de grabar, hacer un `tools/call` sin `X-Payment` a ese upstream y exigir HTTP 200, y guardar la salida como evidencia de «upstream FREE».

#6 [MAYOR] Planos del video sin evidencia verificable definida
Evidencia: el paso 7 habla de «guardar capturas» sin decir formato, ruta ni contenido. El paso 9 cita una checklist [G1-N11] que no está definida en el plan. El paso 8 se apoya solo en una página del explorador. «0,005 BNB ≈ 3,8 USD» (runplan:18) no tiene fuente ni fecha.
Fix: para cada beat (402, 402 con hash de destinatario equivocado, pago, 200, replay), guardar `curl -sS -i` byte a byte en `stockgap-agent/docs/evidence/prueba1/NN_<beat>.txt` con timestamp UTC y su sha256. Guardar además el JSON de `eth_getTransactionByHash` y `eth_getTransactionReceipt` del pago tomado del RPC de testnet, y la fila de `paid_calls` del hash. Copiar la checklist G1-N11 dentro del plan. Citar el precio del BNB con su fuente y hora, o quitar la cifra en USD.

#7 [MENOR] `MCP_CHAIN_ID` no hace nada en el código; la cadena la decide solo el RPC
Evidencia: `MCP_CHAIN_ID` aparece solo en `KEYS` (`b402_gate.py:34-35`). `GateConfig` no tiene un campo para él y `load_config` no lo lee (`b402_gate.py:77-87`). El «fix obligatorio 56→97» (runplan:26) es cosmético; la cadena real la fija `MCP_RPC_URL`. Lo he comprobado: los dos RPC del plan responden `eth_chainId` = 0x61 (97).
Fix: añadir al paso 1 un `eth_chainId == 0x61` contra el `MCP_RPC_URL` configurado y también contra el de respaldo. Cambiar el texto del plan a «la cadena la fija MCP_RPC_URL; MCP_CHAIN_ID es informativo».

#8 [MENOR] Los valores por defecto ponen como merchant a la compradora: un auto-envío pasaría la verificación
Evidencia: `b402_gate.py:28` y `config/mcp.env:11` usan `0x069Ac6…F819` como merchant. Si las variables de entorno no llegan al proceso, el 402 anunciará `payTo=0x069a…`. Un envío de 0x069a a sí misma cumpliría `to==merchant` y canjearía el informe sin que cambie de manos ningún valor.
Fix: convertir la comprobación del paso 3 en un aborto obligatorio antes de «YA»: `accepts[0].payTo` en minúsculas igual a `0xb471f1ab267144cf4cfff4800bea3ebc0e08ae64` y `maxAmountRequired=="5000000000000000"`, y si no, abortar. La cuenta en wei es correcta: 0,005 × 1e18 = 5e15 = `5000000000000000`, 16 dígitos.

#9 [MENOR] El pago se consume aunque el informe falle
Evidencia: `claim` se ejecuta en `b402_gate.py:279` antes de `run_tool` (`mcp_server.py:212-214`). Si la BD falla, `run_tool` devuelve `isError: true` (`mcp_server.py:174-175`) con el hash ya quemado. Además, el gate creará `paid_calls` dentro de la BD viva `data/spread_ticks.db`, donde hoy esa tabla no existe (comprobado).
Fix: lo cubre el pre-check en modo free de #2. Documentar que `paid_calls` de la testnet queda en la BD de producción, o apuntar `STOCKGAP_DB` a una copia hecha con `sqlite3 .backup` para Prueba 1.

#10 [MENOR] Higiene de secretos en la grabación y los transcripts
Evidencia: con `local_receipt` no se cargan credenciales (`b402_gate.py:225-229` solo se usa con el proveedor binance), y la línea de env del paso 2 no contiene secretos. Pero `run-plain.sh:23-28` carga `.studio/.env.local` (WALLET_PASSWORD) si se graba el plano del seller, y el home contiene `~/.vercel-token` y `config/alerts.env`. Además, la cabecera `X-ERC8004-Identity` mostrará `erc8004:bsc-mainnet:REGISTRY_PLACEHOLDER#0` (`b402_gate.py:48`) en las capturas de `curl -i`.
Fix: grabar en una shell limpia, sin `env`, `set -x` ni historial visible. Para el seller, usar `WALLET_KEYSTORE_FILE` sin hacer `cat` en pantalla. Pasar `MCP_ERC8004_ID` por env con un valor real o un valor de testnet explícito, o recortar esa cabecera de las capturas.

#11 [MENOR] El alcance de los tests es correcto, pero `pnpm test` no es de solo lectura
Evidencia: no existe `tests/conftest.py`, y `test_mcp.py` y `test_b402.py` usan `tmp_path`, `monkeypatch` y RPC falso, sin red ni procesos (ver `test_b402.py:22-32` y `test_mcp.py:20-38`). Pero el script `test` de `stockgap-seller/app/agent/package.json:17` es `pnpm run build && node --test ...`, que reescribe `dist/`, y eso es lo que ejecuta `run-plain.sh:32`.
Fix: quitar «solo lectura» de la descripción del paso 1, o correr `pnpm test` antes de cualquier arranque del seller y no durante la grabación.

#12 [MENOR] El saldo alcanza; solo falta revalidarlo justo antes
Evidencia: a las 17:12 UTC, 0x069a tiene 3e17 wei. El flujo necesita 5e15 más gas (21000 × ≤5 gwei ≈ 1,05e14), así que alcanza para unos 50 reintentos. 0xb471 no necesita gas.
Fix: repetir `eth_getBalance` de 0x069a justo antes de «YA» y guardar la salida como evidencia.
