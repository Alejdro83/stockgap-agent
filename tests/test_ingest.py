"""Task 1 (hybrid plan v3/v4): hardened idempotent row ingest + state endpoint.

Every point of the plan's security contract has a test here:
- auth-first (no body touched before auth), fixed-message 401 (no echo, no
  sleep), 503 'ingest disabled' fail-closed, malformed/non-ASCII -> 401.
- byte-capped bodies: Content-Length cheap reject + streamed counter that
  aborts chunked bodies mid-stream at 4 MB.
- validation before the txn: value types (str/int/float/None only, no
  NaN/inf/int64-overflow/nesting), exact column-set equality, NOT NULL ->
  422; unknown column -> 422 carrying 'NF schema older than local — deploy
  first'.
- write semantics: append = INSERT OR IGNORE (idempotent, never rewrites),
  snapshot = real upsert overwrite (heals NULL columns, e.g. feedback_ref).
- schema-ensure: old-schema DB heals via migrate_schema; DB file swapped
  mid-process keeps ingest working (no process-lifetime flag).
- hygiene: /ingest/* hidden from openapi, ingest never imports dashboard,
  oracle/oracle_score import under an EMPTY env, Sentry scrub config present.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent

try:  # red-phase scaffolding: every test fails cleanly until ingest exists
    from stockgap import ingest
except Exception:  # pragma: no cover — replaced once stockgap/ingest.py lands
    ingest = None


@pytest.fixture(autouse=True)
def _require_ingest():
    if ingest is None:
        pytest.fail("stockgap.ingest failed to import (red phase)")


# ---- contract constants (restated independently of the implementation) ----
MAX_BODY_BYTES = 4 * 1024 * 1024  # 4 MiB cap, counted in BYTES
MAX_ROWS = 1000
MIN_TOKEN_LEN = 32
TOKEN = "tok-" + "k" * (MIN_TOKEN_LEN - 4)  # exactly 32 chars
WRONG_TOKEN = "w" * MIN_TOKEN_LEN
INGEST_TABLES = {
    "spread_ticks", "cross_issuer_ticks", "oracle_predictions",
    "oracle_reputation", "oracle_reference", "collector_meta",
}

TS = "2026-10-04T00:00:00.000Z"

SPREAD_COLS = [
    "id", "ts_utc", "tick_id", "underlying_ticker", "token_symbol",
    "token_contract", "platform_id", "token_price", "token_to_share_ratio",
    "reference_price", "token_price_updated_at", "implied_share_price",
    "spread_abs", "spread_bps", "spread_pct", "market_status", "open_state",
    "source_endpoint", "fetched_at", "spread_bps_real",
    "reference_close_independent", "reference_source", "reference_kind",
]
CROSS_COLS = [
    "id", "ts_utc", "tick_id", "underlying_ticker", "n_issuers",
    "cheap_symbol", "cheap_platform", "cheap_implied", "rich_symbol",
    "rich_platform", "rich_implied", "spread_abs", "spread_bps",
    "source_endpoint", "fetched_at",
]
PRED_COLS = [
    "id", "snapshot_id", "snapshot_ts", "cutoff_ts", "underlying_ticker",
    "token_symbol", "token_contract", "platform_id", "token_price",
    "token_to_share_ratio", "implied_open_price", "last_reference_close",
    "deviation_bps", "deviation_flag", "market_status", "source_endpoint",
    "fetched_at", "reference_source",
]
REP_COLS = [
    "id", "run_id", "scored_at", "cutoff_ts", "snapshot_ts", "row_kind",
    "underlying_ticker", "token_symbol", "token_contract", "platform_id",
    "predicted_open", "actual_open", "last_reference_close", "signed_error",
    "signed_error_bps", "abs_error", "abs_pct_error", "direction_predicted",
    "direction_actual", "direction_hit", "oracle_score", "feedback_ref",
]
REF_COLS = [
    "underlying_ticker", "reference_close", "source_endpoint",
    "fetched_epoch", "fetched_at",
]
META_COLS = ["key", "value"]

COLUMN_SETS = {
    "spread_ticks": SPREAD_COLS,
    "cross_issuer_ticks": CROSS_COLS,
    "oracle_predictions": PRED_COLS,
    "oracle_reputation": REP_COLS,
    "oracle_reference": REF_COLS,
    "collector_meta": META_COLS,
}


def spread_row(**over):
    row = dict.fromkeys(SPREAD_COLS)
    row.update(
        id=1, ts_utc=TS, tick_id=1, underlying_ticker="AAA",
        token_symbol="AAAon", token_contract="0xct", platform_id="ondo",
        token_price="10.5", token_to_share_ratio="1", reference_price="10.0",
        token_price_updated_at=1728000000, implied_share_price="10.5",
        spread_abs="0.5", spread_bps="500", spread_pct="5.0",
        market_status="overnight", open_state=0,
        source_endpoint="/api/v1/dex/market/rwa/price", fetched_at=TS,
        spread_bps_real="480", reference_close_independent="10.1",
        reference_source="rwa/price (independent)", reference_kind="independent",
    )
    row.update(over)
    return row


def cross_row(**over):
    row = dict.fromkeys(CROSS_COLS)
    row.update(
        id=1, ts_utc=TS, tick_id=1, underlying_ticker="SOXL", n_issuers=2,
        cheap_symbol="SOXLon", cheap_platform="ondo", cheap_implied="154",
        rich_symbol="SOXLB", rich_platform="bstock", rich_implied="156",
        spread_abs="2", spread_bps="108.4",
        source_endpoint="/api/v1/dex/market/rwa/price", fetched_at=TS,
    )
    row.update(over)
    return row


def pred_row(**over):
    row = dict.fromkeys(PRED_COLS)
    row.update(
        id=1, snapshot_id=1, snapshot_ts=TS, cutoff_ts=TS,
        underlying_ticker="AAA", token_symbol="AAAon", token_contract="0xct",
        platform_id="ondo", token_price="10.5", token_to_share_ratio="1",
        implied_open_price="10.5", last_reference_close="10.0",
        deviation_bps="500", deviation_flag=0, market_status="overnight",
        source_endpoint="/api/v1/dex/market/rwa/price", fetched_at=TS,
        reference_source="rwa/price (independent)",
    )
    row.update(over)
    return row


def rep_row(**over):
    row = dict.fromkeys(REP_COLS)
    row.update(
        id=1, run_id="run-1", scored_at=TS, cutoff_ts=TS, snapshot_ts=TS,
        row_kind="token", underlying_ticker="AAA", token_symbol="AAAon",
        token_contract="0xct", platform_id="ondo", predicted_open="10.5",
        actual_open="10.4", last_reference_close="10.0",
        signed_error="0.1", signed_error_bps="96.15", abs_error="0.1",
        abs_pct_error="0.96", direction_predicted=1, direction_actual=1,
        direction_hit=1, oracle_score="99.04", feedback_ref=None,
    )
    row.update(over)
    return row


def ref_row(**over):
    row = dict.fromkeys(REF_COLS)
    row.update(
        underlying_ticker="AAA", reference_close="100",
        source_endpoint="/api/v1/dex/market/rwa/price", fetched_epoch=1728000000.0,
        fetched_at=TS,
    )
    row.update(over)
    return row


def meta_row(**over):
    row = dict.fromkeys(META_COLS)
    row.update(key="last_tick_ts", value=TS)
    row.update(over)
    return row


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "spread_ticks.db"
    monkeypatch.setenv("STOCKGAP_DB", str(db))
    monkeypatch.setenv("STOCKGAP_INGEST_TOKEN", TOKEN)
    return db


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(ingest.router)
    return TestClient(app)


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def post_rows(client, table, mode, rows, token=TOKEN):
    return client.post(
        "/ingest/rows",
        json={"table": table, "mode": mode, "rows": rows},
        headers=auth(token),
    )


def fetch_rows(db, table):
    con = sqlite3.connect(str(db))
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        con.close()

def count_rows(db, table):
    """Row count; a never-created table counts as 0 (nothing written)."""
    con = sqlite3.connect(str(db))
    try:
        try:
            return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    finally:
        con.close()


def asgi_post(path, headers, chunks):
    """Drive the ASGI app directly: raw bytes headers + streamed body control.

    The httpx-based TestClient cannot send non-ASCII header bytes and drains
    request iterators eagerly, so the two raw-edge cases (malformed header
    bytes, mid-stream abort) are exercised at the ASGI boundary where the
    receive() pull count is observable.
    """
    app = FastAPI()
    app.include_router(ingest.router)
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    queue = list(chunks)
    pulled = 0

    async def receive():
        nonlocal pulled
        pulled += 1
        if queue:
            chunk = queue.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(queue)}
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, pulled


# ----------------------------- auth contract ------------------------------

def test_wrong_token_401_fixed_message_no_echo_no_sleep(client, env):
    started = time.monotonic()
    r = post_rows(client, "spread_ticks", "append", [spread_row()], token=WRONG_TOKEN)
    elapsed = time.monotonic() - started
    assert r.status_code == 401
    assert r.text == "unauthorized"  # fixed message
    assert WRONG_TOKEN not in r.text  # never echo the presented token
    assert elapsed < 1.0  # log-only 401: NO sleep / artificial delay


@pytest.mark.parametrize("header", [
    f"Bearer {WRONG_TOKEN}",
    f"bearer {WRONG_TOKEN}",
    "Basic dXNlcjpwdw==",
    "Bearer",
    "BearerX blabla",
    WRONG_TOKEN,
    "",
])
def test_malformed_header_401_never_500(client, env, header):
    r = client.post(
        "/ingest/rows",
        json={"table": "spread_ticks", "mode": "append", "rows": []},
        headers={"Authorization": header},
    )
    assert r.status_code == 401, f"header {header!r} must yield 401, not {r.status_code}"


def test_non_ascii_header_401_never_500_raw_asgi(env):
    # the httpx client refuses to SEND non-ASCII header bytes, so this walks
    # in over raw ASGI: malformed bytes must give 401, never a 500.
    status, _ = asgi_post(
        "/ingest/rows",
        [(b"authorization", b"Bearer caf\xe9 \xff"), (b"content-length", b"2")],
        [b"{}"],
    )
    assert status == 401


def test_token_unset_503_ingest_disabled(client, env, monkeypatch):
    monkeypatch.delenv("STOCKGAP_INGEST_TOKEN", raising=False)
    r = post_rows(client, "spread_ticks", "append", [spread_row()])
    assert r.status_code == 503
    assert r.text == "ingest disabled"
    r = client.get("/ingest/state", headers=auth())
    assert r.status_code == 503
    assert r.text == "ingest disabled"


def test_short_token_503_ingest_disabled(client, env, monkeypatch):
    monkeypatch.setenv("STOCKGAP_INGEST_TOKEN", TOKEN[:MIN_TOKEN_LEN - 1])
    r = post_rows(client, "spread_ticks", "append", [spread_row()])
    assert r.status_code == 503
    assert r.text == "ingest disabled"


def test_boundary_token_exactly_32_chars_is_valid(client, env, monkeypatch):
    monkeypatch.setenv("STOCKGAP_INGEST_TOKEN", "b" * MIN_TOKEN_LEN)
    r = post_rows(client, "spread_ticks", "append", [spread_row()], token="b" * MIN_TOKEN_LEN)
    assert r.status_code == 200


def test_token_and_db_resolved_at_request_time(client, env, monkeypatch):
    monkeypatch.delenv("STOCKGAP_INGEST_TOKEN", raising=False)
    assert post_rows(client, "spread_ticks", "append", [spread_row()]).status_code == 503
    monkeypatch.setenv("STOCKGAP_INGEST_TOKEN", TOKEN)  # same client, new env
    assert post_rows(client, "spread_ticks", "append", [spread_row()]).status_code == 200


# ------------------------- auth-before-body [N1] --------------------------

def test_auth_before_body_oversized_cl_no_token_503_not_413(client, env, monkeypatch):
    big = b"x" * (MAX_BODY_BYTES + 1)  # genuinely over the cap
    monkeypatch.delenv("STOCKGAP_INGEST_TOKEN", raising=False)
    r = client.post("/ingest/rows", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 503  # ingest disabled wins over the size reject
    assert r.status_code != 413


def test_auth_before_body_oversized_cl_wrong_token_401_not_413(client, env):
    big = b"x" * (MAX_BODY_BYTES + 1)
    r = client.post("/ingest/rows", content=big, headers=auth(WRONG_TOKEN))
    assert r.status_code == 401  # auth runs before any body inspection
    assert r.status_code != 413


# ---------------------------- byte-size caps ------------------------------

def test_oversized_body_in_bytes_413_even_with_few_rows(client, env):
    # 2 rows but a >4 MiB string value: the cap counts BYTES, not rows.
    rows = [spread_row(id=1, token_symbol="x" * (MAX_BODY_BYTES // 2)),
            spread_row(id=2, token_symbol="y" * (MAX_BODY_BYTES // 2))]
    r = post_rows(client, "spread_ticks", "append", rows)
    assert r.status_code == 413


def test_body_at_cap_boundary(client, env):
    # exactly MAX_BODY_BYTES passes the size gates; +1 byte is rejected.
    row = spread_row(id=1)
    row["token_contract"] = ""
    tpl = json.dumps({"table": "spread_ticks", "mode": "append", "rows": [row]})
    pad = MAX_BODY_BYTES - len(tpl.encode())  # fill "" -> "ppp..." to the exact cap
    body = tpl.replace('"token_contract": ""', '"token_contract": "' + "p" * pad + '"')
    assert len(body.encode()) == MAX_BODY_BYTES
    r = client.post("/ingest/rows", content=body, headers=auth())
    assert r.status_code == 200  # at the cap: NOT rejected for size
    over = body + " "  # one trailing byte over the cap
    r = client.post("/ingest/rows", content=over, headers=auth())
    assert r.status_code == 413


def test_chunked_no_content_length_over_cap_413(client, env):
    chunk = b"x" * 65536

    def gen():
        sent = 0
        while sent < 8 * 1024 * 1024:
            sent += len(chunk)
            yield chunk

    probe = client.build_request("POST", "/ingest/rows", content=gen(), headers=auth())
    assert "content-length" not in probe.headers  # precondition: chunked transfer
    r = client.post("/ingest/rows", content=gen(), headers=auth())
    assert r.status_code == 413


def test_chunked_raw_asgi_aborts_mid_stream_at_cap(env):
    # No Content-Length at all: the running byte counter must stop PULLING
    # the stream at the cap instead of buffering the whole body.
    chunk = b"x" * 65536
    total_planned = 8 * 1024 * 1024  # 2x the cap
    chunks = [chunk] * (total_planned // len(chunk))
    status, pulled = asgi_post(
        "/ingest/rows",
        [(b"authorization", b"Bearer " + TOKEN.encode())],  # no content-length
        chunks,
    )
    assert status == 413
    assert pulled * len(chunk) < total_planned  # aborted mid-stream, unread tail


# ------------------------- validation (422 family) ------------------------

def test_422_unknown_table_nothing_written(client, env):
    for table in ("alert_state", "oracle_feedback", "secrets", "sqlite_master"):
        r = post_rows(client, table, "append", [meta_row()])
        assert r.status_code == 422, table
    r = post_rows(client, 123, "append", [meta_row()])
    assert r.status_code == 422


def test_422_unknown_column_says_deploy_first(client, env):
    row = spread_row()
    row["spread_bps_reel_typo"] = "1"  # column the NF-side table lacks
    r = post_rows(client, "spread_ticks", "append", [row])
    assert r.status_code == 422
    assert "NF schema older than local — deploy first" in r.text
    assert fetch_rows(env, "spread_ticks") == []  # nothing written


def test_422_partial_column_set_nothing_written(client, env):
    row = spread_row()
    del row["spread_bps_real"], row["reference_kind"]  # partial: silently-wiping trap
    # mode=append so this pins the COLUMN-SET rule, not the mode/table-class
    # rule (spread_ticks is append-class — a snapshot POST here would 422 on
    # the class mismatch alone; see the mode-contract tests below).
    r = post_rows(client, "spread_ticks", "append", [row])
    assert r.status_code == 422
    assert fetch_rows(env, "spread_ticks") == []


def test_422_nested_value(client, env):
    for bad in ({"a": 1}, [1, 2], True):
        r = post_rows(client, "spread_ticks", "append", [spread_row(token_symbol=bad)])
        assert r.status_code == 422, bad


def test_422_nan_and_inf(client, env):
    tpl = json.dumps({"table": "spread_ticks", "mode": "append", "rows": [spread_row()]})
    for literal in ("NaN", "Infinity", "-Infinity"):
        payload = tpl.replace('"token_symbol": "AAAon"', f'"token_symbol": {literal}')
        r = client.post("/ingest/rows", content=payload, headers=auth())
        assert r.status_code == 422, literal
    # a float that overflows to inf via json (1e400) is also rejected
    payload = (
        '{"table": "spread_ticks", "mode": "append", "rows": ['
        + json.dumps(spread_row()).replace('"token_price_updated_at": 1728000000',
                                          '"token_price_updated_at": 1e400')
        + "]}"
    )
    r = client.post("/ingest/rows", content=payload, headers=auth())
    assert r.status_code == 422


def test_422_int_outside_int64(client, env):
    r = post_rows(client, "spread_ticks", "append", [spread_row(tick_id=2 ** 63)])
    assert r.status_code == 422
    r = post_rows(client, "spread_ticks", "append", [spread_row(tick_id=-(2 ** 63) - 1)])
    assert r.status_code == 422


def test_422_not_null_violation_append_and_snapshot(client, env):
    r = post_rows(client, "spread_ticks", "append", [spread_row(ts_utc=None)])
    assert r.status_code == 422
    assert fetch_rows(env, "spread_ticks") == []
    r = post_rows(client, "oracle_reputation", "snapshot", [rep_row(run_id=None)])
    assert r.status_code == 422


def test_422_over_max_rows(client, env):
    rows = [spread_row(id=i) for i in range(1, MAX_ROWS + 2)]  # 1001 rows
    r = post_rows(client, "spread_ticks", "append", rows)
    assert r.status_code == 422


def test_invalid_mode_422(client, env):
    r = post_rows(client, "spread_ticks", "merge", [spread_row()])
    assert r.status_code == 422


# ------------- mode / table-class contract (diff-gate hardening) ------------
# `snapshot` = REAL upsert overwrite; `append` = INSERT OR IGNORE. The mode
# must match the table's class or a token holder could rewrite immutable
# append history (or freeze snapshot healing) in one POST.

def test_422_mode_snapshot_never_reaches_append_tables(client, env):
    # FULL valid column set — the 422 must come from the class mismatch, not
    # the column rules, and the upsert path must NEVER run (history intact).
    assert post_rows(client, "spread_ticks", "append",
                     [spread_row(id=7, spread_bps="500")]).status_code == 200
    r = post_rows(client, "spread_ticks", "snapshot",
                  [spread_row(id=7, spread_bps="999")])
    assert r.status_code == 422
    stored = fetch_rows(env, "spread_ticks")
    assert len(stored) == 1
    assert stored[0]["spread_bps"] == "500"  # history UNREWRITTEN — no upsert ran
    for table, maker in (("cross_issuer_ticks", cross_row),
                         ("oracle_predictions", pred_row)):
        r = post_rows(client, table, "snapshot", [maker(id=3)])
        assert r.status_code == 422, table
        assert count_rows(env, table) == 0, table


def test_422_mode_append_never_reaches_snapshot_tables(client, env):
    # `append` against a snapshot table would INSERT OR IGNORE one pass's
    # values forever, defeating the upsert heal (e.g. feedback_ref).
    for table, maker in (("oracle_reputation", rep_row),
                         ("oracle_reference", ref_row),
                         ("collector_meta", meta_row)):
        r = post_rows(client, table, "append", [maker()])
        assert r.status_code == 422, table
        assert count_rows(env, table) == 0, table


def test_422_null_pk_rejected_in_both_modes_nothing_written(client, env):
    # A NULL INTEGER PRIMARY KEY auto-assigns a FRESH id in SQLite: every
    # re-POST would insert another row (idempotency silently broken) and a
    # snapshot POST would insert a new row each time. Explicit non-NULL pk
    # or 422 — both modes, pre-validation, nothing written.
    r = post_rows(client, "spread_ticks", "append", [spread_row(id=None)])
    assert r.status_code == 422
    assert "primary key" in r.json()["detail"]      # fixed 422 body style
    assert count_rows(env, "spread_ticks") == 0
    for table, maker, pk in (
        ("oracle_reputation", rep_row, {"id": None}),
        ("oracle_reference", ref_row, {"underlying_ticker": None}),
        ("collector_meta", meta_row, {"key": None}),
    ):
        r = post_rows(client, table, "snapshot", [maker(**pk)])
        assert r.status_code == 422, table
        assert count_rows(env, table) == 0, table
    # sanity: the same shapes with explicit non-NULL pks still land
    assert post_rows(client, "spread_ticks", "append",
                     [spread_row(id=1)]).status_code == 200
    assert post_rows(client, "oracle_reference", "snapshot",
                     [ref_row()]).status_code == 200


def test_422_missing_pk_rejected_in_both_modes_nothing_written(client, env):
    cases = (
        ("spread_ticks", "append", spread_row, "id"),
        ("oracle_reputation", "snapshot", rep_row, "id"),
        ("oracle_reference", "snapshot", ref_row, "underlying_ticker"),
        ("collector_meta", "snapshot", meta_row, "key"),
    )
    for table, mode, maker, pk in cases:
        row = maker()
        del row[pk]
        r = post_rows(client, table, mode, [row])
        assert r.status_code == 422, table
        assert "primary key" in r.json()["detail"], table  # fixed 422 body style
        assert count_rows(env, table) == 0, table


def test_invalid_json_body_422(client, env):
    r = client.post("/ingest/rows", content=b"{not json", headers=auth())
    assert r.status_code == 422


# --------------------------- write semantics ------------------------------

def test_append_idempotent_same_batch_twice(client, env):
    rows = [spread_row(id=1), spread_row(id=2, tick_id=1, token_symbol="BBBon",
                                         underlying_ticker="BBB")]
    for _ in range(2):
        r = post_rows(client, "spread_ticks", "append", rows)
        assert r.status_code == 200
    stored = fetch_rows(env, "spread_ticks")
    assert len(stored) == 2  # counts unchanged
    assert stored[0]["token_symbol"] == "AAAon"  # content intact
    assert stored[1]["token_symbol"] == "BBBon"


def test_append_never_rewrites_same_id(client, env):
    assert post_rows(client, "spread_ticks", "append", [spread_row(id=7, spread_bps="500")]).status_code == 200
    r = post_rows(client, "spread_ticks", "append", [spread_row(id=7, spread_bps="999")])
    assert r.status_code == 200  # replay accepted idempotently
    stored = fetch_rows(env, "spread_ticks")
    assert len(stored) == 1
    assert stored[0]["spread_bps"] == "500"  # original kept — history unrewritable


def test_snapshot_upsert_real_overwrite(client, env):
    assert post_rows(client, "oracle_reference", "snapshot", [ref_row(reference_close="100")]).status_code == 200
    r = post_rows(client, "oracle_reference", "snapshot", [
        ref_row(reference_close="101", fetched_at="2026-10-04T01:00:00.000Z")])
    assert r.status_code == 200
    stored = fetch_rows(env, "oracle_reference")
    assert len(stored) == 1
    assert stored[0]["reference_close"] == "101"  # NOT OR IGNORE: new values win


def test_snapshot_heals_feedback_ref_null(client, env):
    # oracle_reputation is created LAZILY on the first batch (oracle_score.SCHEMA)
    con = sqlite3.connect(str(env))
    assert not [r for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='oracle_reputation'")]
    con.close()

    assert post_rows(client, "oracle_reputation", "snapshot",
                     [rep_row(feedback_ref=None, oracle_score="90")]).status_code == 200

    # the local UPDATE sets feedback_ref later — re-ship must HEAL the row
    r = post_rows(client, "oracle_reputation", "snapshot",
                  [rep_row(feedback_ref="0xfeed", oracle_score="91")])
    assert r.status_code == 200
    stored = fetch_rows(env, "oracle_reputation")
    assert len(stored) == 1
    assert stored[0]["feedback_ref"] == "0xfeed"  # NULL healed, row replaced
    assert stored[0]["oracle_score"] == "91"


def test_success_response_shape(client, env):
    r = post_rows(client, "collector_meta", "snapshot", [meta_row(), meta_row(key="universe_count")])
    assert r.status_code == 200
    assert r.json() == {"table": "collector_meta", "rows": 2}


# ---------------------------- /ingest/state -------------------------------

def test_ingest_state_shape_and_auth(client, env):
    assert client.get("/ingest/state").status_code == 401
    assert client.get("/ingest/state", headers=auth(WRONG_TOKEN)).status_code == 401
    assert post_rows(client, "spread_ticks", "append",
                     [spread_row(id=1, tick_id=7), spread_row(id=2, tick_id=7)]).status_code == 200

    r = client.get("/ingest/state", headers=auth())
    assert r.status_code == 200
    tables = r.json()["tables"]
    assert set(tables) == INGEST_TABLES
    for name, state in tables.items():
        assert set(state) == {"max_id", "max_tick_id"}, name
    assert tables["spread_ticks"] == {"max_id": 2, "max_tick_id": 7}
    assert tables["cross_issuer_ticks"] == {"max_id": 0, "max_tick_id": None}
    assert tables["oracle_predictions"] == {"max_id": 0, "max_tick_id": None}
    assert tables["oracle_reputation"] == {"max_id": 0, "max_tick_id": None}
    assert tables["oracle_reference"] == {"max_id": None, "max_tick_id": None}
    assert tables["collector_meta"] == {"max_id": None, "max_tick_id": None}


# --------------------- schema-ensure / migrate [R2b][N2] ------------------

OLD_SPREAD_DDL = """
CREATE TABLE spread_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    tick_id INTEGER NOT NULL,
    underlying_ticker TEXT,
    token_symbol TEXT,
    token_contract TEXT,
    platform_id TEXT,
    token_price TEXT,
    token_to_share_ratio TEXT,
    reference_price TEXT,
    token_price_updated_at INTEGER,
    implied_share_price TEXT,
    spread_abs TEXT,
    spread_bps TEXT,
    spread_pct TEXT,
    market_status TEXT,
    open_state INTEGER
)"""  # pre-source_endpoint / pre-real-metric: exactly what _migrate heals


def test_old_schema_db_migrates_and_shipped_rows_land(client, env):
    from stockgap import collector

    con = sqlite3.connect(str(env))
    con.executescript(collector.SCHEMA + "DROP TABLE spread_ticks;" + OLD_SPREAD_DDL)
    con.execute("INSERT INTO spread_ticks (id, ts_utc, tick_id, token_symbol) "
                "VALUES (1, ?, 1, 'old')", (TS,))
    con.commit()
    con.close()

    r = post_rows(client, "spread_ticks", "append", [spread_row(id=2, tick_id=2)])
    assert r.status_code == 200  # migrate fills columns -> full row lands

    con = sqlite3.connect(str(env))
    cols = {row[1] for row in con.execute("PRAGMA table_info(spread_ticks)")}
    old = con.execute("SELECT reference_kind, reference_source FROM spread_ticks WHERE id=1").fetchone()
    new = con.execute("SELECT reference_kind, tick_id FROM spread_ticks WHERE id=2").fetchone()
    con.close()
    assert set(SPREAD_COLS) <= cols  # migrate added the missing columns
    assert old == ("derived", collector.DERIVED_REFERENCE_SOURCE)  # honest label
    assert new == ("independent", 2)  # shipped values land untouched


def test_db_file_swapped_mid_process_ingest_keeps_working(client, env):
    assert post_rows(client, "spread_ticks", "append", [spread_row(id=1, tick_id=1)]).status_code == 200

    # cutover: the DB file (and any -wal/-shm) disappears mid-process
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(env) + suffix)
        if p.exists():
            p.rename(p.parent / (p.name + ".precut"))

    r = post_rows(client, "spread_ticks", "append", [spread_row(id=1, tick_id=42)])
    assert r.status_code == 200  # ensure re-creates the tables on a fresh file
    stored = fetch_rows(env, "spread_ticks")
    assert [row["tick_id"] for row in stored] == [42]
    r = client.get("/ingest/state", headers=auth())
    assert r.json()["tables"]["spread_ticks"] == {"max_id": 1, "max_tick_id": 42}


# ------------------------------ hygiene [R9][R3] --------------------------

def test_empty_env_import_oracle_modules():
    code = "import stockgap.oracle, stockgap.oracle_score; print('ok')"
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": str(REPO_ROOT)},  # EMPTY env: no secrets, no config
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_ingest_never_imports_dashboard():
    src = (REPO_ROOT / "stockgap" / "ingest.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [a.name for a in node.names]
        else:
            continue
        for name in names:
            assert "dashboard" not in name.split("."), name


def test_endpoints_hidden_from_openapi(client):
    schema = client.app.openapi()
    assert "/ingest/rows" not in schema["paths"]
    assert "/ingest/state" not in schema["paths"]


def test_sentry_scrub_config_present(monkeypatch):
    from stockgap import telemetry

    captured = {}
    stub = types.ModuleType("sentry_sdk")
    stub_int = types.ModuleType("sentry_sdk.integrations")
    stub_fast = types.ModuleType("sentry_sdk.integrations.fastapi")

    class FastApiIntegration:  # minimal stand-in for the real integration
        pass

    stub_fast.FastApiIntegration = FastApiIntegration
    stub_int.fastapi = stub_fast
    stub.integrations = stub_int
    stub.init = lambda **kw: captured.update(kw)
    stub.set_tag = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "sentry_sdk", stub)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", stub_int)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.fastapi", stub_fast)
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.com/1")
    monkeypatch.setattr(telemetry, "_init_state", None)

    assert telemetry.init_sentry("stockgap-dashboard", fastapi=True) is True
    assert captured.get("send_default_pii") is False
    assert captured.get("max_request_body_size") == "never"
    scrub = captured.get("before_send")
    assert callable(scrub)

    event = {
        "request": {
            "url": "https://x/ingest/rows",
            "data": '{"rows": ["secret-row"]}',
            "headers": {"Authorization": f"Bearer {TOKEN}", "Accept": "*/*"},
        }
    }
    out = scrub(event, None)
    assert "data" not in out["request"]  # /ingest/* bodies never captured
    assert "Authorization" not in out["request"]["headers"]
    assert out["request"]["headers"]["Accept"] == "*/*"
