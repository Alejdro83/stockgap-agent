"""Size-based rotation for the JSONL mirror and the operational logs.

Mechanisms are chosen by WRITER semantics (verified in collector.py and
scripts/deploy/entrypoint.sh, re-verified in the Claude plan review):

- ``maybe_rotate_jsonl``: NO-CLOBBER RENAME (link+unlink — a plain rename would
  silently overwrite a same-second archive). The collector opens the JSONL per
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
    """Delete the oldest archives beyond ``keep`` (>= 1; finding 12)."""
    keep = max(1, keep)
    items = sorted(archives, key=_age_key)   # raw name order mis-sorts -10 vs -2 (review-2.5)
    doomed = items[:-keep] if keep < len(items) else []
    for p in doomed:
        try:
            p.unlink()
        except OSError:
            pass
    return doomed

def maybe_rotate_jsonl(path: Path, max_bytes: int) -> bool:
    """Rename the live JSONL to a unique archive at ``max_bytes``.

    No-clobber: link+unlink makes a name collision fail instead of overwriting
    (finding 7). Returns True when a rotation happened. Never raises.
    """
    try:
        if path.stat().st_size < max_bytes:
            return False
        dst = archive_name(path)
        os.link(path, dst)      # atomic create-if-absent
        path.unlink()
        return True
    except OSError as exc:
        _warn("jsonl rotation skipped for %s: %s", path, exc)   # never silent (review-2.8a)
        return False  # non-fatal mirror — a rotation problem must never fail a tick

def gzip_pending(path: Path, compresslevel: int = 6) -> bool:
    """Crash-safe gzip: ``.gz.tmp`` -> fsync -> atomic replace -> drop source.

    A kill mid-gzip leaves only a ``.gz.tmp`` (never a valid-looking ``.gz``);
    the source archive survives and the next pass retries (finding 3).
    """
    gz = Path(str(path) + ".gz")
    tmp = Path(str(path) + ".gz.tmp")
    try:
        with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=compresslevel) as out:
            shutil.copyfileobj(src, out)
        with tmp.open("r+b") as fh:   # fsync AFTER close: the gzip trailer is written at close (review-2.7)
            os.fsync(fh.fileno())
        os.replace(tmp, gz)
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
    partial archive (finding 3). Logs bytes appended during the copy (the
    accepted loss window, finding 6). Never raises.
    """
    dst: Optional[Path] = None
    try:
        st = path.stat()
        if st.st_size == 0 or st.st_size < max_bytes:
            return False
        dst = archive_name(path)
        shutil.copyfile(path, dst)
        lost = max(0, path.stat().st_size - dst.stat().st_size)  # appended DURING the copy, dropped (review-2.6)
        os.truncate(path, 0)   # O_APPEND holders reposition to EOF (=0) safely
        if lost:
            log.info("rotate_log %s: %d bytes appended during copy were dropped",
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
