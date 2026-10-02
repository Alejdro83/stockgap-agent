# StockGap all-in-one image: dashboard + collector + in-container cron
# supervisor in ONE container (Northflank volumes are per-service).
# See scripts/deploy/entrypoint.sh and DEPLOY.md for the full contract.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STOCKGAP_DB=/data/spread_ticks.db \
    STOCKGAP_JSONL=/data/ticks.jsonl \
    TICK_INTERVAL=30 \
    DASHBOARD_PORT=8098

# flock (oracle cron lock) and setsid (ensure_collector.sh) come from
# util-linux, which is part of the Debian base — assert at build time.
RUN command -v flock >/dev/null && command -v setsid >/dev/null

WORKDIR /home/lab-master/stockgap-agent

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The code uses REPO/data for the DB, logs and pid files. In the container
# that directory IS the persistent volume /data (STOCKGAP_* point there too).
# /data itself is created at runtime by the entrypoint (or the volume mount).
RUN rm -rf data && ln -s /data data \
    && chmod +x scripts/deploy/entrypoint.sh scripts/ensure_collector.sh

EXPOSE 8098

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8098/',timeout=5).status==200 else 1)"

CMD ["scripts/deploy/entrypoint.sh"]
