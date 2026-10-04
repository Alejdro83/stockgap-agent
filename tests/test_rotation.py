"""Rotation primitives — no-clobber rename for per-tick appenders, copytruncate
for O_APPEND held-fd writers. Findings 1-14 of the Claude review are covered
where testable (crash-safe gzip, mixed-form prune, vacuous-glob fix, empty-file
guard, keep clamps, NaN/inf env parsing). Diff-review round 2 (crash-safety):
parent-dir fsync before the source drop, hardlink-less rename fallback +
unlink-failure healing to exactly one archive, prune gz-twin dedupe.
Round 3 (Claude review of _move_to_archive/_fsync_dir): FileExistsError must
never fall through to the rename fallback (no-clobber regression), dir fsync is
best-effort on EINVAL/ENOTSUP mounts (else gzip_pending re-gzips forever), and
the heal-path (d) cleanup runs only while the source is PROVEN alive — never
skippable when the source survives, never run when survival is undeterminable
(review-rotation-r2 round 3: a lone dst IS the archive)."""
import errno
import gzip
import math
import os
import stat
from pathlib import Path

import pytest

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


def test_prune_dedupes_gz_twin_leftover_and_deletes_nothing(tmp_path):
    """A crash between os.replace and the source unlink leaves x.jsonl next to
    x.jsonl.gz with the SAME _age_key. prune must count the pair as ONE archive
    (else it evicts one REAL archive early) and must leave the leftover in
    place — find_archives still returns it so the gzip pass can heal the pair
    (diff-review finding 4)."""
    pair = "ticks-20260102T000000Z.jsonl"
    (tmp_path / pair).write_text("x")
    (tmp_path / (pair + ".gz")).write_text("x")
    old = tmp_path / "ticks-20260101T000000Z.jsonl.gz"
    old.write_text("y")
    doomed = rotation.prune([tmp_path / pair, tmp_path / (pair + ".gz"), old], keep=2)
    assert doomed == []                       # 2 REAL items + 1 twin, keep=2: nothing deleted
    assert sorted(x.name for x in tmp_path.iterdir()) == sorted([pair, pair + ".gz", old.name])


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


def test_maybe_rotate_jsonl_falls_back_to_rename_without_hardlinks(tmp_path, monkeypatch):
    """Some FUSE/network mounts lack hardlinks (os.link raises): rotation must
    still happen via rename — source gone, content preserved (diff-review 2)."""
    p = tmp_path / "ticks.jsonl"
    _fill(p, 100)

    def nolink(*a, **k):
        raise OSError("hardlinks unsupported")

    monkeypatch.setattr(os, "link", nolink)
    assert rotation.maybe_rotate_jsonl(p, max_bytes=100) is True
    assert not p.exists()
    archs = rotation.find_archives(p)
    assert len(archs) == 1 and archs[0].read_bytes() == b"x" * 100


def test_maybe_rotate_jsonl_heals_failed_unlink_to_exactly_one_archive(tmp_path, monkeypatch):
    """link landed but the source unlink failed: HEAL to exactly ONE archive —
    the live name gone and NO full-size duplicate left for the next tick to
    pile up (diff-review 2c/2d)."""
    p = tmp_path / "ticks.jsonl"
    _fill(p, 100)
    real_unlink = Path.unlink

    def flaky_unlink(self, *a, **k):
        if self.name == p.name:              # only the live file's unlink fails
            raise OSError("unlink denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    assert rotation.maybe_rotate_jsonl(p, max_bytes=100) is True
    archs = rotation.find_archives(p)
    assert len(archs) == 1                   # exactly one archive name...
    assert sorted(x.name for x in tmp_path.iterdir()) == [archs[0].name]  # ...and the ONLY file left
    assert archs[0].read_bytes() == b"x" * 100   # content preserved
    assert not p.exists()


def test_move_to_archive_reraises_fileexists_and_never_clobbers(tmp_path, monkeypatch):
    """No-clobber regression (round-3 review): `except OSError` around os.link
    also catches FileExistsError, so a name collision fell through to
    os.rename and SILENTLY REPLACED an existing archive. FileExistsError must
    propagate BEFORE the rename fallback; dst and source both untouched."""
    p = tmp_path / "ticks.jsonl"
    _fill(p, 100)
    dst = tmp_path / "ticks-20260101T000000Z.jsonl"
    dst.write_text("PRECIOUS")

    def collide(*a, **k):
        raise FileExistsError(errno.EEXIST, "target exists")

    monkeypatch.setattr(os, "link", collide)
    with pytest.raises(FileExistsError):
        rotation._move_to_archive(p, dst)
    assert dst.read_text() == "PRECIOUS"      # existing archive NOT replaced
    assert p.read_bytes() == b"x" * 100       # source untouched (no rename happened)


def test_maybe_rotate_jsonl_fileexists_collision_is_nonfatal_no_clobber(tmp_path, monkeypatch):
    """Race form: archive_name probed dst absent, then a same-second twin took
    the name before os.link ran. The rotation is skipped (False, non-fatal) and
    the existing archive keeps its content — never overwritten."""
    p = tmp_path / "ticks.jsonl"
    _fill(p, 100)
    dst = tmp_path / "ticks-20260101T000000Z.jsonl"
    dst.write_text("PRECIOUS")

    def collide(*a, **k):
        raise FileExistsError(errno.EEXIST, "target exists")

    monkeypatch.setattr(rotation, "archive_name", lambda path: dst)
    monkeypatch.setattr(os, "link", collide)
    assert rotation.maybe_rotate_jsonl(p, max_bytes=100) is False
    assert dst.read_text() == "PRECIOUS"
    assert p.read_bytes() == b"x" * 100


def test_gzip_pending_completes_when_dir_fsync_unsupported(tmp_path, monkeypatch):
    """FUSE/network mounts have no directory fsync (EINVAL/ENOTSUP). That must
    be best-effort: swallowing it lets gzip_pending drop the source — otherwise
    every pass re-gzips and the uncompressed source is never reclaimed."""
    for err in (errno.EINVAL, errno.ENOTSUP):
        p = tmp_path / f"ticks-2026010{err}T000000Z.jsonl"
        payload = '{"a": 1}\n{"b": 2}\n'
        p.write_text(payload)
        real_fsync = os.fsync

        def fsync(fd, _err=err):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(_err, "dir fsync unsupported")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fsync)
        assert rotation.gzip_pending(p) is True
        assert not p.exists()                  # source reclaimed
        with gzip.open(str(p) + ".gz", "rt", encoding="utf-8") as fh:
            assert fh.read() == payload
        monkeypatch.undo()


def test_gzip_pending_keeps_source_on_real_dir_fsync_error(tmp_path, monkeypatch):
    """Only EINVAL/ENOTSUP are best-effort: a real fsync failure (EIO) must
    still abort BEFORE the source unlink — the source is protected."""
    p = tmp_path / "ticks-20260101T000000Z.jsonl"
    p.write_text("payload\n")
    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "i/o error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    assert rotation.gzip_pending(p) is False
    assert p.exists() and p.read_text() == "payload\n"   # source kept


def test_move_to_archive_heal_cleanup_not_skipped_by_exists_race(tmp_path, monkeypatch):
    """The bare path.exists() probe in the unlink-failure handler can itself
    raise in a rare race (e.g. EIO on stat). That must NOT skip the (d)
    duplicate cleanup: the full-size dst twin is removed, the source survives,
    and the error still propagates (round-3 review)."""
    p = tmp_path / "ticks.jsonl"
    _fill(p, 100)
    dst = tmp_path / "ticks-20260101T000000Z.jsonl"
    real_exists, real_unlink = Path.exists, Path.unlink
    state = {"probed": False}

    def flaky_exists(self, *a, **k):
        if self == p and not state["probed"]:   # only the FIRST source probe races
            state["probed"] = True
            raise OSError(errno.EIO, "stat race")
        return real_exists(self, *a, **k)

    def flaky_unlink(self, *a, **k):
        if self.name == p.name:                 # source unlink fails -> heal path
            raise OSError("unlink denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "exists", flaky_exists)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with pytest.raises(OSError):
        rotation._move_to_archive(p, dst)
    assert state["probed"]                      # the racing probe really fired
    assert not real_exists(dst)                 # (d) cleanup ran — no full-size twin
    assert p.read_bytes() == b"x" * 100         # source intact


def test_move_to_archive_eio_after_unlink_land_keeps_dst_when_probes_die(tmp_path, monkeypatch):
    """EIO-after-unlink DATA-LOSS regression (review-rotation-r2 round 3):
    ``path.unlink()`` raises EIO but the unlink LANDED and every survival probe
    on the source dies too — a persistent stat EIO makes both ``Path.exists()``
    (3.11 swallows only ENOENT/ENOTDIR/EBADF/ELOOP) and ``os.lstat`` raise.
    Survival is UNDETERMINABLE and ``dst`` may be the ONLY archive left: the
    heal cleanup must NOT delete it (a lone ``dst`` IS the archive — a duplicate
    twin is recoverable, a lost archive is not) and the call must still raise —
    never silently succeed. The old ``except OSError: surviving = True`` deleted
    ``dst`` here = permanent data loss."""
    p = tmp_path / "ticks.jsonl"
    _fill(p, 100)
    dst = tmp_path / "ticks-20260101T000000Z.jsonl"
    real_exists, real_lstat, real_unlink = Path.exists, os.lstat, Path.unlink

    def eio_unlink(self, *a, **k):
        if self.name == p.name:                 # the source unlink...
            real_unlink(self, *a, **k)          # ...LANDS...
            raise OSError(errno.EIO, "i/o error")   # ...but reports EIO
        return real_unlink(self, *a, **k)

    def dead_lstat(target, *a, **k):            # persistent stat error on the source
        if Path(os.fspath(target)) == p:
            raise OSError(errno.EIO, "i/o error")
        return real_lstat(target, *a, **k)

    def dead_exists(self, *a, **k):
        if self == p:
            raise OSError(errno.EIO, "i/o error")
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", dead_exists)
    monkeypatch.setattr(os, "lstat", dead_lstat)
    monkeypatch.setattr(Path, "unlink", eio_unlink)
    with pytest.raises(OSError):                # never silent success
        rotation._move_to_archive(p, dst)
    assert dst.read_bytes() == b"x" * 100       # dst SURVIVES with its original content
    assert not real_exists(p)                   # interleaving sanity: the unlink really landed


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


def test_gzip_pending_fsyncs_parent_dir_after_replace_before_source_unlink(tmp_path, monkeypatch):
    """Crash gap (diff-review 1): the archive rename must be made durable BEFORE
    the source unlink — otherwise a host crash can persist the unlink but lose
    the rename and take BOTH names (archive and source gone)."""
    p = tmp_path / "ticks-20260101T000000Z.jsonl"
    p.write_text("payload\n")
    events = []
    real_fsync, real_replace, real_unlink = os.fsync, os.replace, Path.unlink

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append("fsync-dir")
        return real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    def unlink(self, *a, **k):
        events.append(f"unlink:{self.name}")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert rotation.gzip_pending(p) is True
    assert "fsync-dir" in events
    assert events.index("replace") < events.index("fsync-dir") < events.index(f"unlink:{p.name}")


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


def _load_rotate_data():
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "scripts" / "rotate_data.py"
    spec = importlib.util.spec_from_file_location("rotate_data", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rotate_data_end_to_end_and_idempotent(tmp_path, monkeypatch):
    """Full pass: prune frees space first, gzip pending JSONL archives,
    copytruncate oversized logs; second run is a no-op (finding 14)."""
    mod = _load_rotate_data()
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    # pending uncompressed JSONL archives (the collector's rename output) + 1 old .gz
    (data / "ticks-20260101T000000Z.jsonl").write_text('{"x": 1}\n')
    (data / "ticks-20251201T000000Z.jsonl.gz").write_text("old")
    # streams the plan v1 missed (finding 4): data/*.log + logs/*.log + alerts.jsonl
    (data / "oracle.log").write_text("o" * 70000)      # over the 64 KB floor
    (data / "logs" / "rotate.log").write_text("r" * 70000)
    (data / "alerts.jsonl").write_text('{"a": 1}\n')
    monkeypatch.setenv("LOG_MAX_MB", "0.001")     # floats allowed; floors at 64 KB (review-2.8b)
    monkeypatch.setenv("LOG_KEEP_ARCHIVES", "5")
    monkeypatch.setenv("JSONL_KEEP_ARCHIVES", "1")
    mod.main(data)
    listing1 = sorted(p.name for p in data.iterdir()) + sorted(p.name for p in (data / "logs").iterdir())
    assert (data / "ticks-20260101T000000Z.jsonl.gz").exists()
    assert not (data / "ticks-20260101T000000Z.jsonl").exists()
    assert not (data / "ticks-20251201T000000Z.jsonl.gz").exists()  # pruned (keep=1)
    assert len(rotation.find_archives(data / "oracle.log")) == 1
    assert len(rotation.find_archives(data / "logs" / "rotate.log")) == 1
    assert (data / "oracle.log").stat().st_size == 0
    mod.main(data)   # idempotent: nothing new created
    listing2 = sorted(p.name for p in data.iterdir()) + sorted(p.name for p in (data / "logs").iterdir())
    assert listing1 == listing2
