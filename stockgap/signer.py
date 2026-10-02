"""HMAC-SHA256 request signer for the Binance Web3 API (OC gateway).

Signing spec (verified against the official docs dump
/home/lab-master/docs/.binance-llms-full.txt, "Step 3 — Sign the Request"):

    preHash    = timestamp + METHOD(UPPERCASE) + requestPath + body
    X-OC-SIGN  = Base64(HMAC-SHA256(preHash, secretKey))   # key = secretKey

Rules that matter (each one causes 40102 if violated):
- timestamp: ISO 8601 with milliseconds, UTC, e.g. ``2026-05-11T10:08:57.715Z``.
  This is NOT epoch milliseconds. The same string goes into the preHash and the
  ``X-OC-TIMESTAMP`` header.
- requestPath: the full path exactly as sent on the wire, INCLUDING the
  ``/build`` base-path prefix and the query string raw-encoded exactly as sent
  (spaces as ``%20``, not ``+``; parameter order preserved).
- body: the exact raw JSON string sent on the wire; empty string for GET.
  Never re-serialize JSON after signing.
- Method is UPPERCASED before hashing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Mapping, Optional, Union

__all__ = [
    "BUILD_PREFIX",
    "build_headers",
    "build_query",
    "iso_timestamp",
    "percent_encode",
    "pre_hash",
    "sign",
]

BUILD_PREFIX = "/build"


def iso_timestamp(dt: Optional[datetime] = None) -> str:
    """ISO 8601 UTC timestamp with milliseconds: ``2026-05-11T10:08:57.715Z``."""
    now = dt or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def percent_encode(value: Union[str, int, float, bool]) -> str:
    """Percent-encode one query component the way the wire format needs it.

    Spaces become ``%20`` (NOT ``+``) so the signed string matches the raw
    on-the-wire encoding. Safe chars mirror JS ``encodeURIComponent`` minus
    the unreserved RFC 3986 set.
    """
    from urllib.parse import quote

    if isinstance(value, bool):
        value = "true" if value else "false"
    return quote(str(value), safe="-_.~!*'()")


def build_query(params: Mapping[str, Union[str, int, float, bool]]) -> str:
    """Deterministic query string; key order preserved exactly as given."""
    return "&".join(f"{percent_encode(k)}={percent_encode(v)}" for k, v in params.items())


def pre_hash(timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """Build the preHash string: timestamp + METHOD + requestPath + body."""
    return timestamp + method.upper() + request_path + body


def sign(secret_key: str, prehash: str) -> str:
    """Base64(HMAC-SHA256(preHash, secretKey))."""
    digest = hmac.new(
        secret_key.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_headers(
    api_key: str,
    secret_key: str,
    method: str,
    request_path: str,
    body: str = "",
    timestamp: Optional[str] = None,
    recv_window: Optional[int] = None,
    nonce: Optional[str] = None,
) -> dict:
    """Return the full set of OC auth headers for one request.

    ``request_path`` must include the ``/build`` prefix and any query string,
    raw-encoded exactly as sent.
    """
    ts = timestamp or iso_timestamp()
    headers = {
        "X-OC-APIKEY": api_key,
        "X-OC-TIMESTAMP": ts,
        "X-OC-SIGN": sign(secret_key, pre_hash(ts, method, request_path, body)),
    }
    if recv_window is not None:
        headers["X-OC-RECV-WINDOW"] = str(recv_window)
    if nonce is not None:
        headers["X-OC-NONCE"] = nonce
    return headers
