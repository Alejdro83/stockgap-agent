"""Shared db helpers (WAL assertion, busy_timeout) + read-only visibility.

Regression tests for the Claude-reviewed SQLite reliability fix (2026-10-02):

(d) ``ensure_wal`` must return/ASSERT ``'wal'`` (``PRAGMA journal_mode=WAL``
    silently returns the old mode when it cannot get the lock — that silent
    fallback to rollback-journal mode caused the 176x 'database is locked'
    daemon deaths) and ``busy_timeout`` must be 10000.
(c) the dashboard's read-only connection (``mode=ro``, NO ``immutable=1``) must
    see rows committed AFTER the connection was opened — ``immutable=1`` froze
    the file view and served stale data once the writers went WAL.
"""

import sqlite3

import pytest

from stockgap import db, dashboard


def test_connect_pragmas_and_ensure_wal_returns_wal(tmp_path):
    con = db.connect(tmp_path / "t.db")
    try:
        # (d) ensure_wal asserts the WAL conversion actually happened
        assert db.ensure_wal(con) == "wal"
        # (d) busy_timeout is set (before ensure_wal — see stockgap/db.py)
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        con.close()


def test_ensure_wal_raises_when_conversion_refused():
    class StubCon:
        """Simulates PRAGMA journal_mode=WAL silently answering 'delete'."""

        def execute(self, _sql):
            return self

        def fetchone(self):
            return ("delete",)

    with pytest.raises(RuntimeError, match="WAL"):
        db.ensure_wal(StubCon())


def test_readonly_connection_sees_commits_after_open(tmp_path):
    """(c) REGRESSION: immutable=1 gave readers a frozen view of the file."""
    path = tmp_path / "t.db"
    writer = db.connect(path)
    db.ensure_wal(writer)
    writer.execute("CREATE TABLE t (x INTEGER)")
    writer.commit()
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()

    reader = db.connect_ro(path)  # opened BEFORE the second insert
    assert [row[0] for row in reader.execute("SELECT x FROM t ORDER BY x")] == [1]

    writer.execute("INSERT INTO t VALUES (2)")
    writer.commit()  # committed AFTER the reader opened

    assert [row[0] for row in reader.execute("SELECT x FROM t ORDER BY x")] == [1, 2]
    reader.close()
    writer.close()


def test_dashboard_with_db_sees_fresh_commits(tmp_path, monkeypatch):
    """(c) the actual dashboard connection factory (with_db -> db.connect_ro)."""
    path = tmp_path / "fresh.db"
    writer = db.connect(path)
    db.ensure_wal(writer)
    writer.executescript(
        "CREATE TABLE collector_meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    writer.execute("INSERT INTO collector_meta VALUES ('last_tick_ts', 't1')")
    writer.commit()
    monkeypatch.setenv("STOCKGAP_DB", str(path))

    @dashboard.with_db
    def read_meta(conn):
        return [r[0] for r in conn.execute("SELECT key FROM collector_meta ORDER BY key")]

    assert read_meta() == ["last_tick_ts"]
    writer.execute("INSERT INTO collector_meta VALUES ('started_at', 't0')")
    writer.commit()
    assert read_meta() == ["last_tick_ts", "started_at"]
    writer.close()
