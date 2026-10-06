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
