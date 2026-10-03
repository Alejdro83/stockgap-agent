"""Tests for the on-chain oracle writer (PriceRegistry anchor path, BSC 56).

No network, no real keys: the RPC layer is mocked with a FakeChain that
implements the PriceRegistry contract semantics (owner-gated publish, one
publish per snapshotId) and a FakeSigner captures the unsigned tx shape.
"""
from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from stockgap import onchain_writer as ow
from stockgap import oracle_score

REPO = Path(__file__).resolve().parent.parent
REG = "0x" + "11" * 20
WALLET = "0x" + "22" * 20

PUBLISH_SEL = "0x" + "3a" * 4  # placeholder; FakeChain derives the real selectors
SID = ow.derive_snapshot_id("snapshot-529", "2026-10-04T23:59:00Z", "ACN")


class RpcError(RuntimeError):
    """What the JSON-RPC wrapper raises on an {"error": ...} response."""


class FakeSigner:
    """Captures the unsigned tx dict; returns deterministic raw bytes."""

    def __init__(self, address: str = WALLET):
        self.address = address
        self.signed = []

    def sign_tx(self, tx: dict) -> str:
        self.signed.append(dict(tx))
        return "0x" + "ab" * 32


class FakeChain:
    """Mock RPC layer implementing PriceRegistry semantics.

    eth_call runs contract logic WITHOUT state change; eth_sendRawTransaction
    mines the last simulated publish calldata (the mock cannot RLP-decode raw
    signed txs — the 'simulate before every send' invariant is asserted by the
    recorded call order).
    """

    def __init__(self, confirmed: bool = True, chain_id: int = 56):
        self.published = {}
        self.sent = []
        self.order = []
        self.confirmed = confirmed
        self.chain_id = chain_id
        self.last_publish_calldata = None
        self.contract_address = "0x" + "33" * 20

    def __call__(self, method: str, params: list):
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getTransactionCount":
            return hex(len(self.sent))
        if method == "eth_gasPrice":
            return hex(3_000_000_000)
        if method == "eth_estimateGas":
            self.order.append(method)
            return hex(100_000)
        if method == "eth_call":
            self.order.append(method)
            return self._execute(params[0])
        if method == "eth_sendRawTransaction":
            self.order.append(method)
            self.sent.append(params[0])
            if self.last_publish_calldata:
                sid = "0x" + self.last_publish_calldata[10:74]
                self.published[sid] = True
                self.last_publish_calldata = None
            return "0x" + "cd" * 32
        if method == "eth_getTransactionReceipt":
            if not self.confirmed:
                return {"transactionHash": params[0], "status": "0x0", "blockNumber": "0x1"}
            return {"transactionHash": params[0], "status": "0x1", "blockNumber": "0x1",
                    "contractAddress": self.contract_address if not self.published or self._is_deploy_receipt else None}
        if method == "eth_getCode":
            return "0x60806040"
        raise AssertionError(f"unexpected RPC method {method}")

    _is_deploy_receipt = True  # first receipt is the deploy one

    def _execute(self, call: dict) -> str:
        data = (call.get("data") or "0x").lower()
        if not call.get("to"):  # contract creation simulation
            return "0x"
        sel, args = data[:10], data[10:]
        if sel == ow.PUBLISH_SELECTOR:
            sid = "0x" + args[:64]
            if sid in self.published:
                raise RpcError("execution reverted: PriceRegistry: duplicate snapshotId")
            self.last_publish_calldata = data
            self._is_deploy_receipt = False
            return "0x"
        if sel == ow.PUBLISHED_SELECTOR:
            sid = "0x" + args[:64]
            return hex(1 if sid in self.published else 0)
        raise AssertionError(f"unexpected calldata selector {sel}")


def _payload(ticker: str = "ACN", price: str = "199.6266670000000000001992195") -> dict:
    return {"underlying_ticker": ticker, "predicted_open": price,
            "cutoff_ts": "2026-10-04T23:59:00Z"}


# ---------------------------------------------------------------- quantization
def test_quantize_fractional_price_half_even():
    # documented rounding: Decimal x 1e8, ROUND_HALF_EVEN, integer out
    assert ow.quantize_price_e8("12.345678915") == 1234567892  # ...891.5 -> even
    assert ow.quantize_price_e8("0.000000025") == 2            # 2.5 -> even (2)
    assert ow.quantize_price_e8("0.000000035") == 4            # 3.5 -> even (4)
    assert ow.quantize_price_e8("2.5") == 250_000_000          # exact, no rounding


def test_quantize_live_acnon_price():
    # the real snapshot-529 ACNon implied open (fractional, >18 decimals)
    assert ow.quantize_price_e8("199.6266670000000000001992195") == 19_962_666_700


def test_quantize_rejects_bad_prices():
    for bad in ("-1", "abc", float("nan"), None, "1e400"):
        with pytest.raises(ValueError):
            ow.quantize_price_e8(bad)


# ---------------------------------------------------------------- snapshot id
def test_snapshot_id_deterministic_bytes32():
    a = ow.derive_snapshot_id("snapshot-529", "2026-10-04T23:59:00Z", "ACN")
    assert a == ow.derive_snapshot_id("snapshot-529", "2026-10-04T23:59:00Z", "ACN")
    assert re.fullmatch(r"0x[0-9a-f]{64}", a)
    assert a != ow.derive_snapshot_id("snapshot-530", "2026-10-04T23:59:00Z", "ACN")


# ---------------------------------------------------------------- feature toggle
def test_publish_toggle_default_is_off(monkeypatch):
    monkeypatch.delenv("ORACLE_PUBLISH", raising=False)
    assert ow.publish_enabled({}) is False
    assert ow.publish_enabled({"ORACLE_PUBLISH": "0"}) is False
    assert ow.publish_enabled({"ORACLE_PUBLISH": "1"}) is True
    assert ow.publish_enabled({"ORACLE_PUBLISH": "true"}) is True
    assert ow.publish_enabled() is False  # env unset -> mock ledger


def test_feedback_writer_factory_defaults_to_mock(tmp_path, monkeypatch):
    monkeypatch.delenv("ORACLE_PUBLISH", raising=False)
    con = sqlite3.connect(":memory:")
    writer = oracle_score.build_feedback_writer(con)
    assert isinstance(writer, oracle_score.DbFeedbackWriter)


def test_feedback_writer_factory_on_returns_onchain(tmp_path):
    con = sqlite3.connect(":memory:")
    pub = ow.OnchainPublisher(FakeChain(), FakeSigner(), registry_address=REG)
    writer = oracle_score.build_feedback_writer(con, env={"ORACLE_PUBLISH": "1"}, publisher=pub)
    assert isinstance(writer, oracle_score.OnchainFeedbackWriter)


# ---------------------------------------------------------------- interface
def test_onchain_writer_same_interface_as_mock():
    for cls in (oracle_score.DbFeedbackWriter, oracle_score.OnchainFeedbackWriter):
        params = list(inspect.signature(cls.submit).parameters)
        assert params == ["self", "run_id", "payload"], (cls, params)


def test_submit_returns_bsc_ref_only_after_confirmation():
    chain = FakeChain(confirmed=True)
    pub = ow.OnchainPublisher(chain, FakeSigner(), registry_address=REG)
    writer = oracle_score.OnchainFeedbackWriter(pub)
    ref = writer.submit("run-1", _payload())
    assert ref == "bsc:56:" + "0x" + "cd" * 32
    assert chain.order.index("eth_call") < chain.order.index("eth_sendRawTransaction")


def test_no_feedback_ref_before_confirmation():
    chain = FakeChain(confirmed=False)  # receipt status 0x0
    pub = ow.OnchainPublisher(chain, FakeSigner(), registry_address=REG)
    writer = oracle_score.OnchainFeedbackWriter(pub)
    with pytest.raises(ow.PublishUnconfirmed):
        writer.submit("run-2", _payload())
    assert writer.refs == {}  # no claimed on-chain ref was kept


def test_one_publish_per_run_not_per_row():
    chain = FakeChain()
    pub = ow.OnchainPublisher(chain, FakeSigner(), registry_address=REG)
    writer = oracle_score.OnchainFeedbackWriter(pub)
    refs = [writer.submit("run-9", _payload(t)) for t in ("ACN", "MSTR", "IBIT", "TQQQ")]
    assert len(chain.sent) == 1, "exactly ONE on-chain publish per oracle run"
    assert len(set(refs)) == 1 and refs[0].startswith("bsc:56:0x")


# ---------------------------------------------------------------- publisher / RPC layer
def test_publish_tx_is_legacy_eip155_chain56():
    chain = FakeChain()
    signer = FakeSigner()
    pub = ow.OnchainPublisher(chain, signer, registry_address=REG)
    pub.publish(SID, 123)
    tx = signer.signed[0]
    assert set(tx) == {"nonce", "gasPrice", "gas", "to", "value", "data", "chainId"}
    assert tx["chainId"] == 56 and isinstance(tx["gasPrice"], int) and tx["gasPrice"] > 0
    assert tx["to"] == REG


def test_deploy_tx_is_contract_creation_to_none(tmp_path):
    pytest.importorskip("solcx")
    chain = FakeChain()
    signer = FakeSigner()
    record = tmp_path / "price_registry.json"  # tests never touch the live record
    pub = ow.OnchainPublisher(chain, signer, record_path=record)
    out = pub.deploy()
    tx = signer.signed[0]
    assert tx["to"] is None and tx["data"].startswith("0x60")
    assert tx["chainId"] == 56
    assert out["address"] == chain.contract_address
    assert out["tx_hash"] == "0x" + "cd" * 32
    import json as _json
    assert _json.loads(record.read_text())["address"] == chain.contract_address


def test_simulate_runs_before_every_send():
    chain = FakeChain()
    pub = ow.OnchainPublisher(chain, FakeSigner(), registry_address=REG)
    pub.publish(SID, 1)
    # eth_call + eth_estimateGas is its own simulate step immediately BEFORE
    # the send (earlier eth_calls are read-only pre-checks like is_published)
    assert chain.order[-3:] == ["eth_call", "eth_estimateGas", "eth_sendRawTransaction"]


def test_duplicate_snapshot_id_reverts_and_never_broadcasts():
    chain = FakeChain()
    pub = ow.OnchainPublisher(chain, FakeSigner(), registry_address=REG)
    pub.publish(SID, 1)                       # anchor it
    with pytest.raises(ow.DuplicateSnapshot):
        pub.publish(SID, 2)                   # contract reverts on the mapping check
    assert len(chain.sent) == 1, "duplicate must be rejected before broadcast"


def test_wrong_chain_is_refused():
    chain = FakeChain(chain_id=97)  # BSC testnet
    pub = ow.OnchainPublisher(chain, FakeSigner(), registry_address=REG)
    with pytest.raises(ow.PublishError):
        pub.publish(SID, 1)
    assert chain.sent == []


def test_publish_refuses_without_registry_address():
    pub = ow.OnchainPublisher(FakeChain(), FakeSigner(), registry_address=None)
    with pytest.raises(ow.PublishError):
        pub.publish(SID, 1)
    assert FakeChain().sent == []  # nothing leaves without a pinned registry


def test_publish_fails_clearly_when_registry_has_no_code():
    def rpc(method, params):
        if method == "eth_chainId":
            return hex(56)
        if method == "eth_call" and params[0].get("data", "").startswith(ow.PUBLISHED_SELECTOR):
            return "0x"  # no code at the address -> empty return
        raise AssertionError(f"unexpected {method}")

    pub = ow.OnchainPublisher(rpc, FakeSigner(), registry_address=REG)
    with pytest.raises(ow.PublishError, match="no contract code"):
        pub.publish(SID, 1)


def test_publish_calldata_encoding():
    data = ow.encode_publish(SID, 19_962_666_700)
    assert data.startswith(ow.PUBLISH_SELECTOR)
    assert len(data) == 2 + 8 + 64 + 64
    assert data[10:74] == SID[2:]
    assert int(data[74:138], 16) == 19_962_666_700


def test_real_signer_eip155_recovers_to_wallet(monkeypatch):
    Account = pytest.importorskip("eth_account").Account
    throwaway = Account.create()  # ephemeral TEST key, never used on-chain
    monkeypatch.setenv("WALLET_PRIVATE_KEY", throwaway.key.hex())
    signer = ow.LocalWalletSigner()
    assert signer.address == throwaway.address
    raw = signer.sign_tx({"nonce": 0, "gasPrice": 1, "gas": 21000, "to": REG,
                          "value": 0, "data": "0x", "chainId": 56})
    assert Account.recover_transaction(raw) == throwaway.address


# ---------------------------------------------------------------- contract source
def test_price_registry_source_enforces_rules():
    src = (REPO / "contracts" / "PriceRegistry.sol").read_text()
    assert "event OraclePublish(bytes32 indexed snapshotId, uint256 priceE8, uint256 timestamp)" in src
    assert "emit OraclePublish(snapshotId, priceE8, block.timestamp)" in src
    assert "modifier onlyOwner" in src and "external onlyOwner" in src
    assert "require(!published[snapshotId]" in src
    assert "constructor()" in src and "owner = msg.sender" in src


def test_compile_price_registry():
    pytest.importorskip("solcx")
    artifact = ow.compile_price_registry()
    assert artifact["bytecode"].startswith("0x60")
    names = {e.get("name") for e in artifact["abi"] if e.get("type") == "function"}
    assert {"publish", "published", "owner"} <= names
