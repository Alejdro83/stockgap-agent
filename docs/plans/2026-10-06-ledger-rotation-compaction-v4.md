# Plan v4: compactación/retención del ledger + rotación local
*(v4 = tras gate Opus ronda 3 — `docs/reviews/gate3-plan-review-opus.md`, R1–R15. Cada requisito marcado [R#].)*

**Date:** 2026-10-06 · **Author:** LISA · **Executor:** Sonnet · **Gate:** Opus (round 4)

## Context (hechos verificados en reviews — cumplir literalmente)

- `stockgap/db.py` expone `connect()`, `connect_ro()`, `write_txn()` y `ensure_wal(conn)`. TODA
  escritura pasa por ahí; tests en WAL como prod [R10].
- `scripts/ship_rows.py` expone `read_watermark(meta)` (retorna `None` si falta, lanza
  `ShipError` si corrupto) y `APPEND_TABLES`; el módulo está `__main__`-guarded → importable.
  REUSARLO vía `importlib`; prohibido reimplementar [R1]. Guarda DB restaurada: si
  `wm[t] > MAX(id)` → watermark inutilizable (entero → `None`) [R1].
- `stockgap.collector`: `SCHEMA` es script multi-sentencia (`executescript`) y las columnas
  `spread_bps_real`/`reference_*` las añade `migrate_schema(conn)`. `stockgap.oracle`: `SCHEMA`
  multi-sentencia + su ALTER de `reference_source`. `snapshot_ts` de oracle = 20 chars (sin ms).
- ts de `spread_ticks` = 24 chars `YYYY-MM-DDTHH:MM:SS.sssZ`; ids AUTOINCREMENT crecen con el
  tiempo PERO no hay garantía id↔ts monotónica (NTP, backfills) — de ahí [R2].
- `spread_ticks_hourly` sin columna `id` → prune por `rowid` [R5 previo].
- Ingesta ≈30 filas/s (108k/hora); delete con 3 índices de mantenimiento ≈1–3 s por cada 10k
  filas → **chunks de 10.000 filas, un `write_txn` por chunk** [R11].
- Writers del sistema (parar TODOS en la migración): collector, oracle, alerts, `b402_gate`,
  `oracle_score`, ingest router (montado en el dashboard) [R12].

**Invariante central:** *agregar exactamente el conjunto de ids que se borra, en la misma
transacción, verificado por `rowcount == len(rows)`* — si no, `raise` → rollback de merge+delete
juntos [R2]. Sin watermark válido → nada de agregar ni borrar (solo prune de `spread_ticks_hourly`).

## Task 1 — cron local `rotate_data.py`

1. Manual run: `cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/rotate.lock /usr/bin/nice -n 19 /usr/bin/python3 scripts/rotate_data.py` (exit 0).
2. Verificar cobertura de `data/alerts-cron.log` y `data/oracle-cron.log` en la rotación
   `data/*.log` copytruncate (leer config de `rotate_data.py`; si falta alguno, anotar en el PR).
3. Append crontab (exactamente UNA línea `rotate_data.py` después):
   ```
   */10 * * * * cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/rotate.lock /usr/bin/nice -n 19 /usr/bin/python3 scripts/rotate_data.py >> data/logs/rotate.log 2>&1
   ```

## Task 2 — `stockgap/compaction.py` + `scripts/compact_ledger.py` (TDD) [O4]

### 2a. FAILING TESTS FIRST — `tests/test_compact_ledger.py` (completo)

Fixture v4 [R3][R4][R5][R10]: schema real vía `executescript(SCHEMA)` + `migrate_schema` +
oracle schema/ALTER + `ensure_wal` + `migrate_indexes`; **keeper fresco tras cada straggler**;
reloj inyectado (`now=NOW`); `monkeypatch` para env.

```python
"""Tests for stockgap/compaction.py — delete-exactly-aggregated, read_watermark real, WAL locks."""
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stockgap import db as sdb                                            # noqa: E402
from stockgap.collector import SCHEMA as COLLECTOR_SCHEMA, migrate_schema  # noqa: E402
from stockgap.oracle import SCHEMA as ORACLE_SCHEMA                       # noqa: E402
from stockgap.compaction import (CompactConfig, compact, load_cfg,        # noqa: E402
                                 migrate_indexes)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)          # reloj inyectado [R7c]


def _mk(db: Path, with_indexes: bool = True):
    conn = sqlite3.connect(db)
    conn.executescript(COLLECTOR_SCHEMA)                          # script multi-sentencia [R5]
    migrate_schema(conn)                                          # columnas spread_bps_real/*
    conn.executescript(ORACLE_SCHEMA)
    conn.execute("ALTER TABLE oracle_predictions ADD COLUMN reference_source TEXT")
    sdb.ensure_wal(conn)                                          # WAL como prod [R10]
    conn.commit(); conn.close()
    if with_indexes:
        migrate_indexes(db)                                       # índices para no salir por N7 [R4]
    return db


def _ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _row(conn, h_ago, token, spread, ref="95.0", mkt="regular", ts=None):
    return conn.execute(
        "INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
        " token_contract, platform_id, token_price, token_to_share_ratio, reference_price,"
        " token_price_updated_at, implied_share_price, spread_abs, spread_bps, spread_pct,"
        " market_status, open_state, source_endpoint, fetched_at, spread_bps_real,"
        " reference_close_independent, reference_source, reference_kind)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts or _ts(h_ago), 1, token, token, "0x1", "bstock", "100.0", "1.0", "100.0", 0,
         "95.0", "5.0", "5.0", "0.5", mkt, 1, "e", "f", spread, ref,
         "coingecko", "independent")).lastrowid


def _keeper(conn):
    return _row(conn, 1, "KPR", "0.0")                            # MAX(id) no interfiera [R3]


def _wm(db: Path, spread=10**9, cross=10**9, oracle=10**9):
    (db.parent / "ship_meta.json").write_text(json.dumps(
        {"version": 1, "tables": {"spread_ticks": spread, "cross_issuer_ticks": cross,
                                  "oracle_predictions": oracle}}))


CFG = CompactConfig(raw_keep_days=2, hourly_keep_days=30, cross_keep_days=30, oracle_keep_days=30)


def test_aggregation_math_exact(tmp_path):
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _row(conn, 192, "BBB", "-5.0")
    _row(conn, 191, "AAA", "20.0")
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    rep = compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert rep["failures"] == 0 and rep["hours_with_rows"] == 2
    rows = c.execute("SELECT ts_hour, token_symbol, n_ticks, n_spread, spread_bps_real_sum,"
                     " spread_bps_real_sumsq, spread_bps_real_min, spread_bps_real_max,"
                     " n_implied, implied_share_price_sum FROM spread_ticks_hourly"
                     " ORDER BY token_symbol, ts_hour").fetchall()          # orden estable [R14]
    assert [(r[1], r[2], r[3], r[4], r[8], r[9]) for r in rows] == [
        ("AAA", 1, 1, 10.0, 1, 95.0), ("AAA", 1, 1, 20.0, 1, 95.0),
        ("BBB", 1, 1, -5.0, 1, 95.0)]
    assert rows[0][5] == 100.0 and rows[0][6] == 10.0 and rows[0][7] == 10.0
    assert [r[0] for r in c.execute("SELECT token_symbol FROM spread_ticks")] == ["KPR"]


def test_straggler_merges_then_second_run_stable(tmp_path):
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "30.0")
    _keeper(conn)                                                  # keeper tras straggler [R3]
    conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    bar1 = sqlite3.connect(db).execute(
        "SELECT n_ticks, n_spread, spread_bps_real_sum, spread_bps_real_min,"
        " spread_bps_real_max FROM spread_ticks_hourly").fetchall()
    compact(db, CFG, dry_run=False, now=NOW)                       # 2ª pasada estable [R15]
    assert sqlite3.connect(db).execute(
        "SELECT n_ticks, n_spread, spread_bps_real_sum, spread_bps_real_min,"
        " spread_bps_real_max FROM spread_ticks_hourly").fetchall() == bar1 == [(2, 2, 40.0, 10.0, 30.0)]


def test_nonmonotonic_ids_and_foreign_row(tmp_path):               # [R2]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    a = _row(conn, 192, "AAA", "10.0", ts=_ts(191.0))              # ts tarde, id BAJO
    foreign = _row(conn, 50, "FRG", "7.0")                         # otra hora, id intermedio
    b = _row(conn, 192, "AAA", "20.0", ts=_ts(192.0))              # ts temprano, id ALTO
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    bar1 = sqlite3.connect(db).execute(
        "SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly"
        " WHERE underlying_ticker='AAA'").fetchall()
    assert bar1 == [(2, 30.0)]                                     # merge de los DOS (min/max ids)
    c = sqlite3.connect(db)
    left = {r[0] for r in c.execute("SELECT underlying_ticker FROM spread_ticks")}
    assert "FRG" in left                                           # la de otra hora sobrevive
    compact(db, CFG, dry_run=False, now=NOW)                       # estable en 2ª pasada
    assert sqlite3.connect(db).execute(
        "SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly"
        " WHERE underlying_ticker='AAA'").fetchall() == bar1


def test_second_run_unchanged_when_unshipped_survivors(tmp_path):
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    ids = [_row(conn, 192, "AAA", "10.0"), _row(conn, 192, "AAA", "20.0")]
    _keeper(conn); conn.commit(); conn.close()
    _wm(db, spread=ids[0])
    compact(db, CFG, dry_run=False, now=NOW)
    first = sqlite3.connect(db).execute(
        "SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly").fetchall()
    assert first == [(1, 10.0)]
    _wm(db, spread=ids[0])                                         # watermark sin moverse
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly"
                     ).fetchall() == first                          # sin doble conteo [R15]
    assert {r[0] for r in c.execute("SELECT id FROM spread_ticks")} == {ids[1]}


def test_missing_ship_meta_no_deletes_no_aggregation(tmp_path):
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _row(conn, 191, "BBB", "2.0"); _keeper(conn)
    conn.commit(); conn.close()                                    # SIN ship_meta.json
    for _ in range(2):                                             # ambas pasadas [R15]
        compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] == 0


def test_restored_db_watermark_rejected(tmp_path):                 # [R1] wm > MAX(id)
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _keeper(conn); conn.commit(); conn.close()
    _wm(db, spread=10**9)                                          # wm muy por encima del MAX
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] == 0  # inutilizable


def test_max_id_keeper_survives_and_stable(tmp_path):
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0")
    keeper = _row(conn, 192, "AAA", "99.0")                        # MÁS NUEVA = MAX(id)
    conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    bar1 = sqlite3.connect(db).execute("SELECT n_ticks, spread_bps_real_sum"
                                       " FROM spread_ticks_hourly").fetchall()
    assert bar1 == [(1, 10.0)]                                     # keeper NO agregado
    compact(db, CFG, dry_run=False, now=NOW)                       # 2ª pasada [R15]
    assert sqlite3.connect(db).execute("SELECT n_ticks, spread_bps_real_sum"
                                       " FROM spread_ticks_hourly").fetchall() == bar1
    assert sqlite3.connect(db).execute("SELECT MAX(id) FROM spread_ticks").fetchone()[0] == keeper


def test_dry_run_read_only_and_guarded_plan(tmp_path):
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _row(conn, 192, "AAA", "20.0"); _keeper(conn)
    conn.commit(); conn.close()
    _wm(db, spread=1)
    before = db.read_bytes()
    rep = compact(db, CFG, dry_run=True, now=NOW)
    assert db.read_bytes() == before
    assert rep["index_present"] is True                            # visibilidad [R6]
    assert rep["planned_rows_deleted"] == 1
    assert rep["planned_hours_with_rows"] == 1


def test_dry_run_tolerates_missing_hourly_table(tmp_path):         # [R6]
    db = _mk(tmp_path / "t.db", with_indexes=True)
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _keeper(conn); conn.commit(); conn.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE spread_ticks_hourly"); conn.commit(); conn.close()  # sin barra
    _wm(db)
    rep = compact(db, CFG, dry_run=True, now=NOW)                  # no explota
    assert rep["failures"] == 0 and rep["planned_hourly_pruned"] == 0


def test_hourly_prune_without_id_column(tmp_path):                 # aritmética corregida [R7]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 24 * 20, "AAA", "10.0")                             # -20 días (dentro de 30)
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] == 1  # barra viva
    rep = compact(db, CFG, dry_run=False, now=NOW + timedelta(days=11))  # ahora sí envejece
    assert rep["failures"] == 0 and rep["hourly_pruned"] == 1
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM spread_ticks_hourly"
                                       ).fetchone()[0] == 0


def test_nonfinite_values_and_refs_rejected(tmp_path):             # refs cuentan [R8]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "nan", ref="inf")
    _row(conn, 192, "AAA", "10.0", ref="95.0")                     # mismo ts (tie) [R14]
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    rep = compact(db, CFG, dry_run=False, now=NOW)
    assert rep["rows_reject"] == 2                                  # nan + inf
    c = sqlite3.connect(db)
    assert c.execute("SELECT n_spread, spread_bps_real_sum,"
                     " reference_close_independent_last"
                     " FROM spread_ticks_hourly").fetchone() == (1, 10.0, 95.0)


def test_ref_tie_and_majority_counts(tmp_path):                    # [R9][R11]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    for _ in range(5):
        _row(conn, 192, "AAA", "1.0", ref="1.0", mkt="closed")
    tie1 = _row(conn, 192, "AAA", "2.0", ref="10.0", ts="2026-10-04T12:00:00.000Z")
    tie2 = _row(conn, 192, "AAA", "3.0", ref="20.0", ts="2026-10-04T12:00:00.000Z")  # id mayor
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT reference_close_independent_last"
                     " FROM spread_ticks_hourly").fetchone()[0] == 20.0   # gana id mayor [R9]
    counts = json.loads(c.execute("SELECT market_status_counts"
                                  " FROM spread_ticks_hourly").fetchone()[0])
    assert counts == {"closed": 5, "regular": 2}
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "4.0", ref="30.0", mkt="regular",
         ts="2026-10-04T12:00:00.000Z")                            # straggler, MISMO ts
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT reference_close_independent_last"
                     " FROM spread_ticks_hourly").fetchone()[0] == 30.0   # tie en merge también
    assert json.loads(c.execute("SELECT market_status_counts"
                                " FROM spread_ticks_hourly").fetchone()[0]) == {
        "closed": 5, "regular": 3}


def test_cutoff_boundary_and_month_boundary(tmp_path):
    db = _mk(tmp_path / "t.db")
    cutoff = NOW - timedelta(days=CFG.raw_keep_days)
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0")
    _row(conn, 0, "IN1", "1.0", ts=(cutoff - timedelta(seconds=1)
                                    ).strftime("%Y-%m-%dT%H:%M:%S.000Z"))   # entra
    _row(conn, 0, "EDG", "1.0", ts=cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z"))  # en el borde NO
    _row(conn, 0, "MO", "1.0", ts="2026-09-30T23:00:00.000Z")               # mes anterior
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert sorted(r[0] for r in c.execute("SELECT underlying_ticker FROM spread_ticks")
                  ) == ["EDG", "KPR"]
    assert {r[0] for r in c.execute("SELECT underlying_ticker FROM spread_ticks_hourly")
            } == {"AAA", "IN1", "MO"}


def test_lock_contention_waits_then_proceeds(tmp_path):            # [R10]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    holder = sqlite3.connect(db, check_same_thread=False)
    try:
        sdb.ensure_wal(holder)                                     # camino WAL real
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE spread_ticks SET tick_id=2")
        t0 = time.monotonic()
        timer = threading.Timer(0.7, holder.commit)
        timer.start()
        rep = compact(db, CFG, dry_run=False, now=NOW)
        timer.join()
        assert rep["failures"] == 0 and time.monotonic() - t0 >= 0.7
    finally:
        holder.close()                                             # siempre cerrar [R10]
    assert sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM spread_ticks WHERE token_symbol='AAA'").fetchone()[0] == 0


def test_config_validation(monkeypatch):                           # invalid→default [R13]
    monkeypatch.setenv("RAW_KEEP_DAYS", "0")
    monkeypatch.setenv("HOURLY_KEEP_DAYS", "garbage")
    cfg = load_cfg()
    assert cfg.raw_keep_days == 14 and cfg.hourly_keep_days == 180  # 0 y basura → defaults
    monkeypatch.setenv("RAW_KEEP_DAYS", "-5")
    assert load_cfg().raw_keep_days == 14
    monkeypatch.setenv("RAW_KEEP_DAYS", "1")                       # válido pero bajo → clamp up
    cfg3 = load_cfg()
    assert cfg3.raw_keep_days == 2 and cfg3.hourly_keep_days == 180


def test_migration_creates_schema_first_then_indexes(tmp_path):
    db = tmp_path / "t.db"
    _mk(db, with_indexes=False)
    migrate_indexes(db)                                            # crea HOUR_SCHEMA antes [R5]
    c = sqlite3.connect(db)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_spread_ts", "idx_hourly_ts", "idx_cross_ts", "idx_oracle_ts"} <= names


def test_nightly_refuses_without_index(tmp_path):
    db = _mk(tmp_path / "t.db", with_indexes=False)                # tabla sin índices [R4]
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    rep = compact(db, CFG, dry_run=False, now=NOW)
    assert rep["failures"] == 1 and rep.get("index_present") is False
```

`python3 -m pytest tests/test_compact_ledger.py -x` → todos FALLAN (módulo no existe).

### 2b. IMPLEMENT — `stockgap/compaction.py` (completo)

```python
"""SQLite ledger compaction & retention (CLI: scripts/compact_ledger.py).

INVARIANTE [R2]: se agrega exactamente el conjunto de ids que se borra. Filas elegidas con el
mismo predicado que el DELETE (ts de la hora + id entre min/max del chunk + id <= wm); dentro
del write_txn se mergean ESAS filas, se borran ESAS filas y se VERIFICA rowcount == len(rows),
lanzando excepción para rollback conjunto si no. Chunks de 10k filas = transacciones cortas
[R11]. Sin watermark válido (read_watermark del shipper, guarda DB restaurada) no se agrega ni
se borra nada [R1]. Stragglers se mergean (n/sum/sumsq/min/max/última ref finita).
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
PRUNE_BATCH = 50_000
CHUNK_ROWS = 10_000                                              # [R11]
MAX_CONSECUTIVE_FAILURES = 3                                     # [R11]

# --- reutilizar el parser del shipper, sin reimplementar [R1] -------------------------------
_ship_path = Path(__file__).resolve().parents[1] / "scripts" / "ship_rows.py"
_spec = importlib.util.spec_from_file_location("stockgap_ship_rows", _ship_path)
_ship = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ship)
read_watermark = _ship.read_watermark                             # None si falta, ShipError si malo
ShipError = _ship.ShipError
APPEND_TABLES = _ship.APPEND_TABLES

HOUR_SCHEMA = """...(idéntica a v3: spread_ticks_hourly con market_status_counts JSON TEXT,
PK (ts_hour, underlying_ticker, token_symbol, platform_id) NOT NULL...)"""

INDEXES = (...)  # idéntico a v4: 4 índices (spread/hourly/cross/oracle)


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
        wm = read_watermark(meta)                                 # None si falta
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
        conn.execute("INSERT INTO spread_ticks_hourly VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
           "planned_hours_with_rows": 0, "index_present": True}
    wm = load_watermarks(db_path)
    conn = sdb.connect_ro(db_path) if dry_run else sdb.connect(db_path)
    if not dry_run:
        with sdb.write_txn(conn):
            conn.execute(HOUR_SCHEMA)
        rep["index_present"] = _has_index(conn, "idx_spread_ts")
        if not rep["index_present"]:
            log.error("idx_spread_ts ausente — ejecuta --migrate-indexes (todos los writers "
                      "parados) antes de compactar")
            rep["failures"] += 1
            conn.close()
            return rep
    else:
        rep["index_present"] = _has_index(conn, "idx_spread_ts")
        if not rep["index_present"]:
            log.warning("sin índice — el walk se omite (evita scans de 6M filas) [R6]")

    raw_cutoff = (now - timedelta(days=cfg.raw_keep_days)).strftime(HOUR_FMT)
    tables_exist = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    row = conn.execute("SELECT MIN(ts_utc) FROM spread_ticks").fetchone() \
        if "spread_ticks" in tables_exist else (None,)
    consec = 0                                                     # abortar tras 3 seguidos [R11]
    if row and row[0]:
        if not TS_RE.match(row[0]):
            log.error("MIN(ts_utc) con formato inesperado: %r", row[0])
            rep["failures"] += 1
        elif dry_run and rep["index_present"]:
            hour = row[0][:13]
            wmv = (wm or {}).get("spread_ticks", -1)
            max_id = conn.execute("SELECT MAX(id) FROM spread_ticks").fetchone()[0] or -1
            while hour < raw_cutoff:
                n = conn.execute("SELECT COUNT(*) FROM spread_ticks WHERE ts_utc >= ? AND"
                                 " ts_utc < ? AND id <= ? AND id < ?",
                                 (hour + ":00", hour[:8] + f"{int(hour[8:10])+1:02d}" + "T"
                                  + hour[11:] + ":00" if False else
                                  (datetime.strptime(hour, HOUR_FMT) + timedelta(hours=1)
                                   ).strftime(HOUR_FMT) + ":00",
                                  wmv, max_id)).fetchone()[0]     # paridad con el real [R6]
                rep["planned_rows_deleted"] += n
                rep["planned_hours_with_rows"] += 1 if n else 0
                hour = (datetime.strptime(hour, HOUR_FMT) + timedelta(hours=1)
                        ).strftime(HOUR_FMT)
        elif not dry_run and wm is not None:
            hour = row[0][:13]
            wmv = wm["spread_ticks"]
            while hour < raw_cutoff and consec < MAX_CONSECUTIVE_FAILURES:
                hi = (datetime.strptime(hour, HOUR_FMT) + timedelta(hours=1)).strftime(HOUR_FMT)
                try:
                    rows = conn.execute(
                        "SELECT id, ts_utc, underlying_ticker, token_symbol, platform_id,"
                        " spread_bps_real, implied_share_price, reference_close_independent,"
                        " market_status FROM spread_ticks WHERE ts_utc >= ? AND ts_utc < ?"
                        " AND id <= ? ORDER BY id",
                        (hour + ":00", hi + ":00", wmv)).fetchall()
                    for i in range(0, len(rows), CHUNK_ROWS):     # chunks de 10k [R11]
                        chunk = rows[i:i + CHUNK_ROWS]
                        buckets, rejects = _aggregate_rows(chunk)
                        lo = min(r[0] for r in chunk); hi_id = max(r[0] for r in chunk)
                        with sdb.write_txn(conn):                 # merge+delete+check atómicos
                            for key, b in buckets.items():
                                _merge_bar(conn, hour, key, b)
                            cur = conn.execute(
                                "DELETE FROM spread_ticks WHERE ts_utc >= ? AND ts_utc < ?"
                                " AND id BETWEEN ? AND ? AND id <= ?",
                                (hour + ":00", hi + ":00", lo, hi_id, wmv))
                            if cur.rowcount != len(chunk):        # invariante chequeado [R2]
                                raise RuntimeError(
                                    f"rowcount {cur.rowcount} != {len(chunk)} en {hour}")
                        rep["rows_reject"] += rejects
                        rep["rows_deleted"] += len(chunk)
                    if rows:
                        rep["hours_with_rows"] += 1
                    consec = 0
                    hour = hi
                except Exception as exc:
                    rep["failures"] += 1; consec += 1
                    log.warning("hora %s falló (%s consecutivas): %s", hour, consec, exc)
                    try:
                        conn.rollback()
                    except sqlite3.OperationalError:
                        pass
                    hour = hi                                      # avanzar también en fallo
    elif row and row[0] and not dry_run and wm is None:
        pass                                                       # sin watermark: nada [R1]

    tables_wm = wm if wm else None
    for table, col, days, key, guard in (
            ("spread_ticks_hourly", "ts_hour", cfg.hourly_keep_days, "hourly_pruned", False),
            ("cross_issuer_ticks", "ts_utc", cfg.cross_keep_days, "cross_pruned", True),
            ("oracle_predictions", "snapshot_ts", cfg.oracle_keep_days, "oracle_pruned", True)):
        cutoff = (now - timedelta(days=days)).strftime(HOUR_FMT)
        if table not in tables_exist:                              # dry-run tolerante [R6]
            rep["planned_" + key if dry_run else key] = 0
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
```

*(Nota de implementación: en el walk de dry-run, calcular `hi` con
`datetime.strptime(hour, HOUR_FMT) + timedelta(hours=1)` como en el resto — el condicional
inline del sketch debe simplificarse así. `wm` del shipper ya es dict por tabla: usar
`wm["spread_ticks"]` directo.)*

### 2c. CLI — `scripts/compact_ledger.py` (idéntico a v3 — patrón `rotate_data.py`)

### Tests: `python3 -m pytest tests/ -q` → verde (392 + nuevas).

## Task 3 — migración, docs, cron diferido [O1][R12]

1. **Ventana de mantenimiento — parar TODOS los writers** (comandos exactos):
   ```
   hermes cron pause <job_id de collector/oracle/alerts/b402_gate/oracle_score/ingest router>
   # (el ingest router del dashboard: systemctl --user stop <unidad> o pkill -f según runbook —
   #  listar procesos con `pgrep -af 'collector|oracle|alert|b402|oracle_score|ingest'`)
   ```
2. `python3 scripts/compact_ledger.py --migrate-indexes` (4 índices, 1 txn c/u, tiempos en log).
3. Reanudar todos los writers (`hermes cron resume …` / arrancar unidades) y vigilar
   `data/logs/collector.log` 5 min.
4. `python3 scripts/compact_ledger.py --dry-run` → informe `planned_*` + `index_present` al PR.
5. README «Maintenance»: rotate cada 10 min; compact diario 04:25; knobs `*_KEEP_DAYS`
   (14/180/180/180; inválido → default; válido bajo → clamp up); sin VACUUM automático.
6. Branch `ledger-compaction`, sin mergear.
7. **Cron compactación DIFERIDO hasta post-merge** [O1]:
   ```
   25 4 * * * cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/compact.lock /usr/bin/ionice -c2 -n7 /usr/bin/nice -n 19 /usr/bin/python3 scripts/compact_ledger.py >> data/logs/compact.log 2>&1
   ```
   Primera ejecución real supervisada. [O3] follow-up en el PR: el ledger de Northflank no se
   compacta — trabajo futuro.

## Verification checklist (el executor pega outputs)

- `python3 -m pytest tests/ -q` → 0 fallos
- `python3 scripts/compact_ledger.py --migrate-indexes` (writers parados) → 4 índices
- `python3 scripts/compact_ledger.py --dry-run` → `index_present: True`, `planned_*` con guardas
- `crontab -l | grep -c 'rotate_data.py'` → `1`
- `git status` limpio en `ledger-compaction`
