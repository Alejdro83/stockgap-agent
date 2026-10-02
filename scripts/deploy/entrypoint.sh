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
#   (d) tail -F /data/collector.log so the collector's own log (incl.
#       crash-loop tracebacks) reaches container stdout / Northflank logs
#
# Every loop logs to stdout (container logs) AND /data/logs/. SIGTERM/SIGINT
# shut everything down cleanly and FAST (Northflank grace = 30 s):
#   - the collector (setsid'd and reparented to PID 1, so 'wait' can never
#     reap it) is TERMed and POLLED with kill -0 until it exits — its _stop
#     handler gets the time to flush the last tick to the DB + JSONL — and
#     only SIGKILLed after ~20 s;
#   - every loop wrapper is stopped BEFORE its python/flock children are
#     TERMed (so a dying dashboard can't be respawned mid-shutdown) and the
#     real children/grandchildren get the signal, not just the wrapper.
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

# --- /proc helpers (no procps/pgrep/pkill in slim images) ------------------
# Direct children of PID $1.
children_of() {
    local parent="$1" stat rest ppid pid
    for pid in /proc/[0-9]*; do
        stat="$(cat "$pid/stat" 2>/dev/null || true)"
        [ -n "$stat" ] || continue
        rest="${stat##*) }"      # strip "PID (comm)" — comm may contain ')'
        rest="${rest#* }"        # drop the state field
        ppid="${rest%% *}"
        if [ "$ppid" = "$parent" ]; then
            echo "${pid#/proc/}"
        fi
    done
    return 0
}

# PIDs whose cmdline contains $1.
pids_matching() {
    local needle="$1" pid cmd
    for pid in /proc/[0-9]*; do
        cmd="$(tr '\0' ' ' < "$pid/cmdline" 2>/dev/null || true)"
        case "$cmd" in
            *"$needle"*) echo "${pid#/proc/}" ;;
        esac
    done
    return 0
}

# --- secrets bridge -------------------------------------------------------
# stockgap/client.py reads the Binance Web3 credentials from FILES at the
# hardcoded vault path /home/lab-master/.hermes/secrets/{binance_web3_api_key,
# binance_web3_secret_key} — there is no env support in the code. When the
# values arrive as env secrets (Northflank), materialize them once at startup
# (umask 077; write failures are logged, never silently dropped).
# If the files already exist (e.g. mounted secret volume) they are untouched.
if [ -n "${BINANCE_WEB3_API_KEY:-}" ] || [ -n "${BINANCE_WEB3_SECRET_KEY:-}" ]; then
    umask 077
    mkdir -p "$SECRETS_DIR" || log "ERROR: cannot create $SECRETS_DIR"
    chmod 700 "$SECRETS_DIR" 2>/dev/null || log "WARN: cannot chmod 700 $SECRETS_DIR"
    if [ -n "${BINANCE_WEB3_API_KEY:-}" ]; then
        printf '%s' "$BINANCE_WEB3_API_KEY" > "$SECRETS_DIR/binance_web3_api_key" \
            || log "ERROR: failed to write $SECRETS_DIR/binance_web3_api_key"
    fi
    if [ -n "${BINANCE_WEB3_SECRET_KEY:-}" ]; then
        printf '%s' "$BINANCE_WEB3_SECRET_KEY" > "$SECRETS_DIR/binance_web3_secret_key" \
            || log "ERROR: failed to write $SECRETS_DIR/binance_web3_secret_key"
    fi
    chmod 600 "$SECRETS_DIR/binance_web3_api_key" "$SECRETS_DIR/binance_web3_secret_key" 2>/dev/null \
        || log "WARN: cannot chmod 600 the credential files"
    umask 022
    log "Binance Web3 credentials materialized under $SECRETS_DIR"
fi
if [ ! -s "$SECRETS_DIR/binance_web3_api_key" ] || [ ! -s "$SECRETS_DIR/binance_web3_secret_key" ]; then
    log "WARN: Binance Web3 credentials missing — the collector will fail and the watchdog will keep restarting it"
fi
# Secret hygiene: the dashboard is internet-facing (it serves POST /mcp), so
# the bridged values must NOT linger in its environment. Drop them here —
# every loop inherits this environment. (TELEGRAM_BOT_TOKEN is handled at the
# dashboard spawn: `env -u TELEGRAM_BOT_TOKEN`.)
unset BINANCE_WEB3_API_KEY BINANCE_WEB3_SECRET_KEY

# --- lifecycle ------------------------------------------------------------
PIDS=()

# TERM a wrapper and its real python/flock children. The children are captured
# BEFORE the wrapper is TERMed (otherwise they'd be reparented out of reach),
# and the wrapper is TERMed FIRST so it cannot respawn the role we are
# stopping. Grandchildren (flock -> python) are covered too.
term_tree() {
    local parent="$1" kids k gk g
    kids="$(children_of "$parent")"
    kill -TERM "$parent" 2>/dev/null || true
    for k in $kids; do
        gk="$(children_of "$k")"
        kill -TERM "$k" 2>/dev/null || true
        for g in $gk; do
            kill -TERM "$g" 2>/dev/null || true
        done
    done
    return 0
}

# The collector is setsid'd (own session) and reparented to PID 1 — it is not
# a job of this shell, so 'wait' can never cover it. TERM it, then POLL
# kill -0 until it is gone (run_collector._stop flushes the last tick), then
# SIGKILL. Bounded (~20 s) to stay well under Northflank's 30 s grace.
stop_collector() {
    local pids="" p i alive
    if [ -s "$DATA/collector.pid" ]; then
        pids="$(cat "$DATA/collector.pid" 2>/dev/null || true)"
    fi
    pids="$pids $(pids_matching run_collector.py)"
    for p in $pids; do
        [ "$p" = "$$" ] && continue
        kill -TERM "$p" 2>/dev/null || true
    done
    i=0
    while [ "$i" -lt 20 ]; do
        alive=0
        for p in $pids; do
            [ "$p" = "$$" ] && continue
            if kill -0 "$p" 2>/dev/null; then alive=1; fi
        done
        if [ "$alive" -eq 0 ]; then
            log "collector exited cleanly after ${i}s (last tick flushed)"
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    log "collector still alive after ${i}s — SIGKILL"
    for p in $pids; do
        [ "$p" = "$$" ] && continue
        kill -KILL "$p" 2>/dev/null || true
    done
    return 0
}

shutdown() {
    trap - TERM INT
    log "signal received — stopping collector, then dashboard + cron loops"
    stop_collector
    # Wrappers + their real python/flock children (TERM so uvicorn/the
    # collector helpers can exit cleanly; `wait` below reaps them).
    for pid in ${PIDS[@]+"${PIDS[@]}"}; do
        term_tree "$pid"
    done
    # Belt and braces: anything still parented to PID 1 (tee helpers, a
    # collector that raced the watchdog into respawning, tail -F) is finished
    # off so `wait` cannot hang past the platform grace period.
    local kids p gk g
    kids="$(children_of $$)"
    for p in $kids; do
        kill -TERM "$p" 2>/dev/null || true
        gk="$(children_of "$p")"
        for g in $gk; do
            kill -TERM "$g" 2>/dev/null || true
        done
    done
    sleep 1
    for p in $kids; do
        kill -KILL "$p" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    log "shutdown complete"
    exit 0
}
trap shutdown TERM INT

# (a) dashboard — restarted on ANY exit (clean or crash). The old
#     `python3 ... && break` made a single clean exit permanent death; now
#     only shutdown() (which TERMs the wrapper) ends the loop. The dashboard
#     is internet-facing (POST /mcp) so TELEGRAM_BOT_TOKEN is withheld from
#     its environment.
(
    while true; do
        log "starting dashboard on 0.0.0.0:${DASHBOARD_PORT:-8098}"
        env -u TELEGRAM_BOT_TOKEN python3 scripts/dashboard.py \
            || log "dashboard exited rc=$? — restarting in 5s"
        sleep 5
    done
) > >(tee -a "$LOGS/dashboard.log") 2>&1 &
PIDS+=($!)

# (b) collector — started ONCE through the pidfile-guarded watchdog script,
#     then re-checked every 5 min exactly like the host */5 cron.
bash scripts/ensure_collector.sh || log "WARN: initial ensure_collector.sh failed (watchdog will retry in 300s)"
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

# (d) the collector logs to /data/collector.log (its FileHandler plus the
#     watchdog's stdout/stderr redirect) — relay it to container stdout so
#     credential crash-loops are visible in Northflank logs.
touch "$DATA/collector.log"
tail -n 0 -F "$DATA/collector.log" 2>/dev/null &
PIDS+=($!)

log "all processes started (dashboard + collector + watchdog + oracle + alerts + collector-log tail)"
wait || true
