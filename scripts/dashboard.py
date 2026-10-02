#!/usr/bin/env python3
"""Launch the StockGap mobile dashboard on 0.0.0.0:8098 (read-only SQLite).

    python3 scripts/dashboard.py        # env: STOCKGAP_DB, DASHBOARD_PORT
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stockgap.dashboard import app, main  # noqa: E402,F401

if __name__ == "__main__":
    main()
