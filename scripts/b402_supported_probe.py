#!/usr/bin/env python3
"""Live B402 readiness probe: POST /build/api/v2/b402/supported.

Sends the documented envelope {"body": {}} signed with the standard OC HMAC
headers, then saves the PUBLIC facilitator configuration (kinds, signers) to
docs/evidence/b402-supported-live.json. Credentials are read from the secrets
dir and never printed or written. Read-only: no verify, no settle, no funds.

Env: B402_API_KEY_FILE / B402_SECRET_KEY_FILE override the default key paths.
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockgap.signer import BUILD_PREFIX, build_headers  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SECRETS = Path("/home/lab-master/.hermes/secrets")
OUT = REPO / "docs" / "evidence" / "b402-supported-live.json"
PATH = BUILD_PREFIX + "/api/v2/b402/supported"


def main() -> int:
    key = Path(os.environ.get("B402_API_KEY_FILE", SECRETS / "binance_b402_api_key")).read_text().strip()
    sec = Path(os.environ.get("B402_SECRET_KEY_FILE", SECRETS / "binance_b402_secret_key")).read_text().strip()
    body = json.dumps({"body": {}}, separators=(",", ":"))
    headers = build_headers(key, sec, "POST", PATH, body=body)
    headers["Content-Type"] = "application/json"
    conn = http.client.HTTPSConnection("web3.binance.com", timeout=30, context=ssl.create_default_context())
    conn.request("POST", PATH, body=body, headers=headers)
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", errors="replace")
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        envelope = {"_raw": text[:500]}
    data = envelope.get("data") or {}
    evidence = {
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": "POST " + PATH,
        "request_body": {"body": {}},
        "http_status": resp.status,
        "code": envelope.get("code"),
        "kinds_count": len(data.get("kinds", [])),
        "kinds": data.get("kinds", []),
        "signers": data.get("signers"),
        "extensions": data.get("extensions"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = "HTTP %s code=%s kinds=%s -> %s" % (resp.status, evidence["code"], evidence["kinds_count"], OUT.relative_to(REPO))
    print(summary)
    return 0 if resp.status == 200 and envelope.get("code") == "000000000" else 1


if __name__ == "__main__":
    raise SystemExit(main())
