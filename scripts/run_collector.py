#!/usr/bin/env python3
"""Launch the StockGap spread-tick collector (24/7, SQLite + JSONL).

Usage: python3 scripts/run_collector.py
Env:   TICK_INTERVAL (seconds, default 30), STOCKGAP_DB, STOCKGAP_JSONL
Runs forever; safe under nohup/setsid. SIGTERM/SIGINT exit cleanly.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockgap.client import BinanceWeb3Client, load_credentials  # noqa: E402
from stockgap.collector import SpreadCollector  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
DATA.mkdir(exist_ok=True)

DB_PATH = os.environ.get("STOCKGAP_DB", str(DATA / "spread_ticks.db"))
JSONL_PATH = os.environ.get("STOCKGAP_JSONL", str(DATA / "ticks.jsonl"))
TICK_INTERVAL = float(os.environ.get("TICK_INTERVAL", "30"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(DATA / "collector.log"),
    ],
)
log = logging.getLogger("stockgap.runner")


def main() -> int:
    api_key, secret, _, _ = load_credentials()
    client = BinanceWeb3Client(api_key, secret)
    collector = SpreadCollector(
        db_path=DB_PATH,
        jsonl_path=JSONL_PATH,
        client=client,
        tick_interval=TICK_INTERVAL,
        batch_size=50,
        stagger_s=0.25,
        reference_max_age_s=float(os.environ.get("REFERENCE_MAX_AGE_S", "1800")),
    )

    def _stop(signum, _frame):
        log.info("signal %s received, exiting", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    (DATA / "collector.pid").write_text(str(os.getpid()))
    log.info("StockGap collector: interval=%ss db=%s jsonl=%s pid=%s",
             TICK_INTERVAL, DB_PATH, JSONL_PATH, os.getpid())
    collector.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
