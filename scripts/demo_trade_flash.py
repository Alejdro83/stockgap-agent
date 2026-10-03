#!/usr/bin/env python3
"""NVDAB demo trade via the UNIFIED hardened path (Executor.quote_and_swap).

What changed after the "Route: expired" post-mortem (RECOVERY.md / DX-LOG):
the standalone Flash driver is gone — this script now drives THE single
trading path ``Executor.quote_and_swap()`` (stockgap/executor.py), which has:

* the pre-broadcast route-deadline gate (scans the built calldata's uint64
  ``valid-until`` words at ANY offset, aborts ZERO-GAS when
  ``now + ROUTE_DEADLINE_MARGIN_S > deadline``, then re-quotes and rebuilds) —
  this is the fix for the 2s in-calldata deadline that reverted all three
  demo broadcasts with `Route: expired`;
* prefetched chainId/nonce/gasPrice (BEFORE the quote) so signing is local;
* the simulate-before-broadcast gate (unchanged, plus a deadline re-check
  after simulate);
* a post-revert resend as LAST RESORT, capped at 1, and only after the
  previous tx settled (receipt or nonce advance) — a late tx can still land;
* route-source fallback: Flash ``quote-and-swap`` (vendor LiquidMesh) vs the
  two-step ``/quote`` + ``/swap``; the source with the later embedded
  deadline wins when the preferred one fails the gate.

Modes (nothing is signed or sent outside --live):
  --probe              read-only: quote shapes + DEADLINE PROBE
                       (a) flash vs /quote+/swap calldata-deadline compare,
                       (b) does any request param (deadline/validTo/
                       quoteTtlSeconds) move the in-calldata deadline?
  --live [--dry-run]   run the unified pipeline.  --dry-run forces DRY_RUN so
                       nothing can leave the machine.  Without every guard
                       env open (see below) the run aborts before signing.

Env toggles (full list in `python3 -m stockgap.executor --help`):
  DRY_RUN=1 (default), EXECUTOR_ENABLED, MAX_TRADE_USD,
  STOCKGAP_ALLOW_BROADCAST, STOCKGAP_FORCE_NO_BROADCAST (default "1" —
  broadcast is impossible while set), ROUTE_DEADLINE_MARGIN_S (default 3.0),
  BROADCAST_DIRECT=0 (1 = eth_sendRawTransaction with bumped gasPrice).

NOTE (supersedes the old "EXACTLY ONE broadcast / NEVER re-send" rule): the
unified path may resend ONCE after a CONFIRMED on-chain revert of its own tx
(max-1, post-settlement only).  It never resends an unsettled tx.

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
    CHAIN_ID, NATIVE_BNB, Executor, ExecutorConfig, WalletSigner,
    route_deadline, route_deadline_words, rpc_call, _to_int,
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

def deadline_scan(tx: dict, build_ts: float) -> dict:
    """Deadline words embedded in the built calldata + gate preview."""
    words = route_deadline_words(tx.get("data"), build_ts)
    deadline = words[-1] if words else None
    return {
        "deadline_words": words,
        "deadline_s_after_request": [w - int(build_ts) for w in words],
        "seconds_left": (deadline - time.time()) if deadline else None,
    }

def summarize(method: str, status: int, parsed: dict, raw: str,
              build_ts: float = 0.0) -> dict:
    data = parsed.get("data") or {}
    tx = find_tx(data if isinstance(data, dict) else {})
    rr = (data.get("routerResult") or {}) if isinstance(data, dict) else {}
    out = {
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
    if build_ts:
        out["deadline_scan"] = deadline_scan(tx, build_ts)
    return out

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
        results[method] = summarize(method, status, parsed, raw, build_ts=t0)
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
    return best or "GET"

def probe_route_deadlines(client) -> dict:
    """(item 5) Which route embeds the LONGER calldata deadline? Read-only."""
    ex = Executor(client=client, config=ExecutorConfig())
    out: dict = {}
    for src in ("flash", "swap"):
        try:
            built = ex._build_route(
                src, from_token=NATIVE_BNB, to_token=NVDAB, amount_base=AMOUNT,
                user_wallet=WALLET, slippage_percent=SLIPPAGE, flash_method="GET")
        except Exception as exc:
            out[src] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        scan = deadline_scan(built.tx, built.build_ts)
        out[src] = {"build_ts": built.build_ts, "elapsed_s": built.elapsed_s,
                    "data_len": len(built.calldata), **scan}
    secs = {k: v["seconds_left"] for k, v in out.items()
            if isinstance(v.get("seconds_left"), (int, float))}
    out["winner"] = max(secs, key=lambda k: secs[k]) if secs else "unknown"
    out["note"] = ("the source with the later in-calldata valid-until wins; "
                   "quote_and_swap(prefer='auto') falls back to it whenever the "
                   "preferred source fails the deadline gate")
    return out

def probe_deadline_params(client) -> dict:
    """(item 4) Does ANY request param move the in-calldata deadline? Read-only.

    The published Binance Web3 Trading API docs (web3.binance.com dev-docs,
    llms-full.txt) document NO deadline/expiry/TTL param for /quote, /swap or
    /quote-and-swap — only the server-side quoteId TTL (~30s).  This probe
    verifies empirically that candidate params are ignored/rejected and the
    embedded valid-until stays vendor-fixed at build+N seconds.
    """
    base = flash_params()
    soon = str(int(time.time()) + 600)
    variants = {
        "baseline": {},
        "deadline": {"deadline": soon},
        "validTo": {"validTo": soon},
        "quoteTtlSeconds": {"quoteTtlSeconds": "600"},
    }
    results: dict = {}
    for name, extra in variants.items():
        params = {**base, **extra}
        t0 = time.time()
        try:
            status, parsed, raw = call_qas(client, "GET", params)
        except Exception as exc:
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        data = parsed.get("data") or {}
        tx = find_tx(data if isinstance(data, dict) else {})
        results[name] = {
            "code": parsed.get("code"), "msg": parsed.get("msg"),
            "extra_param": extra, **deadline_scan(tx, t0),
        }
        time.sleep(0.3)   # stay under the 5 RPS/endpoint limit
    return results

def run_live(client, method: str, dry_run: bool = False) -> dict:
    """The unified hardened pipeline: quote_and_swap() does build->gate->
    simulate->sign->broadcast->settlement (broadcast guarded by env + --dry-run)."""
    load_wallet_key()
    signer = WalletSigner()
    acct = signer._account()
    assert acct.address == WALLET, f"key derives {acct.address}, not {WALLET}"
    config = ExecutorConfig.from_env()
    if dry_run:
        config.dry_run = True
    ex = Executor(client=client, config=config, signer=signer)
    if not dry_run:
        reasons = ex.broadcast_block_reasons()
        assert not reasons, f"broadcast blocked before start: {reasons}"
    bnb_wei = _to_int(rpc_call("eth_getBalance", [WALLET, "latest"]))
    assert bnb_wei > _to_int(AMOUNT) + 10**15, "balance too low for trade+gas"

    report = ex.quote_and_swap(
        from_token=NATIVE_BNB, to_token=NVDAB, amount_base=AMOUNT,
        user_wallet=WALLET, slippage_percent=SLIPPAGE, do_simulate=True,
        prefer="auto", flash_method=method)
    report["wallet"] = WALLET
    report["amount_base"] = AMOUNT
    report["slippage_percent"] = SLIPPAGE
    report["bnb_wei_before"] = bnb_wei
    report["bnb_wei_after"] = _to_int(rpc_call("eth_getBalance", [WALLET, "latest"]))
    save(report)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return report

def save(report: dict) -> None:
    (DATA / "demo_trade_flash_run.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str))

def main() -> int:
    ap = argparse.ArgumentParser(
        description="NVDAB demo trade via the unified hardened quote_and_swap path",
        epilog="guard envs: DRY_RUN=1 (default) / EXECUTOR_ENABLED / MAX_TRADE_USD / "
               "STOCKGAP_ALLOW_BROADCAST / STOCKGAP_FORCE_NO_BROADCAST=1 (default) / "
               "ROUTE_DEADLINE_MARGIN_S=3 / BROADCAST_DIRECT=0 — broadcast needs ALL "
               "of them open; --dry-run forces DRY_RUN for one run.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="read-only: quote shapes + deadline/param probe (never signs)")
    ap.add_argument("--live", action="store_true",
                    help="run the unified pipeline (hard-guarded)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --live: force DRY_RUN (nothing can leave the machine)")
    ap.add_argument("--method", choices=["POST", "GET"], default="GET",
                    help="Flash call method (GET is the documented one; POST fails)")
    args = ap.parse_args()
    api_key, secret, _, _ = load_credentials()
    client = BinanceWeb3Client(api_key, secret)
    if args.probe:
        run_probe(client)
        dl = probe_route_deadlines(client)
        params = probe_deadline_params(client)
        combined = {"route_deadline_compare": dl, "deadline_param_probe": params,
                    "ts": time.time()}
        (DATA / "flash_deadline_probe.json").write_text(
            json.dumps(combined, indent=2, ensure_ascii=False, default=str))
        print(json.dumps(combined, indent=2, ensure_ascii=False, default=str))
        return 0
    if not args.live:
        ap.error("choose --probe or --live")
    run_live(client, args.method, dry_run=args.dry_run)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
