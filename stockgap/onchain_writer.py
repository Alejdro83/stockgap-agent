#!/usr/bin/env python3
"""On-chain anchor writer for StockGap oracle snapshots (BSC mainnet, chain 56).

This is the ONLY module in stockgap that signs and broadcasts EVM transactions
outside the aggregator swap path. It is deliberately SEPARATE from
``stockgap/executor.py``: the executor's ``_signable_tx`` refuses any ``to``
that is not the aggregator router and refuses contract-creation txs
(``to=None``) — that guard stays exactly as-is. Deploy and oracle-publish txs
are built, simulated and signed HERE instead.

Safety rules encoded in this module:

* LEGACY (type-0) txs only, ``chainId=56`` (EIP-155); ``eth_chainId`` is
  checked before every sign and anything != 56 aborts.
* ``eth_call`` + ``eth_estimateGas`` run as their own SIMULATE step BEFORE
  every ``eth_sendRawTransaction`` — a failing simulation aborts, nothing is
  broadcast. Broadcast goes over plain JSON-RPC.
* The publish path is pinned to the deployed PriceRegistry address; the deploy
  path sends ``to=None`` contract creation only.
* A receipt with status != 0x1 (or missing) raises :class:`PublishUnconfirmed`
  — callers (``oracle_score.OnchainFeedbackWriter``) issue a ``bsc:56:<txhash>``
  feedback_ref ONLY after confirmation. A claimed ref is never written early.
* Feature toggle ``ORACLE_PUBLISH=0|1`` (default 0 = mock DB ledger). This is
  an oracle-publishing toggle, fully separate from the swap guards in
  executor.py (EXECUTOR_ENABLED / DRY_RUN / STOCKGAP_ALLOW_BROADCAST / ...).

Key material: ``WALLET_PRIVATE_KEY`` is read from the environment AT CALL TIME
(same convention as ``executor.WalletSigner``) — never logged, never persisted,
never printed. The CLI additionally seeds that env var from the demo-wallet
keystore file used by scripts/demo_trade_flash.py when it is unset.

Price quantization (:func:`quantize_price_e8`): explicit ``Decimal`` x 1e8 with
``ROUND_HALF_EVEN`` (banker's rounding — no systematic upward bias across a
price series) to a non-negative integer that must fit uint256.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "CHAIN_ID",
    "DEFAULT_RPC",
    "DuplicateSnapshot",
    "LocalWalletSigner",
    "OnchainPublisher",
    "PUBLISH_SELECTOR",
    "PUBLISHED_SELECTOR",
    "PublishError",
    "PublishUnconfirmed",
    "REF_PREFIX",
    "SimulationFailed",
    "compile_price_registry",
    "default_rpc",
    "derive_snapshot_id",
    "encode_publish",
    "from_env",
    "publish_enabled",
    "quantize_price_e8",
]

CHAIN_ID = 56  # BSC mainnet — the only chain this module will sign for
DEFAULT_RPC = "https://bsc-dataseed.binance.org"
REF_PREFIX = "bsc:56:"
PRICE_SCALE = Decimal("100000000")  # 1e8 — priceE8 units
UINT256_MAX = 2 ** 256 - 1
MIN_GAS_PRICE_WEI = 1_000_000_000  # 1 gwei floor (BSC node minimum)
GAS_HEADROOM = 12 // 10            # +20% over the estimate

REPO = Path(__file__).resolve().parent.parent
REGISTRY_SOURCE = REPO / "contracts" / "PriceRegistry.sol"
ARTIFACT_PATH = REPO / "contracts" / "PriceRegistry.artifact.json"
DEPLOYMENT_PATH = REPO / "data" / "price_registry.json"
KEY_FILE = "/home/lab-master/.hermes/secrets/bnb_wallet_private_key"
KEY_ENV = "WALLET_PRIVATE_KEY"

# function selectors: first 4 bytes of keccak(signature)
try:
    from eth_utils.crypto import keccak as _keccak  # ships with eth_account
except ImportError:  # pragma: no cover - eth_utils is an eth_account dependency
    _keccak = None


def _selector(signature: str) -> str:
    if _keccak is None:
        raise RuntimeError("eth_utils not installed — cannot derive calldata selectors")
    return "0x" + _keccak(text=signature)[:4].hex()

PUBLISH_SELECTOR = _selector("publish(bytes32,uint256)")
PUBLISHED_SELECTOR = _selector("published(bytes32)")


# --------------------------------------------------------------------- errors
class PublishError(RuntimeError):
    """Base error for the on-chain anchor path."""


class SimulationFailed(PublishError):
    """eth_call / eth_estimateGas rejected the tx — nothing was broadcast."""


class DuplicateSnapshot(PublishError):
    """PriceRegistry.publish reverts for an already-published snapshotId."""


class PublishUnconfirmed(PublishError):
    """No receipt with status==0x1 — no feedback_ref may be issued."""


# ------------------------------------------------------------- quantization
def _decimal(value: Any, name: str = "price") -> Decimal:
    if isinstance(value, Decimal):
        out = value
    elif isinstance(value, float):
        out = Decimal(repr(value))  # never float.__str__ artifacts
    else:
        try:
            out = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, AttributeError) as exc:
            raise ValueError(f"{name}: not a number: {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"{name}: not finite: {value!r}")
    return out


def quantize_price_e8(value: Any) -> int:
    """Decimal price -> integer priceE8 (price x 1e8).

    Rounding: ROUND_HALF_EVEN (banker's rounding) to 8 decimal places — ties
    round to the even neighbor, so a long price series has no systematic
    upward bias. Negative prices and non-finite values are rejected; the
    result must fit uint256 (checked before any ABI encoding).
    """
    price = _decimal(value)
    if price < 0:
        raise ValueError(f"price: must be non-negative, got {price}")
    try:
        scaled = (price * PRICE_SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    except DecimalException as exc:  # overflow / precision traps -> clean error
        raise ValueError(f"price: {value!r} not quantizable to priceE8") from exc
    if scaled < 0 or scaled > UINT256_MAX:
        raise ValueError(f"price: {price} overflows uint256 in priceE8 units")
    return int(scaled)


# ------------------------------------------------------------- snapshot ids
def derive_snapshot_id(*parts: Any) -> str:
    """Deterministic bytes32 snapshot id: keccak of the joined identity parts.

    ``0x`` + 64 hex chars. Same parts (in order) always give the same id, which
    is what lets PriceRegistry reject duplicate publishes per snapshot.
    """
    text = "stockgap:oracle:v1|" + "|".join(str(p) for p in parts)
    if _keccak is None:
        raise RuntimeError("eth_utils not installed — cannot derive snapshot ids")
    return "0x" + _keccak(text=text).hex()


def _norm_bytes32(snapshot_id: str) -> str:
    sid = str(snapshot_id).strip().lower()
    if not sid.startswith("0x") or len(sid) != 66:
        raise ValueError(f"snapshotId must be 0x + 64 hex chars: {snapshot_id!r}")
    int(sid, 16)  # raises ValueError on non-hex
    return sid


def encode_publish(snapshot_id: str, price_e8: int) -> str:
    """ABI calldata for ``publish(bytes32,uint256)`` (static args, hand-padded)."""
    sid = _norm_bytes32(snapshot_id)
    price_e8 = int(price_e8)
    if not (0 <= price_e8 <= UINT256_MAX):
        raise ValueError(f"priceE8 out of uint256 range: {price_e8}")
    return PUBLISH_SELECTOR + sid[2:] + f"{price_e8:064x}"


def encode_published(snapshot_id: str) -> str:
    """ABI calldata for the ``published(bytes32)`` getter."""
    return PUBLISHED_SELECTOR + _norm_bytes32(snapshot_id)[2:]


# --------------------------------------------------------------------- RPC
def default_rpc(url: Optional[str] = None) -> Callable[[str, list], Any]:
    """Plain-JSON-RPC callable: raises RuntimeError on ``{"error": ...}``."""
    endpoint = url or os.environ.get("STOCKGAP_RPC_URL") or DEFAULT_RPC

    def call(method: str, params: list) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params}).encode()
        req = urllib.request.Request(endpoint, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.loads(resp.read().decode())
        if out.get("error"):
            raise RuntimeError(f"{method} failed: {out['error']}")
        return out.get("result")

    return call


def _to_int(value: Any) -> int:
    s = str(value).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s, 10)


# ------------------------------------------------------------------- signer
class LocalWalletSigner:
    """Signs LEGACY EVM txs locally with ``WALLET_PRIVATE_KEY`` (env at call time).

    Same convention as ``executor.WalletSigner``: the key is read from the
    environment on every access and never stored on the instance. The raw
    transaction is produced with eth_account (EIP-155 v/r/s, chainId 56).
    """

    def __init__(self, key_env: str = KEY_ENV):
        self.key_env = key_env

    def __repr__(self) -> str:  # never leak key material via repr/logs
        return f"<LocalWalletSigner key_env={self.key_env!r} (key redacted)>"

    @property
    def key(self) -> Optional[str]:
        return os.environ.get(self.key_env) or None

    @property
    def address(self) -> str:
        return self._account().address

    def _account(self):
        if not self.key:
            raise PublishError(f"{self.key_env} not set at runtime — signing unavailable")
        try:
            from eth_account import Account  # lazy: optional dependency
        except ImportError as exc:  # pragma: no cover - env dependent
            raise PublishError(f"eth_account not installed: {exc}") from exc
        return Account.from_key(self.key)

    def sign_tx(self, tx: Mapping[str, Any]) -> str:
        signed = self._account().sign_transaction(dict(tx))
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = raw.hex()
        return h if h.startswith("0x") else "0x" + h


# ---------------------------------------------------------------- compiler
def compile_price_registry(solc_version: str = "0.8.19") -> Dict[str, Any]:
    """Compile contracts/PriceRegistry.sol -> {"abi", "bytecode", "compiler"}.

    Uses py-solc-x (installing the pinned solc binary on first use). The
    artifact is cached at contracts/PriceRegistry.artifact.json.
    """
    try:
        import solcx
    except ImportError as exc:
        raise PublishError(
            "py-solc-x not installed (pip install py-solc-x) — cannot compile"
        ) from exc
    if not REGISTRY_SOURCE.is_file():
        raise PublishError(f"missing contract source: {REGISTRY_SOURCE}")
    installed = [str(v) for v in solcx.get_installed_solc_versions()]
    if solc_version not in installed:
        solcx.install_solc(solc_version)
    source = REGISTRY_SOURCE.read_text()
    out = solcx.compile_source(
        source, output_values=["abi", "bin"], solc_version=solc_version,
        optimize=True, optimize_runs=200,
    )
    key, data = next(iter(out.items()))
    artifact = {"abi": data["abi"], "bytecode": "0x" + data["bin"],
                "compiler": f"solc {solc_version} (py-solc-x, optimizer 200 runs)",
                "contract": key}
    ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2))
    return artifact


def load_artifact() -> Dict[str, Any]:
    if ARTIFACT_PATH.is_file():
        return json.loads(ARTIFACT_PATH.read_text())
    return compile_price_registry()


# ---------------------------------------------------------------- publisher
class OnchainPublisher:
    """Builds, simulates, signs and broadcasts PriceRegistry txs (legacy, chain 56).

    ``rpc`` is any ``(method, params) -> result`` callable (injectable for
    tests); ``signer`` exposes ``.address`` and ``.sign_tx(tx) -> raw hex``.
    Every send is preceded by its own eth_call + eth_estimateGas simulate step.
    """

    def __init__(self, rpc: Callable[[str, list], Any], signer: Any,
                 registry_address: Optional[str] = None,
                 chain_id: int = CHAIN_ID,
                 receipt_timeout_s: float = 120.0, poll_s: float = 2.0,
                 sim_gas_cap: int = 3_000_000,
                 record_path: Optional[Path] = DEPLOYMENT_PATH):
        self.rpc = rpc
        self.signer = signer
        self.registry_address = registry_address
        self.chain_id = chain_id
        self.receipt_timeout_s = receipt_timeout_s
        self.poll_s = poll_s
        self.sim_gas_cap = sim_gas_cap  # explicit cap: the node's default call
        #                                 gas cap trips its balance check
        self.record_path = Path(record_path) if record_path else None

    # ---- guards -----------------------------------------------------------
    def check_chain(self) -> None:
        chain = _to_int(self.rpc("eth_chainId", []))
        if chain != self.chain_id:
            raise PublishError(
                f"refusing to sign: RPC chainId={chain} != {self.chain_id} (BSC mainnet)")

    # ---- tx building + simulate ------------------------------------------
    def _prepare(self, to: Optional[str], data: str) -> Dict[str, Any]:
        """LEGACY tx dict (EIP-155): no maxFeePerGas mix-ins, ever.

        ``to`` is checksummed (eth_account rejects lowercase addresses — same
        pitfall executor._signable_tx documents for the swap path).
        """
        if to is not None:
            from eth_utils.address import to_checksum_address  # lazy dep
            to = to_checksum_address(str(to))
        nonce = _to_int(self.rpc("eth_getTransactionCount",
                                 [self.signer.address, "pending"]))
        gas_price = max(_to_int(self.rpc("eth_gasPrice", [])), MIN_GAS_PRICE_WEI)
        return {"nonce": int(nonce), "gasPrice": int(gas_price), "gas": 0,
                "to": to, "value": 0, "data": data, "chainId": self.chain_id}

    def _simulate(self, tx: Dict[str, Any]) -> None:
        """eth_call + eth_estimateGas BEFORE any send; sets tx['gas'] (+20%).

        The RPC call object hex-encodes numerics (geth TransactionArgs wants
        hexutil strings) and carries an explicit ``gas`` cap — without it the
        node simulates at its (huge) default call gas cap and the funds check
        fails spuriously. The signable tx dict keeps Python ints.
        """
        call = {"from": self.signer.address, "data": tx["data"],
                "value": hex(tx["value"]), "gasPrice": hex(tx["gasPrice"]),
                "nonce": hex(tx["nonce"]), "gas": hex(self.sim_gas_cap)}
        if tx.get("to") is not None:
            call["to"] = tx["to"]
        try:
            self.rpc("eth_call", [call, "latest"])
            gas = _to_int(self.rpc("eth_estimateGas", [call]))
        except RuntimeError as exc:
            msg = str(exc)
            if "duplicate snapshotId" in msg:
                raise DuplicateSnapshot(msg) from exc
            raise SimulationFailed(f"simulate rejected tx: {msg}") from exc
        tx["gas"] = int(gas) * GAS_HEADROOM

    def _send_and_confirm(self, tx: Dict[str, Any]) -> str:
        raw = self.signer.sign_tx(tx)
        tx_hash = self.rpc("eth_sendRawTransaction", [raw])
        receipt = self.wait_receipt(tx_hash)
        if not receipt or receipt.get("status") != "0x1":
            status = (receipt or {}).get("status", "no receipt")
            raise PublishUnconfirmed(
                f"{tx_hash}: receipt status {status} — no feedback_ref issued")
        return str(tx_hash)

    def wait_receipt(self, tx_hash: str) -> Optional[dict]:
        deadline = time.time() + self.receipt_timeout_s
        while time.time() < deadline:
            receipt = self.rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                return receipt
            time.sleep(self.poll_s)
        return None

    # ---- deploy -----------------------------------------------------------
    def deploy(self) -> Dict[str, Any]:
        """Compile + deploy PriceRegistry (to=None contract creation).

        Returns {"address", "tx_hash"} ONLY for a confirmed receipt with code
        on chain; records data/price_registry.json for later publishes.
        """
        self.check_chain()
        artifact = load_artifact()
        tx = self._prepare(None, artifact["bytecode"])
        self._simulate(tx)
        tx_hash = self._send_and_confirm(tx)
        receipt = self.wait_receipt(tx_hash) or {}
        address = receipt.get("contractAddress")
        code = self.rpc("eth_getCode", [address, "latest"]) if address else "0x"
        if not address or not code or code == "0x":
            raise PublishUnconfirmed(f"{tx_hash}: no contract code at {address}")
        self.registry_address = address
        record = {"address": address, "tx_hash": tx_hash, "chain_id": self.chain_id,
                  "compiler": artifact.get("compiler"),
                  "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if self.record_path:  # tests inject tmp paths — never the live record
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            self.record_path.write_text(json.dumps(record, indent=2))
        return {"address": address, "tx_hash": tx_hash}

    # ---- publish ----------------------------------------------------------
    def is_published(self, snapshot_id: str) -> bool:
        out = self.rpc("eth_call", [{"to": self.registry_address,
                                     "data": encode_published(snapshot_id)}, "latest"])
        if not out or out == "0x":
            raise PublishError(
                f"registry {self.registry_address}: empty eth_call response — "
                f"no contract code at that address?")
        return bool(_to_int(out))

    def publish(self, snapshot_id: str, price_e8: int) -> str:
        """Anchor ONE snapshot: simulate -> sign -> broadcast -> confirm.

        Returns the tx hash ONLY after receipt status==0x1; a duplicate
        snapshotId reverts (mapping check in the contract) before broadcast.
        """
        if not self.registry_address:
            raise PublishError("registry_address not set — deploy PriceRegistry first")
        self.check_chain()
        sid = _norm_bytes32(snapshot_id)
        price_int = price_e8 if isinstance(price_e8, int) else quantize_price_e8(price_e8)
        data = encode_publish(sid, price_int)
        if self.is_published(sid):
            raise DuplicateSnapshot(f"snapshotId {sid} already published")
        tx = self._prepare(self.registry_address, data)
        self._simulate(tx)
        return self._send_and_confirm(tx)


# -------------------------------------------------------------------- toggle
def publish_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """ORACLE_PUBLISH=0|1 feature toggle. Default 0 (mock DB ledger)."""
    env = os.environ if env is None else env
    return str(env.get("ORACLE_PUBLISH", "0")).strip().lower() in ("1", "true", "yes", "on")


def load_deployment() -> Dict[str, Any]:
    try:
        return json.loads(DEPLOYMENT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def from_env(registry_address: Optional[str] = None,
             rpc_url: Optional[str] = None) -> OnchainPublisher:
    """Publisher wired from the environment + the deployment record.

    Registry address resolution: explicit arg > PRICE_REGISTRY_ADDRESS env >
    data/price_registry.json (written by deploy()).
    """
    address = (registry_address or os.environ.get("PRICE_REGISTRY_ADDRESS")
               or load_deployment().get("address"))
    return OnchainPublisher(default_rpc(rpc_url), LocalWalletSigner(),
                            registry_address=address)


# --------------------------------------------------------------- BscScan (best-effort)
def verify_bscscan(contract_address: str, source: str, contract_name: str = "PriceRegistry",
                   compiler: str = "v0.8.19+commit.7dd6d404", optimizer_runs: int = 200) -> Dict[str, Any]:
    """Best-effort flat-source verification (Etherscan V2 API, chainid=56).

    Requires BSCSCAN_API_KEY / ETHERSCAN_API_KEY in env; without a key this is
    skipped honestly (returns {"skipped": True, ...}) — never a claimed verify.
    """
    key = os.environ.get("BSCSCAN_API_KEY") or os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        return {"skipped": True, "reason": "no BSCSCAN_API_KEY/ETHERSCAN_API_KEY in env"}
    body = urllib.parse.urlencode({
        "chainid": "56", "module": "contract", "action": "verifysourcecode",
        "apikey": key, "contractaddress": contract_address, "sourceCode": source,
        "codeformat": "solidity-single-file", "contractname": contract_name,
        "compilerversion": compiler, "optimizationUsed": "1",
        "runs": str(optimizer_runs), "evmversion": "default",
    }).encode()
    req = urllib.request.Request("https://api.etherscan.io/v2/api", data=body)
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    return {"skipped": False, "response": out}


# ------------------------------------------------------------------------ CLI
def _seed_key_env() -> None:
    """CLI convenience: seed WALLET_PRIVATE_KEY from the demo-wallet keystore
    file (same convention as scripts/demo_trade_flash.py) when env is unset.
    The key value is NEVER printed/logged."""
    if os.environ.get(KEY_ENV) or not Path(KEY_FILE).is_file():
        return
    os.environ[KEY_ENV] = Path(KEY_FILE).read_text().strip()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="StockGap on-chain oracle writer (BSC 56)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compile", help="compile PriceRegistry.sol -> artifact JSON")
    sub.add_parser("deploy", help="deploy PriceRegistry to BSC MAINNET (real tx)")
    p_pub = sub.add_parser("publish-latest",
                           help="anchor ONE snapshot row (representative underlying)")
    p_pub.add_argument("--db", default=str(REPO / "data" / "spread_ticks.db"))
    p_pub.add_argument("--ticker", default="ACN",
                       help="representative underlying ticker (default: ACN / ACNon)")
    p_pub.add_argument("--snapshot-id", type=int, default=None,
                       help="oracle_predictions snapshot_id (default: latest)")
    args = ap.parse_args(argv)

    if args.cmd == "compile":
        artifact = compile_price_registry()
        print(json.dumps({"compiled": True, "compiler": artifact["compiler"],
                          "bytecode_bytes": (len(artifact["bytecode"]) - 2) // 2,
                          "artifact": str(ARTIFACT_PATH)}, indent=2))
        return 0

    _seed_key_env()
    if not os.environ.get(KEY_ENV):
        print(json.dumps({"error": f"{KEY_ENV} not set and keystore file missing — STOP"}))
        return 1

    if args.cmd == "deploy":
        out = from_env(registry_address=None).deploy()
        verify = verify_bscscan(out["address"], REGISTRY_SOURCE.read_text())
        print(json.dumps({
            "deployed": True, "chain": "BSC mainnet (56)",
            "contract_address": out["address"], "deploy_tx_hash": out["tx_hash"],
            "bscscan_address": f"https://bscscan.com/address/{out['address']}",
            "bscscan_tx": f"https://bscscan.com/tx/{out['tx_hash']}",
            "verification": verify}, indent=2))
        return 0

    # publish-latest: one representative row from the latest real snapshot
    import sqlite3

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    sid = args.snapshot_id
    if sid is None:
        sid = con.execute("SELECT MAX(snapshot_id) FROM oracle_predictions").fetchone()[0]
    row = con.execute(
        "SELECT underlying_ticker, token_symbol, implied_open_price, cutoff_ts"
        " FROM oracle_predictions WHERE snapshot_id = ? AND underlying_ticker = ?"
        " LIMIT 1", (sid, args.ticker)).fetchone()
    if not row:
        print(json.dumps({"error": f"no row for ticker {args.ticker!r} in snapshot {sid}"}))
        return 1
    ticker, symbol, price, cutoff_ts = row
    snap_id = derive_snapshot_id(f"snapshot-{sid}", cutoff_ts, ticker)
    price_e8 = quantize_price_e8(price)
    pub = from_env()
    tx_hash = pub.publish(snap_id, price_e8)
    print(json.dumps({
        "published": True, "snapshot_id_db": sid, "ticker": ticker,
        "token_symbol": symbol, "price": str(price), "price_e8": price_e8,
        "bytes32_snapshot_id": snap_id, "cutoff_ts": cutoff_ts,
        "tx_hash": tx_hash, "feedback_ref": f"{REF_PREFIX}{tx_hash}",
        "bscscan_tx": f"https://bscscan.com/tx/{tx_hash}",
        "bscscan_address": f"https://bscscan.com/address/{pub.registry_address}",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
