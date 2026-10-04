"""Rotation primitives — no-clobber rename for per-tick appenders, copytruncate
for O_APPEND held-fd writers. Findings 1-14 of the Claude review are covered
where testable (crash-safe gzip, mixed-form prune, vacuous-glob fix, empty-file
guard, keep clamps, NaN/inf env parsing)."""
import gzip
import math
import os
from pathlib import Path

from stockgap import rotation


def _fill(path: Path, size: int) -> None:
    path.write_bytes(b"x" * size)


def test_archive_name_is_unique_and_never_overwrites(tmp_path):
    p = tmp_path / "ticks.jsonl"
    a = rotation.archive_name(p)
    a.write_text("first")
    b = rotation.archive_name(p)          # same second possible
    assert b != a                          # uniquified, not overwritten
    assert a.read_text() == "first"


def test_find_archives_strict_match_only(tmp_path):
    """Sibling streams and archive-of-archive must NOT be picked up (finding 1)."""
    live = tmp_path / "oracle-cron.log"
    keep = [
        tmp_path / "oracle-cron-20260101T000000Z.log",
        tmp_path / "oracle-cron-20260102T000000Z-1.log.gz",
    ]
    decoys = [
        tmp_path / "oracle-cron-20260101T000000Z-1-20260103T000000Z.log",  # double rotation
        tmp_path / "oracle-cron-extra.log",                                 # sibling stream
        tmp_path / "oracle-cron-20260101T000000Z.log.gz.tmp",               # crash leftover
    ]
    for p in keep + decoys:
        p.write_text("x")
    got = sorted(p.name for p in rotation.find_archives(live))
    assert got == sorted(p.name for p in keep)


def test_prune_keeps_newest_counts_gz_and_plain_together(tmp_path):
    names = [f"ticks-2026010{i}T000000Z.jsonl" for i in (1, 2, 3)]
    names.append("ticks-20260104T000000Z.jsonl.gz")
    for n in names:
        (tmp_path / n).write_text("x")
    rotation.prune([tmp_path / n for n in names], keep=2)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == sorted(names[-2:])      # oldest two (mixed forms) gone


def test_prune_keep_beyond_len_is_noop(tmp_path):
    p = tmp_path / "ticks-20260101T000000Z.jsonl"
    p.write_text("x")
    assert rotation.prune([p], keep=5) == []
    assert p.exists()


def test_prune_clamps_keep_below_one(tmp_path):
    """keep=0 must not delete the archive just made (finding 12)."""
    ps = []
    for i in (1, 2):
        p = tmp_path / f"ticks-2026010{i}T000000Z.jsonl"
        p.write_text("x")
        ps.append(p)
    rotation.prune(ps, keep=0)
    assert len(list(tmp_path.iterdir())) >= 1


def test_maybe_rotate_jsonl_below_threshold_is_noop(tmp_path):
    p = tmp_path / "ticks.jsonl"
    _fill(p, 100)
    assert rotation.maybe_rotate_jsonl(p, max_bytes=101) is False
    assert p.exists() and not list(tmp_path.glob("ticks-*"))


def test_maybe_rotate_jsonl_renames_at_threshold(tmp_path):
    p = tmp_path / "ticks.jsonl"
    _fill(p, 100)
    assert rotation.maybe_rotate_jsonl(p, max_bytes=100) is True
    assert not p.exists()
    archs = rotation.find_archives(p)
    assert len(archs) == 1 and archs[0].stat().st_size == 100


def test_maybe_rotate_jsonl_never_raises_on_oserror(tmp_path):
    missing = tmp_path / "nope" / "ticks.jsonl"
    assert rotation.maybe_rotate_jsonl(missing, max_bytes=1) is False  # no raise


def test_gzip_pending_preserves_content(tmp_path):
    p = tmp_path / "ticks-20260101T000000Z.jsonl"
    p.write_text('{"a": 1}\n{"b": 2}\n')
    assert rotation.gzip_pending(p) is True
    assert not p.exists()
    with gzip.open(str(p) + ".gz", "rt", encoding="utf-8") as fh:
        assert fh.read() == '{"a": 1}\n{"b": 2}\n'


def test_gzip_pending_failure_keeps_source_and_cleans_tmp(tmp_path, monkeypatch):
    """Crash safety (finding 3): no truncated .gz looks valid, source survives."""
    p = tmp_path / "ticks-20260101T000000Z.jsonl"
    p.write_text("payload\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(rotation.shutil, "copyfileobj", boom)
    assert rotation.gzip_pending(p) is False
    assert p.exists() and p.read_text() == "payload\n"
    assert not (tmp_path / (p.name + ".gz")).exists()
    assert not list(tmp_path.glob("*.gz.tmp"))


def test_rotate_log_copytruncate_with_held_append_fd(tmp_path):
    """Simulates `tee -a`: a long-lived O_APPEND fd must keep working across
    rotation, with NO sparse-file hole after truncation (Claude: verified OK)."""
    p = tmp_path / "dashboard.log"
    p.write_text("old line\n")
    fd = os.open(p, os.O_APPEND | os.O_WRONLY)
    try:
        assert rotation.rotate_log(p, max_bytes=5) is True
        os.write(fd, b"new line\n")
    finally:
        os.close(fd)
    archs = rotation.find_archives(p)
    assert len(archs) == 1 and archs[0].read_text() == "old line\n"
    assert p.read_bytes() == b"new line\n"  # offset 0, no NUL hole


def test_rotate_log_below_threshold_and_empty_are_noop(tmp_path):
    p = tmp_path / "alerts.jsonl"
    _fill(p, 10)
    assert rotation.rotate_log(p, max_bytes=11) is False
    empty = tmp_path / "empty.log"
    empty.touch()
    assert rotation.rotate_log(empty, max_bytes=0) is False  # size==0 guard (finding 11)
    assert sorted(x.name for x in tmp_path.iterdir()) == ["alerts.jsonl", "empty.log"]


def test_env_parsing_never_crashes_on_nan_inf(monkeypatch):
    """Finding 8: nan/inf must fall back to defaults, never raise."""
    for bad in ("nan", "inf", "-inf", "abc", ""):
        monkeypatch.setenv("JSONL_MAX_MB", bad)
        assert rotation.jsonl_max_bytes_from_env() == 100 * 1024 * 1024
    monkeypatch.setenv("JSONL_MAX_MB", "0")       # floor: 1 MB (finding 7)
    assert rotation.jsonl_max_bytes_from_env() == 1 * 1024 * 1024
    monkeypatch.setenv("JSONL_MAX_MB", "7")
    assert rotation.jsonl_max_bytes_from_env() == 7 * 1024 * 1024


def test_record_tick_triggers_rename_rotation(tmp_path):
    """Tiny threshold: the tick still commits to SQLite and the JSONL renames."""
    from tests.test_collector import UNIVERSE, PRICE_ROWS
    from stockgap.collector import SpreadCollector

    jsonl = tmp_path / "ticks.jsonl"
    _fill(jsonl, 50)  # pre-existing oversized mirror
    col = SpreadCollector(db_path=tmp_path / "db.sqlite", jsonl_path=jsonl,
                          client=None)   # real __init__ kwargs confirmed: db_path, jsonl_path, client=None
    col.jsonl_max_bytes = 10             # AFTER construction: bypass the 1 MB floor for this fixture
    spread_rows, cross_rows = col.build_tick_rows(UNIVERSE, PRICE_ROWS, tick_id=1)  # exact shape of tests/test_collector.py:125 (method, not module fn)
    col.record_tick("2026-01-01T00:00:00Z", 1, spread_rows, cross_rows)

    import sqlite3
    con = sqlite3.connect(tmp_path / "db.sqlite")
    n = con.execute("SELECT COUNT(*) FROM spread_ticks").fetchone()[0]
    con.close()
    assert n == len(spread_rows) > 0            # SQLite committed (finding 2)
    archs = rotation.find_archives(tmp_path / "ticks.jsonl")
    assert len(archs) == 1 and archs[0].stat().st_size == 50
    assert jsonl.exists() and jsonl.stat().st_size > 0  # fresh mirror, same tick
