#!/usr/bin/env python3
"""StockGap trade executor: quote -> (simulate) -> broadcast pipeline.

Supports both aggregator execution modes (``executionMode`` in the quote):

* ``SWAP`` — ``GET /quote`` -> ``GET /swap`` -> sign ``data.tx`` -> optional
  ``POST /pre-transaction/simulate`` -> ``POST /pre-transaction/broadcast-transaction``.
* ``RFQ``  — ``GET /quote`` (``userWalletAddress`` REQUIRED) -> ``GET /swap``
  -> sign ``rfq.typedDataToSign`` (EIP-712) -> ``POST /aggregator/order/submit``
  -> poll ``GET /aggregator/order/{orderId}``.  RFQ has no tx to simulate.

Safety model (all layers must be open before anything leaves the machine):

* ``EXECUTOR_ENABLED`` (default False)  — master switch for the executor.
* ``DRY_RUN`` (default True)            — never broadcast while set.
* ``MAX_TRADE_USD`` (default 5)        — per-trade notional cap.
* ``STOCKGAP_ALLOW_BROADCAST`` (default False) — explicit broadcast opt-in.
* ``STOCKGAP_FORCE_NO_BROADCAST`` (default "1") — unconditional no-broadcast
  guard used for this run; while truthy, broadcast is impossible no matter
  what the other toggles say.

Wallet signing is a pluggable hook (:class:`WalletSigner`) that reads the env
var ``WALLET_PRIVATE_KEY`` at RUNTIME — the key never lives in code or config.
The Binance Web3 quote/simulate endpoints need only the HMAC API key, so the
full pipeline minus signing/broadcast runs keyless-wallet.

API error codes handled explicitly:
  40401 QUOTE_EXPIRED        quoteId TTL ~30s -> re-quote and retry
  40462 SWAP_QUOTE_MISMATCH  /swap params must match /quote exactly
  40463 price impact guard   raised as PriceImpactBlocked
  42901 ORDER_SUBMIT_IN_PROGRESS  duplicate requestId; the UUID is persisted
                            to disk BEFORE first submit so retries reuse it.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .client import BinanceWeb3Client, load_credentials

__all__ = [
    "ApiError",
    "BroadcastBlocked",
    "DuplicateRequest",
    "Executor",
    "ExecutorConfig",
    "PriceImpactBlocked",
    "QuoteExpired",
    "RequestIdStore",
    "SwapQuoteMismatch",
    "TradeTooLarge",
    "WalletKeyUnavailable",
    "WalletSigner",
]

CHAIN_ID = "56"
DEFAULT_WALLET = "0x069Ac6d8cf3553416fE5a4f0410ebEb758A8F819"
NATIVE_BNB = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
WEI = 10 ** 18
QUOTE_TTL_SECONDS = 30

ERR_QUOTE_EXPIRED = "40401"
ERR_SWAP_QUOTE_MISMATCH = "40462"
ERR_PRICE_IMPACT = "40463"
ERR_DUPLICATE_REQUEST = "42901"

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "executor_state.json"

EVM_CHAIN_ID = 56  # BSC mainnet; the /swap tx dict carries NO chainId
RPC_URL = os.environ.get("STOCKGAP_RPC_URL", "https://bsc-dataseed.binance.org")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class ApiError(RuntimeError):
    def __init__(self, code: Any, msg: str, raw: Optional[dict] = None):
        super().__init__(f"[{code}] {msg}")
        self.code = str(code)
        self.msg = msg
        self.raw = raw or {}


class QuoteExpired(ApiError):
    """40401 — quoteId TTL (~30 s) elapsed; re-quote instead of retrying."""


class SwapQuoteMismatch(ApiError):
    """40462 — /swap params differ from the cached /quote params."""


class PriceImpactBlocked(ApiError):
    """40463 — price-impact protection tripped."""


class DuplicateRequest(ApiError):
    """42901 — a submit with the same requestId is already in progress."""

    def __init__(self, code: Any, msg: str, request_id: str):
        super().__init__(code, msg)
        self.request_id = request_id


class TradeTooLarge(RuntimeError):
    pass


class WalletKeyUnavailable(RuntimeError):
    pass


class BroadcastBlocked(RuntimeError):
    def __init__(self, reasons: List[str]):
        super().__init__("broadcast blocked: " + "; ".join(reasons))
        self.reasons = reasons


# --------------------------------------------------------------------------
# config + signing hook + requestId persistence
# --------------------------------------------------------------------------

def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ExecutorConfig:
    executor_enabled: bool = False      # EXECUTOR_ENABLED
    dry_run: bool = True                # DRY_RUN
    max_trade_usd: float = 5.0          # MAX_TRADE_USD
    allow_broadcast: bool = False       # STOCKGAP_ALLOW_BROADCAST
    force_no_broadcast: bool = True     # STOCKGAP_FORCE_NO_BROADCAST (this run)
    wallet_address: str = DEFAULT_WALLET

    @classmethod
    def from_env(cls) -> "ExecutorConfig":
        return cls(
            executor_enabled=_env_bool("EXECUTOR_ENABLED", "0"),
            dry_run=_env_bool("DRY_RUN", "1"),
            max_trade_usd=float(os.environ.get("MAX_TRADE_USD", "5")),
            allow_broadcast=_env_bool("STOCKGAP_ALLOW_BROADCAST", "0"),
            force_no_broadcast=_env_bool("STOCKGAP_FORCE_NO_BROADCAST", "1"),
            wallet_address=os.environ.get("STOCKGAP_WALLET", DEFAULT_WALLET),
        )


class WalletSigner:
    """Pluggable wallet-signing hook. Reads WALLET_PRIVATE_KEY at call time.

    The Binance Web3 API never sees the key; signing is fully local (eth_account
    when available). Swap flow signs an EVM tx dict; RFQ flow signs EIP-712
    typed data (``eth_signTyped_data_v4``).
    """

    def __init__(self, key_env: str = "WALLET_PRIVATE_KEY"):
        self.key_env = key_env

    @property
    def key(self) -> Optional[str]:
        return os.environ.get(self.key_env) or None

    @property
    def available(self) -> bool:
        return self.key is not None

    def _account(self):
        if not self.key:
            raise WalletKeyUnavailable(
                f"{self.key_env} not set at runtime — signing unavailable")
        try:
            from eth_account import Account  # lazy: optional dependency
        except ImportError as exc:  # pragma: no cover - env dependent
            raise WalletKeyUnavailable(f"eth_account not installed: {exc}")
        return Account.from_key(self.key)

    def sign_evm_tx(self, tx: Dict[str, Any]) -> str:
        """Sign the unsigned ``data.tx`` payload from /swap; returns raw hex."""
        acct = self._account()
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = raw.hex()
        return h if h.startswith("0x") else "0x" + h

    def sign_eip712(self, typed_data: Dict[str, Any]) -> str:
        """Sign rfq.typedDataToSign (EIP-712) with the user wallet."""
        acct = self._account()
        return acct.sign_typed_data(
            full_message=typed_data if "types" in typed_data
            else typed_data.get("message", typed_data)
        ).signature.hex()


class RequestIdStore:
    """Persists RFQ requestId UUIDs to disk BEFORE the first submit.

    ``/order/submit`` is idempotent per requestId (30 min). If a retry comes
    back 42901 (ORDER_SUBMIT_IN_PROGRESS) the persisted UUID is reused — a
    fresh UUID would create a second order.
    """

    def __init__(self, path: Path = STATE_PATH):
        self.path = Path(path)

    def _load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def get_or_create(self, kind: str, order_key: str) -> str:
        state = self._load()
        entry = state.get(kind, {})
        if entry.get("order_key") == order_key and entry.get("request_id"):
            return entry["request_id"]
        request_id = str(uuid.uuid4())
        state[kind] = {"order_key": order_key, "request_id": request_id,
                       "created_at": time.time(), "sent": False}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2))
        return request_id

    def mark_sent(self, kind: str) -> None:
        state = self._load()
        if kind in state:
            state[kind]["sent"] = True
            self.path.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# JSON-RPC helpers (nonce / gas price / chain check before signing)
# --------------------------------------------------------------------------

ROUTER_ADDRESS = "0xB44446b0c8E56988c34f7Ff73Ae904982b5FdDA5"  # aggregator router


def _to_int(value: Any, default: Any = ...) -> int:
    """API values mix hex ('0x...') and decimal strings — normalize both.

    ``int(s, 0)`` is a trap here: decimal strings with leading zeros would be
    parsed as octal. Use base 16 for 0x-prefixed, base 10 otherwise.
    """
    if value is None:
        if default is ...:
            raise ValueError("missing required numeric tx field")
        return int(default)
    s = str(value).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s, 10)


def rpc_call(method: str, params: List[Any], rpc_url: Optional[str] = None) -> Any:
    """Minimal JSON-RPC call against the BSC public endpoint."""
    url = rpc_url or RPC_URL
    payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        parsed = json.loads(resp.read().decode())
    if parsed.get("error"):
        raise RuntimeError(f"{method} failed: {parsed['error']}")
    return parsed.get("result")


def fetch_nonce(address: str, rpc_url: Optional[str] = None) -> int:
    """eth_getTransactionCount(address, 'pending') — pending avoids collisions."""
    return _to_int(rpc_call("eth_getTransactionCount", [address, "pending"], rpc_url))


def fetch_gas_price(rpc_url: Optional[str] = None) -> int:
    """eth_gasPrice — floor for our gasPrice so we never go 'underpriced'."""
    return _to_int(rpc_call("eth_gasPrice", [], rpc_url))


def fetch_chain_id(rpc_url: Optional[str] = None) -> int:
    return _to_int(rpc_call("eth_chainId", [], rpc_url))


# --------------------------------------------------------------------------
# executor
# --------------------------------------------------------------------------

@dataclass
class Executor:
    client: Any                                     # BinanceWeb3Client (or fake)
    config: ExecutorConfig = field(default_factory=ExecutorConfig)
    signer: Any = field(default_factory=WalletSigner)
    request_ids: Any = field(default_factory=RequestIdStore)

    # ---- guards -----------------------------------------------------------

    def broadcast_block_reasons(self) -> List[str]:
        """Every reason broadcast is forbidden right now (empty = allowed)."""
        reasons: List[str] = []
        if self.config.force_no_broadcast:
            reasons.append("no-broadcast guard active (STOCKGAP_FORCE_NO_BROADCAST)")
        if not self.config.executor_enabled:
            reasons.append("executor_enabled=False")
        if self.config.dry_run:
            reasons.append("dry_run=True")
        if not self.config.allow_broadcast:
            reasons.append("allow_broadcast=False")
        if not getattr(self.signer, "available", False):
            reasons.append("wallet signing key unavailable")
        return reasons

    def _check_notional_usd(self, route: Dict[str, Any], amount_base: str) -> float:
        from_token = route.get("fromToken") or {}
        price = float(from_token.get("tokenUnitPrice") or 0)
        decimals = int(from_token.get("decimal") or 18)
        usd = int(amount_base) / (10 ** decimals) * price
        if usd > self.config.max_trade_usd:
            raise TradeTooLarge(
                f"trade notional ${usd:.2f} exceeds max_trade_usd="
                f"${self.config.max_trade_usd:.2f}")
        return usd

    # ---- API steps --------------------------------------------------------

    def quote(self, *, from_token: str, to_token: str, amount_base: str,
              user_wallet: Optional[str] = None,
              require_rfq: bool = False) -> Dict[str, Any]:
        """GET /aggregator/quote. RFQ routes require ``user_wallet``."""
        params: Dict[str, Any] = {
            "binanceChainId": CHAIN_ID,
            "fromTokenAddress": from_token,
            "toTokenAddress": to_token,
            "amount": str(amount_base),
        }
        if user_wallet:
            params["userWalletAddress"] = user_wallet
        elif require_rfq:
            raise ValueError("RFQ executionMode requires userWalletAddress")
        status, parsed, raw = self.client.request(
            "GET", "/api/v1/dex/aggregator/quote", params=params)
        self._raise_for_code(parsed, raw, context="quote")
        routes = parsed.get("data") or []
        if not routes:
            raise ApiError("EMPTY", "quote returned no routes", parsed)
        route = max(routes, key=lambda r: int(r.get("toTokenAmount") or 0))
        self._check_notional_usd(route, str(amount_base))
        route["_request_params"] = dict(params)  # must match /swap exactly (40462)
        return route

    def build_swap(self, route: Dict[str, Any],
                   slippage_percent: Optional[str] = "5") -> Dict[str, Any]:
        """GET /aggregator/swap — params MUST equal the quote params (40462)."""
        params = dict(route["_request_params"])
        params["quoteId"] = route["quoteId"]           # TTL ~30 s -> 40401
        if slippage_percent is not None:
            params["slippagePercent"] = str(slippage_percent)
        else:
            params["autoSlippage"] = "true"
        status, parsed, raw = self.client.request(
            "GET", "/api/v1/dex/aggregator/swap", params=params)
        self._raise_for_code(parsed, raw, context="swap")
        data = parsed.get("data") or {}
        data["_params_used"] = params
        return data

    def simulate(self, evm_tx: Dict[str, Any]) -> Dict[str, Any]:
        """POST /pre-transaction/simulate — SWAP flow only (RFQ has no tx)."""
        body = {"binanceChainId": CHAIN_ID, "evmTx": evm_tx}
        status, parsed, raw = self.client.request(
            "POST", "/api/v1/dex/pre-transaction/simulate", body=body)
        self._raise_for_code(parsed, raw, context="simulate")
        return parsed.get("data") or {}

    def broadcast(self, signed_tx: str, address: str) -> Dict[str, Any]:
        """POST /pre-transaction/broadcast-transaction — hard-guarded."""
        reasons = self.broadcast_block_reasons()
        if reasons:
            raise BroadcastBlocked(reasons)
        body = {"binanceChainId": CHAIN_ID, "address": address,
                "signedTransaction": signed_tx, "enableMevProtection": False}
        status, parsed, raw = self.client.request(
            "POST", "/api/v1/dex/pre-transaction/broadcast-transaction", body=body)
        self._raise_for_code(parsed, raw, context="broadcast")
        return parsed.get("data") or {}

    def submit_rfq_order(self, *, user_signature: str, vendor: str,
                         rfq_quote_id: str, order_key: str) -> Dict[str, Any]:
        """POST /aggregator/order/submit (idempotent per requestId UUID)."""
        request_id = self.request_ids.get_or_create("order_submit", order_key)
        body = {"userSignature": user_signature, "vendor": vendor,
                "quoteId": rfq_quote_id, "requestId": request_id}
        try:
            status, parsed, raw = self.client.request(
                "POST", "/api/v1/dex/aggregator/order/submit", body=body)
        except Exception:
            raise  # request_id already persisted: safe to retry with same UUID
        if str(parsed.get("code")) == ERR_DUPLICATE_REQUEST:
            raise DuplicateRequest(ERR_DUPLICATE_REQUEST, parsed.get("msg", ""),
                                   request_id)
        self._raise_for_code(parsed, raw, context="order/submit")
        self.request_ids.mark_sent("order_submit")
        data = parsed.get("data") or {}
        data["_requestId"] = request_id
        return data

    def get_rfq_order(self, order_id: str) -> Dict[str, Any]:
        status, parsed, raw = self.client.request(
            "GET", f"/api/v1/dex/aggregator/order/{order_id}")
        self._raise_for_code(parsed, raw, context="order/status")
        return parsed.get("data") or {}

    def poll_rfq_order(self, order_id: str, timeout_s: float = 60.0,
                       interval_s: float = 3.0) -> Dict[str, Any]:
        deadline = time.time() + timeout_s
        while True:
            data = self.get_rfq_order(order_id)
            if str(data.get("status", "")).upper() in ("FILLED", "FAILED"):
                return data
            if time.time() >= deadline:
                return data
            time.sleep(interval_s)

    # ---- pipelines --------------------------------------------------------

    def execute_swap(self, *, from_token: str = NATIVE_BNB, to_token: str,
                     amount_base: str, user_wallet: Optional[str] = None,
                     slippage_percent: Optional[str] = "5",
                     do_simulate: bool = True,
                     retry_on_expiry: bool = True) -> Dict[str, Any]:
        """SWAP pipeline: quote -> swap -> simulate -> (guarded) broadcast."""
        report: Dict[str, Any] = {"mode": "SWAP", "stages": {}}
        # /swap REQUIRES userWalletAddress even for SWAP mode (40001 without it).
        user_wallet = user_wallet or self.config.wallet_address
        attempts = 2 if retry_on_expiry else 1
        route = swap_data = None
        for attempt in range(attempts):
            route = self.quote(from_token=from_token, to_token=to_token,
                               amount_base=amount_base, user_wallet=user_wallet)
            report["quote"] = {
                "quoteId": route.get("quoteId"), "vendorName": route.get("vendorName"),
                "executionMode": route.get("executionMode"),
                "toTokenAmount": route.get("toTokenAmount"),
                "tradeFee": route.get("tradeFee"),
                "priceImpactPercent": route.get("priceImpactPercent"),
                "estimateGasFee": route.get("estimateGasFee"),
                "feeAmount": route.get("feeAmount"), "feeToken": route.get("feeToken"),
            }
            try:
                swap_data = self.build_swap(route, slippage_percent)
                break
            except QuoteExpired:
                report["stages"][f"swap_attempt_{attempt + 1}"] = "40401 QUOTE_EXPIRED"
                if attempt + 1 >= attempts:
                    raise
        assert swap_data is not None  # loop always leaves a built swap or raises
        tx = swap_data.get("tx") or {}
        report["stages"]["swap"] = {"built": True, "to": tx.get("to"),
                                   "minReceiveAmount": tx.get("minReceiveAmount"),
                                   "slippagePercent": tx.get("slippagePercent")}
        evm_tx = self._evm_tx_from(tx)
        if do_simulate:
            sim = self.simulate(evm_tx)
            report["stages"]["simulate"] = sim
            sim_status = str(sim.get("status", "")).upper()
            if sim_status and sim_status != "SUCCESS":
                # simulate re-executes calldata against LIVE pool state: a
                # FAILED simulate means this trade would revert on-chain.
                report["aborted"] = True
                report["stages"]["sign"] = f"skipped: simulate {sim_status}"
                report["stages"]["broadcast"] = {
                    "status": "ABORTED",
                    "reasons": [f"simulate {sim_status}: "
                                f"{sim.get('failReason') or 'no failReason'}"]}
                return report
        signed = None
        try:
            signed = self.signer.sign_evm_tx(self._signable_tx(tx))
            report["stages"]["sign"] = "signed"
        except WalletKeyUnavailable as exc:
            report["stages"]["sign"] = f"unavailable: {exc}"
        try:
            if signed is None:
                raise BroadcastBlocked(self.broadcast_block_reasons()
                                       or ["no signed transaction"])
            report["stages"]["broadcast"] = self.broadcast(
                signed, tx.get("from") or self.config.wallet_address)
        except BroadcastBlocked as exc:
            report["stages"]["broadcast"] = {"status": "BLOCKED",
                                            "reasons": exc.reasons}
        return report

    def execute_rfq(self, *, from_token: str, to_token: str, amount_base: str,
                    user_wallet: Optional[str] = None,
                    slippage_percent: Optional[str] = "5",
                    poll_timeout_s: float = 60.0) -> Dict[str, Any]:
        """RFQ pipeline: quote -> swap -> EIP-712 sign -> order/submit -> poll.

        No simulate step: RFQ has no transaction to simulate — the vendor
        relayer settles the EIP-712 order.
        """
        wallet = user_wallet or self.config.wallet_address
        report: Dict[str, Any] = {"mode": "RFQ", "stages": {}}
        route = self.quote(from_token=from_token, to_token=to_token,
                           amount_base=amount_base, user_wallet=wallet,
                           require_rfq=True)
        report["quote"] = {"quoteId": route.get("quoteId"),
                          "vendorName": route.get("vendorName"),
                          "executionMode": route.get("executionMode"),
                          "toTokenAmount": route.get("toTokenAmount"),
                          "tradeFee": route.get("tradeFee"),
                          "priceImpactPercent": route.get("priceImpactPercent")}
        swap_data = self.build_swap(route, slippage_percent)
        rfq = swap_data.get("rfq") or {}
        report["stages"]["swap"] = {"rfq_keys": sorted(rfq.keys())}
        report["stages"]["simulate"] = {"skipped": True,
                                       "reason": "RFQ flow has no tx to simulate"}
        typed = rfq.get("typedDataToSign") or {}
        signature = self.signer.sign_eip712(typed)
        report["stages"]["sign"] = "signed eip712"
        vendor = str(rfq.get("vendor") or route.get("vendorName") or "")
        # NOTE: order submit quoteId is rfq.orderId, NOT the /quote route id.
        rfq_quote_id = str(rfq.get("orderId") or rfq.get("quoteId") or "")
        order_key = f"{route.get('quoteId')}:{rfq_quote_id}"
        try:
            submitted = self.submit_rfq_order(
                user_signature=signature, vendor=vendor,
                rfq_quote_id=rfq_quote_id, order_key=order_key)
        except DuplicateRequest as exc:
            report["stages"]["order_submit"] = {
                "status": "42901 IN_PROGRESS",
                "action": f"wait, then retry with SAME requestId {exc.request_id}"}
            return report
        report["stages"]["order_submit"] = submitted
        order_id = submitted.get("orderId")
        if order_id:
            report["stages"]["order_status"] = self.poll_rfq_order(
                str(order_id), timeout_s=poll_timeout_s)
        return report

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _evm_tx_from(tx: Dict[str, Any]) -> Dict[str, Any]:
        return {"from": tx.get("from"), "to": tx.get("to"),
                "data": tx.get("data"), "value": str(tx.get("value", "0")),
                "gasLimit": str(tx.get("gas")), "gasPrice": str(tx.get("gasPrice"))}

    @staticmethod
    def _signable_tx(tx: Dict[str, Any], nonce: Optional[int] = None,
                     rpc_url: Optional[str] = None) -> Dict[str, Any]:
        """Sanitize the /swap ``data.tx`` dict into an eth_account-safe LEGACY tx.

        The Binance aggregator returns a display dict, not a signable one.
        Observed shape (data/demo_swap_simulate_slip5.json):
        ``from/to/data/value/gas/gasPrice`` as STRINGS plus non-EVM fields
        ``minReceiveAmount/slippagePercent/signatureData/computeUnitPrice/
        computeUnitLimit/maxPriorityFeePerGas``, and NO ``nonce``/``chainId``.

        Fixes applied here (each one is a hard eth_account or node failure):
        - drop ``from`` and every non-EVM field (unknown kwargs = TypeError);
        - NEVER mix ``maxFeePerGas``/``maxPriorityFeePerGas`` with ``gasPrice``
          (TypeError: Unknown kwargs) — legacy type 0 is correct for BSC;
        - checksum ``to`` (eth_account rejects lowercase addresses);
        - hex ('0x..') AND decimal strings both -> int;
        - inject ``nonce`` via eth_getTransactionCount(addr,'pending');
        - ALWAYS set chainId=56 — without it eth_account signs a pre-EIP-155
          tx (v=27/28), replayable across chains, rejected by BSC nodes;
        - gasPrice = max(api gasPrice, live eth_gasPrice) — quote gasPrice can
          be stale ('transaction underpriced');
        - gas x 1.2 headroom — aggregator swaps can exhaust the exact estimate
          and still burn the fee.
        """
        from eth_utils.address import to_checksum_address  # lazy: optional dep

        to = to_checksum_address(str(tx.get("to")))
        if to != to_checksum_address(ROUTER_ADDRESS):
            raise ValueError(
                f"refusing to sign: tx.to={to} is not the aggregator router "
                f"{ROUTER_ADDRESS}")
        if fetch_chain_id(rpc_url) != EVM_CHAIN_ID:
            raise RuntimeError(
                f"refusing to sign: RPC chainId != {EVM_CHAIN_ID} (BSC mainnet)")
        if nonce is None:
            nonce = (_to_int(tx["nonce"]) if tx.get("nonce") is not None
                     else fetch_nonce(str(tx.get("from")), rpc_url))
        gas = _to_int(tx.get("gas") if tx.get("gas") is not None
                      else tx.get("gasLimit"))
        gas = gas * 12 // 10                     # +20% headroom
        api_gas_price = _to_int(
            tx.get("gasPrice") if tx.get("gasPrice") is not None
            else tx.get("maxFeePerGas"))
        gas_price = max(api_gas_price, fetch_gas_price(rpc_url))
        return {"nonce": int(nonce), "gasPrice": int(gas_price),
                "gas": int(gas), "to": to,
                "value": _to_int(tx.get("value"), default=0),
                "data": tx.get("data") or "0x", "chainId": EVM_CHAIN_ID}

    @staticmethod
    def _raise_for_code(parsed: Dict[str, Any], raw: str, context: str) -> None:
        code = str(parsed.get("code")) if parsed else "NO_JSON"
        if code in ("0", "None") and parsed:
            return
        msg = parsed.get("msg") or (raw[:200] if raw else "empty response")
        if code == ERR_QUOTE_EXPIRED:
            raise QuoteExpired(code, msg, parsed)
        if code == ERR_SWAP_QUOTE_MISMATCH:
            raise SwapQuoteMismatch(code, msg, parsed)
        if code == ERR_PRICE_IMPACT:
            raise PriceImpactBlocked(code, msg, parsed)
        raise ApiError(code, f"{context}: {msg}", parsed)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="StockGap executor (guarded)")
    ap.add_argument("--to-token", required=True, help="target token contract")
    ap.add_argument("--from-token", default=NATIVE_BNB)
    ap.add_argument("--amount-base", required=True,
                    help="input amount in base units (wei)")
    ap.add_argument("--wallet", default=None)
    ap.add_argument("--mode", choices=["SWAP", "RFQ"], default="SWAP")
    ap.add_argument("--slippage-percent", default="5")
    ap.add_argument("--no-simulate", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    api_key, secret, _, _ = load_credentials()
    client = BinanceWeb3Client(api_key, secret)
    config = ExecutorConfig.from_env()
    ex = Executor(client=client, config=config)
    fn = ex.execute_rfq if args.mode == "RFQ" else ex.execute_swap
    kwargs = dict(from_token=args.from_token, to_token=args.to_token,
                  amount_base=args.amount_base, user_wallet=args.wallet,
                  slippage_percent=args.slippage_percent)
    if args.mode == "SWAP":
        kwargs["do_simulate"] = not args.no_simulate
    report = fn(**kwargs)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
