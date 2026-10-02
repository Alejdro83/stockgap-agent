"""MCP endpoint (/mcp): JSON-RPC shape, the 3 tools over a real SQLite fixture, 402 flow, free bypass."""
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from stockgap import b402_gate as g
from stockgap import dashboard, mcp_server
from tests.test_dashboard import make_db

MERCHANT = "0x069Ac6d8cf3553416fE5a4f0410ebEb758A8F819"
TX = "0x" + "ab" * 32


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params is not None else {})}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    make_db(str(db), extra_cols=", spread_bps_real TEXT")
    con = sqlite3.connect(db)
    con.execute("UPDATE spread_ticks SET spread_bps_real = '77.5' WHERE token_symbol='AAAon'")
    con.execute("ALTER TABLE oracle_predictions ADD COLUMN underlying_ticker TEXT")
    con.execute("ALTER TABLE oracle_predictions ADD COLUMN implied_open_price TEXT")
    con.execute("ALTER TABLE oracle_predictions ADD COLUMN last_reference_close TEXT")
    con.execute("ALTER TABLE oracle_predictions ADD COLUMN deviation_bps TEXT")
    con.execute("ALTER TABLE oracle_predictions ADD COLUMN token_symbol TEXT")
    con.execute("ALTER TABLE oracle_predictions ADD COLUMN cutoff_ts TEXT")
    con.execute("UPDATE oracle_predictions SET underlying_ticker='AAA', token_symbol='AAAon', "
                "implied_open_price='101.5', last_reference_close='100', deviation_bps='150', deviation_flag=1")
    con.commit(); con.close()
    monkeypatch.setenv("STOCKGAP_DB", str(db))
    monkeypatch.setenv("STOCKGAP_MCP_CONFIG", str(tmp_path / "none.env"))
    monkeypatch.delenv("MCP_PAY_MODE", raising=False)
    return TestClient(dashboard.app), db


def call(c, name, args=None, headers=None):
    return c.post("/mcp", json=rpc("tools/call", {"name": name, "arguments": args or {}}), headers=headers or {})


def test_initialize_and_tools_list_shape(env):
    c, _ = env
    r = c.post("/mcp", json=rpc("initialize", {}))
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "stockgap"
    r = c.post("/mcp", json=rpc("tools/list"))
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert set(tools) == {"get_spread", "get_cross_issuer", "get_oracle_latest"}
    for t in tools.values():
        assert t["description"] and t["inputSchema"]["type"] == "object"


def test_identity_header_present(env):
    c, _ = env
    assert c.post("/mcp", json=rpc("tools/list")).headers["X-ERC8004-Identity"].startswith("erc8004:")


def test_get_spread_prefers_real_metric(env):
    c, _ = env
    r = call(c, "get_spread")
    assert r.status_code == 200
    out = r.json()["result"]
    assert out["isError"] is False
    data = out["structuredContent"]
    assert data["metric"] == "spread_bps_real"
    assert data["rows"][0]["token_symbol"] == "BBBB" or data["rows"][0]["metric_bps"] is not None
    sym = {x["token_symbol"]: x for x in data["rows"]}
    assert sym["AAAon"]["metric_bps"] == 77.5
    assert out["content"][0]["type"] == "text"


def test_get_spread_filter_symbol(env):
    c, _ = env
    rows = call(c, "get_spread", {"token_symbol": "aaaon"}).json()["result"]["structuredContent"]["rows"]
    assert [r["token_symbol"] for r in rows] == ["AAAon"]


def test_get_spread_without_real_column_falls_back(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    make_db(str(db))
    monkeypatch.setenv("STOCKGAP_DB", str(db))
    monkeypatch.setenv("STOCKGAP_MCP_CONFIG", str(tmp_path / "none.env"))
    d = call(TestClient(dashboard.app), "get_spread").json()["result"]["structuredContent"]
    assert d["metric"] == "spread_bps" and len(d["rows"]) == 2


def test_get_cross_issuer(env):
    c, _ = env
    d = call(c, "get_cross_issuer", {"underlying": "soxl"}).json()["result"]["structuredContent"]
    assert d["rows"][0]["underlying_ticker"] == "SOXL" and d["rows"][0]["spread_bps"] == 108.4
    assert call(c, "get_cross_issuer", {"underlying": "ZZZ"}).json()["result"]["structuredContent"]["rows"] == []


def test_get_oracle_latest(env):
    c, _ = env
    d = call(c, "get_oracle_latest", {"underlying": "AAA"}).json()["result"]["structuredContent"]
    row = d["rows"][0]
    assert row["implied_open_price"] == 101.5 and row["deviation_bps"] == 150.0 and row["deviation_flag"] is True
    assert d["snapshot_ts"]


def test_unknown_tool_and_method_errors(env):
    c, _ = env
    r = call(c, "nope").json()
    assert r["error"]["code"] == -32602
    assert c.post("/mcp", json=rpc("bogus")).json()["error"]["code"] == -32601
    assert c.post("/mcp", content=b"{not json", headers={"content-type": "application/json"}).json()["error"]["code"] == -32700


def test_missing_db_returns_tool_error_not_500(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKGAP_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("STOCKGAP_MCP_CONFIG", str(tmp_path / "none.env"))
    r = call(TestClient(dashboard.app), "get_spread")
    assert r.status_code == 200 and r.json()["result"]["isError"] is True


def test_notification_gets_202(env):
    c, _ = env
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202


# ---- paid mode ----
def paid_gate(db, rpc_fn):
    cfg = g.GateConfig(mode="b402", provider="local_receipt", merchant=MERCHANT,
                       price_wei=100000000000000, asset="native", max_block_age_s=3600)
    return g.Gate(cfg, str(db), rpc=rpc_fn)


def good_rpc(m, p):
    if m == "eth_getTransactionByHash":
        return {"from": "0x" + "11" * 20, "to": MERCHANT, "value": hex(10**14), "input": "0x"}
    if m == "eth_getTransactionReceipt":
        return {"status": "0x1", "to": MERCHANT, "blockNumber": "0x1"}
    return {"timestamp": hex(int(time.time()) - 5)}


def test_b402_mode_402_without_proof_but_list_stays_free(env, monkeypatch):
    c, db = env
    monkeypatch.setattr(mcp_server, "get_gate", lambda: paid_gate(db, good_rpc))
    r = call(c, "get_spread")
    assert r.status_code == 402
    assert r.json()["accepts"][0]["payTo"] == MERCHANT
    assert c.post("/mcp", json=rpc("tools/list")).status_code == 200


def test_b402_mode_accepts_proof_once(env, monkeypatch):
    c, db = env
    gt = paid_gate(db, good_rpc)
    monkeypatch.setattr(mcp_server, "get_gate", lambda: gt)
    r = call(c, "get_spread", headers={"X-Payment": TX})
    assert r.status_code == 200 and r.json()["result"]["isError"] is False
    assert call(c, "get_spread", headers={"X-Payment": TX}).status_code == 402


def test_free_mode_ignores_payment_header(env):
    c, _ = env
    assert call(c, "get_spread", headers={"X-Payment": "garbage"}).status_code == 200


def test_mode_toggle_via_env(env, monkeypatch):
    c, _ = env
    monkeypatch.setenv("MCP_PAY_MODE", "b402")
    assert call(c, "get_spread").status_code == 402
    monkeypatch.setenv("MCP_PAY_MODE", "free")
    assert call(c, "get_spread").status_code == 200
