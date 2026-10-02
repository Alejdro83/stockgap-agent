#!/usr/bin/env python3
"""Day-0 live verification + fixture capture for the Binance Web3 RWA endpoints.

Steps:
1. Probe which secret-key encoding variant produces a valid signature (40101/40102
   vs code==0 distinguishes credential/signature problems).
2. Discover request params for rwa/price and rwa/underlying-market empirically
   (the docs dump has no param tables for the RWA group).
3. Fetch the three target endpoints and save the raw OCResult<T> envelopes as
   fixtures/, plus MANIFEST.json and ops_log.json metadata.
4. Locate the shares-multiplier field in the token list response.

Never prints secret values. Rate limit: 5 RPS/endpoint — we sleep 0.25s between calls.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockgap.client import BinanceWeb3Client, load_credentials  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"
FIXTURES.mkdir(exist_ok=True)

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

ops_log: list = []


def log(step: str, **kw):
    entry = {"step": step, **kw}
    ops_log.append(entry)
    print(f"[{step}] " + json.dumps({k: v for k, v in kw.items() if k != "body"}), flush=True)


def call(client, path, params=None):
    time.sleep(0.25)
    status, parsed, raw = client.request("GET", path, params=params)
    return status, parsed, raw


def probe_secret_variants(api_key: str, variants: dict) -> tuple:
    """Return (variant_name, secret, envelope) of the first variant with a valid signature."""
    path = "/api/v1/dex/market/rwa/tokens"
    for name, secret in variants.items():
        client = BinanceWeb3Client(api_key, secret)
        status, parsed, _ = call(client, path)
        code = parsed.get("code")
        log("probe_secret_variant", variant=name, http_status=status, code=code, msg=parsed.get("msg"))
        if code == 0 or (code is not None and code not in (40101, 40102, 40103)):
            # code 0 = fully working; any non-auth error code also proves the
            # signature verified (auth errors are only 40101/40102/40103).
            return name, secret, parsed
    return None, None, None


def discover_params(client, path: str, candidates: list) -> tuple:
    """Try param sets until one yields code==0. Returns (params, envelope, raw, attempts)."""
    attempts = []
    status, parsed, raw = call(client, path)
    attempts.append({"params": {}, "code": parsed.get("code"), "msg": parsed.get("msg")})
    log("probe_params", path=path, params={}, code=parsed.get("code"), msg=parsed.get("msg"))
    if parsed.get("code") == 0:
        return {}, parsed, raw, attempts
    for params in candidates:
        status, parsed, raw = call(client, path, params=params)
        attempts.append({"params": params, "code": parsed.get("code"), "msg": parsed.get("msg")})
        log("probe_params", path=path, params=params, code=parsed.get("code"), msg=parsed.get("msg"))
        if parsed.get("code") == 0:
            return params, parsed, raw, attempts
    return None, parsed, raw, attempts


def save_fixture(name: str, raw_text: str, meta: dict):
    (FIXTURES / f"{name}.json").write_text(raw_text, encoding="utf-8")
    meta_path = FIXTURES / "MANIFEST.json"
    manifest = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    manifest[name] = meta
    meta_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def find_multiplier_fields(obj, prefix="") -> set:
    """Recursively find field names mentioning share/multiplier conversion."""
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if re.search(r"multiplier|shares?Per|perShare|underlyingAmount", k, re.I):
                found.add(p)
            found |= find_multiplier_fields(v, p)
    elif isinstance(obj, list):
        for item in obj[:5]:
            found |= find_multiplier_fields(item, prefix + "[]")
    return found


def main() -> int:
    api_key, secret, _, variants = load_credentials()
    log("load_credentials", api_key_len=len(api_key), secret_variants=list(variants))

    variant_name, secret, _ = probe_secret_variants(api_key, variants)
    if variant_name is None:
        log("fatal", reason="no secret variant produced a valid signature")
        (FIXTURES / "ops_log.json").write_text(json.dumps(ops_log, indent=2))
        return 1
    client = BinanceWeb3Client(api_key, secret)

    # ---- 1. rwa/tokens (no params expected) ----
    status, parsed, raw = call(client, "/api/v1/dex/market/rwa/tokens")
    log("fetch", path="/api/v1/dex/market/rwa/tokens", http_status=status,
        code=parsed.get("code"), success=parsed.get("success"))
    save_fixture("rwa_tokens", raw, {
        "endpoint": "GET /build/api/v1/dex/market/rwa/tokens",
        "params": {}, "http_status": status, "code": parsed.get("code"),
        "secret_variant": variant_name,
    })

    tokens_data = parsed.get("data") or {}
    token_list = None
    if isinstance(tokens_data, dict):
        for v in tokens_data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                token_list = v
                break
    elif isinstance(tokens_data, list):
        token_list = tokens_data

    sample = token_list[0] if token_list else {}
    sym = sample.get("symbol") or sample.get("tokenSymbol") or sample.get("symbolName") or "AAPLx"
    addr = sample.get("tokenContractAddress") or sample.get("contractAddress") or sample.get("tokenAddress") or ""
    underlying_sym = (sample.get("underlying") or {}).get("symbol") if isinstance(sample.get("underlying"), dict) else None
    underlying_sym = underlying_sym or sample.get("underlyingSymbol") or re.sub(r"(on|x|B)$", "", sym)
    log("sample_token", symbol=sym, address=addr, underlying=underlying_sym, fields=sorted(sample.keys()))

    mult_fields = find_multiplier_fields(parsed)
    log("multiplier_fields", fields=sorted(mult_fields))

    # ---- 2. rwa/price (params undocumented — discover empirically) ----
    price_candidates = [
        {"symbols": sym},
        {"symbol": sym},
        {"symbols": sym, "binanceChainId": "56"},
        {"binanceChainId": "56", "symbol": sym},
        {"tokenContractAddress": addr, "binanceChainId": "56"},
        {"tokenContractAddresses": addr, "binanceChainId": "56"},
        {"symbol": underlying_sym},
        {"symbols": underlying_sym},
    ]
    price_params, price_env, price_raw, price_attempts = discover_params(
        client, "/api/v1/dex/market/rwa/price", price_candidates)
    if price_params is not None:
        save_fixture("rwa_price", price_raw, {
            "endpoint": "GET /build/api/v1/dex/market/rwa/price",
            "params": price_params, "http_status": 200,
            "code": price_env.get("code"), "secret_variant": variant_name,
            "discovered_by": "live probing (no param table in docs)",
        })
        mult_fields |= find_multiplier_fields(price_env)
    log("price_result", working_params=price_params)

    # ---- 3. rwa/underlying-market ----
    um_candidates = [
        {"symbol": underlying_sym},
        {"symbols": underlying_sym},
        {"symbol": sym},
        {"symbol": underlying_sym, "binanceChainId": "56"},
        {"underlyingSymbol": underlying_sym},
        {"tokenSymbol": sym},
        {"symbol": underlying_sym, "market": "US"},
    ]
    um_params, um_env, um_raw, um_attempts = discover_params(
        client, "/api/v1/dex/market/rwa/underlying-market", um_candidates)
    if um_params is not None:
        save_fixture("rwa_underlying_market", um_raw, {
            "endpoint": "GET /build/api/v1/dex/market/rwa/underlying-market",
            "params": um_params, "http_status": 200,
            "code": um_env.get("code"), "secret_variant": variant_name,
            "discovered_by": "live probing (no param table in docs)",
        })
    log("underlying_market_result", working_params=um_params)

    (FIXTURES / "ops_log.json").write_text(json.dumps(ops_log, indent=2), encoding="utf-8")

    summary = {
        "secret_variant_working": variant_name,
        "rwa_tokens_code": parsed.get("code"),
        "rwa_price_params": price_params,
        "rwa_underlying_market_params": um_params,
        "multiplier_fields": sorted(mult_fields),
        "sample_token_fields": sorted(sample.keys()),
        "token_count": len(token_list) if token_list else 0,
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
