#!/usr/bin/env python3
"""CLI de stockgap.compaction. Nocturno bajo flock -n -E 0 + ionice -c2 -n7 + nice -n 19.

    python3 scripts/compact_ledger.py [--data DIR] [--dry-run] [--migrate-indexes]
Env: RAW_KEEP_DAYS=14 HOURLY_KEEP_DAYS=180 CROSS_KEEP_DAYS=180 ORACLE_KEEP_DAYS=180.
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stockgap.compaction import compact, load_cfg, migrate_indexes  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("compact_ledger")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=str(Path(__file__).resolve().parents[1] / "data"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--migrate-indexes", action="store_true")
    args = ap.parse_args()
    db = Path(args.data) / "spread_ticks.db"
    if not db.exists():
        log.error("ledger no encontrado: %s", db)
        return 1
    if args.migrate_indexes:
        migrate_indexes(db)
        return 0
    rep = compact(db, load_cfg(), dry_run=args.dry_run)
    log.info("%s %s", "DRY-RUN" if args.dry_run else "OK", rep)
    return 1 if rep["failures"] or rep.get("aborted") else 0     # invariante/abort → exit 1 [F9]


if __name__ == "__main__":
    sys.exit(main())
