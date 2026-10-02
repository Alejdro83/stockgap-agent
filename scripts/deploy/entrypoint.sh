#!/usr/bin/env bash
# StockGap all-in-one supervisor (Northflank single container).
#
# Runs, in ONE container sharing ONE volume mounted at /data:
#   (a) the FastAPI dashboard on 0.0.0.0:$DASHBOARD_PORT (8098)
#   (b) the spread-tick collector — exactly one instance, via
#       scripts/ensure_collector.sh (pidfile guard), with a watchdog loop
#       that restarts it ONLY when dead (same as the host */5 cron)
#   (c) in-container cron loops replicating the host crontab:
#         */5  flock -n data/oracle.lock python3 stockgap/oracle.py --once
#         */2  python3 scripts/alerts.py
#         */5  scripts/ensure_collector.sh
#
# Every loop logs to stdout (container logs) AND /data/logs/. SIGTERM/SIGINT
# shut everything down cleanly (the collector exits cleanly on SIGTERM).
set -euo pipefail

REPO="/home/lab-master/stockgap-agent"
DATA="/data"
LOGS="$DATA/logs"
SECRETS_DIR="/home/lab-master/.hermes/secrets"

cd "$REPO"
mkdir -p "$LOGS"
mkdir -p "$DATA"

# A stale collector.pid left on the persistent volume by a previous container
# must never fool the watchdog (PID namespaces restart at 1 each boot).
rm -f "$DATA/collector.pid"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [entrypoint] $*"; }

# --- secrets bridge -------------------------------------------------------
# stockgap/client.py reads the Binance Web3 credentials from FILES at the
# hardcoded vault path /home/lab-master/.hermes/secrets/{binance_web3_api_key,
# binance_web3_secret_key} — there is no env support in the code. When the
# values arrive as env secrets (Northflank), materialize them once at startup.
# If the files already exist (e.g. mounted secret volume) they are untouched.
if [ -n "${BINANCE_WEB3_API_KEY:-}" ] || [ -n "${BINANCE_WEB3_SECRET_KEY:-}" ]; then
    mkdir -p "$SECRETS_DIR"
    chmod 700 "$SECRETS_DIR"
    if [ -n "${BINANCE_WEB3_API_KEY:-}" ]; then
        printf '%s' "$BINANCE_WEB3_API_KEY" > "$SECRETS_DIR/binance_web3_api_key"
    fi
    if [ -n "${BINANCE_WEB3_SECRET_KEY:-}" ]; then
        printf '%s' "$BINANCE_WEB3_SECRET_KEY" > "$SECRETS_DIR/binance_web3_secret_key"
    fi
    chmod 600 "$SECRETS_DIR"/binance_web3_api_key "$SECRETS_DIR"/binance_web3_secret_key 2>/dev/null || true
    log "Binance Web3 credentials materialized under $SECRETS_DIR"
fi
if [ ! -s "$SECRETS_DIR/binance_web3_api_key" ] || [ ! -s "$SECRETS_DIR/binance_web3_secret_key" ]; then
    log "WARN: Binance Web3 credentials missing — the collector will fail and the watchdog will keep restarting it"
fi

# --- lifecycle ------------------------------------------------------------
PIDS=()
shutdown() {
    trap - TERM INT
    log "signal received — stopping dashboard + cron loops + collector"
    for pid in ${PIDS[@]+"${PIDS[@]}"}; do
        kill "$pid" 2>/dev/null || true
    done
    if [ -s "$DATA/collector.pid" ]; then
        cpid="$(cat "$DATA/collector.pid" 2>/dev/null || true)"
        if [ -n "${cpid:-}" ]; then
            kill "$cpid" 2>/dev/null || true
        fi
    fi
    wait 2>/dev/null || true
    log "shutdown complete"
    exit 0
}
trap shutdown TERM INT

# (a) dashboard — restarted on crash; a clean exit (SIGTERM) ends the loop.
(
    while true; do
        log "starting dashboard on 0.0.0.0:${DASHBOARD_PORT:-8098}"
        python3 scripts/dashboard.py && break
        log "dashboard exited non-zero — restarting in 5s"
        sleep 5
    done
) > >(tee -a "$LOGS/dashboard.log") 2>&1 &
PIDS+=($!)

# (b) collector — started ONCE through the pidfile-guarded watchdog script,
#     then re-checked every 5 min exactly like the host */5 cron.
bash scripts/ensure_collector.sh
(
    while true; do
        sleep 300
        if ! bash scripts/ensure_collector.sh; then
            log "watchdog: ensure_collector.sh failed (will retry in 300s)"
        fi
    done
) > >(tee -a "$LOGS/watchdog.log") 2>&1 &
PIDS+=($!)

# (c1) oracle — every 5 min under flock (same command/flags as the host cron).
(
    while true; do
        flock -n data/oracle.lock python3 stockgap/oracle.py --once \
            || log "oracle: run failed or lock busy (next try in 300s)"
        sleep 300
    done
) > >(tee -a "$LOGS/oracle-cron.log") 2>&1 &
PIDS+=($!)

# (c2) alerts — one scan every 2 min (same as the host */2 cron).
(
    while true; do
        python3 scripts/alerts.py \
            || log "alerts: scan failed (next try in 120s)"
        sleep 120
    done
) > >(tee -a "$LOGS/alerts-cron.log") 2>&1 &
PIDS+=($!)

log "all processes started (dashboard + collector + watchdog + oracle + alerts)"
wait || true
