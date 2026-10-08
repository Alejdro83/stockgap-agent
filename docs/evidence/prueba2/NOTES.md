# Prueba 2 — Mainnet Live Payment Evidence

**Fecha:** 2026-10-08T02:22:51.564260Z
**Red:** BSC Mainnet (chain 56, eip155:56)
**Merchant (payTo):** 0xb471f1aB267144CF4CFff4800beA3ebC0E08ae64
**Monto:** 0.005 BNB (5000000000000000 wei)
**Firmante (pagador):** 0x069ac6d8cf3553416fe5a4f0410ebeb758a8f819 (wallet A)

## Flujo verificado

1. **402 Challenge** — Gateway en :8198 (RPC mainnet) exige pago
2. **Pago on-chain** — Tx hash: 0x656e0477b5fa3cb0f470c56ef6ec7eb9709047163ae303efb9bcc0a66645957d
   - From: 0x069a...f819 (wallet A, MetaMask móvil)
   - To: 0xb471f1aB267144CF4CFff4800beA3ebC0E08ae64 (merchant A2)
   - Value: 5,000,000,000,000,000 wei = 0.005 BNB
   - Status: 0x1 (success)
   - Block: 126,362,786 (mainnet)
3. **Redeem** — GapReport servido (HTTP 200), tick 2026-10-07T18:56:03.349Z, símbolo NVDAon
4. **Anti-replay** — Reintento con mismo hash rechazado: "Este tx hash ya fue usado para una llamada anterior"
5. **paid_calls** — 2 filas totales (Prueba 1 testnet + Prueba 2 mainnet)

## Evidencia on-chain

- **Explorer:** https://bscscan.com/tx/0x656e0477b5fa3cb0f470c56ef6ec7eb9709047163ae303efb9bcc0a66645957d
- **Verificación RPC:** eth_getTransactionByHash + eth_getTransactionReceipt → status 0x1, to==merchant, value==5e15

## Cumplimiento runbook

- ✅ Puerta propia :8198 (sin tocar :8098 producción)
- ✅ Wallet A como firmante con override MCP_MERCHANT
- ✅ Token correcto: NVDAon (NO NVDA)
- ✅ Ventana 1800s: tx minada ~5 min antes del canje
- ✅ Anti-replay: 1 pago = 1 informe (2 filas en paid_calls = 2 informes servidos)
- ✅ Explorador: bscscan.com (mainnet, sin bot wall)
