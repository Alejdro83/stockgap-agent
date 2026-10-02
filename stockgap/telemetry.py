"""Sentry observability for StockGap — the toggle is the presence of SENTRY_DSN.

DSN resolution (same env-wins convention as config/alerts.env):
  1. `SENTRY_DSN` environment variable (wins)
  2. `SENTRY_DSN=` line in `config/sentry.env`

No DSN  -> one log line and a clean no-op (default posture).
No sentry-sdk / any init failure -> same graceful no-op; observability must
never take the app down.

Wired into the dashboard entrypoint (FastAPI integration) and the collector
entrypoint (`scripts/run_collector.py`). No DSN is ever hardcoded.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("stockgap.telemetry")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config" / "sentry.env"

_init_state: bool | None = None  # None = not attempted; first call wins per process


def resolve_dsn() -> str | None:
    """Return the Sentry DSN from env (wins) or config/sentry.env, else None."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if dsn:
        return dsn
    try:
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                if key.strip() == "SENTRY_DSN" and value.strip():
                    return value.strip()
    except OSError:
        pass
    return None


def init_sentry(service: str, *, fastapi: bool = False) -> bool:
    """Initialize Sentry iff a DSN resolves. True == initialized, False == no-op.

    `fastapi=True` installs the official FastAPI/Starlette integrations
    (dashboard). The collector runs without a web framework. Idempotent per
    process (entrypoint + app factory both wire in; the first call wins).
    Never raises.
    """
    global _init_state
    if _init_state is not None:
        return _init_state
    dsn = resolve_dsn()
    if not dsn:
        log.info("Sentry disabled (no SENTRY_DSN) — observability no-op.")
        _init_state = False
        return False
    try:
        import sentry_sdk

        integrations = []
        if fastapi:
            from sentry_sdk.integrations.fastapi import FastApiIntegration

            integrations.append(FastApiIntegration())
        sentry_sdk.init(
            dsn=dsn,
            integrations=integrations,
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        )
        sentry_sdk.set_tag("service", service)
    except Exception as exc:  # noqa: BLE001 — observability must never break the app
        log.warning("Sentry init failed (%s) — continuing without it.", exc)
        _init_state = False
        return False
    log.info("Sentry enabled for %s.", service)
    _init_state = True
    return True
