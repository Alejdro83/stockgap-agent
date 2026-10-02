#!/usr/bin/env python3
"""Fetch rwa/underlying-market with the params its error messages demanded, then
inspect tokenToShareRatio and the price envelope shape."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stockgap.client import BinanceWeb3Client, load_credentials  # noqa: E402

api_key, secret, variant, _ = load_credentials()
c = BinanceWeb3Client(api_key, secret)

params = {"binanceChainId": "56", "tokenContractAddress": "0x7af44d51d1fb88c5b74fc71d3cba649bb8099d14"}
status, parsed, raw = c.request("GET", "/api/v1/dex/market/rwa/underlying-market", params=params)
print("underlying-market:", status, parsed.get("code"), parsed.get("msg"), parsed.get("success"))
if parsed.get("code") == 0:
    Path("fixtures/rwa_underlying_market.json").write_text(raw, encoding="utf-8")
    mf = Path("fixtures/MANIFEST.json")
    m = json.loads(mf.read_text())
    m["rwa_underlying_market"] = {
        "endpoint": "GET /build/api/v1/dex/market/rwa/underlying-market",
        "params": params, "http_status": status, "code": parsed.get("code"),
        "secret_variant": variant,
        "discovered_by": "live probing (no param table in docs)",
    }
    mf.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")

tok = json.loads(Path("fixtures/rwa_tokens.json").read_text())
data = tok["data"]
lst = next(v for v in data.values() if isinstance(v, list)) if isinstance(data, dict) else data
print("envelope keys:", sorted(tok.keys()))
print("ratio samples:", [(t["tokenSymbol"], t["tokenToShareRatio"]) for t in lst[:6]])
print("ratio distribution:", Counter(str(t.get("tokenToShareRatio")) for t in lst).most_common(5))
print("sample token:", json.dumps(lst[0], ensure_ascii=False)[:600])
pr = json.loads(Path("fixtures/rwa_price.json").read_text())
print("price envelope:", sorted(pr.keys()), "code", pr.get("code"))
print("price data:", json.dumps(pr.get("data"), ensure_ascii=False)[:500])
um = json.loads(Path("fixtures/rwa_underlying_market.json").read_text()) if Path("fixtures/rwa_underlying_market.json").exists() else {}
print("um data:", json.dumps(um.get("data"), ensure_ascii=False)[:500])
