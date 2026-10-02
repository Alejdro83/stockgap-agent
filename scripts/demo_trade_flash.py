#!/usr/bin/env python3
"""Second demo-trade attempt: single-call quote-and-swap (Flash API) flow.

Structurally immune to the quoteId-TTL min-return race that reverted tx
0xfdc9806cd479... : ``/api/v1/dex/aggregator/quote-and-swap`` (vendor=LiquidMesh)
returns quote + swap calldata in ONE round-trip (no quoteId, no TTL window), so
``minReceiveAmount`` is baked from a quote computed in the SAME request that
produced the calldata. Combined with slippagePercent=8 and near-zero delay to
broadcast, adverse-drift exposure before inclusion is bounded to the sign+
broadcast window (~seconds) within an 8% min-return band.

Modes:
  --probe            call quote-and-swap via POST and GET, dump real shapes,
                     exit. NEVER signs or broadcasts.
  (default, --live)  quote-and-swap -> fresh simulate (must be SUCCESS) ->
                     sign -> EXACTLY ONE broadcast -> poll on-chain receipt.

The wallet key is loaded from
``/home/lab-master/.hermes/secrets/bnb_wallet_private_key`` into
``WALLET_PRIVATE_KEY`` at runtime only and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from stockgap.client import BinanceWeb3Client, load_credentials          # noqa: E402
from stockgap.executor import (                                          # noqa: E402
    CHAIN_ID, NATIVE_BNB, ROUTER_ADDRESS, Executor, ExecutorConfig,
    WalletSigner, fetch_nonce, rpc_call, _to_int,
)

WALLET = "0x069Ac6d8cf3553416fE5a4f0410ebEb758A8F819"
NVDAB = "0x02fca66c1d1afb4e2a7884261eb00f63598a7436"
AMOUNT = "3000000000000000"          # 0.003 BNB — the approved notional
SLIPPAGE = "8"                       # wider than 5: survive inclusion drift
KEY_FILE = "/home/lab-master/.hermes/secrets/bnb_wallet_private_key"
QAS_PATH = "/api/v1/dex/aggregator/quote-and-swap"
DATA = REPO / "data"


def load_wallet_key() -> None:
    """Put the wallet key into the env for WalletSigner (never printed)."""
    key = Path(KEY_FILE).read_text().strip()
    if not key.startswith("0x"):
        key = "0x" + key
    os.environ["WALLET_PRIVATE_KEY"] = key


def flash_params() -> dict:
    return {
        "binanceChainId": CHAIN_ID,
        "fromTokenAddress": NATIVE_BNB,
        "toTokenAddress": NVDAB,
        "amount": AMOUNT,
        "userWalletAddress": WALLET,
        "slippagePercent": SLIPPAGE,
        "vendor": "LiquidMesh",
        "executionMode": "SWAP",
    }


def call_qas(client, method: str, params: dict):
    """One signed quote-and-swap call (POST body or GET query)."""
    if method == "POST":
        return client.request("POST", QAS_PATH, body=params)
    return client.request("GET", QAS_PATH, params=params)


def find_tx(data: dict) -> dict:
    """Locate the unsigned EVM tx dict in a quote-and-swap / swap payload."""
    if isinstance(data.get("tx"), dict):
        return data["tx"]
    if {"to", "data", "value"} <= set(data):
        return data
    return {}


def summarize(method: str, status: int, parsed: dict, raw: str) -> dict:
    data = parsed.get("data") or {}
    tx = find_tx(data if isinstance(data, dict) else {})
    rr = (data.get("routerResult") or {}) if isinstance(data, dict) else {}
    return {
        "method": method,
        "http_status": status,
        "code": parsed.get("code"),
        "msg": parsed.get("msg"),
        "data_keys": sorted(data.keys()) if isinstance(data, dict) else f"{type(data).__name__}[{len(data)}]",
        "top_keys": sorted(parsed.keys()),
        "has_quote_id": bool(rr.get("quoteId") or (isinstance(data, dict) and data.get("quoteId"))),
        "executionMode": (data.get("executionMode") if isinstance(data, dict) else None) or rr.get("executionMode"),
        "vendorName": rr.get("vendorName") or (isinstance(data, dict) and data.get("vendorName")),
        "toTokenAmount": rr.get("toTokenAmount") or (isinstance(data, dict) and data.get("toTokenAmount")),
        "tx_keys": sorted(tx.keys()),
        "tx.to": tx.get("to"),
        "tx.value": tx.get("value"),
        "tx.gas": tx.get("gas"),
        "tx.gasPrice": tx.get("gasPrice"),
        "minReceiveAmount": tx.get("minReceiveAmount"),
        "slippagePercent": tx.get("slippagePercent"),
        "tx.data_len": len(tx.get("data") or ""),
        "raw_head": raw[:300] if not parsed else None,
    }


def run_probe(client) -> str:
    """Probe POST (task's stated method) and GET (docs' method); return best."""
    params = flash_params()
    results = {}
    best = ""
    for method in ("POST", "GET"):
        t0 = time.time()
        try:
            status, parsed, raw = call_qas(client, method, params)
        except Exception as exc:  # network-level failure of this probe call
            results[method] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        results[method] = summarize(method, status, parsed, raw)
        results[method]["elapsed_s"] = round(time.time() - t0, 3)
        (DATA / f"flash_quote_and_swap_{method.lower()}.json").write_text(
            json.dumps({"request": params, "response": parsed}, indent=2, ensure_ascii=False))
        data = parsed.get("data")
        if parsed.get("code") == 0 and find_tx(data if isinstance(data, dict) else {}):
            best = best or method
    (DATA / "flash_quote_and_swap_probe.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False))
    print(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nBEST_METHOD={best or 'NONE'}")
    return best or "POST"


def run_live(client, method: str, max_pre_attempts: int = 3) -> dict:
    """quote-and-swap -> fresh simulate -> sign -> ONE broadcast -> receipt."""
    load_wallet_key()
    signer = WalletSigner()
    acct = signer._account()
    assert acct.address == WALLET, f"key derives {acct.address}, not {WALLET}"
    config = ExecutorConfig.from_env()
    ex = Executor(client=client, config=config, signer=signer)
    report: dict = {"wallet": WALLET, "method": method, "amount_base": AMOUNT,
                    "slippage_percent": SLIPPAGE, "stages": {}}

    reasons = ex.broadcast_block_reasons()
    assert not reasons, f"broadcast blocked before start: {reasons}"
    bnb_wei = _to_int(rpc_call("eth_getBalance", [WALLET, "latest"]))
    report["bnb_wei_before"] = bnb_wei
    assert bnb_wei > _to_int(AMOUNT) + 10**15, "balance too low for trade+gas"

    # ---- pre-broadcast attempts (nothing has left the machine yet) --------
    params = flash_params()
    tx = sim = None
    for attempt in range(1, max_pre_attempts + 1):
        t0 = time.time()
        status, parsed, raw = call_qas(client, method, params)
        if parsed.get("code") != 0:
            raise SystemExit(f"quote-and-swap failed [{parsed.get('code')}] "
                             f"{parsed.get('msg')} raw={raw[:300]}")
        data = parsed.get("data") or {}
        tx = find_tx(data)
        assert tx, f"no tx in quote-and-swap response: {sorted(data)}"
        report["stages"][f"quote_and_swap_{attempt}"] = summarize(method, status, parsed, raw)
        report["stages"][f"quote_and_swap_{attempt}"]["elapsed_s"] = round(time.time() - t0, 3)

        # sanity: same trade we approved
        assert tx.get("to") == ROUTER_ADDRESS, f"unexpected router {tx.get('to')}"
        assert _to_int(tx.get("value")) == _to_int(AMOUNT), f"unexpected value {tx.get('value')}"
        assert _to_int(tx.get("minReceiveAmount")) > 0, "no minReceiveAmount baked"
        assert str(tx.get("slippagePercent")) == SLIPPAGE, f"slippage {tx.get('slippagePercent')}"

        evm_tx = Executor._evm_tx_from(tx)
        t1 = time.time()
        sim = ex.simulate(evm_tx)
        report["stages"][f"simulate_{attempt}"] = sim
        report["stages"][f"simulate_{attempt}"]["elapsed_s"] = round(time.time() - t1, 3)
        if str(sim.get("status", "")).upper() == "SUCCESS":
            break
        if attempt == max_pre_attempts:
            raise SystemExit(f"simulate never SUCCESS: {sim}")
        # fresh quote-and-swap (atomic re-quote) and try again — no broadcast yet

    # ---- sign (local) ------------------------------------------------------
    assert tx is not None
    signed = signer.sign_evm_tx(Executor._signable_tx(tx))
    raw_bytes = bytes.fromhex(signed[2:] if signed.startswith("0x") else signed)
    from eth_utils.crypto import keccak                       # lazy, optional dep
    local_hash = "0x" + keccak(raw_bytes).hex()               # keccak(signed raw tx)
    assert local_hash.startswith("0x") and len(local_hash) == 66
    report["stages"]["sign"] = {"status": "signed", "tx_hash_local": local_hash,
                               "nonce": _to_int(rpc_call("eth_getTransactionCount", [WALLET, "pending"]))}

    # ---- the ONE AND ONLY broadcast of this run ----------------------------
    t2 = time.time()
    broadcast: dict
    try:
        broadcast = ex.broadcast(signed, WALLET)
    except Exception as exc:
        # broadcast may or may not have reached the node: NEVER re-send.
        report["stages"]["broadcast"] = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                                        "tx_hash_local": local_hash, "resent": False}
        save(report)
        raise SystemExit(f"broadcast errored after send attempt: {exc}; "
                         f"local tx hash {local_hash}; NOT re-sent")
    report["stages"]["broadcast"] = {**broadcast, "elapsed_s": round(time.time() - t2, 3),
                                    "tx_hash_local": local_hash, "broadcast_calls": 1}
    tx_hash = broadcast.get("txHash") or local_hash
    report["tx_hash"] = tx_hash

    # ---- on-chain receipt (read-only polling; never a re-send) -------------
    report["receipt"] = wait_receipt(tx_hash)
    report["bnb_wei_after"] = _to_int(rpc_call("eth_getBalance", [WALLET, "latest"]))
    save(report)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return report


def wait_receipt(tx_hash: str, timeout_s: float = 180.0, interval_s: float = 3.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rec = rpc_call("eth_getTransactionReceipt", [tx_hash])
        if rec:
            return {"status": rec.get("status"),
                    "blockNumber": _to_int(rec.get("blockNumber")),
                    "gasUsed": _to_int(rec.get("gasUsed")),
                    "effectiveGasPrice": _to_int(rec.get("effectiveGasPrice") or 0),
                    "cumulativeGasUsed": _to_int(rec.get("cumulativeGasUsed") or 0)}
        time.sleep(interval_s)
    return {"status": "PENDING", "note": f"no receipt within {timeout_s}s"}


def save(report: dict) -> None:
    (DATA / "demo_trade_flash_run.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str))


def main() -> int:
    ap = argparse.ArgumentParser(description="NVDAB demo trade via quote-and-swap")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--method", choices=["POST", "GET"], default="POST")
    args = ap.parse_args()
    api_key, secret, _, _ = load_credentials()
    client = BinanceWeb3Client(api_key, secret)
    if args.probe:
        run_probe(client)
        return 0
    if not args.live:
        ap.error("choose --probe or --live")
    run_live(client, args.method)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
