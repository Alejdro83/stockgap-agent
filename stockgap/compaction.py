"""SQLite ledger compaction & retention (CLI: scripts/compact_ledger.py).

INVARIANTE [R2][F10]: se agrega exactamente el conjunto de ids que se borra. Cada chunk se
lee DENTRO del write_txn (keyset ``id > last AND id <= wm AND id < max_id ORDER BY id
LIMIT CHUNK_ROWS``) y merge + delete + verificación viven en esa MISMA transacción: sin
ventana de staleness entre snapshot y delete y sin pico de 108k filas en memoria. El DELETE
lleva el predicado de la hora + id entre min/max del chunk + id <= wm y se verifica
``rowcount == len(rows)``; si no, ``InvariantError`` → rollback de merge+delete juntos y
ABORTA el run completo (nunca seguir borrando) [F9]. ``max_id = MAX(id)`` se lee una vez
antes del walk: la fila MAX(id) jamás se agrega ni se borra (el shipper la necesita) [F2].
Sin watermark válido (read_watermark del shipper + guarda wm > MAX(id) = DB restaurada) no
se agrega ni se borra nada [R1]. Stragglers se mergean (n/sum/sumsq/min/max/última ref
finita; tie de ts → gana el id mayor). Chunks de 10k filas = transacciones cortas [R11];
3 fallos consecutivos abortan el walk [F8c]. Tras una hora vacía se SALTA al siguiente
MIN(ts_utc) — un reloj malo no puede anclar el walk [F13]. Walk, COUNT y DELETE van con
``INDEXED BY idx_spread_ts`` [G5-N1]: si el índice falta, el hint FALLA RUIDOSAMENTE
(OperationalError) — coherente con la regla R4 de negarse sin índice.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stockgap import db as sdb

log = logging.getLogger("compact_ledger")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}")
HOUR_FMT, HOUR_RE = "%Y-%m-%dT%H", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")
PRUNE_BATCH = 10_000     # [G7-2][R11] un lote = una txn ≤10k filas: 1–3 s de write lock < busy_timeout 10 s de los writers
CHUNK_ROWS = 10_000                                              # [R11]
MAX_CONSECUTIVE_FAILURES = 3                                     # [R11][F8c]

# --- SQL del walk en constantes: código y tests comparten el MISMO string [G5-N1] --------------
# INDEXED BY idx_spread_ts fuerza el acceso por ts_utc en walk/COUNT/DELETE y FALLA
# ruidosamente si falta el índice (OperationalError) — coherente con negarse sin índice [R4].
WALK_SELECT = (
    "SELECT id, ts_utc, underlying_ticker, token_symbol, platform_id, spread_bps_real,"
    " implied_share_price, reference_close_independent, market_status"
    " FROM spread_ticks INDEXED BY idx_spread_ts WHERE ts_utc >= ? AND ts_utc < ?"
    " AND id > ? AND id <= ? AND id < ? ORDER BY id LIMIT ?")
WALK_COUNT = ("SELECT COUNT(*) FROM spread_ticks INDEXED BY idx_spread_ts"
              " WHERE ts_utc >= ? AND ts_utc < ? AND id <= ? AND id < ?")
WALK_DELETE = ("DELETE FROM spread_ticks INDEXED BY idx_spread_ts"
               " WHERE ts_utc >= ? AND ts_utc < ? AND id BETWEEN ? AND ? AND id <= ?")

# --- reutilizar el parser del shipper, sin reimplementar [R1] -------------------------------
_ship_path = Path(__file__).resolve().parents[1] / "scripts" / "ship_rows.py"
_spec = importlib.util.spec_from_file_location("stockgap_ship_rows", _ship_path)
_ship = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ship)
read_watermark = _ship.read_watermark                             # None si falta, ShipError si malo
ShipError = _ship.ShipError
APPEND_TABLES = _ship.APPEND_TABLES


class InvariantError(RuntimeError):
    """rowcount != len(rows): el DELETE y lo agregado divergieron — abortar, no seguir."""


HOUR_SCHEMA = """
CREATE TABLE IF NOT EXISTS spread_ticks_hourly (
    ts_hour TEXT NOT NULL,
    underlying_ticker TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    n_ticks INTEGER NOT NULL,
    n_spread INTEGER NOT NULL,
    spread_bps_real_sum REAL NOT NULL,
    spread_bps_real_sumsq REAL NOT NULL,
    spread_bps_real_min REAL,
    spread_bps_real_max REAL,
    n_implied INTEGER NOT NULL,
    implied_share_price_sum REAL NOT NULL,
    reference_close_independent_last REAL,
    reference_close_ts_last TEXT,
    market_status_counts TEXT NOT NULL DEFAULT '{}',   -- JSON conteos por estado [N11]
    PRIMARY KEY (ts_hour, underlying_ticker, token_symbol, platform_id)
)"""

INDEXES = (
    ("idx_spread_ts", "CREATE INDEX IF NOT EXISTS idx_spread_ts ON spread_ticks(ts_utc)"),
    ("idx_hourly_ts", "CREATE INDEX IF NOT EXISTS idx_hourly_ts ON spread_ticks_hourly(ts_hour)"),
    ("idx_cross_ts", "CREATE INDEX IF NOT EXISTS idx_cross_ts ON cross_issuer_ticks(ts_utc)"),
    ("idx_oracle_ts", "CREATE INDEX IF NOT EXISTS idx_oracle_ts ON oracle_predictions(snapshot_ts)"),
)


@dataclass
class CompactConfig:
    raw_keep_days: int = 14
    hourly_keep_days: int = 180
    cross_keep_days: int = 180
    oracle_keep_days: int = 180


def _finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def load_cfg() -> CompactConfig:                                  # invalid→DEFAULT+warn [R13]
    def num(name, default, floor):
        raw = os.getenv(name, str(default))
        try:
            v = int(raw)
        except ValueError:
            log.warning("%s=%r no es entero — usando default %d", name, raw, default)
            return default
        if v < 1:
            log.warning("%s=%d inválido — usando default %d", name, v, default)
            return default
        return max(v, floor)                                      # válido: clamp up

    raw = num("RAW_KEEP_DAYS", 14, 2)
    return CompactConfig(raw, num("HOURLY_KEEP_DAYS", 180, raw + 1),
                         num("CROSS_KEEP_DAYS", 180, 1), num("ORACLE_KEEP_DAYS", 180, 1))


def load_watermarks(db_path: Path) -> dict | None:                # [R1] parser real + guarda
    meta = db_path.parent / "ship_meta.json"
    try:
        wm = read_watermark(meta)                                 # dict PLANO {tabla: int}
    except ShipError as exc:
        log.warning("ship_meta corrupto (%s) — sin agregación ni deletes hoy", exc)
        return None
    if wm is None:
        log.warning("ship_meta ausente — sin agregación ni deletes hoy")
        return None
    try:
        conn = sdb.connect_ro(db_path)
        for t in APPEND_TABLES:                                   # guarda DB restaurada [R1]
            max_id = conn.execute(f"SELECT MAX(id) FROM {t}").fetchone()[0] or 0
            if wm.get(t, 0) > max_id:
                log.warning("watermark %s=%s > MAX(id)=%s (DB restaurada?) — watermark "
                            "inutilizable hoy", t, wm.get(t), max_id)
                conn.close()
                return None
        conn.close()
    except sqlite3.OperationalError as exc:
        log.warning("no pude verificar watermarks (%s) — sin deletes hoy", exc)
        return None
    return wm


def migrate_indexes(db_path: Path) -> None:                       # schema 1º, 1 txn/índice [R5]
    conn = sdb.connect(db_path)
    sdb.ensure_wal(conn)                                          # contrato db.py [F15]
    with sdb.write_txn(conn):
        conn.execute(HOUR_SCHEMA)
    for name, stmt in INDEXES:
        t0 = datetime.now(timezone.utc)
        with sdb.write_txn(conn):
            conn.execute(stmt)
        log.info("%s listo en %.1fs", name, (datetime.now(timezone.utc) - t0).total_seconds())
    conn.close()


def _has_index(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                        (name,)).fetchone() is not None


def _aggregate_rows(rows):                                        # puro, sin txn
    buckets, rejects = {}, 0
    for rid, ts, und, tok, plat, spread, implied, ref, mkt in rows:
        b = buckets.setdefault((und or "", tok or "", plat or ""),
                               {"ids": [], "n": 0, "ns": 0, "ssum": 0.0, "ssumsq": 0.0,
                                "smin": None, "smax": None, "ni": 0, "isum": 0.0,
                                "ref": None, "refts": None, "mk": {}})
        b["ids"].append(rid); b["n"] += 1
        b["mk"][mkt or ""] = b["mk"].get(mkt or "", 0) + 1
        s = _finite(spread)
        if s is None and spread not in (None, ""):
            rejects += 1
        if s is not None:
            b["ns"] += 1; b["ssum"] += s; b["ssumsq"] += s * s
            b["smin"] = s if b["smin"] is None else min(b["smin"], s)
            b["smax"] = s if b["smax"] is None else max(b["smax"], s)
        i = _finite(implied)
        if i is None and implied not in (None, ""):
            rejects += 1
        if i is not None:
            b["ni"] += 1; b["isum"] += i
        r = _finite(ref)
        if r is None and ref not in (None, ""):
            rejects += 1                                            # refs cuentan [R8]
        if r is not None and (b["refts"] is None or ts >= b["refts"]):
            b["ref"], b["refts"] = r, ts                            # tie → id mayor (orden) [R9]
    return buckets, rejects


def _merge_bar(conn, hour, key, b):                               # dict-merge de estados [N11]
    und, tok, plat = key
    row = conn.execute(
        "SELECT n_ticks, n_spread, spread_bps_real_sum, spread_bps_real_sumsq,"
        " spread_bps_real_min, spread_bps_real_max, n_implied, implied_share_price_sum,"
        " reference_close_independent_last, reference_close_ts_last, market_status_counts"
        " FROM spread_ticks_hourly WHERE ts_hour=? AND underlying_ticker=? AND token_symbol=?"
        " AND platform_id=?", (hour, und, tok, plat)).fetchone()
    if row is None:
        conn.execute(                                             # INSERT con nombres [F11]
            "INSERT INTO spread_ticks_hourly (ts_hour, underlying_ticker, token_symbol,"
            " platform_id, n_ticks, n_spread, spread_bps_real_sum, spread_bps_real_sumsq,"
            " spread_bps_real_min, spread_bps_real_max, n_implied, implied_share_price_sum,"
            " reference_close_independent_last, reference_close_ts_last,"
            " market_status_counts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hour, und, tok, plat, b["n"], b["ns"], b["ssum"], b["ssumsq"],
             b["smin"], b["smax"], b["ni"], b["isum"], b["ref"], b["refts"],
             json.dumps(b["mk"], sort_keys=True)))
        return
    counts = json.loads(row[10] or "{}")
    for k, v in b["mk"].items():
        counts[k] = counts.get(k, 0) + v
    smin = min([x for x in (row[4], b["smin"]) if x is not None], default=None)
    smax = max([x for x in (row[5], b["smax"]) if x is not None], default=None)
    ref, refts = row[8], row[9]
    if b["ref"] is not None and (refts is None or (b["refts"] or "") >= refts):
        ref, refts = b["ref"], b["refts"]                            # tie → lo nuevo gana [R9]
    conn.execute(
        "UPDATE spread_ticks_hourly SET n_ticks=?, n_spread=?, spread_bps_real_sum=?,"
        " spread_bps_real_sumsq=?, spread_bps_real_min=?, spread_bps_real_max=?, n_implied=?,"
        " implied_share_price_sum=?, reference_close_independent_last=?,"
        " reference_close_ts_last=?, market_status_counts=? WHERE ts_hour=? AND"
        " underlying_ticker=? AND token_symbol=? AND platform_id=?",
        (row[0] + b["n"], row[1] + b["ns"], row[2] + b["ssum"], row[3] + b["ssumsq"],
         smin, smax, row[6] + b["ni"], row[7] + b["isum"], ref, refts,
         json.dumps(counts, sort_keys=True), hour, und, tok, plat))


def _guarded_delete(conn, table, col, cutoff, wm, guard_ids=True) -> int:
    deleted = 0
    while True:
        with sdb.write_txn(conn):                                  # un lote = una txn [N4]
            if guard_ids:
                if wm is None:
                    return 0
                max_id = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
                if max_id is None:
                    return 0
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table}"
                    f" WHERE {col} < ? AND id <= ? AND id < ? LIMIT {PRUNE_BATCH})",
                    (cutoff, wm, max_id))
            else:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table}"
                    f" WHERE {col} < ? LIMIT {PRUNE_BATCH})", (cutoff,))
        deleted += cur.rowcount
        if cur.rowcount < PRUNE_BATCH:
            return deleted


def compact(db_path: Path, cfg: CompactConfig, dry_run: bool = False,
            now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    rep = {"hours_with_rows": 0, "rows_deleted": 0, "hourly_pruned": 0, "cross_pruned": 0,
           "oracle_pruned": 0, "rows_reject": 0, "failures": 0, "planned_rows_deleted": 0,
           "planned_hours_with_rows": 0, "index_present": True, "aborted": False}
    wm = load_watermarks(db_path)
    conn = sdb.connect_ro(db_path) if dry_run else sdb.connect(db_path)
    if not dry_run:
        sdb.ensure_wal(conn)                                      # contrato db.py [F15]
    rep["index_present"] = _has_index(conn, "idx_spread_ts")  # rechazo ANTES de escribir [G6-N5]
    if not rep["index_present"]:
        if not dry_run:
            log.error("idx_spread_ts ausente — ejecuta --migrate-indexes (todos los writers "
                      "parados) antes de compactar")
            rep["failures"] += 1
            conn.close()
            return rep                               # sin HOUR_SCHEMA: CERO escritos [G6-N5]
        log.warning("sin índice — el walk se omite (evita scans de 6M filas) [R6]")
    if not dry_run:
        with sdb.write_txn(conn):
            conn.execute(HOUR_SCHEMA)

    raw_cutoff = (now - timedelta(days=cfg.raw_keep_days)).strftime(HOUR_FMT)
    tables_exist = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    row = conn.execute("SELECT MIN(ts_utc) FROM spread_ticks").fetchone() \
        if "spread_ticks" in tables_exist else (None,)
    if row and row[0] and rep["index_present"] and (dry_run or wm is not None):
        if not TS_RE.match(row[0]):
            log.error("MIN(ts_utc) con formato inesperado: %r", row[0])
            rep["failures"] += 1
        else:
            max_id = conn.execute("SELECT MAX(id) FROM spread_ticks").fetchone()[0]  # 1 vez [F2]
            wmv = wm["spread_ticks"] if wm else -1                # dry sin wm → 0 filas [F7]
            consec = 0                                             # abortar tras 3 seguidos [F11]
            hour = row[0][:13]
            try:
                while hour < raw_cutoff and consec < MAX_CONSECUTIVE_FAILURES:
                    try:
                        hi = (datetime.strptime(hour, HOUR_FMT)
                              + timedelta(hours=1)).strftime(HOUR_FMT)   # línea final [F7]
                    except ValueError:
                        log.error("hora ilegible %r — corto el walk", hour)
                        rep["failures"] += 1
                        break
                    h0s, h1s = hour + ":00", hi + ":00"
                    n_hour = 0
                    try:
                        if dry_run:                                # mismas guardas que real [F7]
                            n_hour = conn.execute(
                                WALK_COUNT, (h0s, h1s, wmv, max_id)).fetchone()[0]   # [G5-N1]
                            rep["planned_rows_deleted"] += n_hour
                            rep["planned_hours_with_rows"] += 1 if n_hour else 0
                        else:
                            last = 0
                            while True:                # keyset DENTRO del write_txn [F10]
                                with sdb.write_txn(conn):          # merge+delete+check atómicos
                                    rows = conn.execute(
                                        WALK_SELECT,
                                        (h0s, h1s, last, wmv, max_id, CHUNK_ROWS)).fetchall()
                                    if not rows:
                                        break
                                    buckets, rejects = _aggregate_rows(rows)
                                    for key, b in buckets.items():
                                        _merge_bar(conn, hour, key, b)
                                    lo, hi_id = rows[0][0], rows[-1][0]
                                    cur = conn.execute(
                                        WALK_DELETE, (h0s, h1s, lo, hi_id, wmv))     # [G5-N1]
                                    if cur.rowcount != len(rows):  # invariante chequeado [R2]
                                        raise InvariantError(
                                            f"rowcount {cur.rowcount} != {len(rows)} en {hour}")
                                    last = hi_id
                                rep["rows_reject"] += rejects
                                rep["rows_deleted"] += len(rows)
                                n_hour += len(rows)
                                if len(rows) < CHUNK_ROWS:
                                    break
                            if n_hour:
                                rep["hours_with_rows"] += 1
                            consec = 0
                    except InvariantError:
                        raise                                      # aborta el run entero [F9]
                    except Exception as exc:
                        rep["failures"] += 1; consec += 1
                        if n_hour:                             # hora parcial contabilizada [G5-N5]
                            rep["hours_with_rows"] += 1
                        log.warning("hora %s falló (%s consecutivas): %s", hour, consec, exc)
                        try:
                            conn.rollback()
                        except sqlite3.OperationalError:
                            pass
                    if n_hour:                                     # avanzar también en fallo [N17]
                        hour = hi
                    else:                          # hora vacía: saltar el hueco [F13]
                        nxt = conn.execute("SELECT MIN(ts_utc) FROM spread_ticks"
                                           " WHERE ts_utc >= ?", (h1s,)).fetchone()
                        if not nxt or not nxt[0]:
                            break
                        hour = nxt[0][:13]
            except InvariantError as exc:                          # re-lanzada fuera del walk [F9]
                log.error("INVARIANTE rota (%s) — abortando el run completo", exc)
                rep["aborted"] = True
                rep["failures"] += 1
                conn.close()
                return rep                                         # sin prunes: no seguir borrando
            if consec >= MAX_CONSECUTIVE_FAILURES:
                log.error("walk abortado: %s fallos consecutivos — revisar logs", consec)
                rep["aborted"] = True

    tables_wm = wm if wm else None
    for table, col, days, key, guard in (
            ("spread_ticks_hourly", "ts_hour", cfg.hourly_keep_days, "hourly_pruned", False),
            ("cross_issuer_ticks", "ts_utc", cfg.cross_keep_days, "cross_pruned", True),
            ("oracle_predictions", "snapshot_ts", cfg.oracle_keep_days, "oracle_pruned", True)):
        cutoff = (now - timedelta(days=days)).strftime(HOUR_FMT)
        if table not in tables_exist:                              # dry-run tolerante [R6]
            rep[("planned_" + key) if dry_run else key] = 0
            continue
        if dry_run:
            if not guard:
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} < ?",
                                 (cutoff,)).fetchone()[0]
            elif tables_wm is None:
                n = 0
            else:
                wmv = tables_wm.get(table)
                max_id = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or -1
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} < ? AND id <= ? AND id < ?",
                    (cutoff, wmv if wmv is not None else -1, max_id)).fetchone()[0]
            rep["planned_" + key] = n
        else:
            try:
                rep[key] += _guarded_delete(conn, table, col, cutoff,
                                            (tables_wm or {}).get(table) if guard else None,
                                            guard_ids=guard)
            except Exception as exc:
                rep["failures"] += 1
                log.warning("prune %s falló: %s", table, exc)
    if not dry_run:
        try:                                                       # PASSIVE [N14]
            log.info("wal_checkpoint(PASSIVE) -> %s", conn.execute(
                "PRAGMA wal_checkpoint(PASSIVE)").fetchone())
        except sqlite3.OperationalError as exc:
            log.info("checkpoint omitido: %s", exc)
    conn.close()
    return rep
