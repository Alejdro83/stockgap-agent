"""Tests for stockgap.executor — fully mocked API (no network, no wallet key).

Covers: SWAP flow (quote -> swap -> simulate -> guarded broadcast), RFQ flow
(quote -> swap -> EIP-712 sign -> order/submit -> poll), 40401 quote expiry
handling, and the unconditional no-broadcast guard.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import pytest

from stockgap.executor import (
    ApiError,
    DuplicateRequest,
    Executor,
    ExecutorConfig,
    QuoteExpired,
    RequestIdStore,
    SwapQuoteMismatch,
    WalletKeyUnavailable,
)

WALLET = "0x069Ac6d8cf3553416fE5a4f0410ebEb758A8F819"
NATIVE_BNB = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
TOKEN = "0x02fca66c1d1afb4e2a7884261eb00f63598a7436"
AMOUNT = "3000000000000000"  # 0.003 BNB
ROUTER = "0xB44446b0c8E56988c34f7Ff73Ae904982b5FdDA5"

# Throwaway key for signature-shape tests ONLY (never funded, never the wallet).
TEST_KEY = "0x" + "11" * 32


@pytest.fixture(autouse=True)
def _stub_rpc(monkeypatch):
    """No test may touch the network: stub the JSON-RPC layer."""

    def fake_rpc(method, params, rpc_url=None):
        return {"eth_getTransactionCount": "0x7",   # nonce 7
                "eth_gasPrice": "0x989680",         # 10_000_000 (< API gasPrice)
                "eth_chainId": "0x38"}[method]      # 56 = BSC mainnet

    monkeypatch.setattr("stockgap.executor.rpc_call", fake_rpc)


def swap_route(**over: Any) -> Dict[str, Any]:
    route = {
        "quoteId": "qid-1",
        "vendorName": "LiquidMesh",
        "executionMode": "SWAP",
        "fromTokenAmount": AMOUNT,
        "toTokenAmount": "9977223188190925",
        "tradeFee": "0.01806645",
        "estimateGasFee": "450000",
        "priceImpactPercent": "0.0003158178",
        "feeAmount": None,
        "feeToken": None,
        "fromToken": {"tokenContractAddress": NATIVE_BNB, "tokenSymbol": "BNB",
                      "tokenUnitPrice": "770.771105711945267538773850876293",
                      "decimal": "18"},
        "toToken": {"tokenContractAddress": TOKEN, "tokenSymbol": "NVDAB",
                    "tokenUnitPrice": "231.8602988790505261632", "decimal": "18"},
    }
    route.update(over)
    return route


def rfq_route(**over: Any) -> Dict[str, Any]:
    return swap_route(quoteId="qid-rfq", vendorName="PcsXRfq",
                      executionMode="RFQ", **over)


SWAP_TX = {
    "from": WALLET,
    "to": "0xB44446b0c8E56988c34f7Ff73Ae904982b5FdDA5",
    "data": "0xad43f73d",
    "value": AMOUNT,
    "gas": "450000",
    "gasPrice": "50030000",
    "minReceiveAmount": "9877450956309015",
    "slippagePercent": "5",
}

EIP712 = {"types": {"EIP712Domain": [], "Order": []},
          "primaryType": "Order", "domain": {}, "message": {"salt": "1"}}


class FakeClient:
    """Scripted BinanceWeb3Client: maps (method, path-prefix) to responses."""

    def __init__(self, scripts: Dict[Tuple[str, str], List[Dict[str, Any]]]):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: List[Tuple[str, str, Any]] = []

    def request(self, method: str, path: str, params=None, body=None):
        self.calls.append((method, path, {"params": params, "body": body}))
        for (m, prefix), responses in self.scripts.items():
            if m == method.upper() and path.startswith(prefix):
                if not responses:
                    raise AssertionError(f"script exhausted for {m} {path}")
                resp = responses.pop(0) if len(responses) > 1 else responses[0]
                return 200, resp, json.dumps(resp)
        raise AssertionError(f"no script for {method} {path}")

    def paths(self) -> List[str]:
        return [p for _, p, _ in self.calls]


class FakeSigner:
    available = True

    def __init__(self):
        self.evm_signed: List[Dict[str, Any]] = []
        self.eip712_signed: List[Dict[str, Any]] = []

    def sign_evm_tx(self, tx):
        self.evm_signed.append(tx)
        return "0xsignedevm"

    def sign_eip712(self, typed):
        self.eip712_signed.append(typed)
        return "0xsigeip712"


def ok_envelope(data):
    return {"code": 0, "msg": "success", "data": data}


def err_envelope(code, msg):
    return {"code": code, "msg": msg, "data": None}


def make_executor(scripts, tmp_path, config=None, signer=None) -> Tuple[Executor, FakeClient]:
    client = FakeClient(scripts)
    ex = Executor(client=client,
                  config=config or ExecutorConfig(executor_enabled=True,
                                                  dry_run=True,
                                                  force_no_broadcast=True),
                  signer=signer or FakeSigner(),
                  request_ids=RequestIdStore(tmp_path / "state.json"))
    return ex, client


# ---------------------------------------------------------------------------
# SWAP flow
# ---------------------------------------------------------------------------

def test_swap_flow_quote_simulate_and_guarded_broadcast(tmp_path):
    scripts = {
        ("GET", "/api/v1/dex/aggregator/quote"): [ok_envelope([swap_route()])],
        ("GET", "/api/v1/dex/aggregator/swap"): [ok_envelope({"tx": SWAP_TX, "rfq": None})],
        ("POST", "/api/v1/dex/pre-transaction/simulate"): [
            ok_envelope({"status": "SUCCESS",
                         "balanceChanges": [{"contractAddress": NATIVE_BNB,
                                             "change": "-3000000000000000"}],
                         "allowanceChanges": []})],
    }
    ex, client = make_executor(scripts, tmp_path)
    report = ex.execute_swap(from_token=NATIVE_BNB, to_token=TOKEN,
                             amount_base=AMOUNT, user_wallet=WALLET)
    assert report["mode"] == "SWAP"
    assert report["quote"]["quoteId"] == "qid-1"
    assert report["quote"]["vendorName"] == "LiquidMesh"
    assert report["stages"]["simulate"]["status"] == "SUCCESS"
    # the tx was signed locally…
    assert ex.signer.evm_signed and ex.signer.evm_signed[0]["to"] == SWAP_TX["to"]
    # …but broadcast stayed blocked (dry_run) and the broadcast endpoint
    # was never touched.
    assert report["stages"]["broadcast"]["status"] == "BLOCKED"
    assert any("dry_run=True" in r for r in report["stages"]["broadcast"]["reasons"])
    assert not any("/broadcast-transaction" in p for p in client.paths())
    # /swap params must be byte-identical to the quote params (40462) plus
    # quoteId + slippage.
    swap_call = [c for c in client.calls if c[1].endswith("/swap")][0]
    swap_params = swap_call[2]["params"]
    assert swap_params["amount"] == AMOUNT
    assert swap_params["fromTokenAddress"] == NATIVE_BNB
    assert swap_params["quoteId"] == "qid-1"
    assert swap_params["slippagePercent"] == "5"


def test_swap_flow_respects_max_trade_usd(tmp_path):
    scripts = {("GET", "/api/v1/dex/aggregator/quote"): [ok_envelope([swap_route()])]}
    ex, _ = make_executor(scripts, tmp_path,
                          config=ExecutorConfig(executor_enabled=True, dry_run=True,
                                                max_trade_usd=1.0))
    with pytest.raises(Exception, match="max_trade_usd"):
        ex.execute_swap(from_token=NATIVE_BNB, to_token=TOKEN, amount_base=AMOUNT)


# ---------------------------------------------------------------------------
# RFQ flow
# ---------------------------------------------------------------------------

def test_rfq_flow_sign_submit_and_poll(tmp_path):
    submit_body: Dict[str, Any] = {}
    scripts = {
        ("GET", "/api/v1/dex/aggregator/quote"): [ok_envelope([rfq_route()])],
        ("GET", "/api/v1/dex/aggregator/swap"): [ok_envelope({
            "tx": None,
            "rfq": {"typedDataToSign": EIP712, "vendor": "PcsXRfq",
                    "orderId": "rfq-order-99"}})],
        ("POST", "/api/v1/dex/aggregator/order/submit"): [
            ok_envelope({"orderId": "ord-7", "status": "PENDING"})],
        ("GET", "/api/v1/dex/aggregator/order/ord-7"): [
            ok_envelope({"orderId": "ord-7", "status": "FILLED"})],
    }
    ex, client = make_executor(scripts, tmp_path)
    report = ex.execute_rfq(from_token=NATIVE_BNB, to_token=TOKEN,
                            amount_base=AMOUNT, user_wallet=WALLET,
                            poll_timeout_s=0.0)
    assert report["mode"] == "RFQ"
    assert report["quote"]["executionMode"] == "RFQ"
    # RFQ must never hit /simulate — there is no tx to simulate.
    assert report["stages"]["simulate"]["skipped"] is True
    assert not any("/pre-transaction/simulate" in p for p in client.paths())
    assert not any("/broadcast-transaction" in p for p in client.paths())
    # EIP-712 typed data reached the signer.
    assert ex.signer.eip712_signed == [EIP712]
    # submit body: userSignature + vendor + rfq.orderId + persisted requestId
    submit_call = [c for c in client.calls if c[1].endswith("/order/submit")][0]
    submit_body = submit_call[2]["body"]
    assert submit_body["userSignature"] == "0xsigeip712"
    assert submit_body["vendor"] == "PcsXRfq"
    assert submit_body["quoteId"] == "rfq-order-99"   # rfq.orderId, not route id
    assert submit_body["requestId"] == report["stages"]["order_submit"]["_requestId"]
    assert report["stages"]["order_status"]["status"] == "FILLED"


def test_rfq_quote_requires_user_wallet(tmp_path):
    ex, _ = make_executor({("GET", "/api/v1/dex/aggregator/quote"): [ok_envelope([])]},
                          tmp_path)
    with pytest.raises(ValueError, match="userWalletAddress"):
        ex.quote(from_token=NATIVE_BNB, to_token=TOKEN, amount_base=AMOUNT,
                 require_rfq=True)


# ---------------------------------------------------------------------------
# 40401 quote expiry
# ---------------------------------------------------------------------------

def test_40401_quote_expiry_triggers_requote(tmp_path):
    scripts = {
        ("GET", "/api/v1/dex/aggregator/quote"): [
            ok_envelope([swap_route(quoteId="expired-qid")]),
            ok_envelope([swap_route(quoteId="fresh-qid")]),
        ],
        ("GET", "/api/v1/dex/aggregator/swap"): [
            err_envelope("40401", "QUOTE_EXPIRED"),
            ok_envelope({"tx": SWAP_TX, "rfq": None}),
        ],
        ("POST", "/api/v1/dex/pre-transaction/simulate"): [
            ok_envelope({"status": "SUCCESS", "balanceChanges": []})],
    }
    ex, client = make_executor(scripts, tmp_path)
    report = ex.execute_swap(from_token=NATIVE_BNB, to_token=TOKEN,
                             amount_base=AMOUNT, user_wallet=WALLET)
    assert report["stages"]["swap_attempt_1"] == "40401 QUOTE_EXPIRED"
    assert report["quote"]["quoteId"] == "fresh-qid"
    quote_calls = [c for c in client.calls if c[1].endswith("/quote")]
    assert len(quote_calls) == 2


def test_40401_without_retry_raises(tmp_path):
    scripts = {
        ("GET", "/api/v1/dex/aggregator/quote"): [ok_envelope([swap_route()])],
        ("GET", "/api/v1/dex/aggregator/swap"): [err_envelope("40401", "QUOTE_EXPIRED")],
    }
    ex, _ = make_executor(scripts, tmp_path)
    with pytest.raises(QuoteExpired):
        ex.execute_swap(from_token=NATIVE_BNB, to_token=TOKEN, amount_base=AMOUNT,
                        retry_on_expiry=False)


def test_40462_swap_quote_mismatch_surfaces(tmp_path):
    scripts = {
        ("GET", "/api/v1/dex/aggregator/quote"): [ok_envelope([swap_route()])],
        ("GET", "/api/v1/dex/aggregator/swap"): [
            err_envelope("40462", "SWAP_QUOTE_MISMATCH")],
    }
    ex, _ = make_executor(scripts, tmp_path)
    with pytest.raises(SwapQuoteMismatch):
        ex.execute_swap(from_token=NATIVE_BNB, to_token=TOKEN, amount_base=AMOUNT,
                        retry_on_expiry=False)


# ---------------------------------------------------------------------------
# no-broadcast guard
# ---------------------------------------------------------------------------

def test_no_broadcast_guard_is_unconditional(tmp_path, monkeypatch):
    """Even with every toggle open, the force guard blocks broadcast."""
    monkeypatch.delenv("WALLET_PRIVATE_KEY", raising=False)
    scripts = {
        ("GET", "/api/v1/dex/aggregator/quote"): [ok_envelope([swap_route()])],
        ("GET", "/api/v1/dex/aggregator/swap"): [ok_envelope({"tx": SWAP_TX, "rfq": None})],
        ("POST", "/api/v1/dex/pre-transaction/simulate"): [ok_envelope({"status": "SUCCESS"})],
    }
    wide_open = ExecutorConfig(executor_enabled=True, dry_run=False,
                               allow_broadcast=True, force_no_broadcast=True)
    ex, client = make_executor(scripts, tmp_path, config=wide_open)
    report = ex.execute_swap(from_token=NATIVE_BNB, to_token=TOKEN,
                             amount_base=AMOUNT, user_wallet=WALLET)
    stage = report["stages"]["broadcast"]
    assert stage["status"] == "BLOCKED"
    assert any("STOCKGAP_FORCE_NO_BROADCAST" in r for r in stage["reasons"])
    assert not any("/broadcast-transaction" in p for p in client.paths())


def test_broadcast_blocked_when_signing_key_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("WALLET_PRIVATE_KEY", raising=False)
    from stockgap.executor import WalletSigner
    scripts = {
        ("GET", "/api/v1/dex/aggregator/quote"): [ok_envelope([swap_route()])],
        ("GET", "/api/v1/dex/aggregator/swap"): [ok_envelope({"tx": SWAP_TX, "rfq": None})],
    }
    cfg = ExecutorConfig(executor_enabled=True, dry_run=False,
                         allow_broadcast=True, force_no_broadcast=False)
    ex, client = make_executor(scripts, tmp_path, config=cfg, signer=WalletSigner())
    report = ex.execute_swap(from_token=NATIVE_BNB, to_token=TOKEN,
                             amount_base=AMOUNT, do_simulate=False)
    assert "unavailable" in report["stages"]["sign"]
    assert report["stages"]["broadcast"]["status"] == "BLOCKED"
    assert not any("/broadcast-transaction" in p for p in client.paths())


def test_config_defaults_are_safe(monkeypatch):
    for var in ("EXECUTOR_ENABLED", "DRY_RUN", "MAX_TRADE_USD",
                "STOCKGAP_ALLOW_BROADCAST", "STOCKGAP_FORCE_NO_BROADCAST"):
        monkeypatch.delenv(var, raising=False)
    cfg = ExecutorConfig.from_env()
    assert cfg.executor_enabled is False
    assert cfg.dry_run is True
    assert cfg.max_trade_usd == 5.0
    assert cfg.allow_broadcast is False
    assert cfg.force_no_broadcast is True


# ---------------------------------------------------------------------------
# 42901 duplicate requestId handling
# ---------------------------------------------------------------------------

def test_42901_duplicate_request_id_reuses_persisted_uuid(tmp_path):
    scripts = {
        ("GET", "/api/v1/dex/aggregator/quote"): [ok_envelope([rfq_route()])],
        ("GET", "/api/v1/dex/aggregator/swap"): [ok_envelope({
            "rfq": {"typedDataToSign": EIP712, "vendor": "PcsXRfq",
                    "orderId": "rfq-order-99"}})],
        ("POST", "/api/v1/dex/aggregator/order/submit"): [
            err_envelope("42901", "ORDER_SUBMIT_IN_PROGRESS"),
            err_envelope("42901", "ORDER_SUBMIT_IN_PROGRESS"),
        ],
    }
    ex, client = make_executor(scripts, tmp_path)
    report = ex.execute_rfq(from_token=NATIVE_BNB, to_token=TOKEN,
                            amount_base=AMOUNT, user_wallet=WALLET,
                            poll_timeout_s=0.0)
    assert report["stages"]["order_submit"]["status"] == "42901 IN_PROGRESS"
    # the UUID was persisted before the first send…
    state = json.loads((tmp_path / "state.json").read_text())
    request_id = state["order_submit"]["request_id"]
    # …and a manual retry reuses the SAME requestId.
    with pytest.raises(DuplicateRequest) as excinfo:
        ex.submit_rfq_order(user_signature="0xsigeip712", vendor="PcsXRfq",
                            rfq_quote_id="rfq-order-99",
                            order_key="qid-rfq:rfq-order-99")
    assert excinfo.value.request_id == request_id


# ---------------------------------------------------------------------------
# _signable_tx sanitization (regression: /swap data.tx is NOT signable as-is)
# ---------------------------------------------------------------------------

# Byte-for-byte shape of the real capture in data/demo_swap_simulate_slip5.json
# (swap.data.tx): all strings, lowercase `to`, `from` present, non-EVM fields
# alongside, maxPriorityFeePerGas == gasPrice, and NO nonce/chainId.
CAPTURED_SWAP_TX = {
    "from": WALLET,
    "to": ROUTER.lower(),
    "data": "0xad43f73d0000000000000000000000000000000000000000000000004a9c697ef500",
    "value": AMOUNT,
    "gas": "450000",
    "gasPrice": "52210995",
    "maxPriorityFeePerGas": "52210995",
    "minReceiveAmount": "9479738611512385",
    "slippagePercent": "5",
    "signatureData": "None",
    "computeUnitPrice": "None",
    "computeUnitLimit": "None",
}


def test_signable_tx_sanitizes_captured_swap_tx():
    from eth_utils.address import to_checksum_address

    out = Executor._signable_tx(dict(CAPTURED_SWAP_TX), nonce=7)
    # no 'from', no non-EVM fields, no 1559 fee fields mixed with gasPrice
    assert "from" not in out
    assert set(out) == {"nonce", "gasPrice", "gas", "to", "value", "data", "chainId"}
    assert "maxFeePerGas" not in out and "maxPriorityFeePerGas" not in out
    # chainId ALWAYS set -> EIP-155 bound (never pre-EIP-155 v=27/28)
    assert out["chainId"] == 56
    # nonce injected
    assert out["nonce"] == 7
    # decimal strings accepted as ints; gas carries +20% headroom
    assert isinstance(out["gas"], int) and out["gas"] == 450000 * 12 // 10
    assert isinstance(out["value"], int) and out["value"] == int(AMOUNT)
    assert isinstance(out["gasPrice"], int) and out["gasPrice"] == 52210995
    # to_checksum_address applied to the lowercase API address
    assert out["to"] == to_checksum_address(ROUTER.lower()) == ROUTER


def test_signable_tx_fetches_pending_nonce_and_floors_gas_price(monkeypatch):
    seen = []

    def spy_rpc(method, params, rpc_url=None):
        seen.append((method, tuple(params)))
        return {"eth_getTransactionCount": "0x7", "eth_gasPrice": "0x1",
                "eth_chainId": "0x38"}[method]

    monkeypatch.setattr("stockgap.executor.rpc_call", spy_rpc)
    out = Executor._signable_tx(dict(CAPTURED_SWAP_TX))
    assert out["nonce"] == 7
    assert ("eth_getTransactionCount", (WALLET, "pending")) in seen
    # gasPrice floor: live eth_gasPrice wins when higher than the API's
    monkeypatch.setattr(
        "stockgap.executor.rpc_call",
        lambda m, p, rpc_url=None: {"eth_getTransactionCount": "0x7",
                                    "eth_gasPrice": "0x2fe00000",
                                    "eth_chainId": "0x38"}[m])
    out2 = Executor._signable_tx(dict(CAPTURED_SWAP_TX), nonce=1)
    assert out2["gasPrice"] == 0x2FE00000


def test_signable_tx_hex_and_gaslimit_variants_accepted():
    tx = dict(CAPTURED_SWAP_TX)
    del tx["gas"]
    tx["gasLimit"] = "0x1e8480"          # hex string, renamed field
    tx["value"] = "0xaa87bee538000"     # hex string = 3000000000000000
    out = Executor._signable_tx(tx, nonce=3)
    assert out["gas"] == 0x1E8480 * 12 // 10
    assert out["value"] == int(AMOUNT)


def test_signable_tx_refuses_non_router_or_wrong_chain(monkeypatch):
    tx = dict(CAPTURED_SWAP_TX)
    tx["to"] = "0x02fca66c1d1afb4e2a7884261eb00f63598a7436"  # not the router
    with pytest.raises(ValueError):
        Executor._signable_tx(tx, nonce=1)
    monkeypatch.setattr(
        "stockgap.executor.rpc_call",
        lambda m, p, rpc_url=None: {"eth_getTransactionCount": "0x7",
                                    "eth_gasPrice": "0x1",
                                    "eth_chainId": "0x1"}[m])
    with pytest.raises(RuntimeError):
        Executor._signable_tx(dict(CAPTURED_SWAP_TX), nonce=1)


def test_signable_tx_signs_eip155_bound_legacy_tx():
    """End-to-end pin: the sanitized dict signs with v in (147, 148)."""
    Account = pytest.importorskip("eth_account").Account
    acct = Account.from_key(TEST_KEY)  # throwaway key, not the live wallet
    out = Executor._signable_tx(dict(CAPTURED_SWAP_TX))  # nonce fetched = 7
    assert out["nonce"] == 7
    signed = acct.sign_transaction(out)
    # EIP-155 for chainId 56: v = 35 + 2*56 + parity -> 147 or 148.
    # v in (27, 28) would mean a replayable pre-EIP-155 signature.
    assert signed.v in (147, 148)
