#!/bin/sh
# Watchdog: start the StockGap collector if it is not already running.
# Installed in crontab (*/5) so the collector comes back after crashes/reboots.
REPO="/home/lab-master/stockgap-agent"
PIDFILE="$REPO/data/collector.pid"
mkdir -p "$REPO/data"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi

cd "$REPO" || exit 1
# stdout+stderr -> collector.log (never /dev/null): startup/shutdown lines and
# crash-loop tracebacks (e.g. bad credentials) must be visible. run_collector.py
# additionally writes the same file via its logging.FileHandler, so handler
# lines appear twice — accepted cost of never losing a traceback. The entrypoint
# relays the file to container stdout with `tail -F /data/collector.log`.
setsid python3 scripts/run_collector.py </dev/null >>"$REPO/data/collector.log" 2>&1 &
echo $! > "$PIDFILE"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) restarted collector pid=$(cat "$PIDFILE")" >> "$REPO/data/watchdog.log"
