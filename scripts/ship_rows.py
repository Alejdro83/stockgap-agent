#!/usr/bin/env python3
"""Ship locally-collected rows to the NF ingest endpoint (hybrid plan Task 3).

The collector lives on THIS host (Binance Web3 only works from the residential
IP); the judge-facing dashboard lives on Northflank. This one-shot, cron-driven
script drains the local ``data/spread_ticks.db`` and POSTs row batches to
``POST /ingest/rows`` (plan 2026-10-04-hybrid-collector-sync v3/v4).

ORDERING ASSUMPTION [R5]: the append drain ships ``WHERE id > watermark
ORDER BY id`` — this is safe ONLY with a single writer per table (the
collector writes spread_ticks/cross_issuer_ticks, the flock'd oracle writes
oracle_predictions). Never add a second writer to a shipped table: two
interleaved writers can commit a higher id before a lower one and the
watermark would skip the straggler forever.

Sync model:
- ``append`` tables (spread_ticks, cross_issuer_ticks, oracle_predictions):
  id-watermarked, ``INSERT OR IGNORE`` on the far side — replays are
  idempotent and history is unrewritable.
- ``snapshot`` tables (oracle_reputation, oracle_reference, collector_meta):
  whole-table batches per pass, NO watermark (upsert-by-PK far side — the
  only tables that ever REPLACE; this heals ``oracle_reputation.feedback_ref``
  which is NULL at INSERT and UPDATEd later).

Contract highlights (each is tested in tests/test_ship_rows.py):
- All defaults resolve against REPO_ROOT (never cwd — cron has no env and
  cwd is $HOME). Config comes from ``config/ingest.env`` (chmod 600,
  gitignored) holding STOCKGAP_INGEST_URL + STOCKGAP_INGEST_TOKEN; real env
  vars override. The token is NEVER accepted via argv (visible in ``ps`` and
  ``crontab -l``) and never logged.
- SELF-LOCK: ``fcntl.flock`` (non-blocking) on ``data/ship.lock`` — this is
  the ONLY lock in the system; the crontab line (Task 7) has NO ``flock``
  wrapper (a wrapper would hold the lock and make every pass exit doing
  nothing). A second instance exits 0 doing nothing. The drain loop runs under
  a per-pass wall-clock budget (~50s, SHIP_BUDGET_S) so passes end before the
  next cron tick.
- WATERMARK ``data/ship_meta.json``: atomic tmp + fsync of the file AND the
  directory (``rotation._fsync_dir``). It is BORN AT PASS START — before
  the FIRST POST of any pass that derives it (fresh start / ``--reset``)
  the derived starting floor is written atomically — and from then on it
  advances to the batch max id after EACH 2xx and never otherwise.
  Consequence: a crash AFTER any POST always leaves a valid watermark file
  behind — a MISSING watermark file proves no POST ever happened. Corrupt
  watermark = FATAL non-zero exit naming the explicit ``--reset`` flag
  (never silently 0 — that would reship millions of rows); missing
  watermark = the strict fresh-start preflight below. Sanity: local
  ``MAX(id)`` < watermark (DB restored/rebuilt) -> loud non-zero exit.
- BACKFILL bounded and SMALL: ``SHIP_BACKFILL_HOURS`` (default 4, clamped to
  the 2-6h band) per-table ts column (ts_utc for spread_ticks/
  cross_issuer_ticks, snapshot_ts for oracle_predictions) or an explicit
  ``SHIP_BACKFILL_FROM_ID`` — NEVER id 0 by default. This derived floor is
  exactly what BIRTHS the watermark file at pass start.
- PREFLIGHT [R1][v4 trigger]: ``GET /ingest/state`` is called ONLY when NO
  valid watermark file exists (fresh start / after ``--reset``). The
  watermark is born at pass start, so a missing file proves no POST ever
  happened from this host — rows already on NF are FOREIGN.
  * PLAIN fresh start (no ``--reset``) is STRICT: ANY non-empty append
    table on NF (MAX(id) > 0) -> REFUSE — NO lineage inference whatsoever,
    no matter how safe the maxima look. Old-collector rows with aligned
    maxima are INDISTINGUISHABLE from ours (a mixed lineage can hide at
    identical maxima; shipping would collide by id and ``INSERT OR IGNORE``
    would silently drop rows) — REFUSE wins over any lineage inference; no
    content/tick_id/timestamp sniffing is attempted. The message points at
    the cutover runbook (archive the NF DB — all three files) and names
    ``--reset`` and ``SHIP_FORCE=1`` (the explicit override).
  * SAME-LINEAGE INFERENCE ONLY on the explicit ``--reset`` path: NF
    MAX(id) <= local MAX(id) on every non-empty append table -> proceed
    with a LOUD warning (indistinguishability caveat + runbook reference);
    NF MAX(id) > local MAX(id) -> REFUSE, and this REFUSE OVERRIDES
    ``SHIP_FORCE=1``. Plain re-runs never infer.
  When a valid watermark EXISTS the preflight is SKIPPED entirely
  (crash-resume must not false-positive on this lineage's own rows).
- HTTP via ``urllib.request`` (stdlib only): retries 3x (2/4/8s) on network
  error/5xx/429; 413 = FATAL with the hint "halve SHIP_BATCH"; 401/403/422 =
  FATAL, the 422 body is printed (surfaces "NF schema older than local —
  deploy first"); 503 with body "ingest disabled" = clear exit WITHOUT
  retrying. A summary line is logged ONLY when something shipped or failed.
  ``SHIP_DRY_RUN=1`` = no network calls at all (reports what it would ship,
  writes no watermark). One-shot per invocation (cron-driven).
"""
from __future__ import annotations

import errno
import fcntl
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
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stockgap import db, rotation  # noqa: E402 — WAL helpers + _fsync_dir

log = logging.getLogger("ship_rows")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
ENV_FILE = REPO_ROOT / "config" / "ingest.env"
DEFAULT_DB = REPO_ROOT / "data" / "spread_ticks.db"

# The ONLY lock in the system — see the module docstring [N6].
LOCK_NAME = "ship.lock"
WATERMARK_NAME = "ship_meta.json"
WATERMARK_VERSION = 1

APPEND_TABLES = ("spread_ticks", "cross_issuer_ticks", "oracle_predictions")
SNAPSHOT_TABLES = ("oracle_reputation", "oracle_reference", "collector_meta")
TS_COLUMN = {  # per-table ts column for the bounded backfill window [R5]
    "spread_ticks": "ts_utc",
    "cross_issuer_ticks": "ts_utc",
    "oracle_predictions": "snapshot_ts",
}
SNAPSHOT_PK = {
    "oracle_reputation": "id",
    "oracle_reference": "underlying_ticker",
    "collector_meta": "key",
}

DEFAULT_BATCH = 500
MAX_BATCH = 1000           # endpoint MAX_ROWS — anything larger 422s
DEFAULT_BACKFILL_HOURS = 4.0
BACKFILL_HOURS_BAND = (2.0, 6.0)
DEFAULT_BUDGET_S = 50.0    # per-pass drain budget: end before the next cron tick
REQUEST_TIMEOUT_S = 20.0
RETRY_DELAYS = (2, 4, 8)   # 3 retries: 1 initial try + 3 backoffs [R10]

# KNOWN config keys: config/ingest.env values, overridden by real env vars.
KNOWN_KEYS = (
    "STOCKGAP_INGEST_URL", "STOCKGAP_INGEST_TOKEN", "STOCKGAP_DB",
    "SHIP_BATCH", "SHIP_BACKFILL_HOURS", "SHIP_BACKFILL_FROM_ID",
    "SHIP_FORCE", "SHIP_DRY_RUN", "SHIP_BUDGET_S",
)

# Exit codes: 0 ok/no-op, 1 watermark+sanity, 2 config/argv, 3 preflight
# refuse, 4 fatal HTTP / retries exhausted, 5 NF "ingest disabled".
EXIT_OK = 0
EXIT_WATERMARK = 1
EXIT_CONFIG = 2
EXIT_PREFLIGHT = 3
EXIT_HTTP = 4
EXIT_INGEST_DISABLED = 5


class ShipError(Exception):
    """Fatal pass failure: logged once, exit with ``code``."""

    def __init__(self, message: str, code: int = EXIT_HTTP) -> None:
        super().__init__(message)
        self.code = code


class _NetworkError(Exception):
    """Transport-level failure (retryable): DNS, refused, timeout. [R10]"""


def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ------------------------------ config -------------------------------------

class Config:
    def __init__(self, url: str, token: str, db_path: Path, batch: int,
                 backfill_hours: float, backfill_from_id: Optional[int],
                 force: bool, dry_run: bool, budget_s: float) -> None:
        self.url = url
        self.token = token
        self.db = db_path
        self.batch = batch
        self.backfill_hours = backfill_hours
        self.backfill_from_id = backfill_from_id
        self.force = force
        self.dry_run = dry_run
        self.budget_s = budget_s


def _parse_env_file(path: Path) -> Dict[str, str]:
    vals: Dict[str, str] = {}
    if not path.exists():
        return vals
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        vals[key.strip()] = value.strip().strip('"').strip("'")
    return vals


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_config(env_file: Path) -> Config:
    """config/ingest.env (KEY=VALUE) with real env vars overriding. [R7]

    The token value is NEVER echoed in errors or logs.
    """
    vals = _parse_env_file(env_file)
    for key in KNOWN_KEYS:  # real env vars win over the file
        if key in os.environ:
            vals[key] = os.environ[key]

    url = vals.get("STOCKGAP_INGEST_URL", "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ShipError(
            "STOCKGAP_INGEST_URL missing or not http(s) — set it in "
            "config/ingest.env (chmod 600) or the environment", EXIT_CONFIG)
    token = vals.get("STOCKGAP_INGEST_TOKEN", "")
    if len(token) < 32:
        raise ShipError(
            "STOCKGAP_INGEST_TOKEN missing or shorter than 32 chars — set it "
            "in config/ingest.env (chmod 600) or the environment; NEVER via "
            "argv (visible in ps/crontab -l)", EXIT_CONFIG)

    def as_int(key: str, default: Optional[int]) -> Optional[int]:
        raw = vals.get(key, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ShipError(f"{key} must be an integer, got {len(raw)} chars "
                            "of non-integer value", EXIT_CONFIG)

    def as_float(key: str, default: float) -> float:
        raw = vals.get(key, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            raise ShipError(f"{key} must be a number", EXIT_CONFIG)

    batch = max(1, min(MAX_BATCH, int(as_int("SHIP_BATCH", DEFAULT_BATCH) or DEFAULT_BATCH)))
    hours = as_float("SHIP_BACKFILL_HOURS", DEFAULT_BACKFILL_HOURS)
    lo, hi = BACKFILL_HOURS_BAND
    if not (lo <= hours <= hi):
        log.warning("SHIP_BACKFILL_HOURS=%.2f outside the %g-%gh band — clamped",
                    hours, lo, hi)
        hours = max(lo, min(hi, hours))
    from_id = as_int("SHIP_BACKFILL_FROM_ID", None)
    if from_id is not None and from_id < 1:
        raise ShipError("SHIP_BACKFILL_FROM_ID must be >= 1 (id 0 would "
                        "reship everything)", EXIT_CONFIG)
    return Config(
        url=url,
        token=token,
        db_path=Path(vals.get("STOCKGAP_DB", "").strip() or DEFAULT_DB),
        batch=batch,
        backfill_hours=hours,
        backfill_from_id=from_id,
        force=_truthy(vals.get("SHIP_FORCE", "")),
        dry_run=_truthy(vals.get("SHIP_DRY_RUN", "")),
        budget_s=max(1.0, as_float("SHIP_BUDGET_S", DEFAULT_BUDGET_S)),
    )


def _parse_args(argv: Sequence[str]) -> bool:
    """Only ``--reset`` exists. ANY other argv is refused WITHOUT echoing it —
    a token on argv would be visible in ``ps`` and must never land in the log.
    """
    reset = False
    for arg in argv:
        if arg == "--reset":
            reset = True
        else:
            raise ShipError(
                "unrecognized argument — only --reset is accepted; secrets are "
                "never passed via argv (use config/ingest.env, chmod 600)",
                EXIT_CONFIG)
    return reset


# ----------------------------- self-lock -----------------------------------

def _self_lock(path: Path) -> Optional[int]:
    """``fcntl.flock`` LOCK_EX|LOCK_NB — the ONLY lock in the system [N6].

    Returns the held fd, or None when another pass holds the lock (that pass
    owns shipping; this one must exit doing nothing).
    """
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
    return fd


# ----------------------------- watermark -----------------------------------

def read_watermark(path: Path) -> Optional[Dict[str, int]]:
    """Strict load: None when missing, ShipError when corrupt. NEVER guess 0."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ShipError(f"watermark {path} is corrupt ({exc}) — refusing to "
                        "guess (never silently 0). Pass --reset after "
                        "verifying lineage, or follow the cutover runbook",
                        EXIT_WATERMARK)
    tables = doc.get("tables") if isinstance(doc, dict) else None
    if (doc.get("version") != WATERMARK_VERSION if isinstance(doc, dict) else True) \
            or not isinstance(tables, dict) \
            or set(tables) != set(APPEND_TABLES) \
            or any(type(v) is not int or v < 0 for v in tables.values()):
        raise ShipError(f"watermark {path} is corrupt (bad shape) — refusing "
                        "to guess (never silently 0). Pass --reset after "
                        "verifying lineage, or follow the cutover runbook",
                        EXIT_WATERMARK)
    return dict(tables)


def _write_watermark_file(path: Path, wm: Dict[str, int]) -> None:
    """Atomic + durable file write: tmp -> fsync(file) -> replace -> fsync(dir).

    Shared mechanics for BOTH watermark events [R5]: the pass-start BIRTH
    (derived floor, before the FIRST POST) and the per-2xx advance recorded
    by ``save_watermark`` below.
    """
    tmp = path.with_name(path.name + ".tmp")
    doc = json.dumps({"version": WATERMARK_VERSION, "tables": wm}, sort_keys=True)
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(doc)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    rotation._fsync_dir(path.parent)  # the rename itself must be durable


def save_watermark(path: Path, wm: Dict[str, int]) -> None:
    """Record an ADVANCE: called after EACH 2xx and never otherwise. [R5]"""
    _write_watermark_file(path, wm)


# ------------------------------ HTTP ---------------------------------------

def _request(cfg: Config, method: str, url: str,
             payload: Optional[bytes] = None):
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", "Bearer " + cfg.token)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()   # urllib raises for non-2xx
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _NetworkError(str(exc) or type(exc).__name__)


def _request_retried(cfg: Config, method: str, url: str,
                     payload: Optional[bytes] = None):
    """3 retries (2/4/8s) on network error/5xx/429; hard-fatal on 413/4xx. [R10]"""
    what = f"{method} {urllib.parse.urlparse(url).path}"
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            status, body = _request(cfg, method, url, payload)
        except _NetworkError as exc:
            if attempt < len(RETRY_DELAYS):
                _sleep(RETRY_DELAYS[attempt])
                continue
            raise ShipError(f"{what}: network failure after "
                            f"{len(RETRY_DELAYS) + 1} attempts: {exc}", EXIT_HTTP)
        text = body.decode("utf-8", "replace")
        if 200 <= status < 300:
            return status, text
        if status == 413:
            raise ShipError(f"{what}: 413 payload too large — halve SHIP_BATCH "
                            f"(currently {cfg.batch})", EXIT_HTTP)
        if status in (401, 403):
            raise ShipError(f"{what}: {status} — auth rejected; check "
                            "STOCKGAP_INGEST_TOKEN (runbook: a wrong token must "
                            "give 401, 503 means the token is not linked on NF)",
                            EXIT_HTTP)
        if status == 422:
            raise ShipError(f"{what}: 422 — body: {text}", EXIT_HTTP)  # printed [R2b]
        if status == 503 and "ingest disabled" in text:
            raise ShipError(f"{what}: 503 'ingest disabled' — "
                            "STOCKGAP_INGEST_TOKEN is not linked on NF; fix "
                            "before anything else (runbook step 5)",
                            EXIT_INGEST_DISABLED)
        if status == 429 or status >= 500:
            if attempt < len(RETRY_DELAYS):
                _sleep(RETRY_DELAYS[attempt])
                continue
            raise ShipError(f"{what}: HTTP {status} after "
                            f"{len(RETRY_DELAYS) + 1} attempts — body: {text}",
                            EXIT_HTTP)
        raise ShipError(f"{what}: unexpected HTTP {status} — body: {text}",
                        EXIT_HTTP)


def _get_state(cfg: Config) -> dict:
    """GET /ingest/state for the cutover preflight (callers gate the trigger)."""
    _, text = _request_retried(cfg, "GET", cfg.url + "/ingest/state")
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        # Precedence: REFUSE wins over any lineage inference — an
        # indeterminate state view cannot clear a non-empty NF.
        raise ShipError(_REFUSE_HEAD + " (GET /ingest/state returned an "
                        "unparseable view)", EXIT_PREFLIGHT)
    return doc


def _post_rows(cfg: Config, table: str, mode: str,
               rows: Sequence[Dict[str, Any]]) -> None:
    payload = json.dumps({"table": table, "mode": mode, "rows": list(rows)}).encode("utf-8")
    _request_retried(cfg, "POST", cfg.url + "/ingest/rows", payload)


# ----------------------------- preflight -----------------------------------

_REFUSE_HEAD = (
    "REFUSING to ship: the NF DB already holds rows and no valid watermark "
    "exists to prove which are this lineage's — old-collector rows cannot be "
    "distinguished (REFUSE wins over any lineage inference)")

_REFUSE_TAIL_BASE = (
    ". Follow the cutover runbook: stop the service and archive the NF DB — "
    "ALL THREE files (spread_ticks.db, spread_ticks.db-wal, spread_ticks.db-"
    "shm) — then re-run with --reset")

_REFUSE_TAIL_STRICT = _REFUSE_TAIL_BASE + (
    ". A PLAIN fresh start never infers lineage (any non-empty append table "
    "-> REFUSE). If you have verified the rows already on NF are safe to "
    "ship alongside this lineage's, override with SHIP_FORCE=1")

_REFUSE_TAIL_RESET = _REFUSE_TAIL_BASE + (
    ". This --reset-path REFUSE overrides SHIP_FORCE=1 (NF MAX(id) > local "
    "MAX(id): rows this host cannot account for)")

_REFUSE_TAIL_VIEW = _REFUSE_TAIL_BASE + (
    " (the /ingest/state view is unusable — nothing is inferred or "
    "overridden against an indeterminate view)")


def _preflight(state: dict, local_max: Dict[str, int], force: bool,
               reset: bool) -> None:
    """Cutover preflight [R1][v4]: ONLY when no valid watermark file exists.

    The watermark is BORN at pass start, so a missing file proves no POST
    ever happened from this host — rows already on NF are FOREIGN.

    PLAIN fresh start (no ``--reset``) is STRICT: ANY non-empty append table
    (NF MAX(id) > 0) -> REFUSE with NO lineage inference whatsoever
    (``SHIP_FORCE=1`` is the explicit override). Same-lineage inference
    lives ONLY on the explicit ``--reset`` path: NF MAX(id) <= local MAX(id)
    on every non-empty append table -> proceed with a LOUD warning
    (indistinguishability caveat + runbook); NF MAX(id) > local MAX(id) ->
    REFUSE, and that REFUSE overrides ``SHIP_FORCE=1``.
    """
    tables = state.get("tables")
    if not isinstance(tables, dict):
        raise ShipError(_REFUSE_HEAD + " (bad /ingest/state shape)"
                        + _REFUSE_TAIL_VIEW, EXIT_PREFLIGHT)
    dirty: List[tuple] = []
    for table in APPEND_TABLES:
        entry = tables.get(table)
        nf_max = entry.get("max_id") if isinstance(entry, dict) else None
        if type(nf_max) is not int or nf_max < 0:
            raise ShipError(_REFUSE_HEAD + f" (bad /ingest/state for {table})"
                            + _REFUSE_TAIL_VIEW, EXIT_PREFLIGHT)
        if nf_max > 0:
            dirty.append((table, nf_max))
    if not dirty:
        return  # NF is clean: nothing to protect
    detail = ", ".join(f"{t}: NF MAX(id)={nf} vs local {local_max[t]}"
                       for t, nf in dirty)
    if not reset:
        # STRICT fresh-start preflight: REFUSE beats ANY lineage inference —
        # low maxima prove nothing (see _REFUSE_HEAD).
        if force:
            log.warning("SHIP_FORCE=1: shipping into a NON-EMPTY NF despite "
                        "the strict fresh-start REFUSE (%s) — the rows "
                        "already on NF cannot be proven this lineage's; see "
                        "the cutover runbook (archive the NF DB — all three "
                        "files)", detail)
            return
        raise ShipError(_REFUSE_HEAD + f" (fresh start: {detail})"
                        + _REFUSE_TAIL_STRICT, EXIT_PREFLIGHT)
    # --reset (explicit operator opt-in): same-lineage inference allowed.
    same_lineage = all(nf_max <= local_max[table] for table, nf_max in dirty)
    if same_lineage:
        log.warning(
            "preflight (--reset, same-lineage inference): NF already holds "
            "rows (%s) and NF MAX(id) <= local MAX(id) everywhere — "
            "proceeding (re-POSTs are idempotent). LOUD caveat: if the NF DB "
            "was NOT archived at cutover, old-collector rows cannot be "
            "distinguished from this lineage's (a mixed lineage can hide at "
            "aligned maxima) — see the cutover runbook (archive the NF DB — "
            "all three files)", detail)
        return
    raise ShipError(_REFUSE_HEAD + f" (mixed lineage: {detail})"
                    + _REFUSE_TAIL_RESET, EXIT_PREFLIGHT)


# ------------------------- local DB read helpers ---------------------------

def _columns(con: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def _local_max_id(con: sqlite3.Connection, table: str) -> int:
    cols = _columns(con, table)
    if "id" not in cols:
        return 0
    row = con.execute(f'SELECT MAX("id") FROM "{table}"').fetchone()
    return int(row[0] or 0)


def _select_rows(con: sqlite3.Connection, table: str, where: str = "",
                 params: Sequence[Any] = (), order: str = "id",
                 limit: Optional[int] = None) -> List[Dict[str, Any]]:
    cols = _columns(con, table)
    if not cols:
        return []  # table absent locally: nothing to ship
    quoted = ", ".join(f'"{c}"' for c in cols)
    sql = f'SELECT {quoted} FROM "{table}"{where} ORDER BY "{order}"'
    args: List[Any] = list(params)
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    cur = con.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _init_watermark(con: sqlite3.Connection, cfg: Config) -> Dict[str, int]:
    """Derived starting floor — what BIRTHS the watermark at pass start. [R5]

    SHIP_BACKFILL_FROM_ID=N ships ids >= N (every append table); otherwise the
    watermark lands just before the oldest row inside the SHIP_BACKFILL_HOURS
    window (per-table ts column). Nothing inside the window (or garbage ts
    values) -> watermark = local MAX(id): ship from NOW on, never reship
    ancient history. The result is written to disk BEFORE the first POST.
    """
    wm: Dict[str, int] = {}
    for table in APPEND_TABLES:
        if cfg.backfill_from_id is not None:
            wm[table] = cfg.backfill_from_id - 1
            continue
        cols = _columns(con, table)
        if not cols:
            wm[table] = 0
            continue
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.gmtime(time.time() - cfg.backfill_hours * 3600))
        row = con.execute(
            f'SELECT MIN("id") FROM "{table}" WHERE "{TS_COLUMN[table]}" >= ?',
            (cutoff,)).fetchone()
        if row and row[0] is not None:
            wm[table] = int(row[0]) - 1
        else:
            wm[table] = _local_max_id(con, table)
    return wm


# ------------------------------ drain loop ---------------------------------

def _drain(con: sqlite3.Connection, cfg: Config, wm: Dict[str, int],
           wm_path: Path) -> Dict[str, int]:
    """One pass: per append table, batch until drained or the budget [N4].

    ``WHERE id > watermark ORDER BY id`` (single-writer assumption — see the
    module docstring); watermark -> batch max id after EACH 2xx (atomic
    save_watermark). Snapshot tables ship whole-table batches per pass with no
    watermark. Returns per-table shipped row counts.
    """
    counts = {t: 0 for t in APPEND_TABLES + SNAPSHOT_TABLES}
    deadline = _now() + cfg.budget_s
    budget_hit = False

    for table in APPEND_TABLES:
        while True:
            if _now() >= deadline:
                budget_hit = True
                break
            rows = _select_rows(con, table,
                               where=' WHERE "id" > ?',
                               params=(wm[table],), order="id", limit=cfg.batch)
            if not rows:
                break
            if cfg.dry_run:
                log.info("DRY-RUN: would ship %s mode=append rows=%d ids=%s..%s",
                         table, len(rows), rows[0]["id"], rows[-1]["id"])
            else:
                _post_rows(cfg, table, "append", rows)
                wm[table] = int(rows[-1]["id"])  # batch max id — ORDER BY id
                save_watermark(wm_path, wm)      # durable AFTER EACH 2xx [R5]
            counts[table] += len(rows)
        if budget_hit:
            break

    if not budget_hit:
        for table in SNAPSHOT_TABLES:
            rows = _select_rows(con, table, order=SNAPSHOT_PK[table])
            for start in range(0, len(rows), cfg.batch):
                if _now() >= deadline:
                    budget_hit = True
                    break
                chunk = rows[start:start + cfg.batch]
                if cfg.dry_run:
                    log.info("DRY-RUN: would ship %s mode=snapshot rows=%d",
                             table, len(chunk))
                else:
                    _post_rows(cfg, table, "snapshot", chunk)  # no watermark
                counts[table] += len(chunk)
            if budget_hit:
                break
    return counts


# --------------------------------- main ------------------------------------

def _run(cfg: Config, reset: bool) -> int:
    wm_path = DATA_DIR / WATERMARK_NAME
    wm: Optional[Dict[str, int]] = None
    try:
        con = db.connect_ro(str(cfg.db))
    except sqlite3.Error as exc:
        raise ShipError(f"local DB not readable at {cfg.db}: {exc}",
                        EXIT_WATERMARK)
    try:
        local_max = {t: _local_max_id(con, t) for t in APPEND_TABLES}
        wm = None if reset else read_watermark(wm_path)
        if wm is not None:
            for table in APPEND_TABLES:  # sanity: restored/rebuilt local DB [R5]
                if local_max[table] < wm[table]:
                    raise ShipError(
                        f"local MAX(id) for {table} is {local_max[table]} but "
                        f"the watermark says {wm[table]} — the local DB was "
                        "restored/rebuilt? loud refusal: continuing would "
                        "silently skip rows", EXIT_WATERMARK)
        else:
            # Fresh start (missing watermark — born at pass start, so its
            # absence proves no POST ever happened) or explicit --reset:
            # preflight FIRST, then derive the floor and BIRTH the watermark
            # BEFORE the first POST. [R1]
            if not cfg.dry_run:  # preflight ONLY with no valid watermark [v4]
                _preflight(_get_state(cfg), local_max, cfg.force, reset)
            wm = _init_watermark(con, cfg)
            if not cfg.dry_run:
                _write_watermark_file(wm_path, wm)  # BORN AT PASS START [R1]

        counts = _drain(con, cfg, wm, wm_path)
    except ShipError:
        if wm is not None and not cfg.dry_run:
            _safe_save(wm_path, wm)  # progress is durable; advances only on 2xx
        raise
    finally:
        con.close()

    if cfg.dry_run:
        total = sum(counts.values())
        if total:
            log.info("DRY-RUN: pass complete — would have shipped %d rows "
                     "(no network calls made, no watermark written)", total)
        return EXIT_OK
    total = sum(counts.values())
    if total:  # summary line ONLY when something shipped (or failed) [R10]
        log.info("ship pass: shipped %d rows (%s)", total,
                 ", ".join(f"{t}={n}" for t, n in counts.items() if n))
    return EXIT_OK


def _safe_save(wm_path: Path, wm: Dict[str, int]) -> None:
    try:
        save_watermark(wm_path, wm)
    except OSError as exc:
        log.warning("watermark save failed (%s) — will retry next pass", exc)


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        reset = _parse_args(argv)
        cfg = load_config(ENV_FILE)
    except ShipError as exc:
        log.error("ship_rows: %s", exc)
        return exc.code

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = _self_lock(DATA_DIR / LOCK_NAME)
    if lock_fd is None:
        log.debug("another ship pass holds data/ship.lock — exiting, nothing done")
        return EXIT_OK  # the other pass owns shipping; this one does NOTHING
    try:
        return _run(cfg, reset)
    except ShipError as exc:
        log.error("ship pass FAILED: %s", exc)
        return exc.code
    finally:
        os.close(lock_fd)  # releases the flock


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
