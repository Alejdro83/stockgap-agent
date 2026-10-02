#!/usr/bin/env python3
"""Wallet balance CLI for the StockGap demo wallet (BSC mainnet, chain 56).

Shows BNB (native) + token balances using two independent sources:
  * BSC JSON-RPC (``eth_getBalance`` for BNB, ``eth_call balanceOf`` for ERC-20)
  * Binance Web3 Wallet API ``GET /api/v1/dex/balance/all-token-balances-by-address``
    (HMAC API key only — no wallet private key needed)

Usage:
    python3 -m stockgap.balance [--wallet 0x...] [--json] [--tokens 0x...,0x...]

Never prints secret values.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any, Dict, List, Optional

from .client import BinanceWeb3Client, load_credentials

CHAIN_ID = "56"
DEFAULT_WALLET = "0x069Ac6d8cf3553416fE5a4f0410ebEb758A8F819"
RPC_URLS = [
    "https://bsc-dataseed.binance.org/",
    "https://bsc-dataseed.defibit.io/",
    "https://bsc.publicnode.com",
]
WEI = 10 ** 18

# Well-known BSC tokens for the RPC fallback path.
KNOWN_TOKENS = [
    ("USDT", "0x55d398326f99059fF775485246999027B3197955", 18),
    ("WBNB", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", 18),
    ("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18),
]


def rpc_call(method: str, params: List[Any], timeout: float = 15.0) -> Any:
    """One JSON-RPC call against the first BSC endpoint that answers."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err: Optional[Exception] = None
    for url in RPC_URLS:
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode())
            if "error" in out and out["error"]:
                raise RuntimeError(f"RPC error: {out['error']}")
            return out.get("result")
        except Exception as exc:  # try next endpoint
            last_err = exc
    raise RuntimeError(f"all RPC endpoints failed: {last_err}")


def bnb_balance_rpc(wallet: str) -> float:
    wei_hex = rpc_call("eth_getBalance", [wallet, "latest"])
    return int(wei_hex, 16) / WEI


def erc20_balance_rpc(wallet: str, token: str, decimals: int) -> float:
    data = "0x70a08231" + wallet.lower().replace("0x", "").rjust(64, "0")
    raw = rpc_call("eth_call", [{"to": token, "data": data}, "latest"])
    if not raw or raw == "0x":
        return 0.0
    return int(raw, 16) / (10 ** decimals)


def api_all_token_balances(wallet: str) -> Dict[str, Any]:
    """Wallet API all-token balances (HMAC key only)."""
    api_key, secret, _, _ = load_credentials()
    client = BinanceWeb3Client(api_key, secret)
    status, parsed, raw = client.request(
        "GET",
        "/api/v1/dex/balance/all-token-balances-by-address",
        params={"binanceChainId": CHAIN_ID, "walletAddress": wallet},
    )
    return {"http_status": status, "code": parsed.get("code"), "msg": parsed.get("msg"),
            "data": parsed.get("data"), "raw": raw[:2000] if not parsed else None}


def collect(wallet: str, tokens: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"wallet": wallet, "chain": "BSC mainnet (56)"}
    try:
        out["bnb"] = {"source": "rpc", "balance": bnb_balance_rpc(wallet)}
    except Exception as exc:
        out["bnb"] = {"source": "rpc", "error": str(exc)}

    rpc_tokens = []
    wanted = tokens or [t[1] for t in KNOWN_TOKENS]
    meta = {t[1].lower(): t for t in KNOWN_TOKENS}
    for addr in wanted:
        name, dec = "token", 18
        if addr.lower() in meta:
            name, dec = meta[addr.lower()][0], meta[addr.lower()][2]
        try:
            bal = erc20_balance_rpc(wallet, addr, dec)
            rpc_tokens.append({"symbol": name, "contract": addr, "balance": bal})
        except Exception as exc:
            rpc_tokens.append({"symbol": name, "contract": addr, "error": str(exc)})
    out["tokens_rpc"] = rpc_tokens

    try:
        out["tokens_api"] = api_all_token_balances(wallet)
    except Exception as exc:
        out["tokens_api"] = {"error": str(exc)}
    return out


def render(report: Dict[str, Any]) -> str:
    lines = [f"Wallet: {report['wallet']}  [{report['chain']}]"]
    bnb = report.get("bnb", {})
    if "balance" in bnb:
        lines.append(f"BNB (native, RPC): {bnb['balance']:.6f} BNB")
    else:
        lines.append(f"BNB (native, RPC): ERROR {bnb.get('error')}")
    lines.append("Tokens (RPC balanceOf):")
    for t in report.get("tokens_rpc", []):
        if "balance" in t:
            lines.append(f"  {t['symbol']:<6} {t['balance']:.6f}  {t['contract']}")
        else:
            lines.append(f"  {t['symbol']:<6} ERROR {t.get('error')}  {t['contract']}")
    api = report.get("tokens_api", {})
    lines.append(f"Tokens (Wallet API, code={api.get('code')} http={api.get('http_status')}):")
    rows = api.get("data")
    if isinstance(rows, list):
        # The endpoint returns pagination envelopes: [{page, pageSize,
        # tokenAssets: [...]}] — flatten to token rows.
        flat: List[Dict[str, Any]] = []
        for r in rows:
            if isinstance(r, dict) and isinstance(r.get("tokenAssets"), list):
                flat.extend(x for x in r["tokenAssets"] if isinstance(x, dict))
            elif isinstance(r, dict):
                flat.append(r)
        if not flat:
            lines.append("  (no token holdings)")
        for r in flat:
            sym = (r.get("tokenSymbol") or r.get("symbol") or r.get("assetName")
                   or "?")
            amt = (r.get("tokenAmount") or r.get("amount") or r.get("balance")
                   or r.get("totalAmount") or "?")
            contract = (r.get("tokenContractAddress") or r.get("contractAddress")
                        or "")
            lines.append(f"  {sym:<8} {amt}  {contract}")
    elif isinstance(rows, dict):
        lines.append("  " + json.dumps(rows, ensure_ascii=False)[:1500])
    elif api.get("error"):
        lines.append(f"  ERROR {api['error']}")
    else:
        lines.append(f"  (no rows) raw={api.get('raw')}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="StockGap wallet balance (BSC 56)")
    ap.add_argument("--wallet", default=DEFAULT_WALLET)
    ap.add_argument("--json", action="store_true", help="print full JSON report")
    ap.add_argument("--tokens", default=None,
                    help="comma-separated ERC-20 contracts for the RPC balanceOf path")
    args = ap.parse_args(argv)
    tokens = [t.strip() for t in args.tokens.split(",") if t.strip()] if args.tokens else None
    report = collect(args.wallet, tokens)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
