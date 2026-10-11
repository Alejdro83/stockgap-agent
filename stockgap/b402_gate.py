"""Pay-per-call gate (x402/b402 style) for the StockGap MCP endpoint.

Modes (config/mcp.env, env vars win):
  free  — no charge (default, demo/judges).
  b402  — HTTP 402 with payment requirements; the buyer sends proof in `X-Payment`:
            provider=local_receipt: an on-chain tx hash (we verify the RECEIPT via plain RPC)
            provider=binance:       base64 JSON payload, verified+settled by Binance b402 v2.
Nothing here signs or broadcasts transactions. A proof never pays twice (table paid_calls).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import db  # shared WAL/busy_timeout connection factory

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "mcp.env"
DEFAULT_MERCHANT = "0x069Ac6d8cf3553416fE5a4f0410ebEb758A8F819"
DEFAULT_PRICE_WEI = 100_000_000_000_000  # 0.0001 BNB
DEFAULT_RPC = "https://bsc-dataseed.binance.org"
ERC20_TRANSFER = "0xa9059cbb"
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

KEYS = ["MCP_PAY_MODE", "B402_PROVIDER", "MCP_MERCHANT", "MCP_PRICE_WEI", "MCP_ASSET",
        "MCP_CHAIN_ID", "MCP_NETWORK", "MCP_RPC_URL", "MCP_MAX_BLOCK_AGE_S", "MCP_ERC8004_ID"]


@dataclass
class GateConfig:
    mode: str = "free"
    provider: str = "local_receipt"
    merchant: str = DEFAULT_MERCHANT
    price_wei: int = DEFAULT_PRICE_WEI
    asset: str = "native"
    network: str = "eip155:56"
    rpc_url: str = DEFAULT_RPC
    max_block_age_s: int = 3600
    identity: str = "erc8004:bsc-mainnet:REGISTRY_PLACEHOLDER#0"


def _int(v: Any, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def load_config(env: Optional[Mapping[str, str]] = None, path: Any = None) -> GateConfig:
    """Real env > config file > defaults. Invalid mode/provider fall back to the safe default."""
    env = os.environ if env is None else env
    path = Path(path or env.get("STOCKGAP_MCP_CONFIG") or DEFAULT_CONFIG)
    raw: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() in KEYS:
                    raw[k.strip()] = v.strip()
    except OSError:
        pass
    for k in KEYS:
        if k in env:
            raw[k] = env[k]
    mode = raw.get("MCP_PAY_MODE", "free").lower()
    provider = raw.get("B402_PROVIDER", "local_receipt").lower()
    return GateConfig(
        mode=mode if mode in ("free", "b402") else "free",
        provider=provider if provider in ("local_receipt", "binance") else "local_receipt",
        merchant=raw.get("MCP_MERCHANT", DEFAULT_MERCHANT),
        price_wei=_int(raw.get("MCP_PRICE_WEI"), DEFAULT_PRICE_WEI),
        asset=raw.get("MCP_ASSET", "native"),
        network=raw.get("MCP_NETWORK", "eip155:56"),
        rpc_url=raw.get("MCP_RPC_URL", DEFAULT_RPC),
        max_block_age_s=_int(raw.get("MCP_MAX_BLOCK_AGE_S"), 3600),
        identity=raw.get("MCP_ERC8004_ID", GateConfig.identity),
    )


# --------------------------------------------------------------------------- identity (ERC-8004 stub)
class IdentityStub:
    """Interface for the future ERC-8004 reputation write. No on-chain calls."""

    def __init__(self, identity: str):
        self.identity = identity

    def header_value(self) -> str:
        return self.identity

    def record_call(self, *, tool: str, payer: Optional[str], tx_hash: Optional[str]) -> None:
        """Hook for the reputation write (giveFeedback). Intentionally a no-op for now."""
        return None


# --------------------------------------------------------------------------- receipt verification
@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""
    payer: Optional[str] = None
    amount: int = 0


def json_rpc(url: str) -> Callable[[str, list], Any]:
    def call(method: str, params: list) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode())
        if "error" in out:
            raise RuntimeError(str(out["error"]))
        return out.get("result")
    return call


def _hex(v: Any) -> int:
    return int(v, 16) if isinstance(v, str) and v else 0


def verify_receipt(tx_hash: str, cfg: GateConfig, rpc: Callable[[str, list], Any],
                   now: Optional[float] = None) -> VerifyResult:
    """Verify a payment tx: status 0x1, to == merchant, value >= price, recent block."""
    if not isinstance(tx_hash, str) or not TX_RE.match(tx_hash):
        return VerifyResult(False, "invalid tx hash")
    merchant = cfg.merchant.lower()
    try:
        tx = rpc("eth_getTransactionByHash", [tx_hash])
        rcpt = rpc("eth_getTransactionReceipt", [tx_hash])
        if not tx or not rcpt:
            return VerifyResult(False, "transaction not found or no receipt yet")
        if rcpt.get("status") != "0x1":
            return VerifyResult(False, f"receipt status {rcpt.get('status')} (not successful)")
        if cfg.asset == "native":
            to, amount = (tx.get("to") or "").lower(), _hex(tx.get("value"))
        else:
            data = tx.get("input") or ""
            if (tx.get("to") or "").lower() != cfg.asset.lower() or not data.startswith(ERC20_TRANSFER) \
                    or len(data) < 10 + 128:
                return VerifyResult(False, "not a transfer of the accepted token")
            to = "0x" + data[10:74][-40:].lower()
            amount = int(data[74:138], 16)
        if to != merchant:
            return VerifyResult(False, "recipient is not the merchant")
        if amount < cfg.price_wei:
            return VerifyResult(False, f"insufficient amount ({amount} < {cfg.price_wei})")
        block = rpc("eth_getBlockByNumber", [rcpt.get("blockNumber"), False])
        age = (time.time() if now is None else now) - _hex((block or {}).get("timestamp"))
        if age > cfg.max_block_age_s:
            return VerifyResult(False, f"payment too old ({int(age)}s > {cfg.max_block_age_s}s)")
        return VerifyResult(True, payer=tx.get("from"), amount=amount)
    except Exception as exc:  # network/RPC failure => reject, never crash the endpoint
        return VerifyResult(False, f"RPC error: {type(exc).__name__}")


# --------------------------------------------------------------------------- idempotency
class PaidCalls:
    """SQLite table paid_calls: a proof id (tx hash / payload digest) is claimed exactly once."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as c:
            db.ensure_wal(c)  # once at startup, asserted (stockgap/db.py)
            c.execute("CREATE TABLE IF NOT EXISTS paid_calls (tx_hash TEXT PRIMARY KEY, payer TEXT, "
                      "amount TEXT, tool TEXT, provider TEXT, paid_at TEXT)")

    def _conn(self) -> sqlite3.Connection:
        # WAL + busy_timeout(10s) via the shared factory — paid_calls writes
        # (claim/release) must not 'database is locked' on the tick DB.
        return db.connect(self.db_path, timeout=10.0)

    @staticmethod
    def _key(proof_id: str) -> str:
        return proof_id.lower()

    def is_used(self, proof_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM paid_calls WHERE tx_hash=?", (self._key(proof_id),)).fetchone() is not None

    def claim(self, proof_id: str, payer: Optional[str], amount: int, tool: str, provider: str = "local_receipt") -> bool:
        with self._conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO paid_calls VALUES (?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                            (self._key(proof_id), payer, str(amount), tool, provider))
            return cur.rowcount == 1

    def release(self, proof_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM paid_calls WHERE tx_hash=?", (self._key(proof_id),))


# --------------------------------------------------------------------------- Binance b402 v2 adapter
class BinanceB402Adapter:
    """Native path: POST /api/v2/b402/{supported,verify,settle} (v2 only, never legacy v1)."""

    PREFIX = "/api/v2/b402"

    def __init__(self, client: Any):
        self.client = client

    def _post(self, op: str, body: Optional[dict] = None) -> dict:
        status, env, _ = self.client.request("POST", f"{self.PREFIX}/{op}", body=body or {})
        if status != 200 or env.get("code") not in (0, "0", "000000", None):
            return {"_error": f"HTTP {status} code={env.get('code')}"}
        return env.get("data") or {}

    def supported(self) -> dict:
        return self._post("supported")

    def verify(self, payload: dict, requirements: dict) -> dict:
        return self._post("verify", {"paymentPayload": payload, "paymentRequirements": requirements})

    def settle(self, payload: dict, requirements: dict) -> dict:
        return self._post("settle", {"paymentPayload": payload, "paymentRequirements": requirements})


def _default_binance() -> BinanceB402Adapter:
    from .client import BinanceWeb3Client, load_credentials

    key, secret, _, _ = load_credentials()
    return BinanceB402Adapter(BinanceWeb3Client(key, secret))


# --------------------------------------------------------------------------- the gate
@dataclass
class Decision:
    allowed: bool
    status: int = 200
    body: dict = field(default_factory=dict)
    payer: Optional[str] = None
    proof_id: Optional[str] = None


class Gate:
    def __init__(self, cfg: GateConfig, db_path: str, rpc: Optional[Callable] = None,
                 binance: Optional[BinanceB402Adapter] = None):
        self.cfg = cfg
        self.db_path = db_path
        self._rpc = rpc
        self._binance = binance
        self._binance_factory: Callable[[], BinanceB402Adapter] = _default_binance
        self.identity = IdentityStub(cfg.identity)

    def requirements(self, resource: str, tool: str) -> dict:
        return {"scheme": "exact", "network": self.cfg.network, "asset": self.cfg.asset,
                "payTo": self.cfg.merchant, "maxAmountRequired": str(self.cfg.price_wei),
                "resource": resource, "description": f"StockGap MCP: {tool} (pay per call)",
                "provider": self.cfg.provider, "maxTimeoutSeconds": self.cfg.max_block_age_s}

    def _deny(self, resource: str, tool: str, error: str) -> Decision:
        return Decision(False, 402, {"x402Version": 1, "error": error,
                                     "accepts": [self.requirements(resource, tool)]})

    def check(self, proof: Optional[str], tool: str, resource: str) -> Decision:
        if self.cfg.mode != "b402":
            return Decision(True)
        proof = (proof or "").strip()
        if not proof:
            return self._deny(resource, tool, "Payment required: send the payment proof in the X-Payment header")
        paid = PaidCalls(self.db_path)
        if self.cfg.provider == "binance":
            return self._check_binance(proof, tool, resource, paid)
        if not TX_RE.match(proof):
            return self._deny(resource, tool, "Invalid proof: expected a 0x… 32-byte tx hash")
        if paid.is_used(proof):
            return self._deny(resource, tool, "This tx hash was already used for a previous call")
        rpc = self._rpc or json_rpc(self.cfg.rpc_url)
        res = verify_receipt(proof, self.cfg, rpc)
        if not res.ok:
            return self._deny(resource, tool, f"Payment rejected: {res.reason}")
        if not paid.claim(proof, res.payer, res.amount, tool):  # lost a race with a concurrent replay
            return self._deny(resource, tool, "This tx hash was already used for a previous call")
        return Decision(True, payer=res.payer, proof_id=proof)

    def _check_binance(self, proof: str, tool: str, resource: str, paid: PaidCalls) -> Decision:
        try:
            payload = json.loads(base64.b64decode(proof, validate=True))
        except Exception:
            return self._deny(resource, tool, "Invalid proof: expected base64 JSON (b402 payload)")
        proof_id = "b402:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if paid.is_used(proof_id):
            return self._deny(resource, tool, "This payment was already used for a previous call")
        try:
            adapter = self._binance or self._binance_factory()
        except Exception:
            return self._deny(resource, tool, "Binance b402 provider unavailable: merchant credentials missing")
        reqs = self.requirements(resource, tool)
        v = adapter.verify(payload, reqs)
        if not v.get("isValid"):
            return self._deny(resource, tool, f"Payment rejected: {v.get('invalidReason') or v.get('_error') or 'invalid'}")
        if not paid.claim(proof_id, v.get("payer"), self.cfg.price_wei, tool, "binance"):
            return self._deny(resource, tool, "This payment was already used for a previous call")
        s = adapter.settle(payload, reqs)
        if not s.get("success"):
            paid.release(proof_id)  # nothing moved on-chain: let the buyer retry
            return self._deny(resource, tool, f"Settlement failed: {s.get('errorReason') or s.get('_error') or 'no detail'}")
        return Decision(True, payer=v.get("payer"), proof_id=proof_id)
