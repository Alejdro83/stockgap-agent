#!/usr/bin/env python3
"""StockGap windowed summary CLI.

Prints, for a time window over data/spread_ticks.db:
* per-token spread stats (min/max/mean spread_bps, tick count)
* top 5 widest gaps in the window
* cross-issuer leaders (best arb pairs)
* how many ticks the window contains

Usage:
    python3 scripts/summary.py                    # last 24h
    python3 scripts/summary.py --hours 6
    python3 scripts/summary.py --minutes 90
    python3 scripts/summary.py --since 2026-10-01T00:00:00Z
    python3 scripts/summary.py --json             # machine-readable

Env: STOCKGAP_DB overrides the DB path.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
DB_PATH = os.environ.get("STOCKGAP_DB", str(DATA / "spread_ticks.db"))


def _window_start(hours: Optional[float], minutes: Optional[float],
                  since: Optional[str]) -> str:
    if since:
        return since
    delta = timedelta(hours=hours or 0, minutes=minutes or 0)
    if delta.total_seconds() <= 0:
        delta = timedelta(hours=24)
    start = datetime.now(timezone.utc) - delta
    return start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start.microsecond // 1000:03d}Z"


def collect(db_path: str, start_iso: str) -> Dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        def rows(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

        total_ticks = rows(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT tick_id) AS ticks, "
            "       MIN(ts_utc) AS first_ts, MAX(ts_utc) AS last_ts "
            "FROM spread_ticks WHERE ts_utc >= ?",
            (start_iso,),
        )[0]

        per_token = rows(
            "SELECT token_symbol, underlying_ticker, platform_id, "
            "       COUNT(*) AS n, "
            "       ROUND(MIN(CAST(spread_bps AS REAL)), 2) AS min_bps, "
            "       ROUND(MAX(CAST(spread_bps AS REAL)), 2) AS max_bps, "
            "       ROUND(AVG(CAST(spread_bps AS REAL)), 2) AS mean_bps "
            "FROM spread_ticks WHERE ts_utc >= ? "
            "GROUP BY token_symbol "
            "ORDER BY ABS(MAX(CAST(spread_bps AS REAL))) DESC",
            (start_iso,),
        )

        widest = rows(
            "SELECT token_symbol, underlying_ticker, platform_id, ts_utc, "
            "       ROUND(CAST(spread_bps AS REAL), 2) AS spread_bps, "
            "       ROUND(CAST(spread_pct AS REAL), 2) AS spread_pct, "
            "       ROUND(CAST(token_price AS REAL), 4) AS token_price, "
            "       ROUND(CAST(reference_price AS REAL), 4) AS reference_price, "
            "       market_status, open_state "
            "FROM spread_ticks WHERE ts_utc >= ? "
            "ORDER BY ABS(CAST(spread_bps AS REAL)) DESC LIMIT 5",
            (start_iso,),
        )

        cross_total = rows(
            "SELECT COUNT(*) AS n FROM cross_issuer_ticks WHERE ts_utc >= ?",
            (start_iso,),
        )[0]["n"]

        cross_leaders = rows(
            "SELECT underlying_ticker, n_issuers, cheap_symbol, cheap_platform, "
            "       rich_symbol, rich_platform, ts_utc, "
            "       ROUND(CAST(spread_bps AS REAL), 2) AS spread_bps "
            "FROM cross_issuer_ticks WHERE ts_utc >= ? "
            "ORDER BY ABS(CAST(spread_bps AS REAL)) DESC LIMIT 5",
            (start_iso,),
        )

        return {
            "window_start": start_iso,
            "window_end": total_ticks["last_ts"],
            "ticks_in_window": total_ticks["n"],
            "tick_ids_in_window": total_ticks["ticks"],
            "cross_ticks_in_window": cross_total,
            "per_token": per_token,
            "top5_widest_gaps": widest,
            "cross_issuer_leaders": cross_leaders,
        }
    finally:
        con.close()


def render_text(data: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("═══ StockGap — resumen de ventana ═══")
    out.append(f"Ventana: {data['window_start']} → {data['window_end'] or '—'}")
    out.append(f"Ticks en ventana: {data['ticks_in_window']} "
               f"({data['tick_ids_in_window']} tick_ids, "
               f"{data['cross_ticks_in_window']} cross-issuer)")
    out.append("")

    out.append(f"Tokens con datos: {len(data['per_token'])}")
    out.append(f"{'token':<10} {'ticker':<7} {'plataforma':<10} "
               f"{'n':>5} {'min_bps':>9} {'mean_bps':>9} {'max_bps':>9}")
    for r in data["per_token"][:15]:
        out.append(f"{r['token_symbol']:<10} {r['underlying_ticker']:<7} "
                   f"{r['platform_id']:<10} {r['n']:>5} "
                   f"{r['min_bps']:>9} {r['mean_bps']:>9} {r['max_bps']:>9}")
    if len(data["per_token"]) > 15:
        out.append(f"... y {len(data['per_token']) - 15} tokens más (usa --json)")
    out.append("")

    out.append("Top 5 gaps más amplios (|spread_bps|):")
    for i, r in enumerate(data["top5_widest_gaps"], 1):
        state = "abierto" if r["open_state"] else "cerrado"
        out.append(
            f"  {i}. {r['token_symbol']} ({r['underlying_ticker']}, "
            f"{r['platform_id']}): {r['spread_bps']:+.2f} bps "
            f"[{r['spread_pct']:+.2f}%] token {r['token_price']} vs ref "
            f"{r['reference_price']} · {r['market_status']}/{state} · {r['ts_utc']}"
        )
    if not data["top5_widest_gaps"]:
        out.append("  (sin ticks en la ventana)")
    out.append("")

    out.append("Cross-issuer (líderes de arbitraje entre emisores):")
    for i, r in enumerate(data["cross_issuer_leaders"], 1):
        out.append(
            f"  {i}. {r['underlying_ticker']} ({r['n_issuers']} emisores): "
            f"{r['spread_bps']:+.2f} bps — barato {r['cheap_symbol']}"
            f"({r['cheap_platform']}) → rico {r['rich_symbol']}"
            f"({r['rich_platform']}) · {r['ts_utc']}"
        )
    if not data["cross_issuer_leaders"]:
        out.append("  (sin ticks cross-issuer en la ventana)")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=None,
                        help="ventana en horas (default 24)")
    parser.add_argument("--minutes", type=float, default=None,
                        help="ventana en minutos")
    parser.add_argument("--since", type=str, default=None,
                        help="inicio ISO-8601 UTC (ej 2026-10-01T00:00:00Z)")
    parser.add_argument("--json", action="store_true", help="salida JSON")
    parser.add_argument("--db", type=str, default=DB_PATH, help="ruta al SQLite")
    args = parser.parse_args()

    start = _window_start(args.hours, args.minutes, args.since)
    data = collect(args.db, start)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_text(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
