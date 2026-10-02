#!/usr/bin/env python3
"""Feature A probe: aggregator quotes for the 5 largest underlying tickers.

Probes GET /api/v1/dex/aggregator/quote (USDT -> equity token) for a small
sample and records every fee/slippage/price-impact field present in the quote
response — input to the net-executable-gap cost model. Full pipeline later.

Output: fixtures/quote_probe.json (raw envelopes + field inventory).
Never prints secret values. Staggered calls (rate limit 5 RPS/endpoint).
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockgap.client import BinanceWeb3Client, load_credentials  # noqa: E402
from stockgap.spread import extract_fee_slippage_fields  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"

USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"  # Binance-Peg USDT (18 dec)
# Public example address from the official docs — quoting only, nothing is signed.
PROBE_WALLET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
AMOUNT = str(1000 * 10 ** 18)  # 1,000 USDT in base units
N_TOP = 5


def top_underlying_tokens(n: int = N_TOP) -> list:
    """Top-n underlying tickers by market cap, one representative token each."""
    universe = json.loads((FIXTURES / "rwa_tokens.json").read_text())["data"]
    by_und = defaultdict(list)
    for t in universe:
        if t.get("underlyingTicker"):
            by_und[t["underlyingTicker"]].append(t)

    def max_mc(tokens):
        try:
            return max(float(t.get("marketCap") or 0) for t in tokens)
        except ValueError:
            return 0.0

    ranked = sorted(by_und.items(), key=lambda kv: max_mc(kv[1]), reverse=True)[:n]
    picked = []
    for ticker, tokens in ranked:
        tok = sorted(tokens, key=lambda t: t["tokenSymbol"])[0]
        picked.append({
            "underlying_ticker": ticker,
            "token_symbol": tok["tokenSymbol"],
            "platform_id": tok["platformId"],
            "token_contract": tok["tokenContractAddress"],
            "market_cap": max_mc(tokens),
        })
    return picked


def main() -> int:
    api_key, secret, _, _ = load_credentials()
    client = BinanceWeb3Client(api_key, secret)
    picks = top_underlying_tokens()
    print("PICKS " + json.dumps(picks), flush=True)

    results = []
    all_fields: set = set()
    for i, pick in enumerate(picks):
        if i:
            time.sleep(0.3)
        params = {
            "binanceChainId": "56",
            "fromTokenAddress": USDT_BSC,
            "toTokenAddress": pick["token_contract"],
            "amount": AMOUNT,
            "userWalletAddress": PROBE_WALLET,
        }
        status, parsed, raw = client.request(
            "GET", "/api/v1/dex/aggregator/quote", params=params)
        fields = extract_fee_slippage_fields(parsed)
        all_fields |= set(fields)
        code = parsed.get("code")
        print(f"[{pick['underlying_ticker']}] http={status} code={code} "
              f"fee/slippage fields={fields}", flush=True)
        results.append({
            **pick,
            "http_status": status,
            "code": code,
            "msg": parsed.get("msg"),
            "fee_slippage_fields": fields,
            "envelope": parsed if parsed else {"_raw": raw[:2000]},
        })

    out = {
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": "GET /build/api/v1/dex/aggregator/quote",
        "request_shape": {
            "binanceChainId": "56", "fromTokenAddress": "<USDT_BSC>",
            "toTokenAddress": "<token>", "amount": "<1000 USDT base units>",
            "userWalletAddress": "<probe wallet>",
        },
        "fee_slippage_fields_union": sorted(all_fields),
        "results": results,
    }
    (FIXTURES / "quote_probe.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print("SUMMARY " + json.dumps({"fee_slippage_fields": sorted(all_fields),
                                   "codes": [r["code"] for r in results]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
