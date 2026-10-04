"""Hardened, token-protected, idempotent row ingest — the hybrid sync target.

`POST /ingest/rows` + `GET /ingest/state` (both hidden from openapi). The
local host collects (Binance Web3 works only from the residential IP) and a
shipper POSTs row batches here so the judge-facing dashboard and `POST /mcp`
on Northflank see live data (plan 2026-10-04-hybrid-collector-sync v3/v4).

This module is INTERNET-FACING surface on a service that sells this data —
the security contract below is load-bearing and every point is tested in
tests/test_ingest.py:

- Auth FIRST — no body byte is touched before it passes:
  ``Authorization: Bearer $STOCKGAP_INGEST_TOKEN`` (env read at REQUEST
  time, like STOCKGAP_DB). Unset or < 32 chars -> 503 "ingest disabled"
  (fail closed). ``hmac.compare_digest`` on BYTES; malformed / non-ASCII
  header -> 401, never a 500. 401 is log-only with a fixed message (NO
  token echo) and NO sleep — a fixed delay inside the threadpool lets a
  bad-token flood exhaust it and stall /mcp [N5].
- Byte-capped bodies (4 MiB): ``Content-Length`` over the cap -> 413 from
  the header alone (cheap reject, explicitly NOT a security boundary), and
  the streamed read (``request.stream()``) keeps a running byte counter that
  aborts mid-stream at the cap -> 413 — this caps chunked bodies too, which
  ``request.body()`` would buffer whole [N1].
- JSON parse + validation + the DB write run under ``run_in_threadpool``
  (starlette.concurrency) so a 1000-row write never blocks /mcp [R3].
- Values: ``str | int | float | None`` only; NaN/inf, ints outside int64 and
  nested objects/lists -> 422 BEFORE the txn. NOT NULL violations -> 422
  (pre-checked against ``PRAGMA table_info``; ``sqlite3.IntegrityError`` is
  caught as belt and braces — ``INSERT OR IGNORE`` would otherwise SILENTLY
  drop such rows in append mode). SQL identifiers come ONLY from
  ``PRAGMA table_info`` names, double-quoted. Unknown table/column or a
  column-set mismatch (must EQUAL exactly) -> 422, nothing written; a column
  the NF-side table lacks answers 422 "NF schema older than local — deploy
  first" [R2b].
- Write semantics: ONE ``db.write_txn`` per request (BEGIN IMMEDIATE).
  ``append`` = ``INSERT OR IGNORE`` with explicit id — replays are
  idempotent and history is unrewritable. ``snapshot`` = REAL upsert
  overwrite by PK (``ON CONFLICT(<pk>) DO UPDATE SET ...`` — NEVER OR
  IGNORE): a re-shipped row with a now-NULL-healed column (e.g.
  ``oracle_reputation.feedback_ref``) must REPLACE the old row [R2][v4].
- ``ensure_schema`` runs at the TOP of every write request and on
  /ingest/state — no process-lifetime "done" flag (it goes stale when the
  cutover ``mv`` swaps the DB file) [N2]. Cheap ``sqlite_master`` probe;
  DDL runs ONLY when something is missing: ``collector.SCHEMA`` +
  ``collector.migrate_schema`` + ``oracle.SCHEMA``;
  ``oracle_score.SCHEMA`` (oracle_reputation + oracle_feedback) lazily on
  the first ``oracle_reputation`` batch [R8]. DDL (``executescript``
  implies COMMIT) stays OUTSIDE ``write_txn``, under its own lock.

This module NEVER imports ``dashboard`` (circular: dashboard imports it)
and resolves the DB via its own ``os.environ.get("STOCKGAP_DB", ...)``
per request [R3].
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from . import collector, db, oracle

log = logging.getLogger("stockgap.ingest")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "spread_ticks.db"

MAX_BODY_BYTES = 4 * 1024 * 1024  # 4 MiB — Content-Length gate + stream counter
MAX_ROWS = 1000
MIN_TOKEN_LEN = 32
DEPLOY_FIRST = "NF schema older than local — deploy first"

# The ONLY tables that accept rows. alert_state is host-owned (NOT shipped)
# and oracle_feedback is explicitly NOT shipped (sync model, plan v4).
INGEST_TABLES = frozenset({
    "spread_ticks", "cross_issuer_ticks", "oracle_predictions",
    "oracle_reputation", "oracle_reference", "collector_meta",
})
MODES = ("append", "snapshot")

# Tables created by collector.SCHEMA + oracle.SCHEMA. oracle_reputation is
# NOT here: it is created lazily via oracle_score.SCHEMA on the first
# oracle_reputation batch [R8].
_BASE_TABLES = frozenset({
    "spread_ticks", "cross_issuer_ticks", "collector_meta",
    "oracle_predictions", "oracle_reference",
})

_DDL_LOCK = threading.Lock()  # serializes ensure_schema DDL only

router = APIRouter(prefix="/ingest", include_in_schema=False)


class IngestError(Exception):
    """Validation failure raised inside ``_apply`` -> HTTP 422."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _db_path() -> Path:
    """DB resolved at REQUEST time (same pattern as dashboard.get_db_path)."""
    return Path(os.environ.get("STOCKGAP_DB", str(DEFAULT_DB)))


# ----------------------------- schema-ensure ------------------------------

def _table_names(con: sqlite3.Connection) -> set:
    return {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(con: sqlite3.Connection, table: str) -> List[Tuple[str, int, int]]:
    """[(name, notnull, pk)] from PRAGMA table_info — the ONLY SQL identifiers."""
    return [
        (r[1], r[3], r[5]) for r in con.execute(f'PRAGMA table_info("{table}")')
    ]


def _needs_migrate(con: sqlite3.Connection) -> bool:
    """True when a pre-provenance DB is missing columns migrate_schema adds."""
    wanted = {
        "spread_ticks": {
            "source_endpoint", "fetched_at", "spread_bps_real",
            "reference_close_independent", "reference_source", "reference_kind",
        },
        "cross_issuer_ticks": {"source_endpoint", "fetched_at"},
    }
    for table, cols_needed in wanted.items():
        cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
        if cols_needed - cols:
            return True
    return False


def ensure_schema(con: sqlite3.Connection, *, include_score: bool = False) -> None:
    """Idempotent schema-ensure at the TOP of every write + on /ingest/state.

    Cheap sqlite_master probe; DDL runs ONLY when something is missing —
    there is deliberately no process-lifetime flag (it goes stale after the
    cutover ``mv`` swaps the DB file under us) [N2]. DDL stays OUTSIDE
    ``write_txn`` (executescript implies COMMIT) under its own lock.
    """
    with _DDL_LOCK:
        existing = _table_names(con)
        if _BASE_TABLES - existing:
            con.executescript(collector.SCHEMA)
            collector.migrate_schema(con)
            con.executescript(oracle.SCHEMA)
        elif _needs_migrate(con):
            collector.migrate_schema(con)  # old DB: provenance columns + labels
        if include_score and "oracle_reputation" not in existing:
            from . import oracle_score  # lazy: score tables on first batch only
            con.executescript(oracle_score.SCHEMA)
        con.commit()


# ------------------------------- handlers ---------------------------------

def _auth_response(request: Request) -> Optional[Response]:
    """Auth check FIRST — no body touched. None means authorized."""
    token = os.environ.get("STOCKGAP_INGEST_TOKEN", "")  # read at REQUEST time
    if len(token) < MIN_TOKEN_LEN:
        return PlainTextResponse("ingest disabled", status_code=503)
    try:
        provided = request.headers.get("authorization", "").encode("ascii")
    except UnicodeEncodeError:
        provided = b"\xff\xff"  # malformed/non-ASCII -> mismatch -> 401, never 500
    expected = ("Bearer " + token).encode("utf-8")
    if not hmac.compare_digest(provided, expected):
        log.warning("ingest auth failed")  # fixed message, NO token echo, NO sleep
        return PlainTextResponse("unauthorized", status_code=401)
    return None


@router.post("/rows")
async def ingest_rows(request: Request) -> Response:
    # (1) auth FIRST — body untouched.
    err = _auth_response(request)
    if err is not None:
        return err
    # (2) cheap header-only reject — NOT a security boundary.
    raw_len = request.headers.get("content-length")
    if raw_len is not None:
        try:
            declared = int(raw_len)
        except ValueError:
            declared = -1  # garbage header: the streamed counter is the real cap
        if declared > MAX_BODY_BYTES:
            return PlainTextResponse("payload too large", status_code=413)
    # (3) streamed read with a running byte counter — aborts mid-stream at the
    #     cap, which also bounds chunked bodies (no Content-Length at all).
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return PlainTextResponse("payload too large", status_code=413)
    try:
        result = await run_in_threadpool(_apply, bytes(body))
    except IngestError as exc:
        return JSONResponse({"detail": exc.detail}, status_code=422)
    return JSONResponse(result)


@router.get("/state")
async def ingest_state(request: Request) -> Response:
    err = _auth_response(request)
    if err is not None:
        return err
    try:
        result = await run_in_threadpool(_state)
    except IngestError as exc:  # pragma: no cover — defensive
        return JSONResponse({"detail": exc.detail}, status_code=422)
    return JSONResponse(result)


# --------------------- threadpool: parse/validate/write --------------------

def _validate_value(i: int, key: str, value: Any) -> None:
    """str | int | float | None only — BEFORE the txn [R3]."""
    vtype = type(value)
    if vtype is float:
        if not math.isfinite(value):
            raise IngestError(f"NaN/Inf not allowed (rows[{i}].{key})")
    elif vtype is int:
        if not (-(2 ** 63) <= value < 2 ** 63):
            raise IngestError(f"integer out of int64 range (rows[{i}].{key})")
    elif vtype is bool or isinstance(value, (dict, list)):
        raise IngestError(f"nested/boolean values not allowed (rows[{i}].{key})")
    elif vtype not in (str, type(None)):
        raise IngestError(f"unsupported value type (rows[{i}].{key})")


def _apply(payload: bytes) -> dict:
    """JSON parse + validation + ONE write_txn — runs in the threadpool."""
    try:
        doc = json.loads(payload)
    except ValueError as exc:
        raise IngestError(f"invalid JSON body: {exc}")
    if not isinstance(doc, dict):
        raise IngestError("body must be a JSON object")
    table = doc.get("table")
    mode = doc.get("mode")
    rows = doc.get("rows")
    if not isinstance(table, str) or table not in INGEST_TABLES:
        raise IngestError(f"unknown table: {table!r}")
    if mode not in MODES:
        raise IngestError("mode must be 'append' or 'snapshot'")
    if not isinstance(rows, list):
        raise IngestError("rows must be a list of objects")
    if len(rows) > MAX_ROWS:
        raise IngestError(f"too many rows (max {MAX_ROWS})")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise IngestError(f"rows[{i}] must be a JSON object")
        for key, value in row.items():
            _validate_value(i, key, value)

    con = db.connect(_db_path())
    try:
        db.ensure_wal(con)
        ensure_schema(con, include_score=(table == "oracle_reputation"))
        _write(con, table, mode, rows)
    finally:
        con.close()
    return {"table": table, "rows": len(rows)}


def _write(
    con: sqlite3.Connection, table: str, mode: str, rows: Sequence[Mapping[str, Any]]
) -> None:
    info = _columns(con, table)
    cols = [name for name, _, _ in info]
    colset = set(cols)
    pks = [name for name, _, pk in sorted(info, key=lambda t: t[2]) if pk]
    notnull = {name for name, nn, _ in info if nn}

    # Column-set must EQUAL the table's exactly — no partial rows that could
    # silently wipe columns [R2].
    for i, row in enumerate(rows):
        keys = set(row)
        extra = keys - colset
        if extra:
            raise IngestError(
                f"unknown column(s) for {table}: {sorted(extra)} — {DEPLOY_FIRST}"
            )
        missing = colset - keys
        if missing:
            raise IngestError(
                f"row column set must match {table} exactly (missing: {sorted(missing)})"
            )
        for col in notnull:
            if row[col] is None:
                raise IngestError(f"NULL in NOT NULL column {col!r} (rows[{i}])")

    quoted = ", ".join(f'"{c}"' for c in cols)
    marks = ", ".join("?" for _ in cols)
    if mode == "append":
        # Explicit id + OR IGNORE: replays idempotent, history unrewritable.
        sql = f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({marks})'
    else:
        if not pks:
            raise IngestError(f"table {table} has no primary key — snapshot upsert impossible")
        non_pk = [c for c in cols if c not in colset.intersection(pks)]
        tail = (
            "DO UPDATE SET " + ", ".join(f'"{c}"=excluded."{c}"' for c in non_pk)
            if non_pk else "DO NOTHING"
        )
        conflict = ", ".join(f'"{c}"' for c in pks)
        sql = (
            f'INSERT INTO "{table}" ({quoted}) VALUES ({marks}) '
            f"ON CONFLICT({conflict}) {tail}"
        )
    params = [tuple(row[c] for c in cols) for row in rows]
    try:
        with db.write_txn(con):  # ONE BEGIN IMMEDIATE per request
            con.executemany(sql, params)
    except sqlite3.IntegrityError as exc:  # NOT NULL et al -> 422, never 500
        raise IngestError(f"integrity violation: {exc}")


def _state() -> dict:
    """Watermark view for the cutover preflight: max_id / max_tick_id per table.

    ``max_id`` is 0 for an empty id table and null for tables without an id
    column (oracle_reference, collector_meta); a lazily-created
    oracle_reputation reports as empty until its first batch.
    """
    con = db.connect(_db_path())
    try:
        db.ensure_wal(con)
        ensure_schema(con)
        existing = _table_names(con)
        tables = {}
        for name in sorted(INGEST_TABLES):
            cols = {c for c, _, _ in _columns(con, name)} if name in existing else set()
            if "id" in cols:
                row = con.execute(f'SELECT MAX("id") FROM "{name}"').fetchone()
                max_id: Optional[int] = int(row[0] or 0)
            elif name in existing:
                max_id = None  # key-PK table (oracle_reference, collector_meta)
            else:
                max_id = 0  # lazily-created table: nothing there yet
            if "tick_id" in cols:
                row = con.execute(f'SELECT MAX("tick_id") FROM "{name}"').fetchone()
                max_tick: Optional[int] = int(row[0]) if row and row[0] is not None else None
            else:
                max_tick = None
            tables[name] = {"max_id": max_id, "max_tick_id": max_tick}
        return {"tables": tables}
    finally:
        con.close()
