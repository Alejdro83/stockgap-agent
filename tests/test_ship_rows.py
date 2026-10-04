"""Task 3 (hybrid plan v3/v4): scripts/ship_rows.py — local->NF row shipper.

Contract under test (plan 2026-10-04-hybrid-collector-sync-v3.md, Task 3):
- defaults resolved against REPO_ROOT (never cwd); config from
  config/ingest.env (gitignored, chmod 600) with real env vars overriding;
  the token is NEVER accepted via argv and never logged.
- self-lock via fcntl.flock on data/ship.lock — the ONLY lock in the system
  (the crontab line has NO flock wrapper); a second instance exits doing
  nothing.
- watermark data/ship_meta.json: atomic tmp + fsync(file) + fsync(dir);
  corrupt/missing = FATAL + explicit --reset (never silently 0); local
  MAX(id) < watermark (DB restored/rebuilt) = loud refusal.
- bounded backfill: SHIP_BACKFILL_HOURS (default 4, band 2-6) per-table ts
  column or explicit SHIP_BACKFILL_FROM_ID; NEVER id 0 by default.
- preflight (GET /ingest/state) ONLY when no valid watermark file exists:
  mixed lineage (NF rows the local DB cannot account for) -> REFUSE
  (precedence: REFUSE wins over any lineage inference — old-collector rows
  can't be distinguished), cutover-runbook message, SHIP_FORCE=1 override;
  same-lineage rule on the --reset/derive path (NF MAX(id) <= local MAX(id))
  -> proceed. Watermark exists -> preflight SKIPPED (crash-resume must not
  false-positive).
- drain loop: per pass, per append table, batches of SHIP_BATCH (default
  500) `WHERE id > watermark ORDER BY id` until drained or the ~50s budget;
  watermark -> batch max id after EACH 2xx (atomic save). Snapshot tables
  (oracle_reputation, oracle_reference, collector_meta): whole-table batches
  per pass, NO watermark.
- HTTP via urllib.request: retries 3x (2/4/8s) on network error/5xx/429;
  413 = FATAL + "halve SHIP_BATCH"; 401/403/422 = FATAL (422 body printed);
  503 "ingest disabled" = clear exit WITHOUT retrying. Summary log line ONLY
  when something shipped or failed. SHIP_DRY_RUN=1 = zero network calls.

The fake HTTP layer dispatches through the REAL stockgap.ingest handlers
(``_apply`` / ``_state``) against a tmp "NF" DB, so every wire payload must
satisfy the endpoint's exact-column-set rule byte for byte — a payload drift
turns into a 422 and a red test here.
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from tests.test_ingest import (  # noqa: E402 — the endpoint's column contract
    COLUMN_SETS, cross_row, meta_row, pred_row, ref_row, rep_row, spread_row,
)

try:  # red-phase scaffolding: every test fails cleanly until the file lands
    from stockgap import ingest
except Exception:  # pragma: no cover
    ingest = None

try:  # red-phase scaffolding
    _spec = importlib.util.spec_from_file_location(
        "ship_rows", REPO_ROOT / "scripts" / "ship_rows.py")
    ship_rows = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ship_rows)
except Exception:  # pragma: no cover — replaced once scripts/ship_rows.py lands
    ship_rows = None


@pytest.fixture(autouse=True)
def _require_shipper():
    if ship_rows is None or ingest is None:
        pytest.fail("scripts/ship_rows.py failed to import (red phase)")


TOKEN = "tok-" + "k" * 28  # exactly 32 chars (endpoint minimum)
URL = "https://nf.example"
APPEND_TABLES = ("spread_ticks", "cross_issuer_ticks", "oracle_predictions")
SNAPSHOT_TABLES = ("oracle_reputation", "oracle_reference", "collector_meta")


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".000Z"


def recent(offset: float = 60.0) -> str:
    return iso(time.time() - offset)


def http_error(code: int, body: bytes = b"", url: str = "https://nf.example/x"):
    """Faithful urllib failure: urlopen RAISES HTTPError for non-2xx."""
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body))


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNF:
    """Wire-level stand-in dispatching through the REAL ingest handlers.

    ``script`` is consumed ONE item per HTTP call, in order: ``None`` means
    real dispatch, an ``(status, body)`` tuple is answered/raised exactly like
    urllib would (non-2xx raises HTTPError), any Exception instance is raised
    (network failures).
    """

    def __init__(self, monkeypatch):
        self.calls = []      # dicts: method, path, url, auth, payload
        self.script = []
        self.on_call = None  # optional hook: on_call(call_dict) after dispatch
        monkeypatch.setattr(urllib.request, "urlopen", self)

    def __call__(self, req, timeout=None):
        body = req.data
        call = {
            "method": req.get_method(),
            "url": req.full_url,
            "path": urllib.parse.urlparse(req.full_url).path,
            "auth": req.headers.get("Authorization") or req.headers.get("authorization"),
            "payload": json.loads(body) if body else None,
        }
        self.calls.append(call)
        item = self.script.pop(0) if self.script else None
        try:
            if item is None:
                out = self._dispatch(req, body)
            elif isinstance(item, Exception):
                raise item
            else:
                status, text = item
                out = self._respond(req, status, text)
        finally:
            if self.on_call is not None:
                self.on_call(call)
        return out

    def _respond(self, req, status, text):
        if isinstance(text, str):
            text = text.encode("utf-8")
        if not (200 <= status < 300):
            raise http_error(status, text, req.full_url)
        return FakeResponse(status, text)

    def _dispatch(self, req, body):
        if req.get_method() == "GET" and req.full_url.endswith("/ingest/state"):
            return self._respond(req, 200, json.dumps(ingest._state()))
        if req.get_method() == "POST" and req.full_url.endswith("/ingest/rows"):
            try:
                return self._respond(req, 200, json.dumps(ingest._apply(body)))
            except ingest.IngestError as exc:
                return self._respond(req, 422, json.dumps({"detail": exc.detail}))
        return self._respond(req, 404, "not found")

    # -- assertion helpers --------------------------------------------------
    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]

    def state_gets(self):
        return [c for c in self.calls if c["path"] == "/ingest/state"]


def mk_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    ingest.ensure_schema(con, include_score=True)
    con.close()


def insert_rows(db_path: Path, table: str, rows) -> None:
    con = sqlite3.connect(str(db_path))
    cols = list(COLUMN_SETS[table])
    qcols = ", ".join(f'"{c}"' for c in cols)
    marks = ", ".join("?" for _ in cols)
    con.executemany(
        f'INSERT INTO "{table}" ({qcols}) VALUES ({marks})',
        [tuple(r[c] for c in cols) for r in rows],
    )
    con.commit()
    con.close()


def rows_in(db_path: Path, table: str) -> list:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')]
    con.close()
    return out


@pytest.fixture
def world(tmp_path, monkeypatch):
    local_db = tmp_path / "local" / "spread_ticks.db"
    nf_db = tmp_path / "nf" / "spread_ticks.db"
    (tmp_path / "local").mkdir()
    (tmp_path / "nf").mkdir()
    mk_db(local_db)
    mk_db(nf_db)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_file = tmp_path / "config" / "ingest.env"  # absent unless a test writes it

    monkeypatch.setattr(ship_rows, "DATA_DIR", data_dir)
    monkeypatch.setattr(ship_rows, "ENV_FILE", env_file)
    monkeypatch.setenv("STOCKGAP_DB", str(local_db))
    monkeypatch.setenv("STOCKGAP_INGEST_URL", URL)
    monkeypatch.setenv("STOCKGAP_INGEST_TOKEN", TOKEN)
    for key in ("SHIP_BATCH", "SHIP_BACKFILL_HOURS", "SHIP_BACKFILL_FROM_ID",
                "SHIP_FORCE", "SHIP_DRY_RUN", "SHIP_BUDGET_S"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ingest, "_db_path", lambda: nf_db)  # the fake server's DB

    delays = []
    monkeypatch.setattr(ship_rows, "_sleep", delays.append)
    fake = FakeNF(monkeypatch)

    def write_wm(**tables):
        wm = {t: 0 for t in APPEND_TABLES}
        wm.update(tables)
        path = data_dir / "ship_meta.json"
        path.write_text(json.dumps({"version": 1, "tables": wm}))

    def read_wm():
        return json.loads((data_dir / "ship_meta.json").read_text())["tables"]

    def seed(table, n, start=1, ts=None):
        ts = ts or recent()
        makers = {"spread_ticks": spread_row, "cross_issuer_ticks": cross_row,
                  "oracle_predictions": pred_row, "oracle_reputation": rep_row}
        rows = []
        for i in range(start, start + n):
            over = {"id": i, "tick_id": i}
            if table in ("spread_ticks", "cross_issuer_ticks"):
                over["ts_utc"] = ts
            elif table == "oracle_predictions":
                over["snapshot_ts"] = ts
            rows.append(makers[table](**over))
        insert_rows(local_db, table, rows)
        return rows

    def seed_snapshots():
        insert_rows(local_db, "oracle_reputation", [rep_row(id=1)])
        insert_rows(local_db, "oracle_reference", [ref_row()])
        insert_rows(local_db, "collector_meta", [meta_row()])

    return SimpleNamespace(
        tmp=tmp_path, local_db=local_db, nf_db=nf_db, data_dir=data_dir,
        env_file=env_file, fake=fake, delays=delays, write_wm=write_wm,
        read_wm=read_wm, seed=seed, seed_snapshots=seed_snapshots,
        nf_rows=lambda table: rows_in(nf_db, table),
        wm_path=data_dir / "ship_meta.json",
    )


# ------------------------- watermark durability ----------------------------

def test_watermark_save_is_atomic_fsync_file_and_dir(world, monkeypatch):
    from stockgap import rotation

    fsyncs = []
    fsync_dirs = []
    real_fsync = os.fsync
    real_fsync_dir = rotation._fsync_dir

    def fsync(fd):
        fsyncs.append(fd)
        return real_fsync(fd)

    def fsync_dir(d):
        fsync_dirs.append(Path(d))
        return real_fsync_dir(d)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(rotation, "_fsync_dir", fsync_dir)

    wm = {t: 0 for t in APPEND_TABLES}
    wm["spread_ticks"] = 42
    ship_rows.save_watermark(world.wm_path, wm)

    assert len(fsyncs) >= 2          # the tmp FILE and the directory fd
    assert fsync_dirs == [world.data_dir]
    assert not list(world.data_dir.glob("*.tmp"))  # tmp never left behind
    saved = json.loads(world.wm_path.read_text())
    assert saved["tables"]["spread_ticks"] == 42


def test_watermark_advances_only_on_2xx(world):
    world.seed("spread_ticks", 1500)
    # call order: state (preflight), batch1 OK (real), batch2 -> 422 (fatal)
    world.fake.script = [None, None, (422, '{"detail":"boom"}')]

    assert ship_rows.main(["--reset"]) == 4  # fatal HTTP semantics

    posts = world.fake.posts()
    assert len(posts) == 2                      # batch2 was REJECTED, no batch3
    assert world.read_wm()["spread_ticks"] == 500  # advanced by batch1's 2xx ONLY
    assert len(world.nf_rows("spread_ticks")) == 500


def test_watermark_corrupt_is_fatal_reset_recovers(world, caplog):
    world.wm_path.write_text("{not json")
    caplog.set_level(logging.INFO)

    rc = ship_rows.main([])
    assert rc != 0
    assert "--reset" in caplog.text          # explicit recovery flag named
    assert world.fake.calls == []            # fatal before any network use

    world.fake.script = []                   # preflight state -> real dispatch
    assert ship_rows.main(["--reset"]) == 0  # fresh, NF empty -> clean start
    assert world.read_wm()["spread_ticks"] == 0


def test_watermark_missing_is_fatal_never_silently_zero(world, caplog):
    caplog.set_level(logging.INFO)
    rc = ship_rows.main([])
    assert rc != 0
    assert "--reset" in caplog.text
    assert world.fake.calls == []
    assert not world.wm_path.exists()        # never silently created at 0


def test_local_max_id_below_watermark_refused(world, caplog):
    # watermark claims ids up to 500 shipped but the local DB only has 10 rows
    # (restored/rebuilt): continuing would silently skip 500 rows.
    world.seed("spread_ticks", 10)
    world.write_wm(spread_ticks=500)
    caplog.set_level(logging.INFO)

    rc = ship_rows.main([])
    assert rc != 0
    assert world.fake.calls == []            # refused before any network use
    assert world.read_wm()["spread_ticks"] == 500  # untouched


# ------------------------------- drain loop --------------------------------

def test_drain_loop_ships_backlog_in_one_pass_watermark_in_steps(world, monkeypatch):
    world.seed("spread_ticks", 1500)
    monkeypatch.setenv("SHIP_BATCH", "500")
    saves = []
    real_save = ship_rows.save_watermark

    def save(path, wm):
        saves.append(dict(wm))
        return real_save(path, wm)

    monkeypatch.setattr(ship_rows, "save_watermark", save)

    assert ship_rows.main(["--reset"]) == 0

    posts = world.fake.posts()
    assert [len(p["payload"]["rows"]) for p in posts] == [500, 500, 500]
    assert [p["payload"]["rows"][0]["id"] for p in posts] == [1, 501, 1001]
    assert [p["payload"]["mode"] for p in posts] == ["append"] * 3
    # watermark -> batch max id after EACH 2xx: advances 500, 1000, 1500 in steps
    assert [s["spread_ticks"] for s in saves[:3]] == [500, 1000, 1500]
    assert world.read_wm()["spread_ticks"] == 1500
    assert len(world.nf_rows("spread_ticks")) == 1500


def test_drain_stops_at_budget_and_resumes_next_pass(world, monkeypatch):
    world.seed("spread_ticks", 1500)
    monkeypatch.setenv("SHIP_BATCH", "500")
    monkeypatch.setenv("SHIP_BUDGET_S", "50")

    clock = [0.0]
    monkeypatch.setattr(ship_rows, "_now", lambda: clock[0])
    world.fake.on_call = lambda call: clock.__setitem__(0, clock[0] + 30.0)  # network latency

    assert ship_rows.main(["--reset"]) == 0
    assert len(world.fake.posts()) == 2           # 3rd batch would blow the budget
    assert world.read_wm()["spread_ticks"] == 1000

    world.fake.calls.clear()
    clock[0] = 0.0
    assert ship_rows.main([]) == 0
    assert [p["payload"]["rows"][0]["id"] for p in world.fake.posts()] == [1001]
    assert world.read_wm()["spread_ticks"] == 1500


def test_snapshot_tables_whole_table_per_pass_no_watermark(world):
    world.seed_snapshots()
    world.write_wm()

    assert ship_rows.main([]) == 0
    assert ship_rows.main([]) == 0  # second pass re-ships (heals) snapshots

    posts = world.fake.posts()
    assert [(p["payload"]["table"], p["payload"]["mode"]) for p in posts] == [
        ("oracle_reputation", "snapshot"),
        ("oracle_reference", "snapshot"),
        ("collector_meta", "snapshot"),
    ] * 2
    for p in posts:
        assert len(p["payload"]["rows"]) == 1
    wm = world.read_wm()
    assert set(wm) == set(APPEND_TABLES)  # NO snapshot keys, NO watermark movement
    assert wm == {t: 0 for t in APPEND_TABLES}


def test_wire_payloads_match_endpoint_column_sets(world):
    world.seed("spread_ticks", 1)
    world.seed("cross_issuer_ticks", 1)
    world.seed("oracle_predictions", 1)
    world.seed_snapshots()

    assert ship_rows.main(["--reset"]) == 0

    posts = world.fake.posts()
    assert {p["payload"]["table"] for p in posts} == set(COLUMN_SETS)
    for p in posts:
        table = p["payload"]["table"]
        mode = p["payload"]["mode"]
        assert mode == ("append" if table in APPEND_TABLES else "snapshot")
        for row in p["payload"]["rows"]:
            # exact column set — the endpoint 422s on anything else [R2]
            assert set(row) == set(COLUMN_SETS[table])


# -------------------------------- backfill ---------------------------------

def test_backfill_bounded_by_hours_window_never_id_zero_by_default(world):
    old_ts = iso(time.time() - 30 * 86400)  # far outside the 4h default window
    world.seed("spread_ticks", 5, start=1, ts=old_ts)
    world.seed("spread_ticks", 5, start=6, ts=recent())

    assert ship_rows.main(["--reset"]) == 0

    posts = world.fake.posts()
    ids = [r["id"] for p in posts for r in p["payload"]["rows"]]
    assert ids == [6, 7, 8, 9, 10]            # window only, NOT id 0 by default
    assert world.read_wm()["spread_ticks"] == 10


def test_backfill_all_rows_out_of_window_ships_nothing(world):
    old_ts = iso(time.time() - 30 * 86400)
    world.seed("spread_ticks", 5, ts=old_ts)

    assert ship_rows.main(["--reset"]) == 0

    assert world.fake.posts() == []           # bounded: ancient rows never reship
    assert world.read_wm()["spread_ticks"] == 5  # watermark = local MAX(id)


def test_backfill_from_id_overrides_window(world, monkeypatch):
    old_ts = iso(time.time() - 30 * 86400)
    world.seed("spread_ticks", 10, ts=old_ts)
    monkeypatch.setenv("SHIP_BACKFILL_FROM_ID", "8")  # explicit: ship ids >= 8

    assert ship_rows.main(["--reset"]) == 0

    ids = [r["id"] for p in world.fake.posts() for r in p["payload"]["rows"]]
    assert ids == [8, 9, 10]


# ------------------------------- HTTP policy -------------------------------

@pytest.mark.parametrize("first", [
    urllib.error.URLError("netdown"),
    (500, b"db locked"),
    (429, b"slow down"),
])
def test_retry_then_success(world, first):
    world.seed("spread_ticks", 3)
    world.write_wm()
    world.fake.script = [first, None]

    assert ship_rows.main([]) == 0

    assert len(world.fake.calls) == 2         # one failure, one retry, then 2xx
    assert world.delays == [2]                # backoff schedule starts 2/4/8
    assert world.read_wm()["spread_ticks"] == 3


def test_retries_exhausted_after_three_retries(world, caplog):
    world.seed("spread_ticks", 3)
    world.write_wm()
    world.fake.script = [(503, b"upstream down")] * 4  # NOT 'ingest disabled'
    caplog.set_level(logging.INFO)

    rc = ship_rows.main([])

    assert rc == 4
    assert len(world.fake.calls) == 4         # 1 try + 3 retries
    assert world.delays == [2, 4, 8]
    assert world.read_wm()["spread_ticks"] == 0  # never advanced without a 2xx
    assert "ship pass" in caplog.text         # failure summary line


def test_fatal_401_and_403_no_retry(world, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    for code in (401, 403):
        world.seed("spread_ticks", 1)
        world.write_wm()
        world.fake.calls.clear()
        world.fake.script = [(code, b"unauthorized")]
        world.delays.clear()

        rc = ship_rows.main([])

        assert rc == 4, code
        assert len(world.fake.calls) == 1, code   # FATAL: no retry storm
        assert world.delays == [], code
        assert world.read_wm()["spread_ticks"] == 0
        # wipe the row so the next iteration starts clean
        con = sqlite3.connect(str(world.local_db))
        con.execute("DELETE FROM spread_ticks")
        con.commit()
        con.close()


def test_fatal_422_body_printed(world, caplog):
    world.seed("spread_ticks", 1)
    world.write_wm()
    world.fake.script = [
        (422, '{"detail":"unknown column(s) for spread_ticks: [\'x\'] — '
              'NF schema older than local \u2014 deploy first"}')]
    caplog.set_level(logging.INFO)

    rc = ship_rows.main([])

    assert rc == 4
    assert len(world.fake.calls) == 1
    assert "NF schema older than local" in caplog.text  # body PRINTED [R2b]


def test_fatal_413_hint_halve_ship_batch(world, monkeypatch, caplog):
    world.seed("spread_ticks", 1)
    world.write_wm()
    monkeypatch.setenv("SHIP_BATCH", "500")
    world.fake.script = [(413, b"payload too large")]
    caplog.set_level(logging.INFO)

    rc = ship_rows.main([])

    assert rc == 4
    assert len(world.fake.calls) == 1
    assert "halve SHIP_BATCH" in caplog.text


def test_503_ingest_disabled_clear_exit_without_retry(world, caplog):
    world.seed("spread_ticks", 1)
    world.write_wm()
    world.fake.script = [(503, b"ingest disabled")]
    caplog.set_level(logging.INFO)

    rc = ship_rows.main([])

    assert rc != 0                       # nothing shipped — a failure, loudly
    assert len(world.fake.calls) == 1    # NO retry storm [R10]
    assert world.delays == []
    assert "ingest disabled" in caplog.text


# -------------------------------- preflight --------------------------------

def _state_doc(**max_ids):
    tables = {}
    for name in COLUMN_SETS:
        tables[name] = {"max_id": max_ids.get(name, 0), "max_tick_id": None}
    return json.dumps({"tables": tables})


def test_preflight_refuses_mixed_lineage_only_without_watermark(world, caplog):
    caplog.set_level(logging.INFO)
    world.seed("spread_ticks", 10)
    mixed = (200, _state_doc(spread_ticks=100))  # NF rows the local DB cannot account for

    # (a) no valid watermark -> preflight RUNS -> REFUSE (NF max_id > local MAX)
    world.fake.script = [mixed]
    rc = ship_rows.main(["--reset"])
    assert rc != 0
    assert len(world.fake.state_gets()) == 1     # GET /ingest/state ran
    assert world.fake.posts() == []              # nothing shipped
    assert not world.wm_path.exists()
    assert "spread_ticks.db-wal" in caplog.text  # message: archive ALL THREE files
    assert "runbook" in caplog.text.lower()

    # (b) valid watermark EXISTS -> preflight SKIPPED -> same NF state, no refusal
    world.fake.calls.clear()
    world.fake.script = [mixed]                  # would 100% refuse if consulted
    world.write_wm(spread_ticks=10)
    assert ship_rows.main([]) == 0
    assert world.fake.state_gets() == []         # preflight SKIPPED — no false positive
    assert world.fake.posts() == []


def test_preflight_mixed_lineage_ship_force_override(world, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    world.seed("spread_ticks", 2)
    monkeypatch.setenv("SHIP_FORCE", "1")
    world.fake.script = [(200, _state_doc(spread_ticks=100))]

    assert ship_rows.main(["--reset"]) == 0
    assert [r["id"] for p in world.fake.posts() for r in p["payload"]["rows"]] == [1, 2]
    assert "SHIP_FORCE" in caplog.text           # loud override warning


def test_crash_before_watermark_write_rerun_proceeds_and_reposts_idempotently(
        world, monkeypatch):
    # Pass 1 (--reset): batch1 POSTs and LANDS on NF, then the host dies
    # BEFORE the watermark write — no ship_meta.json exists at all.
    world.seed("spread_ticks", 1500)
    monkeypatch.setenv("SHIP_BATCH", "500")

    real_post = ship_rows._post_rows

    def crash_after_first_post(cfg, table, mode, rows):
        real_post(cfg, table, mode, rows)
        if table == "spread_ticks" and rows[0]["id"] == 1:
            raise KeyboardInterrupt  # kill: no finally/except handler runs

    monkeypatch.setattr(ship_rows, "_post_rows", crash_after_first_post)
    with pytest.raises(KeyboardInterrupt):
        ship_rows.main(["--reset"])
    assert len(world.nf_rows("spread_ticks")) == 500   # the batch landed
    assert not world.wm_path.exists()                 # crash-before-watermark-write

    # Re-run (--reset, the documented recovery): preflight SEES the rows but
    # the same-lineage rule (NF MAX(id) 500 <= local MAX(id) 1500) must NOT
    # false-positive — proceed and re-POST idempotently.
    monkeypatch.setattr(ship_rows, "_post_rows", real_post)
    assert ship_rows.main(["--reset"]) == 0

    posts = world.fake.posts()
    assert [r["id"] for p in posts[:2] for r in p["payload"]["rows"]] == \
        list(range(1, 501)) + list(range(1, 501))     # the SAME batch re-POSTed
    assert len(world.nf_rows("spread_ticks")) == 1500  # INSERT OR IGNORE: idempotent
    assert world.read_wm()["spread_ticks"] == 1500


def test_crash_resume_skips_preflight_and_reposts_idempotently(world, monkeypatch):
    # Pass 1: batch1 saved to the watermark, batch2 lands on NF and the host
    # dies before batch2's watermark write.
    world.seed("spread_ticks", 1500)
    monkeypatch.setenv("SHIP_BATCH", "500")
    real_post = ship_rows._post_rows
    seen = []

    def crash_after_second_post(cfg, table, mode, rows):
        real_post(cfg, table, mode, rows)
        seen.append(rows[0]["id"])
        if len(seen) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(ship_rows, "_post_rows", crash_after_second_post)
    with pytest.raises(KeyboardInterrupt):
        ship_rows.main(["--reset"])
    assert world.read_wm()["spread_ticks"] == 500     # batch1's 2xx is durable

    # Re-run: valid watermark exists -> preflight SKIPPED (crash-resume must
    # not false-positive on this lineage's own rows) -> re-POST idempotently.
    monkeypatch.setattr(ship_rows, "_post_rows", real_post)
    world.fake.calls.clear()
    assert ship_rows.main([]) == 0

    assert world.fake.state_gets() == []              # preflight SKIPPED
    reposted = [r["id"] for p in world.fake.posts() for r in p["payload"]["rows"]]
    assert reposted[:500] == list(range(501, 1001))   # crashed batch re-POSTed
    assert len(world.nf_rows("spread_ticks")) == 1500  # idempotent replay
    assert world.read_wm()["spread_ticks"] == 1500


# ------------------------------ one-shot / cron -----------------------------

def test_dry_run_makes_zero_network_calls(world, monkeypatch, caplog):
    world.seed("spread_ticks", 10)
    world.seed_snapshots()
    monkeypatch.setenv("SHIP_DRY_RUN", "1")
    caplog.set_level(logging.INFO)

    assert ship_rows.main(["--reset"]) == 0

    assert world.fake.calls == []             # zero network calls AT ALL
    assert not world.wm_path.exists()         # no state change
    assert len(world.nf_rows("spread_ticks")) == 0
    assert "DRY-RUN" in caplog.text           # reports what it WOULD ship


def test_second_instance_under_flock_exits_doing_nothing(world, caplog):
    import fcntl

    world.seed("spread_ticks", 5)
    lock_fd = os.open(str(world.data_dir / "ship.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # pass 1 holds the lock
    caplog.set_level(logging.INFO)
    try:
        rc = ship_rows.main(["--reset"])
    finally:
        os.close(lock_fd)

    assert rc == 0                        # normal no-op, not an error
    assert world.fake.calls == []         # doing nothing: no network
    assert not world.wm_path.exists()     # doing nothing: no state change
    assert "ship pass" not in caplog.text  # no summary line either


def test_summary_log_only_when_shipped_or_failed(world, caplog):
    caplog.set_level(logging.INFO)
    world.write_wm()

    assert ship_rows.main([]) == 0                       # idle pass
    assert not any("ship pass" in r.getMessage() for r in caplog.records)

    world.seed("spread_ticks", 2)
    assert ship_rows.main([]) == 0                       # active pass
    assert any("ship pass" in r.getMessage() for r in caplog.records)


# --------------------------- config / hygiene -------------------------------

def test_config_env_file_with_env_override_and_cwd_independence(world, monkeypatch):
    trap = world.tmp / "trapcwd"
    trap.mkdir()
    (trap / "config").mkdir()
    (trap / "config" / "ingest.env").write_text(  # CWD trap: must NEVER be read
        "STOCKGAP_INGEST_URL=https://trap.example\n"
        "STOCKGAP_INGEST_TOKEN=" + "trap" * 8 + "\n")
    world.env_file.parent.mkdir(exist_ok=True)
    world.env_file.write_text(
        "STOCKGAP_INGEST_URL=https://file.example\n"
        "STOCKGAP_INGEST_TOKEN=" + "fromfile" * 4 + "\n")
    monkeypatch.chdir(trap)

    # real env vars override the file
    world.seed("spread_ticks", 1)
    assert ship_rows.main(["--reset"]) == 0
    calls = world.fake.calls
    assert calls                                   # phase 1 really talked HTTP
    assert all(c["url"].startswith(URL) for c in calls)          # env URL wins
    assert all(c["auth"] == "Bearer " + TOKEN for c in calls)    # env token wins

    # env var removed -> the FILE config is used (still not the CWD trap)
    world.fake.calls.clear()
    world.seed("spread_ticks", 1, start=2)
    monkeypatch.delenv("STOCKGAP_INGEST_URL")
    monkeypatch.delenv("STOCKGAP_INGEST_TOKEN")
    monkeypatch.setenv("STOCKGAP_DB", str(world.local_db))
    assert ship_rows.main([]) == 0
    calls = world.fake.calls
    assert calls                                   # phase 2 really talked HTTP
    assert all(c["url"].startswith("https://file.example") for c in calls)
    assert all(c["auth"] == "Bearer " + "fromfile" * 4 for c in calls)
    assert not any("trap" in (c["auth"] or "") for c in calls)


def test_token_never_via_argv_and_never_logged(world, caplog, capsys):
    caplog.set_level(logging.DEBUG)

    rc = ship_rows.main(["--token=" + "sekret" * 4])

    assert rc != 0                       # argv token is never accepted
    out, err = capsys.readouterr()       # and never echoed anywhere
    assert "sekret" not in out + err + caplog.text

    # a normal run never logs the configured token either
    caplog.clear()
    world.seed("spread_ticks", 1)
    assert ship_rows.main(["--reset"]) == 0
    out, err = capsys.readouterr()
    assert TOKEN not in out + err + caplog.text


def test_defaults_resolve_against_repo_root_never_cwd():
    assert ship_rows.REPO_ROOT == REPO_ROOT
    assert ship_rows.DATA_DIR == REPO_ROOT / "data"
    assert ship_rows.ENV_FILE == REPO_ROOT / "config" / "ingest.env"
    assert ship_rows.DEFAULT_DB == REPO_ROOT / "data" / "spread_ticks.db"


def test_docstring_states_ordering_assumption():
    doc = ship_rows.__doc__ or ""
    assert "single writer" in doc           # ordering assumption [R5]
    assert "ORDER BY id" in doc
    assert "watermark" in doc
    assert "flock" in doc                   # the ONLY lock in the system [N6]


def test_ship_log_covered_by_rotate_data_patterns(tmp_path, monkeypatch):
    # the crontab line appends to data/ship.log (Task 7) — rotate_data.py's
    # data/*.log patterns must pick it up for rotation/pruning [R10].
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "rotate_data", REPO_ROOT / "scripts" / "rotate_data.py")
    rotate_data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rotate_data)

    from stockgap import rotation

    (tmp_path / "ship.log").write_text("x")
    (tmp_path / "logs").mkdir()
    live = rotate_data._live_streams(tmp_path)
    assert (tmp_path / "ship.log") in live   # matched by the data/*.log glob
    assert not rotation.is_archive_name("ship.log")  # never mistaken for an archive
