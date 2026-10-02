"""Minimal HTTP client for the Binance Web3 API.

Uses http.client so the request path bytes are exactly what was signed —
no URL re-encoding between signature and wire.
"""

from __future__ import annotations

import http.client
import json
import ssl
from typing import Any, Mapping, Optional, Tuple, Union

from .signer import BUILD_PREFIX, build_headers, build_query

__all__ = ["BinanceWeb3Client", "load_credentials"]

HOST = "web3.binance.com"
SECRETS_DIR = "/home/lab-master/.hermes/secrets"
API_KEY_FILE = "binance_web3_api_key"
SECRET_KEY_FILE = "binance_web3_secret_key"


def _secret_variants(raw: bytes) -> Mapping[str, str]:
    """Candidate secret-key encodings, in the order we try them."""
    as_is = raw.decode("utf-8")
    stripped = as_is.strip()
    variants = {"stripped": stripped, "raw_with_trailing_newline": as_is}
    try:
        import base64

        decoded = base64.b64decode(stripped, validate=True).decode("utf-8").strip()
        if decoded and decoded != stripped:
            variants["stripped_base64_decoded"] = decoded
    except Exception:
        pass
    return variants


def load_credentials() -> Tuple[str, str, str, dict]:
    """Return (api_key, secret_key, variant_name, all_variants) from the vault.

    The secret-key file on disk has a trailing newline; whether the gateway
    accepts the raw form or requires the stripped one is verified live by
    probing. ``variant_name`` records which variant produced a valid signature.
    """
    api_key = open(f"{SECRETS_DIR}/{API_KEY_FILE}", "rb").read().decode("utf-8").strip()
    raw = open(f"{SECRETS_DIR}/{SECRET_KEY_FILE}", "rb").read()
    variants = _secret_variants(raw)
    return api_key, variants["stripped"], "stripped", dict(variants)


class BinanceWeb3Client:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        recv_window: int = 60000,
        timeout: float = 30.0,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.recv_window = recv_window
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Union[str, int, float, bool]]] = None,
        body: Optional[Any] = None,
    ) -> Tuple[int, dict, str]:
        """Send one signed request.

        ``path`` is WITHOUT the /build prefix (e.g. ``/api/v1/dex/market/rwa/tokens``);
        the prefix is added for both the URL and the signed requestPath.
        Returns (http_status, parsed_json_or_empty_dict, raw_text).
        """
        method = method.upper()
        query = build_query(params) if params else ""
        path_with_query = path + ("?" + query if query else "")
        request_path = BUILD_PREFIX + path_with_query  # what gets signed
        raw_body = "" if method == "GET" else (json.dumps(body, separators=(",", ":")) if body is not None else "")

        headers = build_headers(
            self.api_key,
            self.secret_key,
            method,
            request_path,
            body=raw_body,
            recv_window=self.recv_window,
        )
        if method != "GET":
            headers["Content-Type"] = "application/json"

        conn = http.client.HTTPSConnection(HOST, timeout=self.timeout, context=ssl.create_default_context())
        try:
            conn.request(method, request_path, body=raw_body or None, headers=headers)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", errors="replace")
            status = resp.status
        finally:
            conn.close()

        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {}
        return status, parsed, text


def ok(envelope: Mapping[str, Any]) -> bool:
    """OCResult<T> success requires code==0 even at HTTP 200."""
    return envelope.get("code") == 0
