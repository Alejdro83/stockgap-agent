"""Size-based rotation for the JSONL mirror and the operational logs.

Mechanisms are chosen by WRITER semantics (verified in collector.py and
scripts/deploy/entrypoint.sh, re-verified in the Claude plan review):

- ``maybe_rotate_jsonl``: NO-CLOBBER RENAME (link+unlink — a plain rename would
  silently overwrite a same-second archive). ``_move_to_archive`` carries the
  full ladder: a rename fallback where hardlinks are unsupported (some
  FUSE/network mounts) and healing to exactly one archive when the source
  unlink fails. The collector opens the JSONL per
  tick, so a rename between ticks is atomic and lossless — and O(1), so the
  tick never pays gzip cost. Compression/retention belong to
  ``scripts/rotate_data.py``.

- ``rotate_log``: COPYTRUNCATE. ``tee -a``, logging ``FileHandler`` ('a') and
  shell ``>>`` all hold O_APPEND handles: every write lands at current EOF, so
  truncation is safe for them (a rename would strand their writes in the
  archive). Accepted cost: lines appended while ``shutil.copyfile`` runs are
  lost — the window scales with file size (seconds on a 20 MB file over network
  storage), it is NOT milliseconds. Operational logs only.

All functions are best-effort and NON-FATAL: rotation must never break a tick
or crash a writer. Archives carry sortable UTC timestamps (``%Y%m%dT%H%M%SZ``,
plus a ``-N`` uniquifier on collision), so ``_age_key`` order == age order (raw name order mis-sorts ``-10`` vs ``-2``). Truncated
crash leftovers (``*.gz.tmp``) are never counted as archives.
"""
from __future__ import annotations

import gzip
import logging
import math
import os
import re
import shutil
import time
from pathlib import Path
from typing import Iterable, List, Optional, Pattern

log = logging.getLogger("stockgap.rotation")
_last_warn = 0.0


def _warn(msg: str, *args) -> None:   # rate-limited: a dead volume must not flood the collector log
    global _last_warn
    now = time.monotonic()
    if now - _last_warn >= 300:
        _last_warn = now
        log.warning(msg, *args)

TS_FMT = "%Y%m%dT%H%M%SZ"
# One archive-name shape for every stream: <stem>-<TS>[-<N>]<suffix>[.gz]
_ARCHIVE_NAME_RE = re.compile(r"-\d{8}T\d{6}Z(?:-\d+)?\.[A-Za-z0-9]+(\.gz)?$")

def _ts() -> str:
    return time.strftime(TS_FMT, time.gmtime())

def _age_key(p: Path):
    """Sort key = (timestamp, uniquifier): '...-TS-2.jsonl' is NEWER than '...-TS.jsonl' (bare = 0)."""
    m = re.search(r"-(\d{8}T\d{6}Z)(?:-(\d+))?", p.name)
    return (m.group(1), int(m.group(2)) if m and m.group(2) else 0) if m else ("", 0)

def is_archive_name(name: str) -> bool:
    """True for any generated archive name (used to skip archives-as-live)."""
    return _ARCHIVE_NAME_RE.search(name) is not None

def find_archives(path: Path) -> List[Path]:
    """Archives of ONE stream: <stem>-<TS>[-N]<suffix>[.gz], age order.

    Strict per-stream match (finding 1): a sibling stream ``x-y.log`` and a
    double-rotation name are NOT archives of ``x.log``.
    """
    rx = re.compile(
        rf"^{re.escape(path.stem)}-\d{{8}}T\d{{6}}Z(?:-\d+)?"
        rf"{re.escape(path.suffix)}(\.gz)?$"
    )
    try:
        out = [p for p in path.parent.iterdir() if rx.match(p.name)]
    except OSError:
        return []
    return sorted(out, key=_age_key)   # (ts, -N) key: raw name order mis-sorts -10 vs -2 (review-2.5)

def archive_name(path: Path) -> Path:
    """Unique ``<stem>-<UTC>[-N]<suffix>`` target; never an existing file."""
    ts = _ts()
    cand = path.with_name(f"{path.stem}-{ts}{path.suffix}")
    n = 0
    while cand.exists() or Path(str(cand) + ".gz").exists():   # never clobber a same-second .gz (review-3 nit)
        n += 1
        cand = path.with_name(f"{path.stem}-{ts}-{n}{path.suffix}")
    return cand

def prune(archives: Iterable[Path], keep: int) -> List[Path]:
    """Delete the oldest archives beyond ``keep`` (>= 1; finding 12).

    Crash-leftover twins are dropped from the ACCOUNTING only (finding 4): an
    uncompressed archive whose ``.gz`` twin exists (a kill between
    ``os.replace`` and the source unlink) shares its ``_age_key`` — counting
    both would evict one REAL archive early. The leftover itself is never
    deleted here: ``find_archives`` still returns it so ``rotate_data``'s gzip
    pass can re-gzip and heal the pair.
    """
    keep = max(1, keep)
    items = sorted((p for p in archives
                    if p.name.endswith(".gz") or not Path(str(p) + ".gz").exists()),
                   key=_age_key)   # (ts, -N) key: raw name order mis-sorts -10 vs -2 (review-2.5)
    doomed = items[:-keep] if keep < len(items) else []
    for p in doomed:
        try:
            p.unlink()
        except OSError:
            pass
    return doomed

def _move_to_archive(path: Path, dst: Path) -> None:
    """No-clobber move of the live JSONL to archive name ``dst``. Raises OSError.

    (a) ``os.link`` + ``path.unlink``: atomic create-if-absent — a name
        collision FAILS instead of silently overwriting (finding 7).
    (b) Hardlink-less mounts (some FUSE/network filesystems): ``os.link`` raises
        -> fall back to ``os.rename`` (``dst`` was probed absent by
        ``archive_name``; the collector is the single writer).
    (c) ``path.unlink()`` failed after the link landed: one inode under two
        names. ``os.rename`` resolves both at once when they are different
        files; between same-inode hardlinks it is a POSIX no-op (verified on
        this kernel), so drop the redundant ``dst`` twin and repeat the rename
        — the end state is exactly one archive name.
    (d) Anything still failing: best-effort ``dst.unlink()`` (only while the
        source survives — a lone ``dst`` IS the archive) before re-raising, so
        the next tick cannot accumulate full-size duplicate archives.
    """
    try:
        os.link(path, dst)                     # (a) atomic create-if-absent
    except OSError:
        os.rename(path, dst)                   # (b) no hardlinks on this mount: plain move
        return
    try:
        path.unlink()
    except OSError:
        if not path.exists():
            return   # the unlink actually landed (e.g. EIO-after-unlink): dst is the archive
        try:
            os.rename(path, dst)               # (c) resolve the twin pair in one op
            if path.exists():                  # same-inode no-op: dst is a redundant twin
                dst.unlink()
                os.rename(path, dst)
        except OSError:
            if path.exists():
                try:
                    dst.unlink()               # (d) never leave a full-size duplicate behind
                except OSError:
                    pass
            raise

def maybe_rotate_jsonl(path: Path, max_bytes: int) -> bool:
    """Rename the live JSONL to a unique archive at ``max_bytes``.

    No-clobber move via ``_move_to_archive`` (link+unlink, rename fallback on
    hardlink-less mounts, healing to exactly one archive — never overwrites a
    same-second archive, finding 7). Returns True when a rotation happened.
    Never raises.
    """
    try:
        if path.stat().st_size < max_bytes:
            return False
        _move_to_archive(path, archive_name(path))
        return True
    except OSError as exc:
        _warn("jsonl rotation skipped for %s: %s", path, exc)   # never silent (review-2.8a)
        return False  # non-fatal mirror — a rotation problem must never fail a tick

def _fsync_dir(d: Path) -> None:
    """fsync a directory fd: makes the renames/unlinks inside it durable."""
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def gzip_pending(path: Path, compresslevel: int = 6) -> bool:
    """Crash-safe gzip: ``.gz.tmp`` -> fsync -> atomic replace -> parent-DIR fsync -> drop source.

    A kill mid-gzip leaves only a ``.gz.tmp`` (never a valid-looking ``.gz``);
    the source archive survives and the next pass retries (finding 3). The
    parent-dir fsync lands BETWEEN the replace and the source unlink: it makes
    the archive name durable before the source name can go away — without it a
    host crash could persist the unlink but lose the rename and take BOTH
    names. A process kill in that window leaves source + ``.gz`` side by side:
    self-healing (the next pass re-gzips over the twin and ``prune`` counts the
    pair as one archive).
    """
    gz = Path(str(path) + ".gz")
    tmp = Path(str(path) + ".gz.tmp")
    try:
        with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=compresslevel) as out:
            shutil.copyfileobj(src, out)
        with tmp.open("r+b") as fh:   # fsync AFTER close: the gzip trailer is written at close (review-2.7)
            os.fsync(fh.fileno())
        os.replace(tmp, gz)
        _fsync_dir(path.parent)   # rename durable BEFORE the unlink (crash gap: both names could vanish)
        path.unlink()
        return True
    except OSError as exc:
        log.warning("gzip_pending failed for %s: %s", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False

def rotate_log(path: Path, max_bytes: int) -> bool:
    """Copytruncate at ``max_bytes``: copy to a unique archive, truncate to 0.

    ``size == 0`` is always a no-op (finding 11). A failed copy removes its
    partial archive (finding 3). Logs a LOWER-BOUND estimate of the dropped
    bytes — appends between the post-copy stat and the truncate are uncounted
    (the accepted loss window, finding 6). Never raises.
    """
    dst: Optional[Path] = None
    try:
        st = path.stat()
        if st.st_size == 0 or st.st_size < max_bytes:
            return False
        dst = archive_name(path)
        shutil.copyfile(path, dst)
        lost = max(0, path.stat().st_size - dst.stat().st_size)  # lower bound of the dropped bytes: appends between THIS stat and the truncate are uncounted (review-2.6)
        os.truncate(path, 0)   # O_APPEND holders reposition to EOF (=0) safely
        if lost:
            log.info("rotate_log %s: at least %d bytes appended during copy were dropped (lower bound — appends between the post-copy stat and the truncate are uncounted)",
                     path.name, lost)
        return True
    except OSError as exc:
        log.warning("rotate_log failed for %s: %s", path, exc)
        if dst is not None:
            try:
                dst.unlink()
            except OSError:
                pass
        return False

def jsonl_max_bytes_from_env() -> int:
    """JSONL_MAX_MB (default 100, floor 1 MB) -> bytes. Never raises (finding 8)."""
    try:
        mb = float(os.environ.get("JSONL_MAX_MB", "100") or "100")
        if not math.isfinite(mb):
            raise ValueError("non-finite")
        return max(1024 * 1024, int(mb * 1024 * 1024))   # 1 MB floor (the test is the contract)
    except (ValueError, OverflowError):
        return 100 * 1024 * 1024
