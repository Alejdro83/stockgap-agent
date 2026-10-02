"""Pay-per-call gate: receipt verification, idempotency, 402 payload, Binance b402 v2 adapter (mocked)."""
import sqlite3
import time

import pytest

from stockgap import b402_gate as g

MERCHANT = "0x069Ac6d8cf3553416fE5a4f0410ebEb758A8F819"
TX = "0x" + "ab" * 32
TX2 = "0x" + "cd" * 32
PRICE = 100000000000000


def cfg(**kw):
    base = dict(mode="b402", provider="local_receipt", merchant=MERCHANT, price_wei=PRICE,
                asset="native", max_block_age_s=3600)
    base.update(kw)
    return g.GateConfig(**base)


def fake_rpc(*, to=MERCHANT, value=PRICE, status="0x1", age_s=30, tx_found=True):
    def rpc(method, params):
        if method == "eth_getTransactionByHash":
            return {"hash": params[0], "from": "0x" + "11" * 20, "to": to, "value": hex(value),
                    "input": "0x"} if tx_found else None
        if method == "eth_getTransactionReceipt":
            return {"status": status, "to": to, "blockNumber": "0x10"} if tx_found else None
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(int(time.time()) - age_s)}
        raise AssertionError(method)
    return rpc


# ---- config ----
def test_config_defaults_free(tmp_path):
    c = g.load_config(env={}, path=tmp_path / "nope.env")
    assert c.mode == "free" and c.provider == "local_receipt"
    assert c.merchant.lower() == MERCHANT.lower() and c.price_wei == PRICE


def test_config_file_and_env_precedence(tmp_path):
    f = tmp_path / "mcp.env"
    f.write_text("MCP_PAY_MODE=b402\nB402_PROVIDER=binance\nMCP_PRICE_WEI=5\n")
    c = g.load_config(env={"MCP_PRICE_WEI": "7"}, path=f)
    assert (c.mode, c.provider, c.price_wei) == ("b402", "binance", 7)


def test_config_invalid_mode_falls_back_to_free(tmp_path):
    assert g.load_config(env={"MCP_PAY_MODE": "xx"}, path=tmp_path / "n").mode == "free"


# ---- receipt verification ----
def test_receipt_accepts_valid():
    r = g.verify_receipt(TX, cfg(), fake_rpc())
    assert r.ok and r.amount == PRICE and r.payer == "0x" + "11" * 20


def test_receipt_accepts_overpay():
    assert g.verify_receipt(TX, cfg(), fake_rpc(value=PRICE * 3)).ok


def test_receipt_rejects_failed_status():
    r = g.verify_receipt(TX, cfg(), fake_rpc(status="0x0"))
    assert not r.ok and "status" in r.reason


def test_receipt_rejects_wrong_recipient():
    r = g.verify_receipt(TX, cfg(), fake_rpc(to="0x" + "22" * 20))
    assert not r.ok and "destinatario" in r.reason


def test_receipt_rejects_underpay():
    r = g.verify_receipt(TX, cfg(), fake_rpc(value=PRICE - 1))
    assert not r.ok and "monto" in r.reason


def test_receipt_rejects_old_block():
    r = g.verify_receipt(TX, cfg(), fake_rpc(age_s=7200))
    assert not r.ok and "antig" in r.reason


def test_receipt_rejects_unknown_tx_and_bad_hash():
    assert not g.verify_receipt(TX, cfg(), fake_rpc(tx_found=False)).ok
    assert not g.verify_receipt("0x123", cfg(), fake_rpc()).ok


def test_receipt_rpc_error_is_rejection_not_crash():
    def boom(m, p):
        raise OSError("down")
    r = g.verify_receipt(TX, cfg(), boom)
    assert not r.ok and "RPC" in r.reason


def test_receipt_erc20_transfer():
    token = "0x" + "99" * 20
    data = "0xa9059cbb" + MERCHANT[2:].lower().rjust(64, "0") + hex(PRICE)[2:].rjust(64, "0")

    def rpc(method, params):
        if method == "eth_getTransactionByHash":
            return {"from": "0x" + "11" * 20, "to": token, "value": "0x0", "input": data}
        return fake_rpc()(method, params)
    assert g.verify_receipt(TX, cfg(asset=token), rpc).ok


# ---- idempotency ----
def test_paid_calls_claim_once(tmp_path):
    p = g.PaidCalls(str(tmp_path / "d.db"))
    assert p.claim(TX, "0xp", PRICE, "get_spread") is True
    assert p.claim(TX.upper().replace("0X", "0x"), "0xp", PRICE, "get_spread") is False
    assert p.claim(TX2, "0xp", PRICE, "get_spread") is True
    p.release(TX2)
    assert p.claim(TX2, "0xp", PRICE, "get_spread") is True


# ---- gate ----
def gate(tmp_path, **kw):
    rpc = kw.pop("rpc", fake_rpc())
    return g.Gate(cfg(**kw), str(tmp_path / "d.db"), rpc=rpc, binance=kw.get("binance"))


def test_free_mode_bypasses(tmp_path):
    d = g.Gate(cfg(mode="free"), str(tmp_path / "d.db")).check(None, "get_spread", "/mcp")
    assert d.allowed and d.status == 200


def test_no_proof_returns_402_requirements(tmp_path):
    d = gate(tmp_path).check(None, "get_spread", "/mcp")
    assert not d.allowed and d.status == 402
    acc = d.body["accepts"][0]
    assert acc["payTo"] == MERCHANT and acc["maxAmountRequired"] == str(PRICE)
    assert acc["asset"] == "native" and d.body["x402Version"] == 1 and "Pago requerido" in d.body["error"]


def test_valid_proof_allows_then_replay_rejected(tmp_path):
    gt = gate(tmp_path)
    assert gt.check(TX, "get_spread", "/mcp").allowed
    d2 = gt.check(TX, "get_spread", "/mcp")
    assert not d2.allowed and d2.status == 402 and "ya fue usado" in d2.body["error"]


def test_invalid_proof_402_and_not_consumed(tmp_path):
    bad = gate(tmp_path, rpc=fake_rpc(status="0x0"))
    d = bad.check(TX, "get_spread", "/mcp")
    assert d.status == 402 and "status" in d.body["error"]
    rows = sqlite3.connect(str(tmp_path / "d.db")).execute("SELECT COUNT(*) FROM paid_calls").fetchone()[0]
    assert rows == 0


# ---- Binance b402 v2 adapter (mocked client) ----
class FakeClient:
    def __init__(self, verify_valid=True, settle_ok=True):
        self.calls = []
        self.v, self.s = verify_valid, settle_ok

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/supported"):
            data = {"kinds": [{"scheme": "exact", "network": "eip155:56"}]}
        elif path.endswith("/verify"):
            data = {"isValid": self.v, "payer": "0xpayer", "invalidReason": None if self.v else "bad_sig"}
        else:
            data = {"success": self.s, "transaction": TX, "payer": "0xpayer"}
        return 200, {"code": 0, "data": data}, ""


def test_binance_adapter_uses_v2_endpoints():
    fc = FakeClient()
    a = g.BinanceB402Adapter(fc)
    assert a.supported()["kinds"]
    assert a.verify({"p": 1}, {"r": 1}).get("isValid") is True
    assert a.settle({"p": 1}, {"r": 1})["success"] is True
    paths = [c[1] for c in fc.calls]
    assert paths == ["/api/v2/b402/supported", "/api/v2/b402/verify", "/api/v2/b402/settle"]
    assert all(c[0] == "POST" for c in fc.calls)


def test_binance_gate_flow_verify_then_settle(tmp_path):
    import base64, json
    proof = base64.b64encode(json.dumps({"signature": "0xsig", "nonce": "n1"}).encode()).decode()
    fc = FakeClient()
    gt = g.Gate(cfg(provider="binance"), str(tmp_path / "d.db"), binance=g.BinanceB402Adapter(fc))
    assert gt.check(proof, "get_spread", "/mcp").allowed
    assert [c[1].rsplit("/", 1)[1] for c in fc.calls] == ["verify", "settle"]
    d2 = gt.check(proof, "get_spread", "/mcp")  # same payload -> replay
    assert d2.status == 402


def test_binance_invalid_not_settled(tmp_path):
    import base64, json
    proof = base64.b64encode(json.dumps({"signature": "0xsig"}).encode()).decode()
    fc = FakeClient(verify_valid=False)
    gt = g.Gate(cfg(provider="binance"), str(tmp_path / "d.db"), binance=g.BinanceB402Adapter(fc))
    d = gt.check(proof, "get_spread", "/mcp")
    assert d.status == 402 and "bad_sig" in d.body["error"]
    assert all(not c[1].endswith("/settle") for c in fc.calls)


def test_binance_settle_failure_releases_claim(tmp_path):
    import base64, json
    proof = base64.b64encode(json.dumps({"signature": "0xsig"}).encode()).decode()
    fc = FakeClient(settle_ok=False)
    gt = g.Gate(cfg(provider="binance"), str(tmp_path / "d.db"), binance=g.BinanceB402Adapter(fc))
    assert gt.check(proof, "get_spread", "/mcp").status == 402
    fc.s = True
    assert gt.check(proof, "get_spread", "/mcp").allowed


def test_binance_without_credentials_gives_clear_402_not_crash(tmp_path):
    import base64, json
    proof = base64.b64encode(json.dumps({"s": 1}).encode()).decode()
    gt = g.Gate(cfg(provider="binance"), str(tmp_path / "d.db"), binance=None)
    gt._binance_factory = lambda: (_ for _ in ()).throw(FileNotFoundError("x"))
    d = gt.check(proof, "get_spread", "/mcp")
    assert d.status == 402 and "credenciales" in d.body["error"]


# ---- identity stub ----
def test_identity_stub_header_and_noop_reputation():
    i = g.IdentityStub("erc8004:bsc-testnet:REG#7")
    assert i.header_value() == "erc8004:bsc-testnet:REG#7"
    assert i.record_call(tool="get_spread", payer="0xp", tx_hash=TX) is None
