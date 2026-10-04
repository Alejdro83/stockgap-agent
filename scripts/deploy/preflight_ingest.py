#!/usr/bin/env python3
"""Deploy-time fail-closed check of STOCKGAP_INGEST_TOKEN (hybrid plan v4).

Dashboard-mode containers accept rows over ``POST /ingest/rows`` behind this
token (see stockgap/ingest.py), so a missing/weak token must abort the deploy
BEFORE anything serves — not surface later as a runtime 503. Run by
scripts/deploy/entrypoint.sh in the dashboard branch only.

Contract (tested in tests/test_deploy_preflight.py):
- The token is read from the ENVIRONMENT ONLY — no argv, no files.
- Valid = present, >= 32 chars (same floor as stockgap.ingest.MIN_TOKEN_LEN)
  and printable ASCII with NO whitespace/control chars (ord 33..126).
- Invalid -> exit non-zero with ONE clear stderr line stating the FAILING
  PROPERTY (e.g. "too short") — NEVER the value, never a prefix of it.
- Valid -> exit 0 with no output at all.
"""
from __future__ import annotations

import os
import sys

ENV_VAR = "STOCKGAP_INGEST_TOKEN"
MIN_TOKEN_LEN = 32  # keep in sync with stockgap.ingest.MIN_TOKEN_LEN


def check(token: "str | None") -> "str | None":
    """None when valid, else the failing property — never the value itself."""
    if token is None:
        return f"{ENV_VAR} is not set"
    if not token:
        return f"{ENV_VAR} is empty"
    if len(token) < MIN_TOKEN_LEN:
        return f"{ENV_VAR} is too short (minimum {MIN_TOKEN_LEN} characters)"
    for ch in token:
        if not 33 <= ord(ch) <= 126:  # control, whitespace (incl. space), non-ASCII
            return (
                f"{ENV_VAR} has disallowed characters "
                "(printable ASCII only, no whitespace/control)"
            )
    return None


def main() -> int:
    problem = check(os.environ.get(ENV_VAR))
    if problem is not None:
        print(f"preflight_ingest: {problem}", file=sys.stderr)  # ONE line, no value
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
