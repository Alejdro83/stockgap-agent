#!/usr/bin/env python3
"""Launch the StockGap mobile dashboard on 0.0.0.0:8098 (read-only SQLite).

    python3 scripts/dashboard.py        # env: STOCKGAP_DB, DASHBOARD_PORT

Observability: set SENTRY_DSN to enable Sentry error tracking (FastAPI
integration); with no SENTRY_DSN this is a clean no-op (see stockgap/telemetry.py).
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from stockgap import telemetry  # noqa: E402

# Sentry toggle (SENTRY_DSN): wired before the app module is imported so the
# FastAPI integration patches the framework before the app is created.
telemetry.init_sentry("stockgap-dashboard", fastapi=True)

from stockgap.dashboard import app, main  # noqa: E402,F401

if __name__ == "__main__":
    main()
