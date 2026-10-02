"""StockGap mobile-first dashboard — read-only SQLite viewer, port 8098.

Stack: FastAPI + Jinja2 + HTMX + Tailwind CDN (no build step).
Every state (loading / empty / stale / error) renders a visible message.

Run:  python3 scripts/dashboard.py     (restart one-liner: see DX-LOG.md)
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import mcp_server

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "spread_ticks.db"
ALERTS_CONFIG = REPO_ROOT / "config" / "alerts.env"
TEST_STATUS_FILE = REPO_ROOT / "data" / "pytest_status.json"

STALE_AFTER_S = 300  # > 5 min without ticks => stale warning
GREEN_MAX_S = 120
STARTED_AT = time.time()

# The 3 real broadcast transactions (BSC) — all reverted; no more are sent.
REAL_TXS = [
    "0xfdc9806cd4791e10ca806075b2dfb84d040d414d55cab37ea08f943cf8f32021",
    "0x9ca395c7db3b277fe3317c45be4ddc7bf1e09755241dff32911a88c42c7c7dfc",
    "0x7c3379a18636c68c3bb0dc23dbcee6fefcff916baf74c1302ae4a88c4817446f",
]
TX_STATUS = "REVERTED"
TX_ROOT_CAUSE = "caducidad de ruta de 2s embebida en el calldata — quirk no documentado"

ALERT_KEYS = [
    "SPREAD_THRESHOLD_BPS", "CROSS_ISSUER_THRESHOLD_BPS", "ALERT_RATE_LIMIT_MIN",
    "ALERT_SCAN_INTERVAL", "ALERT_DIGEST_MODE", "ALERTS_ENABLED",
    "ALERT_SPREAD_ENABLED", "ALERT_CROSS_ENABLED", "TELEGRAM_ALERTS", "ALERT_FILE_LOG",
]

app = FastAPI(title="StockGap Dashboard")
app.include_router(mcp_server.router)  # POST /mcp (MCP + pay-per-call gate)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class DbError(Exception):
    """DB unreadable — the UI shows a visible error banner."""


def get_db_path() -> Path:
    return Path(os.environ.get("STOCKGAP_DB", str(DEFAULT_DB)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_s(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (_now() - dt).total_seconds())


def fmt_age(age: float | None) -> str:
    if age is None:
        return "sin dato"
    age = int(age)
    if age < 90:
        return f"{age}s"
    if age < 5400:
        return f"{age // 60} min"
    if age < 172800:
        return f"{age // 3600} h"
    return f"{age // 86400} d"


def health_badge(age: float | None) -> dict:
    if age is None:
        return {"level": "RED", "text": "SIN DATOS: el collector nunca registró un tick"}
    if age <= GREEN_MAX_S:
        return {"level": "GREEN", "text": f"OK — último tick hace {fmt_age(age)}"}
    if age <= STALE_AFTER_S:
        return {"level": "YELLOW", "text": f"RETARDO — último tick hace {fmt_age(age)}"}
    return {"level": "RED", "text": f"ESTANCADO — último tick hace {fmt_age(age)} (>5 min)"}


def fnum(v, nd: int = 2):
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return "—"


templates.env.filters["num"] = fnum
templates.env.filters["age"] = lambda ts: fmt_age(_age_s(ts))


def with_db(fn):
    """Open the DB read-only+immutable and run fn(conn, ...). Any failure -> DbError."""

    def wrapper(*args, **kwargs):
        path = get_db_path()
        if not path.exists():
            raise DbError(f"No se pudo leer la base de datos: {path.name} no existe.")
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                return fn(conn, *args, **kwargs)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise DbError(f"No se pudo leer la base de datos ({exc}).") from exc

    return wrapper


def table_columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def load_alerts_config() -> dict:
    """Thresholds/toggles from config/alerts.env (real env vars win, like alerts.py)."""
    cfg: dict[str, str] = {}
    try:
        for line in ALERTS_CONFIG.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() in ALERT_KEYS:
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    for k in ALERT_KEYS:
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg


def _latest_tick_where(conn, table: str) -> tuple[str, tuple]:
    row = conn.execute(f"SELECT MAX(tick_id) FROM {table}").fetchone()
    return ("tick_id = ?", (row[0],)) if row and row[0] is not None else ("", ())


@with_db
def gaps_data(conn, closed_only: bool) -> dict:
    """Top 20 |spread| of the latest tick. Prefers spread_bps_real when present."""
    cols = table_columns(conn, "spread_ticks")
    metric = "spread_bps_real" if "spread_bps_real" in cols else "spread_bps"
    where, params = _latest_tick_where(conn, "spread_ticks")
    out = {"rows": [], "metric": metric, "closed_only": closed_only,
           "last_ts": None, "total": 0}
    if not where:
        return out
    out["last_ts"] = conn.execute(
        "SELECT MAX(ts_utc) FROM spread_ticks WHERE " + where, params).fetchone()[0]
    out["total"] = conn.execute(
        "SELECT COUNT(*) FROM spread_ticks WHERE " + where, params).fetchone()[0]
    # Fall back to spread_bps per row if the preferred column is NULL there.
    expr = (f"COALESCE(CAST({metric} AS REAL), CAST(spread_bps AS REAL))"
            if metric != "spread_bps" else "CAST(spread_bps AS REAL)")
    kind = ", reference_kind" if "reference_kind" in cols else ""
    closed = " AND COALESCE(open_state, 0) = 0" if closed_only else ""
    sql = (f"SELECT token_symbol, underlying_ticker, platform_id, token_price, "
           f"token_to_share_ratio, reference_price, market_status, open_state, "
           f"{expr} AS metric_bps{kind} FROM spread_ticks WHERE {where}{closed} "
           f"AND {expr} IS NOT NULL ORDER BY ABS({expr}) DESC LIMIT 20")
    rows = []
    for r in conn.execute(sql, params):
        d = dict(r)
        try:
            d["ratio_differs"] = abs(float(d["token_to_share_ratio"]) - 1.0) > 1e-6
        except (TypeError, ValueError):
            d["ratio_differs"] = False
        rows.append(d)
    out["rows"] = rows
    return out


@with_db
def cross_data(conn) -> dict:
    where, params = _latest_tick_where(conn, "cross_issuer_ticks")
    if not where:
        return {"rows": [], "last_ts": None}
    rows = [dict(r) for r in conn.execute(
        "SELECT underlying_ticker, n_issuers, cheap_symbol, cheap_platform, cheap_implied, "
        "rich_symbol, rich_platform, rich_implied, ts_utc, CAST(spread_bps AS REAL) AS bps "
        "FROM cross_issuer_ticks WHERE " + where +
        " ORDER BY ABS(CAST(spread_bps AS REAL)) DESC LIMIT 20", params)]
    return {"rows": rows, "last_ts": rows[0]["ts_utc"] if rows else None}


@with_db
def oracle_data(conn) -> dict:
    today = _now().strftime("%Y-%m-%d")
    count_today = conn.execute(
        "SELECT COUNT(*) FROM oracle_predictions WHERE snapshot_ts >= ?", (today,)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM oracle_predictions").fetchone()[0]
    latest = conn.execute("SELECT MAX(snapshot_ts) FROM oracle_predictions").fetchone()[0]
    flagged = 0
    if latest:
        flagged = conn.execute(
            "SELECT COUNT(*) FROM oracle_predictions WHERE snapshot_ts = ? AND deviation_flag = 1",
            (latest,)).fetchone()[0]
    rep_exists = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oracle_reputation'").fetchone())
    reputation, rep_n = [], 0
    if rep_exists:
        rep_n = conn.execute("SELECT COUNT(*) FROM oracle_reputation").fetchone()[0]
        reputation = [dict(r) for r in conn.execute(
            "SELECT underlying_ticker, token_symbol, predicted_open, actual_open, "
            "abs_pct_error, direction_hit, oracle_score FROM oracle_reputation "
            "ORDER BY id DESC LIMIT 20")]
    return {"count_today": count_today, "total": total, "latest_ts": latest,
            "flagged": flagged, "reputation_exists": rep_exists,
            "reputation": reputation, "rep_n": rep_n}


def read_test_status() -> dict | None:
    try:
        return json.loads(TEST_STATUS_FILE.read_text())
    except (OSError, ValueError):
        return None


@with_db
def sistema_data(conn) -> dict:
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM collector_meta")}
    last = meta.get("last_tick_ts")
    if not last:
        last = conn.execute("SELECT MAX(ts_utc) FROM spread_ticks").fetchone()[0]
    age = _age_s(last)
    return {
        "badge": health_badge(age), "last_tick_ts": last,
        "stale": age is None or age > STALE_AFTER_S,
        "universe_count": meta.get("universe_count"),
        "n_ticks": conn.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0],
        "n_cross": conn.execute("SELECT COUNT(*) FROM cross_issuer_ticks").fetchone()[0],
    }


def _ctx(request: Request, **extra) -> dict:
    return {"request": request, "now_utc": _now().strftime("%Y-%m-%d %H:%M:%SZ"), **extra}


def _render(request: Request, template: str, loader, *args):
    try:
        data = loader(*args)
    except DbError as exc:
        return templates.TemplateResponse(request, "frag_error.html", _ctx(request, error=str(exc)))
    return templates.TemplateResponse(request, template, _ctx(request, **data))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


@app.get("/frag/gaps", response_class=HTMLResponse)
def frag_gaps(request: Request, closed_only: int = 0):
    return _render(request, "frag_gaps.html", gaps_data, bool(closed_only))


@app.get("/frag/cross", response_class=HTMLResponse)
def frag_cross(request: Request):
    return _render(request, "frag_cross.html", cross_data)


@app.get("/frag/oraculo", response_class=HTMLResponse)
def frag_oraculo(request: Request):
    return _render(request, "frag_oraculo.html", oracle_data)


@app.get("/frag/sistema", response_class=HTMLResponse)
def frag_sistema(request: Request):
    try:
        data = sistema_data()
    except DbError as exc:
        return templates.TemplateResponse(request, "frag_error.html", _ctx(request, error=str(exc)))
    # Non-DB info is still shown when the DB is fine; on DB error the banner replaces all.
    data.update(
        alerts=load_alerts_config(), txs=REAL_TXS, tx_status=TX_STATUS,
        tx_root_cause=TX_ROOT_CAUSE, tests=read_test_status(),
        uptime=fmt_age(time.time() - STARTED_AT),
    )
    return templates.TemplateResponse(request, "frag_sistema.html", _ctx(request, **data))


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DASHBOARD_PORT", "8098")),
                log_level="warning")


if __name__ == "__main__":
    main()
