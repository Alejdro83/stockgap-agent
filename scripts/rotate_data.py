#!/usr/bin/env python3
"""Maintenance pass: gzip pending JSONL archives, rotate oversized logs, prune.

Run every 10 min by the entrypoint (see DEPLOY.md "Env contract"). Idempotent
and best-effort: every failure is logged and skipped, one stream at a time.
Prune runs FIRST so disk space is freed before new archives are written.

    python3 scripts/rotate_data.py [--data DIR]

Env: JSONL_MAX_MB/JSONL_KEEP_ARCHIVES (the collector renames the live mirror;
this script gzips + prunes those archives), LOG_MAX_MB (bytes threshold as MB,
floats OK — copytruncate for O_APPEND held-fd logs), LOG_KEEP_ARCHIVES.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stockgap import rotation  # noqa: E402

log = logging.getLogger("rotate_data")

def _env_keep(name: str, default: int) -> int:
    """keep counts clamp to >= 1 (a keep=0 would delete the fresh archive)."""
    try:
        v = float(os.environ.get(name, str(default)) or str(default))
        if not math.isfinite(v):
            raise ValueError("non-finite")
        return max(1, int(v))
    except (ValueError, OverflowError):
        return default


def _env_bytes(name: str, default_mb: float) -> int:
    """LOG_MAX_MB floored at 64 KB: 0 must not rotate every log every pass (review-2.8b)."""
    try:
        v = float(os.environ.get(name, str(default_mb)) or str(default_mb))
        if not math.isfinite(v):
            raise ValueError("non-finite")
        return max(64 * 1024, int(v * 1024 * 1024))
    except (ValueError, OverflowError):
        return int(default_mb * 1024 * 1024)


def _live_streams(data: Path) -> list[Path]:
    """Every file that has a live writer: data/*.log, data/logs/*.log, alerts.jsonl.

    Archive names (even uncompressed ones) are NOT live streams (finding 1:
    a failed gzip must not cause double rotation).
    """
    out = []
    for pattern_dir, pattern in ((data, "*.log"), (data / "logs", "*.log")):
        if pattern_dir.is_dir():
            out.extend(p for p in sorted(pattern_dir.glob(pattern))
                       if not rotation.is_archive_name(p.name))
    alerts = data / "alerts.jsonl"
    if alerts.exists():
        out.append(alerts)
    return out

def main(data: Path) -> None:
    log_max = _env_bytes("LOG_MAX_MB", 20)
    log_keep = _env_keep("LOG_KEEP_ARCHIVES", 3)
    jsonl_keep = _env_keep("JSONL_KEEP_ARCHIVES", 5)

    # 1) JSONL stream: the collector already renamed live -> archive.
    #    Prune FIRST (free space before writing new archives), then gzip.
    jsonl = data / "ticks.jsonl"
    try:
        rotation.prune(rotation.find_archives(jsonl), jsonl_keep)
        for arch in rotation.find_archives(jsonl):
            if not arch.name.endswith(".gz"):
                rotation.gzip_pending(arch)      # crash-safe; logs its own errors
    except OSError as exc:
        log.warning("jsonl stream pass failed: %s", exc)

    # 2) Log streams: copytruncate at size, gzip the fresh archive, prune.
    for lf in _live_streams(data):
        try:
            if rotation.rotate_log(lf, log_max):
                for arch in rotation.find_archives(lf):
                    if not arch.name.endswith(".gz"):
                        rotation.gzip_pending(arch)
            rotation.prune(rotation.find_archives(lf), log_keep)
        except OSError as exc:
            log.warning("log stream pass failed for %s: %s", lf, exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(Path(__file__).resolve().parents[1] / "data"))
    main(Path(ap.parse_args().data))
