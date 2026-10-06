# Plan v8: compactación/retención del ledger + rotación local
*(v8 = tras gate Opus ronda 7 — `docs/reviews/gate7-plan-review-opus.md`, 3 comentarios
menores [G7-1..3]. Requisitos previos [R#]/[N#]/[C#]/[O#]/[F#] conservados; correcciones de la
ronda 5 marcadas [G5-N#], las de la ronda 6 [G6-N#] y las de la ronda 7 [G7-#].)*

**Date:** 2026-10-06 · **Author:** LISA · **Executor:** Sonnet · **Gate:** Opus (round 8)

## Context (hechos verificados en reviews — cumplir literalmente)

- `stockgap/db.py` expone `connect()`, `connect_ro()`, `write_txn()` y `ensure_wal(conn)`. TODA
  escritura pasa por ahí; tests en WAL como prod [R10][F15].
- `scripts/ship_rows.py` expone `read_watermark(meta)` (retorna `None` si falta, lanza
  `ShipError` si corrupto; retorna dict PLANO `{tabla: int}`) y `APPEND_TABLES`; el módulo está
  `__main__`-guarded → importable. REUSARLO vía `importlib`; prohibido reimplementar [R1].
  Guarda DB restaurada: si `wm[t] > MAX(id)` → watermark inutilizable (entero → `None`) [R1].
- `stockgap.collector`: `SCHEMA` es script multi-sentencia (`executescript`) y las columnas
  `spread_bps_real`/`reference_*` las añade `migrate_schema(conn)`. `stockgap.oracle`:
  `SCHEMA` multi-sentencia que YA declara `reference_source` (oracle.py:119) — el ALTER
  extra del fixture de v4 fallaba con `duplicate column name` [F3]. `snapshot_ts` de oracle =
  20 chars (sin ms).
- ts de `spread_ticks` = 24 chars `YYYY-MM-DDTHH:MM:SS.sssZ`; ids AUTOINCREMENT crecen con el
  tiempo PERO no hay garantía id↔ts monotónica (NTP, backfills) — de ahí [R2].
- `spread_ticks_hourly` sin columna `id` → prune por `rowid` [R5 previo].
- Ingesta ≈30 filas/s (108k/hora); delete con 3 índices de mantenimiento ≈1–3 s por cada 10k
  filas → **chunks de 10.000 filas, un `write_txn` por chunk** [R11]; la lectura del chunk es
  keyset DENTRO del propio `write_txn` (sin ventana snapshot→delete ni pico de 108k filas
  en memoria) [F10].
- Writers del sistema [R12][F6] (runbook exacto en Task 3): entradas de **crontab** —
  `ensure_collector.sh` cada 5 min (watchdog que REARRANCA el collector en ≤5 min si lo
  matas: puede aterrizar en pleno build de índices), `scripts/alerts.py` cada 2 min,
  `stockgap/oracle.py --once` cada 5 min, `scripts/ship_rows.py` cada 5 min; procesos
  long-running — `scripts/run_collector.py` y `scripts/dashboard.py` (el dashboard alberga el
  ingest router: `b402_gate`, `oracle_score` vía `stockgap/ingest.py`). Nada de esto es un
  job de hermes: es crontab de usuario + procesos.

**Invariante central:** *agregar exactamente el conjunto de ids que se borra, en la misma
transacción, verificado por `rowcount == len(rows)`* — si no, `InvariantError` → rollback de
merge+delete juntos y **aborta el run completo** (exit 1; nunca seguir borrando) [R2][F9].
Filas leídas dentro del `write_txn` con el predicado del DELETE más `id < MAX(id)`: hora de
ts + id entre min/max del chunk + id <= wm [F2][F10]; la fila `MAX(id)` jamás se agrega ni se
borra (el shipper la necesita). Sin watermark válido → nada de agregar ni borrar (solo prune
de `spread_ticks_hourly`) [R1].

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

Fixture v5 [R3][R4][R5][R10][F1][F3]: schema real vía `executescript(COLLECTOR_SCHEMA)` +
`migrate_schema` + `executescript(ORACLE_SCHEMA)` (SIN ALTER: `oracle.SCHEMA` ya incluye
`reference_source`) + `ensure_wal` + `migrate_indexes`; `_wm()` con guardas reales por
defecto (`spread=MAX(id)` real, `cross=0`, `oracle=0` — nunca `10**9`, que disparaba la
guarda R1 contra las tablas vacías) [F1]; **keeper fresco tras cada straggler**; reloj
inyectado (`now=NOW`); `monkeypatch` para env y para constantes de `compaction`. Tests nuevos
de la ronda 4: paridad dry-run↔real [F7], chunking con tie de ts en frontera de chunk [F8a],
fallo parcial con chunk previo commiteado [F8b], abort a los 3 fallos consecutivos [F8c],
abort por invariante rota [F9], salto de horas vacías [F13].

```python
"""Tests for stockgap/compaction.py — delete-exactly-aggregated, read_watermark real, WAL locks."""
import json
import pytest
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
import stockgap.compaction as compaction                                  # noqa: E402

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)          # reloj inyectado [R7c]


def _mk(db: Path, with_indexes: bool = True):
    conn = sqlite3.connect(db)
    conn.executescript(COLLECTOR_SCHEMA)                          # script multi-sentencia [R5]
    migrate_schema(conn)                                          # columnas spread_bps_real/*
    conn.executescript(ORACLE_SCHEMA)                             # oracle.SCHEMA ya incluye
    # reference_source (oracle.py:119) — el ALTER duplicado rompía el fixture [F3]
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


def _wm(db: Path, spread=None, cross=0, oracle=0):                # guardas creíbles [F1]
    if spread is None:                                            # spread = MAX(id) REAL
        spread = sqlite3.connect(db).execute(
            "SELECT COALESCE(MAX(id), 0) FROM spread_ticks").fetchone()[0]
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


def test_nonmonotonic_ids_and_foreign_row(tmp_path):               # [R2][F4]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    a = _row(conn, 191.2, "AAA", "10.0", ts=_ts(191.2))            # MISMA hora, ts tarde, id BAJO
    foreign = _row(conn, 10, "FRG", "7.0")                         # id intermedio, NO compactable
    b = _row(conn, 192, "AAA", "20.0", ts=_ts(192.0))              # MISMA hora, ts temprano, id ALTO
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    bar1 = sqlite3.connect(db).execute(
        "SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly"
        " WHERE underlying_ticker='AAA'").fetchall()
    assert bar1 == [(2, 30.0)]                                     # merge de los DOS (min/max ids)
    c = sqlite3.connect(db)
    left = {r[0] for r in c.execute("SELECT underlying_ticker FROM spread_ticks")}
    assert "FRG" in left                                           # h=10: más nueva que el cutoff
    compact(db, CFG, dry_run=False, now=NOW)                       # estable en 2ª pasada
    assert sqlite3.connect(db).execute(
        "SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly"
        " WHERE underlying_ticker='AAA'").fetchall() == bar1


def test_second_run_unchanged_when_unshipped_survivors(tmp_path):
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    ids = [_row(conn, 192, "AAA", "10.0"), _row(conn, 192, "AAA", "20.0")]
    keeper = _keeper(conn); conn.commit(); conn.close()
    _wm(db, spread=ids[0])                                         # cross/oracle default 0 [F1]
    compact(db, CFG, dry_run=False, now=NOW)
    first = sqlite3.connect(db).execute(
        "SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly").fetchall()
    assert first == [(1, 10.0)]
    _wm(db, spread=ids[0])                                         # watermark sin moverse
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly"
                     ).fetchall() == first                          # sin doble conteo [R15]
    assert {r[0] for r in c.execute("SELECT id FROM spread_ticks")} == {ids[1], keeper}  # [F12]


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


def test_restored_db_watermark_rejected(tmp_path):                 # [R1][F1] wm spread > MAX(id)
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    old = _row(conn, 192, "AAA", "10.0"); keeper = _keeper(conn)
    conn.commit(); conn.close()
    _wm(db, spread=keeper + 1)                                     # SOLO spread por encima [F1]
    rep = compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert rep["failures"] == 0
    assert c.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] == 0  # inutilizable
    assert {r[0] for r in c.execute("SELECT id FROM spread_ticks")} == {old, keeper}  # intactas


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
    compact(db, CFG, dry_run=False, now=NOW)                       # 2ª pasada [R15][F2]
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
    assert db.read_bytes() == before                                # el .db no cambia [F16]
    assert rep["index_present"] is True                            # visibilidad [R6]
    assert rep["planned_rows_deleted"] == 1
    assert rep["planned_hours_with_rows"] == 1
    assert rep["aborted"] is False


def test_dry_run_tolerates_missing_hourly_table(tmp_path):         # [R6]
    db = _mk(tmp_path / "t.db", with_indexes=True)
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _keeper(conn); conn.commit(); conn.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE spread_ticks_hourly"); conn.commit(); conn.close()  # sin barra
    _wm(db)
    rep = compact(db, CFG, dry_run=True, now=NOW)                  # no explota
    assert rep["failures"] == 0 and rep["planned_hourly_pruned"] == 0


def test_dry_run_matches_real(tmp_path):                           # paridad exacta [F2][F7]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "10.0"); _row(conn, 191, "AAA", "20.0")
    _row(conn, 192, "AAA", "99.0")                                 # MAX(id) en hora VIEJA [F7]
    conn.commit(); conn.close()
    _wm(db)
    d = compact(db, CFG, dry_run=True, now=NOW)
    r = compact(db, CFG, dry_run=False, now=NOW)
    assert d["planned_rows_deleted"] == r["rows_deleted"] == 2
    assert d["planned_hours_with_rows"] == r["hours_with_rows"] == 2


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
                                       ).fetchone()[0] == 0        # el keeper NO revivía [F2]


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


def test_ref_tie_and_majority_counts(tmp_path):                    # [R9][R11][F5]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    for _ in range(5):
        _row(conn, 192, "AAA", "1.0", ref="1.0", mkt="closed")
    tie1 = _row(conn, 192, "AAA", "2.0", ref="10.0", ts=_ts(192))  # MISMA hora y ts [F5]
    tie2 = _row(conn, 192, "AAA", "3.0", ref="20.0", ts=_ts(192))  # id mayor gana el tie
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
         ts=_ts(192))                                              # straggler, MISMO ts [F5]
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


def test_chunked_merge_tie_across_chunk_boundary(tmp_path, monkeypatch):   # [F8a]
    monkeypatch.setattr(compaction, "CHUNK_ROWS", 2)               # chunks [1,2] [3,4] [5]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    t3 = "2026-09-28T12:00:03.000Z"
    _row(conn, 192, "AAA", "1.0", ref="1.0", ts="2026-09-28T12:00:00.000Z")
    _row(conn, 192, "AAA", "2.0", ref="2.0", ts="2026-09-28T12:00:01.000Z")
    _row(conn, 192, "AAA", "3.0", ref="3.0", ts="2026-09-28T12:00:02.000Z")
    _row(conn, 192, "AAA", "40.0", ref="4.0", ts=t3)               # chunk 2
    _row(conn, 192, "AAA", "50.0", ref="5.0", ts=t3)               # chunk 3: mismo ts, id mayor
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    rep = compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert rep["failures"] == 0 and rep["rows_deleted"] == 5
    assert c.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone() == (1,)
    assert c.execute("SELECT n_ticks, n_spread, spread_bps_real_sum,"
                     " reference_close_independent_last"
                     " FROM spread_ticks_hourly").fetchone() == (5, 5, 96.0, 5.0)


def test_chunk_failure_keeps_committed_chunks_no_double_count(tmp_path, monkeypatch):  # [F8b]
    monkeypatch.setattr(compaction, "CHUNK_ROWS", 2)
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    for i in range(1, 6):
        _row(conn, 192, "AAA", f"{i}.0", ref=f"{i}.0", ts=_ts(192))    # ids 1..5
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    real_merge = compaction._merge_bar
    calls = {"n": 0}

    def flaky_merge(conn, hour, key, b):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("chunk 2 exploded")
        return real_merge(conn, hour, key, b)

    monkeypatch.setattr(compaction, "_merge_bar", flaky_merge)
    rep = compact(db, CFG, dry_run=False, now=NOW)
    assert rep["failures"] == 1 and rep["rows_deleted"] == 2           # solo chunk 1 contado
    assert rep["hours_with_rows"] == 1                                 # hora parcial cuenta [G5-N5]
    c = sqlite3.connect(db)
    assert c.execute("SELECT n_ticks, spread_bps_real_sum"
                     " FROM spread_ticks_hourly").fetchone() == (2, 3.0)   # chunk 1 commiteado
    assert c.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0] == 4  # 3 + keeper
    monkeypatch.setattr(compaction, "_merge_bar", real_merge)
    rep2 = compact(db, CFG, dry_run=False, now=NOW)                   # 2ª pasada los mergea
    assert rep2["failures"] == 0 and rep2["rows_deleted"] == 3 and rep2["hours_with_rows"] == 1
    assert sqlite3.connect(db).execute(
        "SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly"
        ).fetchone() == (5, 15.0)                                     # sin doble conteo


def test_three_consecutive_hour_failures_abort_walk(tmp_path, monkeypatch):  # [F8c][F9]
    def always_fail(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(compaction, "_merge_bar", always_fail)
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    for h in (200, 199, 198, 197, 196):                               # 5 horas CONSECUTIVAS
        _row(conn, h, "AAA", "1.0")
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    rep = compact(db, CFG, dry_run=False, now=NOW)
    assert rep["failures"] == 3 and rep["aborted"] is True             # corta en 3, marca abort
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM spread_ticks"
                                       ).fetchone()[0] == 6           # el walk se detuvo


def test_rowcount_mismatch_aborts_and_rolls_back(tmp_path, monkeypatch):     # [F9]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    a = _row(conn, 192, "AAA", "10.0")
    b = _row(conn, 192, "AAA", "20.0")
    keeper = _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    real_merge = compaction._merge_bar

    def destructive_merge(conn, hour, key, bucket):
        real_merge(conn, hour, key, bucket)
        conn.execute("DELETE FROM spread_ticks WHERE id = ?", (bucket["ids"][-1],))  # invariante rota

    monkeypatch.setattr(compaction, "_merge_bar", destructive_merge)
    rep = compact(db, CFG, dry_run=False, now=NOW)
    assert rep["aborted"] is True and rep["failures"] == 1             # aborta, exit 1 [F9]
    assert rep["rows_deleted"] == 0
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] == 0  # rollback
    assert {r[0] for r in c.execute("SELECT id FROM spread_ticks")} == {a, b, keeper}
    monkeypatch.setattr(compaction, "_merge_bar", real_merge)
    rep2 = compact(db, CFG, dry_run=False, now=NOW)                   # run limpio después
    assert rep2["failures"] == 0 and rep2["rows_deleted"] == 2
    assert sqlite3.connect(db).execute("SELECT n_ticks FROM spread_ticks_hourly"
                                       ).fetchone() == (2,)


def test_empty_hour_jump_skips_gap(tmp_path, monkeypatch):            # [F13]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    _row(conn, 0, "OLD", "1.0", ts="1970-01-01T00:00:00.000Z")        # fila de reloj malo
    _row(conn, 192, "AAA", "10.0")
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    statements = []
    real_connect = sdb.connect

    def tracing_connect(*a, **kw):
        c = real_connect(*a, **kw)
        c.set_trace_callback(statements.append)                      # cuenta SQL ejecutado
        return c

    monkeypatch.setattr(sdb, "connect", tracing_connect)
    t0 = time.monotonic()
    rep = compact(db, CFG, dry_run=False, now=NOW)
    assert rep["failures"] == 0 and rep["rows_deleted"] == 2
    assert rep["hours_with_rows"] == 2
    assert time.monotonic() - t0 < 5
    # sin el salto de hora vacía serían ~500k SELECTs (1970 → 2026, uno por hora)
    assert len(statements) < 200


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
    names = {r[0] for r in sqlite3.connect(db).execute(       # rechaza ANTES de escribir [G7-3]
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "spread_ticks_hourly" not in names                 # ni siquiera la tabla creada [G7-3]


@pytest.mark.parametrize("sql,args", [                 # las 3 constantes, aridad propia [G6-N3]
    (compaction.WALK_SELECT,
     ("2026-10-04T00:00:00.000Z", "2026-10-04T01:00:00.000Z", 1, 2, 2, 2)),
    (compaction.WALK_COUNT,
     ("2026-10-04T00:00:00.000Z", "2026-10-04T01:00:00.000Z", 1, 2)),
    (compaction.WALK_DELETE,
     ("2026-10-04T00:00:00.000Z", "2026-10-04T01:00:00.000Z", 1, 2, 2)),
], ids=["WALK_SELECT", "WALK_COUNT", "WALK_DELETE"])
def test_walk_sql_uses_idx_spread_ts(tmp_path, sql, args):   # EXPLAIN QUERY PLAN [G5-N1][G6-N3]
    assert "INDEXED BY idx_spread_ts" in sql                       # hint pineado también [G6-N3]
    db = _mk(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    plan = conn.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()
    conn.close()
    assert "idx_spread_ts" in " ".join(r[3] for r in plan)          # nunca scan por rowid
```

`python3 -m pytest tests/test_compact_ledger.py -x` → todos FALLAN (módulo no existe).

### 2b. IMPLEMENT — `stockgap/compaction.py` (completo)

```python
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
```

### 2c. CLI — `scripts/compact_ledger.py` (patrón `rotate_data.py` [O4])

```python
#!/usr/bin/env python3
"""CLI de stockgap.compaction. Nocturno bajo flock -n -E 0 + ionice -c2 -n7 + nice -n 19.

    python3 scripts/compact_ledger.py [--data DIR] [--dry-run] [--migrate-indexes]
Env: RAW_KEEP_DAYS=14 HOURLY_KEEP_DAYS=180 CROSS_KEEP_DAYS=180 ORACLE_KEEP_DAYS=180.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stockgap.compaction import compact, load_cfg, migrate_indexes  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("compact_ledger")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=str(Path(__file__).resolve().parents[1] / "data"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--migrate-indexes", action="store_true")
    args = ap.parse_args()
    db = Path(args.data) / "spread_ticks.db"
    if not db.exists():
        log.error("ledger no encontrado: %s", db)
        return 1
    if args.migrate_indexes:
        migrate_indexes(db)
        return 0
    rep = compact(db, load_cfg(), dry_run=args.dry_run)
    log.info("%s %s", "DRY-RUN" if args.dry_run else "OK", rep)
    return 1 if rep["failures"] or rep.get("aborted") else 0     # invariante/abort → exit 1 [F9]


if __name__ == "__main__":
    sys.exit(main())
```

### Tests: `python3 -m pytest tests/ -q --ignore=tests/test_deploy_contract.py` [G6-N1]

NEVER run `tests/test_deploy_contract.py` on b-max: it TERMs `run_collector.py` processes and
can kill the executor's process group. Its 3 failures here are environmental and already
known. [G6-N1]

26 new tests in `tests/test_compact_ledger.py`; full suite with
`--ignore=tests/test_deploy_contract.py`: 402 passed, 0 failed — probado pre-gate en copia
limpia `/tmp/g10`; evidencia completa en `docs/evidence/g7-pytest.log`.

## Task 3 — migración, docs, cron diferido [O1][R12][F6][F17]

1. **Informe PRE-migración** [F17]: `python3 scripts/compact_ledger.py --dry-run` → pegar el
   informe en el PR ANTES de tocar nada: `index_present: False`, walk omitido (scan de 6M
   filas evitado), `planned_*` con guardas. (Verificado en review: funciona sobre una DB
   pre-migración sin crash.)
2. **Ventana de mantenimiento — parar TODOS los writers** [R12][F6]. Nada de esto es hermes:
   son entradas de **crontab de usuario** + procesos long-running. Ejecutar las líneas COMO
   PASOS SEPARADOS — una única shell que contenga `scripts/dashboard.py` en su propia cmdline
   se mata a sí misma con el pkill (StockGap/README.md, «Restart note»: kill y relanzamiento
   en pasos distintos). El orden importa: primero fuera el crontab (si no, `ensure_collector.sh`
   rearranca el collector en ≤5 min y puede aterrizar en pleno build de índices), luego los
   procesos, y solo entonces migrar:
   ```
   test -e data/crontab.pre-migration.bak || crontab -l > data/crontab.pre-migration.bak
   crontab -l | grep -v -E 'ensure_collector\.sh|scripts/alerts\.py|stockgap/oracle\.py|ship_rows\.py' | crontab -
   pkill -TERM -f '^python3 scripts/[r]un_collector\.py'          # anclado; o: kill -TERM "$(cat data/collector.pid)" [G6-N2][G7-1]
   pkill -TERM -f '^python3 scripts/[d]ashboard\.py'              # anclado al cmdline real [G6-N2]
   for i in $(seq 60); do pgrep -f '^(/usr/bin/)?python3 (scripts/(run_collector|dashboard|alerts|ship_rows)|stockgap/oracle)\.py' >/dev/null || { echo "all writers stopped"; break; }; sleep 1; done
   python3 scripts/compact_ledger.py --migrate-indexes
   crontab data/crontab.pre-migration.bak && crontab -l | diff - data/crontab.pre-migration.bak
   scripts/ensure_collector.sh
   nohup python3 scripts/dashboard.py >> data/dashboard.log 2>&1 &
   for i in $(seq 15); do c=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8098/); [ "$c" = 200 ] && break; sleep 1; done; echo $c
   ```
   El patrón `pkill` va anclado (`^python3 scripts/…`) al cmdline real verificado para que
   nunca matchee el argv-prompt de un executor [G6-N2]; el colector también puede matarse por
   pidfile: `kill -TERM "$(cat data/collector.pid)"`. La comprobación de parada es un bucle de
   60×1 s que imprime `all writers stopped`: **NO ejecutar `--migrate-indexes` salvo que ese
   mensaje se haya impreso** [G6-N2]. El `curl` final es también bucle de reintento (15×1 s
   hasta 200) e imprime el código final [G6-N2].
   El dashboard se arranca normalmente con `nohup python3 scripts/dashboard.py >> data/dashboard.log 2>&1 &`
   (README.md; NO hay unit systemd — es un proceso lanzado desde shell, puerto 8098).
3. Vigilar `data/collector.log` y `data/dashboard.log` 5 min.
4. **Informe POST-migración**: `python3 scripts/compact_ledger.py --dry-run` → pegar
   `index_present: True` y `planned_*` (guardas wm + `id < MAX(id)`) en el PR.
5. README «Maintenance»: rotate cada 10 min; compact diario 04:25; knobs `*_KEEP_DAYS`
   (14/180/180/180; inválido → default; válido bajo → clamp up); sin VACUUM automático.
   Dry-run: NO escribe datos (`data/spread_ticks.db` byte-idéntico) PERO puede crear
   `spread_ticks.db-wal`/`-shm` al abrir la conexión de lectura — no describirlo como
   «no toca nada» [F16].
6. Branch `ledger-compaction`, sin mergear.
7. **Cron compactación DIFERIDO hasta post-merge** [O1]:
   ```
   25 4 * * * cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/compact.lock /usr/bin/ionice -c2 -n7 /usr/bin/nice -n 19 /usr/bin/python3 scripts/compact_ledger.py >> data/logs/compact.log 2>&1
   ```
   Primera ejecución real supervisada. [O3] follow-up en el PR: el ledger de Northflank no se
   compacta — trabajo futuro.

## Verification checklist (el executor pega outputs)

- `python3 -m pytest tests/ -q --ignore=tests/test_deploy_contract.py` → 0 fallos [G6-N1]
  (NEVER run `tests/test_deploy_contract.py` en b-max — ver aviso arriba)
- `python3 scripts/compact_ledger.py --migrate-indexes` (writers parados) → 4 índices
- `python3 scripts/compact_ledger.py --dry-run` (pre y post migración) → informes `planned_*` + `index_present` [F17]
- `crontab -l | grep -c 'rotate_data.py'` → `1`
- `git status` limpio en `ledger-compaction`

## Gate history

- Round 1 (gate1) → changes_requested, 21 findings (B1–B9, C1–C7, O1–O5)
- Round 2 (gate2) → changes_requested, 18 findings (N1–N18)
- Round 3 (gate3) → changes_requested, 15 findings (R1–R15)
- Round 4 (gate4) → changes_requested, 17 findings (F1–F17)
- Round 5 (gate5) → changes_requested, 5 findings (N1–N5: 1 high, 1 medium, 3 low)
- Round 6 (gate6) → changes_requested, 5 findings (N1–N5: 1 medium, 4 low)
- Round 7 (gate7) → approve_with_minor_comments, 3 minor comments: pidfile hint, PRUNE_BATCH vs [R11], N5 test gap
