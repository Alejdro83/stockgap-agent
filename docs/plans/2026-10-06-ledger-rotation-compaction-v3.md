# Plan v3: compactación/retención del ledger + rotación local
*(v3 = tras gate Opus ronda 2 — `docs/reviews/gate2-plan-review-opus.md`, VERDICT: changes_requested con N1–N18. Cada requisito se marca [N#] en línea.)*

**Date:** 2026-10-06 · **Author:** LISA · **Executor:** Sonnet · **Gate:** Opus (round 3)

## Context (fatos verificados por el review — respetar literalmente)

- Python 3.11.15 → `sqlite3` en modo BEGIN implícito: toda escritura pasa por
  `stockgap/db.py` (`connect()`, `connect_ro()` y `write_txn()` — EXISTEN; importar y usar,
  nunca `sqlite3.connect` crudo) [N3][N4][N6][N10].
- `data/ship_meta.json` formato real: `{"version": 1, "tables": {"spread_ticks": <int>,
  "cross_issuer_ticks": <int>, "oracle_predictions": <int>}}` — validar `version == 1` y que
  cada watermark entero; sin `assert` (se elimina con `-O`). REUSAR el parser estricto del
  shipper (`grep -n 'def read_watermark\|watermark' scripts/ship_rows.py` → importar desde su
  hogar real; no reimplementar) [N1]. Fixtures = formato real [N1].
- `spread_ticks` ids 1..5.987.472 (AUTOINCREMENT, id crece con el tiempo); la fila más vieja
  es `2026-10-02T00:21`. TODOS los ts = 24 chars `YYYY-MM-DDTHH:MM:SS.sssZ` UTC.
- `spread_ticks_hourly` NO tiene columna `id` (PK compuesta) — su prune va por `rowid` [N5].
- Índices de ts NO existen aún: crearlos es migración explícita con collector PARADO [N6][N7].
- Ingesta ~30 filas/s (≈108k/hora): jamás retener `BEGIN IMMEDIATE` mientras Python procesa —
  agregar desde snapshot de lectura y transacción de escritura solo para merge+delete [N8].
- Schema real de las 3 tablas (verbatim en §Fixtures) — importar los DDL desde
  `stockgap.collector` / `stockgap.oracle` (grep de `CREATE TABLE` en esos módulos; si no
  hubiera constantes exportables, snapshot desde `sqlite_master` de un clone schema-only en
  `conftest.py`); NUNCA copiar strings a mano [N18].

**Invariante central del diseño [N2]:** *agregar exactamente el conjunto de ids que se borra,
en la misma transacción*. SELECT con el mismo predicado que el DELETE (`ts in hora AND id <= wm
AND id < MAX(id)`), merge de ESAS filas, delete de ESAS filas, COMMIT. Sin watermark válido →
NO se agrega NADA y NO se borra NADA (solo el prune de `spread_ticks_hourly`, que no viaja al
mirror) [N2][N13].

## Task 1 — cron local `rotate_data.py` [O5, N8]

1. Manual run: `cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/rotate.lock /usr/bin/nice -n 19 /usr/bin/python3 scripts/rotate_data.py` (exit 0).
2. Verificar que `data/alerts-cron.log` y `data/oracle-cron.log` caen en la rotación `data/*.log`
   copytruncate (leer config de `rotate_data.py`; si falta alguno, anotarlo en el PR sin tocar código).
3. Append crontab (exactamente UNA línea `rotate_data.py` después):
   ```
   */10 * * * * cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/rotate.lock /usr/bin/nice -n 19 /usr/bin/python3 scripts/rotate_data.py >> data/logs/rotate.log 2>&1
   ```

## Task 2 — `stockgap/compaction.py` + `scripts/compact_ledger.py` (TDD) [O4]

### 2a. FAILING TESTS FIRST — `tests/test_compact_ledger.py` (completo)

Convenciones v3 [N9]: toda fixture siembra una **fila keeper reciente** (ts = ahora−1 h) para que
`MAX(id)` no interfiera con lo que se mide; `compact(..., now=fixed)` inyecta reloj [N15];
`monkeypatch.setenv` para env [N16]; asserts sobre `hours_with_rows`, no horas vacías.

```python
"""Tests for stockgap/compaction.py — delete-exactly-aggregated, watermarks reales, locks."""
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stockgap.compaction import CompactConfig, compact, load_cfg, migrate_indexes  # noqa: E402

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)          # reloj inyectado [N15]
DDL = [...]  # los 3 CREATE TABLE importados de stockgap.collector/oracle [N18]


def _mk(db: Path):
    conn = sqlite3.connect(db)
    for stmt in DDL:
        conn.execute(stmt)
    conn.commit()
    return conn


def _ts(hours_ago: int) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _row(conn, h_ago, token, spread, ref="95.0"):
    return conn.execute(
        "INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
        " token_contract, platform_id, token_price, token_to_share_ratio, reference_price,"
        " token_price_updated_at, implied_share_price, spread_abs, spread_bps, spread_pct,"
        " market_status, open_state, source_endpoint, fetched_at, spread_bps_real,"
        " reference_close_independent, reference_source, reference_kind)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_ts(h_ago), 1, token, token, "0x1", "bstock", "100.0", "1.0", "100.0", 0,
         "95.0", "5.0", "5.0", "0.5", "regular", 1, "e", "f", spread, ref,
         "coingecko", "independent")).lastrowid


def _keeper(conn):                                               # fila reciente = MAX(id) [N9]
    return _row(conn, 1, "KPR", "0.0")


def _wm(db: Path, spread=10**9, cross=10**9, oracle=10**9):
    (db.parent / "ship_meta.json").write_text(json.dumps(
        {"version": 1, "tables": {"spread_ticks": spread, "cross_issuer_ticks": cross,
                                  "oracle_predictions": oracle}}))   # formato real [N1]


CFG = CompactConfig(raw_keep_days=2, hourly_keep_days=30, cross_keep_days=30, oracle_keep_days=30)


def test_aggregation_math_exact(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _row(conn, 192, "AAA", "10.0"); _row(conn, 192, "BBB", "-5.0")   # hora vieja 1
    _row(conn, 191, "AAA", "20.0")                                   # hora vieja 2
    keeper = _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    rep = compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert rep["failures"] == 0 and rep["hours_with_rows"] == 2      # no horas vacías [N9]
    rows = c.execute("SELECT token_symbol, n_ticks, n_spread, spread_bps_real_sum,"
                     " spread_bps_real_sumsq, spread_bps_real_min, spread_bps_real_max,"
                     " n_implied, implied_share_price_sum FROM spread_ticks_hourly"
                     " ORDER BY token_symbol").fetchall()
    assert rows == [("AAA", 1, 1, 10.0, 100.0, 10.0, 10.0, 1, 95.0),
                    ("AAA", 1, 1, 20.0, 400.0, 20.0, 20.0, 1, 95.0),
                    ("BBB", 1, 1, -5.0, 25.0, -5.0, -5.0, 1, 95.0)]
    left = [r[0] for r in c.execute("SELECT token_symbol FROM spread_ticks")]
    assert left == ["KPR"]                                          # solo queda el keeper [B5]


def test_straggler_merges_then_second_run_stable(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _row(conn, 192, "AAA", "10.0"); keeper = _keeper(conn)
    conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "30.0")                                  # tarde en hora ya barrida
    conn.commit(); conn.close()
    _wm(db)                                                         # watermark lo cubre ya
    compact(db, CFG, dry_run=False, now=NOW)
    compact(db, CFG, dry_run=False, now=NOW)                        # 2ª pasada = estable [N2]
    c = sqlite3.connect(db)
    row = c.execute("SELECT n_ticks, n_spread, spread_bps_real_sum, spread_bps_real_min,"
                    " spread_bps_real_max FROM spread_ticks_hourly WHERE token_symbol='AAA'"
                    " AND underlying_ticker='AAA'").fetchone()
    assert row == (2, 2, 40.0, 10.0, 30.0)


def test_second_run_unchanged_when_unshipped_survivors(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    ids = [_row(conn, 192, "AAA", "10.0"), _row(conn, 192, "AAA", "20.0")]
    keeper = _keeper(conn); conn.commit(); conn.close()
    _wm(db, spread=ids[0])                                          # solo el 1º es enviable
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    first = c.execute("SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly").fetchall()
    conn2 = c; conn2.close()
    _wm(db, spread=ids[0])                                          # watermark sin moverse
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT n_ticks, spread_bps_real_sum FROM spread_ticks_hourly"
                     ).fetchall() == first == [(1, 10.0)]           # sin doble conteo [N2]
    assert sorted(r[0] for r in c.execute("SELECT id FROM spread_ticks")) == sorted(
        [ids[1], keeper])                                           # sobrevive lo no enviado [B4]


def test_missing_ship_meta_no_deletes_no_aggregation(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _row(conn, 192, "AAA", "10.0"); _row(conn, 191, "BBB", "2.0"); _keeper(conn)
    conn.commit(); conn.close()                                     # SIN ship_meta.json
    compact(db, CFG, dry_run=False, now=NOW)
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0] == 3    # nada borrado
    assert c.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] == 0  # nada agregado [N2]


def test_max_id_keeper_survives(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _row(conn, 192, "AAA", "10.0")
    keeper = _row(conn, 192, "AAA", "99.0")                         # MÁS NUEVA del mismo día
    conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    assert c.execute("SELECT MAX(id) FROM spread_ticks").fetchone()[0] == keeper
    row = c.execute("SELECT n_ticks FROM spread_ticks_hourly").fetchone()
    assert row[0] == 1                                              # keeper NO agregado [N2]


def test_dry_run_read_only_and_guarded_plan(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _row(conn, 192, "AAA", "10.0"); _row(conn, 192, "AAA", "20.0"); _keeper(conn)
    conn.commit(); conn.close()
    _wm(db, spread=1)                                               # solo 1 enviable
    before = db.read_bytes()
    rep = compact(db, CFG, dry_run=True, now=NOW)
    assert db.read_bytes() == before                                # byte a byte [B8][N10]
    assert rep["planned_rows_deleted"] == 1                         # con guardas wm+MAX(id) [N10]
    assert rep["planned_hours_with_rows"] == 1


def test_hourly_prune_without_id_column(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _row(conn, 24 * 40, "AAA", "10.0"); _keeper(conn)
    conn.commit(); conn.close()
    _wm(db)
    cfg = CompactConfig(raw_keep_days=2, hourly_keep_days=30, cross_keep_days=30,
                        oracle_keep_days=30)
    compact(db, cfg, dry_run=False, now=NOW)                        # crea barra de -40 días
    rep = compact(db, cfg, dry_run=False, now=NOW + timedelta(days=31))  # la pasa de edad
    c = sqlite3.connect(db)
    assert rep["failures"] == 0 and rep["hourly_pruned"] == 1       # rowid batches [N5]
    assert c.execute("SELECT COUNT(*) FROM spread_ticks_hourly").fetchone()[0] == 0


def test_nonfinite_values_and_refs_rejected(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _row(conn, 192, "AAA", "nan", ref="inf")
    _row(conn, 192, "AAA", "10.0", ref="95.0")                      # misma hora, ts 1 s después
    _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    rep = compact(db, CFG, dry_run=False, now=NOW)
    assert rep["rows_reject"] >= 2                                  # spread nan + ref inf [C4][N12]
    c = sqlite3.connect(db)
    row = c.execute("SELECT n_spread, spread_bps_real_sum, reference_close_independent_last"
                    " FROM spread_ticks_hourly").fetchone()
    assert row == (1, 10.0, 95.0)                                   # ref finita, no el inf [N12]


def test_majority_and_ref_tie_merge(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    for _ in range(5):
        conn.execute(                                              # 5 'closed'
            "INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
            " token_contract, platform_id, token_price, token_to_share_ratio, reference_price,"
            " token_price_updated_at, implied_share_price, spread_abs, spread_bps, spread_pct,"
            " market_status, open_state, source_endpoint, fetched_at, spread_bps_real,"
            " reference_close_independent, reference_source, reference_kind)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_ts(192), 1, "AAA", "AAA", "0x1", "bstock", "1", "1", "1", 0, "1", "1", "1",
             "1", "closed", 1, "e", "f", "1.0", "1", "c", "independent"))
    keeper = _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    conn = sqlite3.connect(db)
    _row(conn, 192, "AAA", "2.0")                                   # straggler 'regular'
    conn.execute("UPDATE spread_ticks SET market_status='regular' WHERE token_symbol='AAA'")
    conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    counts = json.loads(c.execute("SELECT market_status_counts FROM spread_ticks_hourly"
                                  ).fetchone()[0])
    assert counts == {"closed": 5, "regular": 1}                    # conteos, no etiquetas [N11]


def test_cutoff_boundary_and_month_boundary(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    cutoff = NOW - timedelta(days=CFG.raw_keep_days)
    _row(conn, 192, "AAA", "10.0")
    conn.execute("INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
                 " token_contract, platform_id, token_price, token_to_share_ratio,"
                 " reference_price, token_price_updated_at, implied_share_price, spread_abs,"
                 " spread_bps, spread_pct, market_status, open_state, source_endpoint,"
                 " fetched_at, spread_bps_real, reference_close_independent, reference_source,"
                 " reference_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ((cutoff - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                  1, "IN1", "IN1", "0x1", "bstock", "1", "1", "1", 0, "1", "1", "1", "1",
                  "regular", 1, "e", "f", "1.0", "1", "c", "independent"))
    conn.execute("INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
                 " token_contract, platform_id, token_price, token_to_share_ratio,"
                 " reference_price, token_price_updated_at, implied_share_price, spread_abs,"
                 " spread_bps, spread_pct, market_status, open_state, source_endpoint,"
                 " fetched_at, spread_bps_real, reference_close_independent, reference_source,"
                 " reference_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                  1, "EDG", "EDG", "0x1", "bstock", "1", "1", "1", 0, "1", "1", "1", "1",
                  "regular", 1, "e", "f", "1.0", "1", "c", "independent"))
    conn.execute("INSERT INTO spread_ticks (ts_utc, tick_id, underlying_ticker, token_symbol,"
                 " token_contract, platform_id, token_price, token_to_share_ratio,"
                 " reference_price, token_price_updated_at, implied_share_price, spread_abs,"
                 " spread_bps, spread_pct, market_status, open_state, source_endpoint,"
                 " fetched_at, spread_bps_real, reference_close_independent, reference_source,"
                 " reference_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("2026-09-30T23:00:00.000Z",
                  1, "MO", "MO", "0x1", "bstock", "1", "1", "1", 0, "1", "1", "1", "1",
                  "regular", 1, "e", "f", "1.0", "1", "c", "independent"))   # mes anterior
    keeper = _keeper(conn); conn.commit(); conn.close()
    _wm(db)
    compact(db, CFG, dry_run=False, now=NOW)
    c = sqlite3.connect(db)
    left = sorted(r[0] for r in c.execute("SELECT underlying_ticker FROM spread_ticks"))
    assert left == ["EDG", "KPR"]                                   # en el borde NO se toca [C7]
    done = {r[0] for r in c.execute("SELECT underlying_ticker FROM spread_ticks_hourly")}
    assert done == {"AAA", "IN1", "MO"}                             # mes anterior sí [C7]


def test_lock_contention_waits_then_proceeds(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _row(conn, 192, "AAA", "10.0"); _keeper(conn)
    conn.commit(); conn.close()
    _wm(db)
    holder = sqlite3.connect(db, check_same_thread=False)           # [N9]
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE spread_ticks SET tick_id=2")
    t0 = time.monotonic()
    timer = threading.Timer(0.7, holder.commit)
    timer.start()
    rep = compact(db, CFG, dry_run=False, now=NOW)                  # espera busy_timeout [B6]
    timer.join()
    assert rep["failures"] == 0 and time.monotonic() - t0 >= 0.7
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM spread_ticks WHERE token_symbol='AAA'"
                     ).fetchone()[0] == 0                          # y el delete ocurrió


def test_config_validation(monkeypatch):                            # [N16]
    monkeypatch.setenv("RAW_KEEP_DAYS", "0")
    monkeypatch.setenv("HOURLY_KEEP_DAYS", "garbage")
    cfg = load_cfg()
    assert cfg.raw_keep_days >= 2 and cfg.hourly_keep_days >= cfg.raw_keep_days + 1
    monkeypatch.setenv("RAW_KEEP_DAYS", "-5")
    cfg2 = load_cfg()
    assert cfg2.raw_keep_days == 2 and cfg2.hourly_keep_days == 180  # garbage→default [C5]


def test_migration_creates_schema_first_then_indexes(tmp_path):
    db = tmp_path / "t.db"
    _mk(db)
    migrate_indexes(db)                                             # crea HOUR_SCHEMA antes [N6]
    c = sqlite3.connect(db)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_spread_ts", "idx_hourly_ts", "idx_cross_ts", "idx_oracle_ts"} <= names


def test_nightly_refuses_without_index(tmp_path):
    db = tmp_path / "t.db"
    conn = _mk(db)
    _row(conn, 192, "AAA", "10.0"); _keeper(conn)
    conn.commit(); conn.close()
    _wm(db)
    rep = compact(db, CFG, dry_run=False, now=NOW)                  # sin migración previa
    assert rep["failures"] == 1                                     # falla ruidoso [N7]
```

`python3 -m pytest tests/test_compact_ledger.py -x` → todos FALLAN (módulo no existe).

### 2b. IMPLEMENT — `stockgap/compaction.py` (completo)

Flujo por hora [N2][N3][N8] — **snapshot de lectura → transacción corta**:

```python
"""SQLite ledger compaction & retention (CLI: scripts/compact_ledger.py).

INVARIANTE: se agrega exactamente el conjunto de ids que se borra, en la misma transacción
corta. Agregación desde snapshot de lectura; merge+delete bajo write_txn (BEGIN IMMEDIATE).
Sin ship_meta.json válido no se agrega ni se borra nada (solo se poda spread_ticks_hourly,
que no viaja al mirror). Los stragglers se mergean (n/sum/sumsq/min/max/última ref).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stockgap import db as sdb                                    # connect/connect_ro/write_txn

log = logging.getLogger("compact_ledger")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
HOUR_FMT, HOUR_RE = "%Y-%m-%dT%H", re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")
PRUNE_BATCH = 50_000

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


def _finite(v):                                                    # [C4][N12]
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def load_cfg() -> CompactConfig:                                   # [C5][N16]
    def num(name, default, lo):
        try:
            v = int(os.getenv(name, str(default)))
        except ValueError:
            return default
        return v if v >= lo else lo

    raw = num("RAW_KEEP_DAYS", 14, 2)
    return CompactConfig(raw, num("HOURLY_KEEP_DAYS", 180, raw + 1),
                         num("CROSS_KEEP_DAYS", 180, 1), num("ORACLE_KEEP_DAYS", 180, 1))


def load_watermarks(db_path: Path) -> dict | None:                 # [N1] formato real, estricto
    meta = db_path.parent / "ship_meta.json"
    try:
        data = json.loads(meta.read_text())
        if data.get("version") != 1:
            raise ValueError("ship_meta version != 1")
        tables = data["tables"]                                    # {"version":1,"tables":{…}}
        out = {}
        for t in ("spread_ticks", "cross_issuer_ticks", "oracle_predictions"):
            v = tables.get(t)
            if not isinstance(v, int):
                raise ValueError(f"bad watermark for {t}: {v!r}")
            out[t] = v
        return {"version": 1, "tables": out}
    except Exception as exc:
        log.warning("ship_meta.json unusable (%s) — sin agregación ni deletes hoy", exc)
        return None
    # Nota: si scripts/ship_rows.py expone read_watermark()/parse estricto, IMPORTARLO y
    # delegar en él en vez de duplicar (grep antes de escribir esta función).


def migrate_indexes(db_path: Path) -> None:                        # [N6] schema 1º, 1 txn/índice
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


def _aggregate_rows(rows):                                         # [N8] puro, sin txn
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
        if r is not None and (b["refts"] is None or ts >= b["refts"]):   # tie → último id [N12]
            b["ref"], b["refts"] = r, ts
    return buckets, rejects


def _merge_bar(conn, hour, key, b):                                # [B3][N2][N11]
    und, tok, plat = key
    row = conn.execute(
        "SELECT n_ticks, n_spread, spread_bps_real_sum, spread_bps_real_sumsq,"
        " spread_bps_real_min, spread_bps_real_max, n_implied, implied_share_price_sum,"
        " reference_close_independent_last, reference_close_ts_last, market_status_counts"
        " FROM spread_ticks_hourly WHERE ts_hour=? AND underlying_ticker=? AND token_symbol=?"
        " AND platform_id=?", (hour, und, tok, plat)).fetchone()
    new_counts = dict(b["mk"])
    if row is None:
        conn.execute("INSERT INTO spread_ticks_hourly VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (hour, und, tok, plat, b["n"], b["ns"], b["ssum"], b["ssumsq"],
                      b["smin"], b["smax"], b["ni"], b["isum"], b["ref"], b["refts"],
                      json.dumps(new_counts, sort_keys=True)))
        return
    old_counts = json.loads(row[10] or "{}")
    for k, v in b["mk"].items():
        old_counts[k] = old_counts.get(k, 0) + v                     # dict merge [N11]
    smin = min([x for x in (row[4], b["smin"]) if x is not None], default=None)
    smax = max([x for x in (row[5], b["smax"]) if x is not None], default=None)
    ref, refts = row[8], row[9]
    if b["ref"] is not None and (refts is None or (b["refts"] or "") >= refts):
        ref, refts = b["ref"], b["refts"]
    conn.execute(
        "UPDATE spread_ticks_hourly SET n_ticks=?, n_spread=?, spread_bps_real_sum=?,"
        " spread_bps_real_sumsq=?, spread_bps_real_min=?, spread_bps_real_max=?, n_implied=?,"
        " implied_share_price_sum=?, reference_close_independent_last=?,"
        " reference_close_ts_last=?, market_status_counts=? WHERE ts_hour=? AND"
        " underlying_ticker=? AND token_symbol=? AND platform_id=?",
        (row[0] + b["n"], row[1] + b["ns"], row[2] + b["ssum"], row[3] + b["ssumsq"],
         smin, smax, row[6] + b["ni"], row[7] + b["isum"], ref, refts,
         json.dumps(old_counts, sort_keys=True), hour, und, tok, plat))


def _guarded_delete(conn, table, col, cutoff, wm, guard_ids=True) -> int:   # [N4][N5][N13]
    deleted = 0
    while True:
        with sdb.write_txn(conn):                                  # cada lote, txn propia [N4]
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
            else:                                                  # hourly: sin id [N5]
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table}"
                    f" WHERE {col} < ? LIMIT {PRUNE_BATCH})", (cutoff,))
        deleted += cur.rowcount
        if cur.rowcount < PRUNE_BATCH:
            return deleted


def compact(db_path: Path, cfg: CompactConfig, dry_run: bool = False,
            now: datetime | None = None) -> dict:                  # [N15]
    now = (now or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    rep = {"hours_with_rows": 0, "rows_deleted": 0, "hourly_pruned": 0, "cross_pruned": 0,
           "oracle_pruned": 0, "rows_reject": 0, "failures": 0, "planned_rows_deleted": 0,
           "planned_hours_with_rows": 0}
    wm = load_watermarks(db_path)
    conn = sdb.connect_ro(db_path) if dry_run else sdb.connect(db_path)   # [N8][N10]
    if not dry_run:
        with sdb.write_txn(conn):
            conn.execute(HOUR_SCHEMA)                              # barato, permitido [N7]
        if not _has_index(conn, "idx_spread_ts"):
            log.error("idx_spread_ts ausente — ejecuta primero --migrate-indexes (collector parado)")
            rep["failures"] += 1
            conn.close()
            return rep
    raw_cutoff = (now - timedelta(days=cfg.raw_keep_days)).strftime(HOUR_FMT)

    row = conn.execute("SELECT MIN(ts_utc) FROM spread_ticks").fetchone()
    if row and row[0]:
        if not TS_RE.match(row[0]):                                # [N17]
            log.error("MIN(ts_utc) con formato inesperado: %r", row[0])
            rep["failures"] += 1
        else:
            hour = row[0][:13]
            while hour < raw_cutoff and HOUR_RE.match(hour):
                hi = (datetime.strptime(hour, HOUR_FMT) + timedelta(hours=1)).strftime(HOUR_FMT)
                h0s, h1s = hour + ":00", hi + ":00"
                if dry_run:
                    wmv = (wm or {}).get("tables", {}).get("spread_ticks", -1) if wm else -1
                    max_id = conn.execute("SELECT MAX(id) FROM spread_ticks").fetchone()[0] or -1
                    n = conn.execute("SELECT COUNT(*) FROM spread_ticks WHERE ts_utc >= ? AND"
                                     " ts_utc < ? AND id <= ? AND id < ?",
                                     (h0s, h1s, wmv, max_id)).fetchone()[0]
                    rep["planned_rows_deleted"] += n
                    rep["planned_hours_with_rows"] += 1 if n else 0   # guardas reales [N10]
                    hour = hi
                    continue
                if wm is None:                                     # nada sin watermark [N2]
                    break
                wmv = wm["tables"]["spread_ticks"]
                try:
                    max_id = conn.execute("SELECT MAX(id) FROM spread_ticks").fetchone()[0]
                    rows = conn.execute(
                        "SELECT id, ts_utc, underlying_ticker, token_symbol, platform_id,"
                        " spread_bps_real, implied_share_price, reference_close_independent,"
                        " market_status FROM spread_ticks WHERE ts_utc >= ? AND ts_utc < ?"
                        " AND id <= ? AND id < ? ORDER BY ts_utc, id",
                        (h0s, h1s, wmv, max_id)).fetchall()        # EXACTO lo que se borra [N2]
                    if rows:
                        buckets, rejects = _aggregate_rows(rows)   # snapshot de lectura [N8]
                        lo, hi_id = rows[0][0], rows[-1][0]
                        with sdb.write_txn(conn):                  # txn corta: merge+delete [N8]
                            for key, b in buckets.items():
                                _merge_bar(conn, hour, key, b)
                            conn.execute(
                                "DELETE FROM spread_ticks WHERE id BETWEEN ? AND ?"
                                " AND id <= ? AND id < (SELECT MAX(id) FROM spread_ticks)",
                                (lo, hi_id, wmv))
                        rep["rows_reject"] += rejects
                        rep["rows_deleted"] += len(rows)
                        rep["hours_with_rows"] += 1
                    hour = hi
                except Exception as exc:
                    rep["failures"] += 1
                    log.warning("hora %s falló: %s", hour, exc)
                    try:
                        conn.rollback()
                    except sqlite3.OperationalError:
                        pass
                    hour = hi                                      # avanzar también en fallo [N17]

    tables_wm = wm["tables"] if wm else None
    for table, col, days, key, guard in (
            ("spread_ticks_hourly", "ts_hour", cfg.hourly_keep_days, "hourly_pruned", False),
            ("cross_issuer_ticks", "ts_utc", cfg.cross_keep_days, "cross_pruned", True),
            ("oracle_predictions", "snapshot_ts", cfg.oracle_keep_days, "oracle_pruned", True)):
        cutoff = (now - timedelta(days=days)).strftime(HOUR_FMT)
        if dry_run:
            if not guard:                                      # hourly: rowid, sin watermark [N5]
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} < ?",
                                 (cutoff,)).fetchone()[0]
            elif tables_wm is None:                            # nada sin watermark [N2]
                n = 0
            else:
                wmv = tables_wm.get(table)
                max_id = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} < ? AND id <= ? AND id < ?",
                    (cutoff, wmv if wmv is not None else -1,
                     max_id if max_id is not None else -1)).fetchone()[0]
            rep["planned_" + key] = n                          # mismas guardas que real [N10]
        else:
            try:
                rep[key] += _guarded_delete(conn, table, col, cutoff,
                                            (tables_wm or {}).get(table) if guard else None,
                                            guard_ids=guard)       # default None [N13]
            except Exception as exc:
                rep["failures"] += 1
                log.warning("prune %s falló: %s", table, exc)
    if not dry_run:
        try:                                                       # PASSIVE, no TRUNCATE [N14]
            log.info("wal_checkpoint(PASSIVE) -> %s", conn.execute(
                "PRAGMA wal_checkpoint(PASSIVE)").fetchone())
        except sqlite3.OperationalError as exc:
            log.info("checkpoint omitido: %s", exc)
    conn.close()
    return rep
```

### 2c. CLI — `scripts/compact_ledger.py` (fino, patrón `rotate_data.py` [O4])

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
    return 1 if rep["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
```

`python3 -m pytest tests/ -q` → TODO verde (392 existentes + nuevas).

## Task 3 — migración, docs, cron diferido [O1][O2][N6][N7][N8]

1. **PARAR el collector** (`hermes cron pause` del job collector / `pkill -f collector.py` según
   runbook) — obligatorio para la migración [N6].
2. `python3 scripts/compact_ledger.py --migrate-indexes` (4 índices, uno por txn, tiempos en log).
3. Reiniciar collector; vigilar `data/logs/collector.log` 5 min.
4. `python3 scripts/compact_ledger.py --dry-run` → informe `planned_*` al PR.
5. README «Maintenance»: rotate cada 10 min; compact diario 04:25; knobs `*_KEEP_DAYS`
   (14/180/180/180); sin VACUUM automático (manual con collector parado si hace falta).
6. Branch `ledger-compaction`, sin mergear (operador decide).
7. **Cron compactación DIFERIDO hasta post-merge** [O1] — línea documentada en el PR:
   ```
   25 4 * * * cd /home/lab-master/stockgap-agent && /usr/bin/flock -n -E 0 data/compact.lock /usr/bin/ionice -c2 -n7 /usr/bin/nice -n 19 /usr/bin/python3 scripts/compact_ledger.py >> data/logs/compact.log 2>&1
   ```
   Primera ejecución real supervisada (collector log tailed). [O3] follow-up anotado: el ledger
   de Northflank no se compacta — trabajo futuro.

## Verification checklist (el executor pega outputs)

- `python3 -m pytest tests/ -q` → 0 fallos
- `python3 scripts/compact_ledger.py --migrate-indexes` (collector parado) → 4 índices
- `python3 scripts/compact_ledger.py --dry-run` → `planned_*` con guardas; DB byte-idéntica
- `crontab -l | grep -c 'rotate_data.py'` → `1`
- `git status` limpio en `ledger-compaction`
