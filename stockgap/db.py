"""Shared SQLite helpers: WAL + busy_timeout for every writer in the repo.

Every writer (collector, oracle, oracle_score, alerts, b402_gate) opens its
connection through :func:`connect` and calls :func:`ensure_wal` ONCE at
startup. Rules this module enforces (Claude-reviewed SQLite reliability fix,
2026-10-02):

- ``busy_timeout=10000`` is set BEFORE ``ensure_wal()`` — a writer that has to
  wait for a lock waits up to 10 s instead of failing instantly.
- ``PRAGMA synchronous=NORMAL`` — safe under WAL (fsync only at checkpoint),
  the durability loss window is bounded by the OS crash, not corruption.
- ``PRAGMA foreign_keys=ON`` — referential integrity is not free-by-default.
- ``PRAGMA journal_size_limit=67108864`` — cap the WAL file at 64 MiB so
  checkpoints keep it from growing unbounded (the tick DB writes ~30 rows/s).
- :func:`ensure_wal` runs ``PRAGMA journal_mode=WAL`` and ASSERTS the returned
  value is ``wal``: SQLite silently returns ``'delete'`` when the conversion
  cannot get the lock, and a silent fallback to rollback journal mode is
  exactly how the 176x ``database is locked`` outage happened.
- Write batches go through :func:`write_txn` (``BEGIN IMMEDIATE ... COMMIT``):
  under WAL a read-then-write transaction can fail with ``SQLITE_BUSY_SNAPSHOT``
  which IGNORES busy_timeout; ``BEGIN IMMEDIATE`` takes the write lock up front
  so the batch only ever holds it for milliseconds.

Readers (dashboard, MCP, summary) use :func:`connect_ro` — ``mode=ro`` WITHOUT
``immutable=1``: immutable would serve stale pages now that writers live in WAL.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any, Iterator

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "JOURNAL_SIZE_LIMIT",
    "begin_immediate",
    "commit",
    "connect",
    "connect_ro",
    "ensure_wal",
    "write_txn",
]

DEFAULT_BUSY_TIMEOUT_MS = 10000
JOURNAL_SIZE_LIMIT = 67108864  # 64 MiB


def connect(
    db_path: Any,
    *,
    busy_timeout_ms: int | None = None,
    timeout: float | None = None,
) -> sqlite3.Connection:
    """Writer connection factory: busy_timeout first, then the PRAGMAs above.

    ``busy_timeout_ms`` is the lock-wait budget (also mirrored into the
    connect ``timeout`` unless overridden); it defaults to
    ``DEFAULT_BUSY_TIMEOUT_MS`` (read at call time, so tests can shrink it).
    Production stays at 10 s.
    """
    busy = DEFAULT_BUSY_TIMEOUT_MS if busy_timeout_ms is None else int(busy_timeout_ms)
    con = sqlite3.connect(
        str(db_path),
        timeout=(busy / 1000.0 if timeout is None else float(timeout)),
    )
    con.execute(f"PRAGMA busy_timeout={busy}")  # BEFORE ensure_wal() — see module docstring
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
    return con


def connect_ro(db_path: Any, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Read-only connection for dashboards/MCP/summary.

    Deliberately NOT ``immutable=1``: that flag promises the file never
    changes, which serves stale pages (or garbage) once the writers switched
    to WAL. ``mode=ro`` alone keeps the connection strictly read-only while
    still seeing freshly committed rows.
    """
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout)


def ensure_wal(con: sqlite3.Connection) -> str:
    """Switch the DB to WAL and ASSERT the switch actually happened.

    ``PRAGMA journal_mode=WAL`` returns ``'wal'`` on success but silently
    returns the old mode (usually ``'delete'``) when it cannot take the lock —
    callers used to ignore that and ran for hours in rollback-journal mode
    where every writer serializes and times out. In-memory databases report
    ``'memory'`` (no journal file at all) and are accepted as-is.
    """
    row = con.execute("PRAGMA journal_mode=WAL").fetchone()
    mode = str(row[0]).lower() if row and row[0] is not None else ""
    if mode == "memory":  # ':memory:' DB: nothing to convert, nothing to lock
        return mode
    if mode != "wal":
        raise RuntimeError(
            f"PRAGMA journal_mode=WAL refused (got {mode!r}) — another "
            "connection holds a lock; WAL is required for writer concurrency"
        )
    return mode


def begin_immediate(con: sqlite3.Connection) -> None:
    """Take the write lock up front (avoids SQLITE_BUSY_SNAPSHOT under WAL)."""
    con.execute("BEGIN IMMEDIATE")


def commit(con: sqlite3.Connection) -> None:
    con.execute("COMMIT")


@contextlib.contextmanager
def write_txn(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """ONE short write transaction: ``BEGIN IMMEDIATE ... COMMIT``.

    Hold it for milliseconds only — never across a network call. Rolls back
    and re-raises on any error so a failed batch never leaves a half-open
    transaction behind (the next statement would otherwise fail with
    "cannot start a transaction within a transaction").
    """
    begin_immediate(con)
    try:
        yield con
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    else:
        commit(con)
