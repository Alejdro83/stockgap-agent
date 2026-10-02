# StockGap — Northflank deployment (all-in-one container)

Everything runs in **ONE container** with **ONE persistent volume mounted at `/data`**
(Northflank volumes are per-service, so dashboard + collector + all crons share it):

| Process | What | Cadence |
|---|---|---|
| dashboard | `scripts/dashboard.py` (FastAPI, read-only SQLite) | continuous, `0.0.0.0:8098` |
| collector | `scripts/run_collector.py` via `scripts/ensure_collector.sh` | tick every `TICK_INTERVAL` (30 s) |
| watchdog | `scripts/ensure_collector.sh` | every 5 min (restarts collector only when dead) |
| oracle | `flock -n data/oracle.lock python3 stockgap/oracle.py --once` | every 5 min |
| alerts | `python3 scripts/alerts.py` (one scan) | every 2 min |

Supervisor: `scripts/deploy/entrypoint.sh` (bash loops + `tee` to stdout and
`/data/logs/`; SIGTERM/SIGINT stop everything cleanly). Inside the container
`REPO/data` is a symlink to `/data`, so every default path (DB, JSONL, logs,
pid/lock files) lands on the volume. The image runs no secrets — all secrets
are injected at start (see below).

Local smoke test:

```bash
docker build -t stockgap .
docker run --rm -p 8098:8098 -v stockgap-data:/data stockgap
curl -fsS localhost:8098/ >/dev/null   # health check = HTTP 200 on /
```

## Env contract

Precedence everywhere: real environment > `config/*.env` file > code defaults
(this is the code's own convention — nothing new was invented).

### Required

| Var | Value / note |
|---|---|
| `STOCKGAP_DB` | `/data/spread_ticks.db` (set in Dockerfile) |
| `STOCKGAP_JSONL` | `/data/ticks.jsonl` (set in Dockerfile; code's JSONL filename is `ticks.jsonl`) |
| `TICK_INTERVAL` | `30` seconds (set in Dockerfile) |
| `DASHBOARD_PORT` | `8098` (set in Dockerfile) |
| `BINANCE_WEB3_API_KEY` | **secret** — Binance Web3 API key. `stockgap/client.py` reads it from the FILE `/home/lab-master/.hermes/secrets/binance_web3_api_key` (hardcoded vault path, no env support in code); `entrypoint.sh` bridges this env var → that file at startup. Alternatively mount the file at that exact path and leave the env unset. |
| `BINANCE_WEB3_SECRET_KEY` | **secret** — same, file `/home/lab-master/.hermes/secrets/binance_web3_secret_key`. |
| `TELEGRAM_BOT_TOKEN` | **secret** — Telegram alert delivery (`config/alerts.env` documents it is injected at runtime, never committed). Without it alert scans still run and file-log; Telegram sends fail. |

### Optional (all default from `config/*.env` / code)

- Observability: `SENTRY_DSN` (toggle; no-op when unset), `SENTRY_TRACES_SAMPLE_RATE`
- Alerts (`config/alerts.env`, override via `STOCKGAP_ALERTS_CONFIG`): `ALERTS_ENABLED`, `ALERT_SPREAD_ENABLED`, `ALERT_CROSS_ENABLED`, `SPREAD_THRESHOLD_BPS`, `CROSS_ISSUER_THRESHOLD_BPS`, `ALERT_RATE_LIMIT_MIN`, `ALERT_SCAN_INTERVAL`, `ALERT_DIGEST_MODE`, `ALERT_DIGEST_INTERVAL_MIN`, `ALERT_DIGEST_MAX`, `ALERT_METRIC` (`real`/`derived`), `TELEGRAM_ALERTS`, `TELEGRAM_CHAT_ID`, `ALERT_FILE_LOG`, `ALERT_LOG`
- Oracle (`config/oracle.env`, override via `STOCKGAP_ORACLE_CONFIG`): `ORACLE_ENABLED`, `ORACLE_INTERVAL_MIN`, `ORACLE_CUTOFF_WEEKDAY`, `ORACLE_CUTOFF_HOUR`, `ORACLE_CUTOFF_MINUTE`, `ORACLE_DEVIATION_FLAG_BPS`, `ORACLE_UNIVERSE_TTL_S`, `ORACLE_BATCH_SIZE`, `ORACLE_STAGGER_S`, `ORACLE_REF_TTL_S`, `ORACLE_REF_MAX_PER_TICK`, `ORACLE_REF_STAGGER_S`
- Collector: `REFERENCE_MAX_AGE_S` (default 1800)
- MCP / pay-per-call (`config/mcp.env`, override via `STOCKGAP_MCP_CONFIG`): `MCP_PAY_MODE`, `B402_PROVIDER`, `MCP_MERCHANT`, `MCP_PRICE_WEI`, `MCP_ASSET`, `MCP_CHAIN_ID`, `MCP_NETWORK`, `MCP_RPC_URL`, `MCP_MAX_BLOCK_AGE_S`, `MCP_ERC8004_ID`
- Executor (off by default): `EXECUTOR_ENABLED`, `MAX_TRADE_USD`, `STOCKGAP_WALLET`, `STOCKGAP_RPC_URL`, `WALLET_PRIVATE_KEY` (runtime-only secret — leave unset)

**Known pitfall:** Northflank secrets must be **linked to the service BEFORE the
first container start**; a secret linked after start needs a service restart to
appear in the environment.

## Northflank resource inventory (read-only check, 2026-10-02)

- Team token scope → project **`StockGap`** (id `stockgap`) already exists — use it, don't create a new project.
- GitHub app already linked (`Alejdro83`, `vcsLinkId 6ac0362d327671b9c4eb4d22`) — likely no "user click" needed for repo access; confirm when the first build runs.
- Available plans include `nf-compute-10/20/100-1/200/400...` (deployment) and build plans; a **build plan** (e.g. `nf-compute-400-16`) is required for combined services.

## Build paths

- **(A) local CLI push** — `docker build` locally, push the image to a container registry, create an image-based deployment service via `nf`/REST. No GitHub dependency, but needs a registry Northflank can pull from (private image ⇒ registry credentials as another secret) and every code change = build+push by hand/script.
- **(B) Northflank GitHub build** — combined service, Dockerfile builder (`/Dockerfile`, workdir `/`) from `Alejdro83/stockgap-agent`, branch `master`; auto-deploy on `git push` (`disabledCD: false`).

**Recommended: (B)** — the GitHub app is already linked (no registry to operate, no image mirroring) and `git push origin master` triggers rebuild+deploy automatically.

## Execution plan (LATER phase — nothing below has been run)

1. `nf login --token <token>` (or `Authorization: Bearer <token without the `NF-` prefix>` on `https://api.northflank.com/v1`) — token file `~/.hermes/secrets/northflank_stockgap_token`.
2. **Secrets first**: `POST /v1/projects/stockgap/secrets` (or `nf create secret`) for `BINANCE_WEB3_API_KEY`, `BINANCE_WEB3_SECRET_KEY`, `TELEGRAM_BOT_TOKEN` (`type: secret`, `secretType: environment`).
3. Create the **combined** service `stockgap-app` in project `stockgap`:
   - build: repo `Alejdro83/stockgap-agent`, branch `master`, Dockerfile `/Dockerfile`, workdir `/`, `vcsLinkId 6ac0362d327671b9c4eb4d22`
   - billing: `deploymentPlan` (e.g. `nf-compute-100-1`) **plus** `buildPlan` (e.g. `nf-compute-400-16`) — billing is mandatory
   - `runtime.ports`: name `http`, port `8098`, protocol `HTTP`, `publicNetworkAccess: true`
   - `docker.configType: "default"` (the Dockerfile CMD is the supervisor)
4. **Volume**: attach a persistent volume to the service, mount path `/data`, ≥10 GB (host data: DB ~356 MB + JSONL ~740 MB and growing; logs rotate nowhere).
5. **Link the three secrets to the service** (`PATCH /v1/projects/stockgap/secrets/{name}` with `{"serviceIds": ["<service-id>"]}`) — BEFORE first start.
6. Health check: HTTP **200 on `/`** (port 8098). (The image also carries a Docker `HEALTHCHECK` doing the same.)
7. Deploy; poll `GET /v1/projects/stockgap/services` until `status.build == SUCCESS` and `status.deployment` is `RUNNING` (`data.services[]` nesting).
8. Verify: `GET /` returns the dashboard; on the volume `/data/spread_ticks.db` is created and growing, `/data/logs/{dashboard,watchdog,oracle-cron,alerts-cron}.log` and `/data/collector.log` are tailing; `nf command-exec` → exactly one `run_collector.py` process (pidfile `/data/collector.pid`).
9. Keep `disabledCD: false` so pushes to `master` auto-rebuild.

## Risks / caveats

- Binance Web3 credentials are file-based at a hardcoded path; the entrypoint bridges `BINANCE_WEB3_*` env → files. Missing both ⇒ collector crash-loops under the watchdog (dashboard keeps working).
- All crons live in one container as sleep loops: cadence drifts by the task duration (minutes-level, fine for 2–5 min crons). Oracle overlap is guarded by `flock -n` exactly like the host.
- A stale `collector.pid` on the volume can't fool the watchdog (entrypoint deletes it at startup), but PID checks only work within the container's PID namespace — correct here.
- SQLite WAL runs on the Northflank volume; `busy_timeout=10s` + `BEGIN IMMEDIATE` (see `stockgap/db.py`) absorb slower storage, but volume I/O latency directly affects tick cadence.
- `/data` grows without rotation (JSONL especially) — monitor volume usage.
- Port 8098 is public; the dashboard is read-only but also serves `POST /mcp` (pay-per-call gate, `MCP_PAY_MODE=free` by default). Restrict network access if this should not be internet-facing.
