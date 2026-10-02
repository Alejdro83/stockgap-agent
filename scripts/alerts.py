#!/usr/bin/env python3
"""StockGap alert runner: scan new spread ticks → Telegram/file alerts.

Usage:
    python3 scripts/alerts.py             # one scan (cron-friendly)
    python3 scripts/alerts.py --loop      # standalone loop (scan_interval_s)
    python3 scripts/alerts.py --dry-run   # print alerts, nothing is sent
    python3 scripts/alerts.py --flush-digest  # send pending digest now
    python3 scripts/alerts.py --test      # send one delivery-test message

State lives in the small ``alert_state`` table of the collector DB:
``last_seen_spread_rowid`` / ``last_seen_cross_rowid`` (scan watermarks),
``prev_bps_json`` (per-key previous spread_bps, for crossing detection) and
``rate_json`` (per-key last-alert epoch, for the rate limit).

All thresholds and toggles come from env vars / config/alerts.env (see
stockgap/alerts.py:load_config) — nothing is hardcoded here.

Env: STOCKGAP_DB (default data/spread_ticks.db) overrides the DB path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockgap.alerts import (  # noqa: E402
    AlertConfig,
    AlertEvent,
    RateLimiter,
    build_transport,
    find_crossings,
    format_cross_alert,
    format_spread_alert,
    load_config,
    metric_column,
)

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = os.environ.get("STOCKGAP_DB", str(DATA / "spread_ticks.db"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA / "alerts.log"),
    ],
)
log = logging.getLogger("stockgap.alerts.runner")

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class AlertState:
    """Tiny key/value state table in the collector DB."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.con.executescript(STATE_SCHEMA)
        self.con.commit()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.con.execute(
            "SELECT value FROM alert_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO alert_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.con.commit()

    def get_json(self, key: str) -> Dict[str, Any]:
        raw = self.get(key)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def set_json(self, key: str, obj: Dict[str, Any]) -> None:
        self.set(key, json.dumps(obj))


def _rows(con: sqlite3.Connection, sql: str, params: tuple) -> List[Dict[str, Any]]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _bootstrap_watermarks(con: sqlite3.Connection, state: AlertState,
                          metric: str = "real") -> None:
    """First run: start from the newest rows instead of alerting on history."""
    if state.get("last_seen_spread_rowid") is None:
        row = con.execute("SELECT MAX(id) FROM spread_ticks").fetchone()
        state.set("last_seen_spread_rowid", str(row[0] or 0))
    if state.get("last_seen_cross_rowid") is None:
        row = con.execute("SELECT MAX(id) FROM cross_issuer_ticks").fetchone()
        state.set("last_seen_cross_rowid", str(row[0] or 0))
    if not state.get_json("prev_bps_json"):
        # seed per-key previous values from the newest row per key so the
        # first real scan only fires on genuine crossings. Spread keys use the
        # ALERT_METRIC column (spread_bps_real by default) and skip rows whose
        # metric is missing (historical / cache-cold rows) — those never seeded
        # a 'real' observation, so the first real one counts as prev=None.
        prev: Dict[str, Any] = {}
        col = metric_column(metric)
        for r in _rows(
            con,
            f"SELECT token_symbol, {col} AS bps FROM spread_ticks s "
            "WHERE id = (SELECT MAX(id) FROM spread_ticks "
            "            WHERE token_symbol = s.token_symbol)",
            (),
        ):
            if r["bps"] is None:
                continue
            prev["spread:" + str(r["token_symbol"])] = str(r["bps"])
        for r in _rows(
            con,
            "SELECT underlying_ticker, spread_bps FROM cross_issuer_ticks c "
            "WHERE id = (SELECT MAX(id) FROM cross_issuer_ticks "
            "            WHERE underlying_ticker = c.underlying_ticker)",
            (),
        ):
            prev["cross:" + str(r["underlying_ticker"])] = str(r["spread_bps"])
        state.set_json("prev_bps_json", prev)


def _market_state_for(con: sqlite3.Connection, tick_id: Any,
                      ticker: Any) -> Dict[str, Any]:
    rows = _rows(
        con,
        "SELECT market_status, open_state FROM spread_ticks "
        "WHERE tick_id = ? AND underlying_ticker = ? LIMIT 1",
        (tick_id, ticker),
    )
    return rows[0] if rows else {"market_status": None, "open_state": None}


def build_events(con: sqlite3.Connection, cfg: AlertConfig,
                 state: AlertState) -> List[AlertEvent]:
    """Scan new rows since the watermarks and build threshold-crossing events."""
    events: List[AlertEvent] = []
    prev_map: Dict[str, Any] = state.get_json("prev_bps_json")

    if cfg.spread_enabled:
        last_id = int(state.get("last_seen_spread_rowid", "0") or 0)
        rows = _rows(
            con,
            "SELECT * FROM spread_ticks WHERE id > ? ORDER BY id ASC",
            (last_id,),
        )
        for hit in find_crossings(
            rows, prev_map, cfg.spread_threshold_bps,
            key_fn=lambda r: "spread:" + str(r.get("token_symbol")),
            metric=cfg.alert_metric,  # real (default) | derived — see ALERT_METRIC
        ):
            events.append(
                AlertEvent(kind="spread", key=hit["key"],
                           row=hit["row"], message=format_spread_alert(
                               hit["row"], metric=cfg.alert_metric))
            )
        if rows:
            state.set("last_seen_spread_rowid", str(rows[-1]["id"]))

    if cfg.cross_enabled:
        last_id = int(state.get("last_seen_cross_rowid", "0") or 0)
        rows = _rows(
            con,
            "SELECT * FROM cross_issuer_ticks WHERE id > ? ORDER BY id ASC",
            (last_id,),
        )
        for hit in find_crossings(
            rows, prev_map, cfg.cross_threshold_bps,
            key_fn=lambda r: "cross:" + str(r.get("underlying_ticker")),
            metric="derived",  # cross_issuer_ticks only has spread_bps — and it
                               # is issuer-vs-issuer, never the derived reference
        ):
            market = _market_state_for(
                con, hit["row"].get("tick_id"), hit["row"].get("underlying_ticker")
            )
            events.append(
                AlertEvent(
                    kind="cross", key=hit["key"], row=hit["row"],
                    message=format_cross_alert(
                        hit["row"],
                        open_state=market.get("open_state"),
                        market_status=market.get("market_status"),
                    ),
                )
            )
        if rows:
            state.set("last_seen_cross_rowid", str(rows[-1]["id"]))

    state.set_json("prev_bps_json", prev_map)
    return events


def deliver(events: List[AlertEvent], cfg: AlertConfig, state: AlertState,
            transport: Any, dry_run: bool = False) -> int:
    """Apply the per-token rate limit and send (immediate or digest mode)."""
    if not events:
        return 0
    limiter = RateLimiter(cfg.rate_limit_s, state.get_json("rate_json"))
    allowed = [e for e in events if limiter.allow(e.key)]
    dropped = len(events) - len(allowed)
    if dropped:
        log.info("rate limit: %d evento(s) suprimido(s)", dropped)
    state.set_json("rate_json", limiter.state)
    if not allowed:
        return 0

    if dry_run:
        for e in allowed:
            print(f"--- [{e.kind}] {e.key}\n{e.message}\n")
        return len(allowed)

    if cfg.digest_mode:
        buf: List[Dict[str, str]] = state.get_json("digest_buffer").get("events", [])
        buf.extend({"kind": e.kind, "key": e.key, "message": e.message}
                   for e in allowed)
        state.set_json("digest_buffer", {"events": buf})
        last_digest = float(state.get("last_digest_ts", "0") or 0)
        max_buf = int(os.environ.get("ALERT_DIGEST_MAX", "20"))
        if (time.time() - last_digest) >= cfg.digest_interval_s or len(buf) >= max_buf:
            return flush_digest(cfg, state, transport)
        log.info("digest: %d evento(s) en buffer", len(buf))
        return len(allowed)

    sent = 0
    for e in allowed:
        if transport.send(e.message):
            sent += 1
    log.info("enviadas %d/%d alertas", sent, len(allowed))
    return sent


def flush_digest(cfg: AlertConfig, state: AlertState, transport: Any) -> int:
    buf = state.get_json("digest_buffer").get("events", [])
    if not buf:
        return 0
    header = f"📬 DIGEST StockGap — {len(buf)} alerta(s)"
    body = "\n\n".join(e["message"] for e in buf)
    ok = transport.send(f"{header}\n\n{body}")
    state.set_json("digest_buffer", {"events": []})
    state.set("last_digest_ts", str(time.time()))
    log.info("digest enviado: %d eventos (ok=%s)", len(buf), ok)
    return len(buf) if ok else 0


def scan_once(cfg: AlertConfig, dry_run: bool = False) -> int:
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        state = AlertState(con)
        _bootstrap_watermarks(con, state, cfg.alert_metric)
        if not cfg.enabled:
            log.info("ALERTS_ENABLED=0 — scan omitido")
            return 0
        events = build_events(con, cfg, state)
        if events:
            log.info("%d cruce(s) de umbral detectados", len(events))
        transport = build_transport(cfg)
        return deliver(events, cfg, state, transport, dry_run=dry_run)
    finally:
        con.close()


def send_test(cfg: AlertConfig) -> bool:
    transport = build_transport(cfg)
    msg = (
        "✅ StockGap ALERTAS ACTIVAS (mensaje de prueba)\n"
        f"Umbral spread: {cfg.spread_threshold_bps} bps | "
        f"cross-issuer: {cfg.cross_threshold_bps} bps\n"
        f"Rate limit: 1 alerta/token/{cfg.rate_limit_min:.0f} min | "
        f"digest: {'sí' if cfg.digest_mode else 'no'}"
    )
    ok = transport.send(msg)
    log.info("mensaje de prueba enviado (ok=%s)", ok)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true",
                        help="modo bucle (por defecto: un scan, apto para cron)")
    parser.add_argument("--dry-run", action="store_true",
                        help="imprime alertas sin enviar nada")
    parser.add_argument("--flush-digest", action="store_true",
                        help="envía ya el digest pendiente")
    parser.add_argument("--test", action="store_true",
                        help="envía un mensaje de prueba de entrega")
    args = parser.parse_args()

    cfg = load_config()

    def _stop(signum, _frame):
        log.info("signal %s recibido, saliendo", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if args.test:
        return 0 if send_test(cfg) else 1

    if args.flush_digest:
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            return 0 if flush_digest(cfg, AlertState(con), build_transport(cfg)) else 1
        finally:
            con.close()

    if args.loop:
        log.info("alert loop: interval=%ss db=%s digest=%s",
                 cfg.scan_interval_s, DB_PATH, cfg.digest_mode)
        while True:
            started = time.time()
            try:
                scan_once(cfg, dry_run=args.dry_run)
            except Exception as exc:  # noqa: BLE001 — 24/7 survival
                log.exception("scan failed: %s", exc)
            elapsed = time.time() - started
            time.sleep(max(1.0, cfg.scan_interval_s - elapsed))

    scan_once(cfg, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
