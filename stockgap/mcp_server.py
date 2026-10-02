"""StockGap MCP server — minimal MCP over HTTP (JSON-RPC 2.0) at POST /mcp.

Tools (read-only SQLite): get_spread, get_cross_issuer, get_oracle_latest.
`initialize` / `tools/list` are always free; `tools/call` goes through the pay-per-call gate
(stockgap.b402_gate, config/mcp.env). Mounted on the dashboard app (port 8098) via `router`.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import b402_gate

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "spread_ticks.db"
PROTOCOL_VERSION = "2025-03-26"
LIMIT = 20

router = APIRouter()


class ToolError(Exception):
    pass


def get_db_path() -> Path:
    return Path(os.environ.get("STOCKGAP_DB", str(DEFAULT_DB)))


def get_gate() -> b402_gate.Gate:
    return b402_gate.Gate(b402_gate.load_config(), str(get_db_path()))


def _connect() -> sqlite3.Connection:
    path = get_db_path()
    if not path.exists():
        raise ToolError(f"No se pudo leer la base de datos: {path.name} no existe.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cols(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _latest(conn, table: str) -> int | None:
    return conn.execute(f"SELECT MAX(tick_id) FROM {table}").fetchone()[0]


# --------------------------------------------------------------------------- tools
def tool_get_spread(conn, args: dict) -> dict:
    cols = _cols(conn, "spread_ticks")
    metric = "spread_bps_real" if "spread_bps_real" in cols else "spread_bps"
    tick = _latest(conn, "spread_ticks")
    out: dict = {"metric": metric, "tick_ts": None, "rows": []}
    if tick is None:
        return out
    expr = (f"COALESCE(CAST({metric} AS REAL), CAST(spread_bps AS REAL))"
            if metric != "spread_bps" else "CAST(spread_bps AS REAL)")
    sym = str(args.get("token_symbol") or "").strip()
    where, params = "tick_id = ? AND " + expr + " IS NOT NULL", [tick]
    if sym:
        where += " AND UPPER(token_symbol) = UPPER(?)"
        params.append(sym)
    out["tick_ts"] = conn.execute("SELECT MAX(ts_utc) FROM spread_ticks WHERE tick_id=?", (tick,)).fetchone()[0]
    for r in conn.execute(
            f"SELECT token_symbol, underlying_ticker, platform_id, token_price, reference_price, "
            f"market_status, open_state, {expr} AS metric_bps FROM spread_ticks WHERE {where} "
            f"ORDER BY ABS({expr}) DESC LIMIT {LIMIT}", params):
        d = dict(r)
        d["metric_bps"] = _f(d["metric_bps"])
        d["token_price"], d["reference_price"] = _f(d["token_price"]), _f(d["reference_price"])
        out["rows"].append(d)
    return out


def tool_get_cross_issuer(conn, args: dict) -> dict:
    tick = _latest(conn, "cross_issuer_ticks")
    out: dict = {"tick_ts": None, "rows": []}
    if tick is None:
        return out
    und = str(args.get("underlying") or "").strip()
    where, params = "tick_id = ?", [tick]
    if und:
        where += " AND UPPER(underlying_ticker) = UPPER(?)"
        params.append(und)
    for r in conn.execute(
            "SELECT underlying_ticker, n_issuers, cheap_symbol, cheap_platform, cheap_implied, rich_symbol, "
            "rich_platform, rich_implied, ts_utc, CAST(spread_bps AS REAL) AS spread_bps FROM cross_issuer_ticks "
            f"WHERE {where} ORDER BY ABS(CAST(spread_bps AS REAL)) DESC LIMIT {LIMIT}", params):
        d = dict(r)
        for k in ("cheap_implied", "rich_implied"):
            d[k] = _f(d[k])
        out["rows"].append(d)
    out["tick_ts"] = out["rows"][0]["ts_utc"] if out["rows"] else None
    return out


def tool_get_oracle_latest(conn, args: dict) -> dict:
    snap = conn.execute("SELECT MAX(snapshot_ts) FROM oracle_predictions").fetchone()[0]
    out: dict = {"snapshot_ts": snap, "rows": []}
    if not snap:
        return out
    und = str(args.get("underlying") or "").strip()
    where, params = "snapshot_ts = ?", [snap]
    if und:
        where += " AND UPPER(underlying_ticker) = UPPER(?)"
        params.append(und)
    for r in conn.execute(
            "SELECT underlying_ticker, token_symbol, implied_open_price, last_reference_close, deviation_bps, "
            f"deviation_flag, cutoff_ts FROM oracle_predictions WHERE {where} "
            f"ORDER BY ABS(CAST(deviation_bps AS REAL)) DESC LIMIT {LIMIT}", params):
        d = dict(r)
        for k in ("implied_open_price", "last_reference_close", "deviation_bps"):
            d[k] = _f(d[k])
        d["deviation_flag"] = bool(d["deviation_flag"])
        out["rows"].append(d)
    return out


TOOLS: dict[str, tuple[str, dict, Callable]] = {
    "get_spread": (
        "Mayores spreads actuales (bps) entre el token tokenizado y su subyacente; "
        "`metric` indica la métrica usada (spread_bps_real si existe, si no spread_bps).",
        {"token_symbol": {"type": "string", "description": "Filtra por símbolo del token (opcional)"}},
        tool_get_spread),
    "get_cross_issuer": (
        "Líderes de spread entre emisores del mismo subyacente (barato vs caro).",
        {"underlying": {"type": "string", "description": "Ticker del subyacente (opcional)"}},
        tool_get_cross_issuer),
    "get_oracle_latest": (
        "Último snapshot del oráculo de fin de semana: precio de apertura del lunes implícito y desviación vs cierre.",
        {"underlying": {"type": "string", "description": "Ticker del subyacente (opcional)"}},
        tool_get_oracle_latest),
}


def tools_list() -> list[dict]:
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": p, "additionalProperties": False}}
            for n, (d, p, _) in TOOLS.items()]


# --------------------------------------------------------------------------- JSON-RPC plumbing
def _ok(id_, result) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


def run_tool(name: str, args: dict) -> dict:
    try:
        conn = _connect()
        try:
            data = TOOLS[name][2](conn, args)
        finally:
            conn.close()
    except (ToolError, sqlite3.Error) as exc:
        return {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "structuredContent": data, "isError": False}


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    gate = get_gate()
    headers = {"X-ERC8004-Identity": gate.identity.header_value()}

    def reply(content, status=200):
        return JSONResponse(content, status_code=status, headers=headers)

    try:
        msg = await request.json()
    except ValueError:
        return reply(_err(None, -32700, "JSON inválido"))
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return reply(_err(None, -32600, "Solicitud JSON-RPC inválida"))
    id_, method = msg.get("id"), msg["method"]
    params = msg.get("params") or {}
    if "id" not in msg:  # notification
        return JSONResponse(None, status_code=202, headers=headers)
    if method == "initialize":
        return reply(_ok(id_, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                               "serverInfo": {"name": "stockgap", "version": "0.1.0"}}))
    if method == "ping":
        return reply(_ok(id_, {}))
    if method == "tools/list":
        return reply(_ok(id_, {"tools": tools_list()}))
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS or not isinstance(args, dict):
            return reply(_err(id_, -32602, f"Herramienta desconocida o argumentos inválidos: {name}"))
        decision = gate.check(request.headers.get("x-payment"), name, str(request.url.path))
        if not decision.allowed:
            return reply(decision.body, decision.status)
        if decision.proof_id:
            gate.identity.record_call(tool=name, payer=decision.payer, tx_hash=decision.proof_id)
        return reply(_ok(id_, run_tool(name, args)))
    return reply(_err(id_, -32601, f"Método no encontrado: {method}"))
