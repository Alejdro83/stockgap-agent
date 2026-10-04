# StockGap hybrid sync — OWNER RUNBOOK (Task 2)

> Owner-facing runbook for the hybrid collector sync (plan `2026-10-04-hybrid-collector-sync` v3/v4, Tasks 1–5). Every command and flag below is cross-checked against the code: `scripts/ship_rows.py`, `stockgap/ingest.py`, `scripts/deploy/entrypoint.sh`, `scripts/deploy/preflight_ingest.py`, `config/ingest.env.example`.
>
> **Who does what:** the OWNER (**Alex**) alone runs `git push`, the Northflank deploy, the cutover (§2) and the crontab install (§4). The implementer never pushes and never installs the crontab line.

**The system in one paragraph:** the collector runs on THIS host (Binance Web3 only works from the residential IP) and writes rows to SQLite `data/spread_ticks.db`. `scripts/ship_rows.py` — one-shot, cron-driven, **self-locking** via `fcntl` on `data/ship.lock` (no `flock` wrapper anywhere) — ships new rows to the NF dashboard over `POST /ingest/rows` (Bearer `STOCKGAP_INGEST_TOKEN`; idempotent `INSERT OR IGNORE` on the append tables, real upsert-by-PK on the snapshot tables). The watermark `data/ship_meta.json` is **born at pass start** (atomic tmp + fsync + rename) and advances only after each 2xx. The NF pod runs `STOCKGAP_MODE=dashboard` (fetch loops off, rotation + collector-log tail on) and `scripts/deploy/preflight_ingest.py` **fail-closes the pod start** if the token is missing/weak (≥ 32 chars, printable ASCII 33–126).

---

## 1 — Setup (before anything ships)

### STEP 1 — "GENERATE-TOKEN" (owner, on this host)

Generate a strong token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

(64 chars of URL-safe ASCII — every char is in the accepted printable-ASCII 33–126 range, and comfortably over the 32-char floor that `stockgap/ingest.py` and `scripts/deploy/preflight_ingest.py` enforce.)

Store it **locally** in `config/ingest.env`:

```bash
cd /home/lab-master/stockgap-agent
cp config/ingest.env.example config/ingest.env
chmod 600 config/ingest.env
# edit it: set STOCKGAP_INGEST_URL (the NF service base URL) and STOCKGAP_INGEST_TOKEN
```

Rules (enforced by the code where enforceable):
- `STOCKGAP_INGEST_URL` must be an `https://` URL — the token rides every request as a `Bearer` header (token on the wire).
- `config/ingest.env` is **gitignored** (`.gitignore` line: `config/ingest.env`) — **NEVER commit it, NEVER paste the token into chat, tickets or logs.**
- The token is **NEVER passed via argv** — `scripts/ship_rows.py` refuses ANY argv except `--reset` (a token in argv would be visible in `ps` and `crontab -l`) and never logs the value. Real environment variables override the file values (`STOCKGAP_INGEST_URL`, `STOCKGAP_INGEST_TOKEN`, `STOCKGAP_DB`, `SHIP_*`).
- The same value must exist on both sides: local `config/ingest.env` **and** the NF secret (STEP 2).

### STEP 2 — "LINK-NF-SECRET" (owner, Northflank — BEFORE the pod starts)

Create the Northflank secret **`STOCKGAP_INGEST_TOKEN`** with the exact same value as `config/ingest.env` and **link it to the StockGap service BEFORE the container starts**.

**Also set the runtime env var `STOCKGAP_MODE=dashboard` on the StockGap service (service → variables), BEFORE the deploy.** The Dockerfile does **not** set it, so an unset variable means `full` mode: the pod would run the in-container collector/oracle/alerts loops (a second DB writer — the exact lineage collision this cutover exists to prevent) and skip the token preflight entirely. Unset ≠ dashboard — set it explicitly.

> **KNOWN PITFALL (recorded deliberately):** Northflank injects secrets into a container **at start**. A secret linked to an already-running service is invisible to it — **a restart is needed**. And if the token is missing/weak at start, `scripts/deploy/preflight_ingest.py` fail-closes the pod: startup aborts before anything serves (one clear stderr line, the value never printed). If the token is simply not linked while the pod runs, `POST /ingest/rows` answers **503 "ingest disabled"** and the shipper exits with code 5 telling you the token is not linked on NF.

Valid = present, **≥ 32 chars**, printable ASCII **33–126** only (no whitespace, no control chars) — same rule on both sides.

### STEP 3 — "OWNER-PUSH-AND-DEPLOY" (owner only)

- The OWNER (Alex) alone runs `git push` and owns the Northflank deploy. **The implementer never pushes.**
- Deploy **before** the first ship: if the NF schema lags the local one, the ingest endpoint answers `422 "NF schema older than local — deploy first"` (hard fatal in the shipper, by design).
- **Verify `STOCKGAP_MODE` from the container log** (STEP 2 set it; here you prove it reached the pod): the pod must log `MODE=dashboard: fetch loops disabled (hybrid: data arrives via /ingest)`. If it logs the full-mode line instead (`all processes started (dashboard + collector + watchdog + oracle + alerts + collector-log tail)`), **STOP — do not ship**: `STOCKGAP_MODE=dashboard` is not set on the service. Set it (STEP 2), restart/redeploy, and re-check the log line before anything ships. Unset/`full` is the pre-hybrid behavior, exactly — and that is **not** a dashboard pod.

---

## 2 — CUTOVER — **MANDATORY before the first ship**

### STEP 4 — "ARCHIVE-THE-NF-DB" (all three SQLite files)

**Why this is mandatory, not hygiene:** the strict fresh-start preflight REFUSES to ship into any non-empty append table, and §6 documents the lineage case **no preflight can detect**. Archiving the NF DB is the only step that actually guarantees a clean NF side. Do it **before** the first ship — never as cleanup after a REFUSE.

- **Volume headroom first (before cutover):** `df -h /data` — the volume needs free space ≥ 2× the local DB size (the backfill+ship and the archived copies need headroom).

1. **Stop the StockGap service on Northflank** (console → service → stop) so nothing can write while you archive.
2. On the NF volume (`STOCKGAP_DB=/data/spread_ticks.db`, set in the Dockerfile — see `DEPLOY.md`), via `nf command-exec` or any shell with the volume mounted, archive **ALL THREE** SQLite files **together**:

```bash
ARCH=/data/db-archive/cutover-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$ARCH"
for f in /data/spread_ticks.db /data/spread_ticks.db-wal /data/spread_ticks.db-shm; do
  [ -e "$f" ] && mv "$f" "$ARCH"/
done
ls -la "$ARCH"
```

**All three files: `spread_ticks.db` + `spread_ticks.db-wal` + `spread_ticks.db-shm`.** Move them TOGETHER: after a hard stop the `-wal` can hold committed rows that are not in the main file, and a stale `-wal`/`-shm` left beside a fresh DB gets replayed into it — foreign rows sneaking past a "clean" cutover. After a clean stop SQLite may already have removed `-wal`/`-shm`; the `[ -e ... ]` guard covers that. **Never leave one of the three behind if it exists.**

3. **Start the service again.** First-ship expectation (STEP 5): `GET /ingest/state` now reports `max_id: 0` (or `null` for the key-PK tables) for every append table — a **clean** NF.

---

## 3 — First ship

### STEP 5 — "FIRST-SHIP-AND-VERIFY" (owner, on this host)

Optional shadow pass first (reports what would ship; **no network calls at all**, writes no watermark, skips the preflight — so it cannot validate the NF side; only the real run does):

```bash
cd /home/lab-master/stockgap-agent
SHIP_DRY_RUN=1 python3 scripts/ship_rows.py
```

The real first ship:

```bash
cd /home/lab-master/stockgap-agent
python3 scripts/ship_rows.py
```

**What to expect (all tested in `tests/test_ship_rows.py`):**
- **Clean NF (after §2):** the preflight (`GET /ingest/state`) sees every append table empty → the pass proceeds and **`data/ship_meta.json` is born before the first POST** (its starting floor: the `SHIP_BACKFILL_HOURS` window — default 4 h, clamped to the 2–6 h band, per table's ts column — or an explicit `SHIP_BACKFILL_FROM_ID=N` to ship ids ≥ N; never id 0 by default).
- **Any non-empty append table on NF:** **REFUSE** (exit 3) with a message pointing at this runbook's §2. `SHIP_FORCE=1` overrides **ONLY** that one REFUSE (dangerous — see §6). `--reset` is the **only automatic recovery**: same-lineage inference (NF `MAX(id)` ≤ local `MAX(id)` on every non-empty append table → proceed with a LOUD warning). On the `--reset` path, NF `MAX(id)` **>** local `MAX(id)` refuses **even with `SHIP_FORCE=1`**.
- The watermark advances to the batch max id after **each** 2xx and never otherwise; a crash after a POST is safe — the re-run re-POSTs idempotently (append = `INSERT OR IGNORE` with explicit id; snapshot = real upsert-by-PK) and, when a valid watermark exists, skips the preflight entirely.
- A summary line is logged only when something shipped or failed. Exit codes: `0` ok/no-op (incl. a second instance finding `data/ship.lock` held), `1` watermark/sanity, `2` config/argv, `3` preflight refuse, `4` fatal HTTP/retries exhausted, `5` NF "ingest disabled".

**Verify** — compare `GET /ingest/state` per-table `max_id` against the local `MAX(id)` (the token never touches argv, `ps` or the log):

```bash
cd /home/lab-master/stockgap-agent
python3 - <<'PY'
# Prints the NF ingest state next to the local MAX(id) values. The token is
# read from config/ingest.env and never touches argv, ps or the log.
import json, sqlite3, urllib.request

vals = {}
for line in open("config/ingest.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip().strip('"').strip("'")
url = vals["STOCKGAP_INGEST_URL"].rstrip("/")
req = urllib.request.Request(url + "/ingest/state", method="GET")
req.add_header("Authorization", "Bearer " + vals["STOCKGAP_INGEST_TOKEN"])
state = json.load(urllib.request.urlopen(req, timeout=20))
con = sqlite3.connect("file:data/spread_ticks.db?mode=ro", uri=True)
equal = True
for table in ("spread_ticks", "cross_issuer_ticks", "oracle_predictions"):
    nf = state["tables"][table]["max_id"]
    try:
        local = con.execute(f'SELECT MAX("id") FROM "{table}"').fetchone()[0] or 0
    except sqlite3.OperationalError:
        local = "table missing locally"
    print(f"{table}: NF max_id={nf}  local MAX(id)={local}")
    equal = equal and nf == local
print("OK: NF matches the local MAX(id) values above" if equal else
      "not equal: rows still draining (re-run) or foreign rows on NF")
PY
```

A fully drained pass leaves NF `max_id` == the local `MAX(id)` values (the collector keeps ticking, so re-run the shipper or compare again in a minute if they moved). `data/ship_meta.json` must exist after the first real pass — its absence would prove no POST ever happened.

---

## 4 — Steady state

### STEP 6 — "ENABLE-CRON" (post-green OWNER action — the line is READY, **NOT installed**)

**Only after the deploy is green and STEP 5 verified.** Exact crontab line, ready to paste (`crontab -e`):

```
* * * * * cd /home/lab-master/stockgap-agent && /usr/bin/python3 scripts/ship_rows.py >> data/ship.log 2>&1
```

- **No `flock` wrapper** — the shipper self-locks (`fcntl` on `data/ship.lock`); a `flock` wrapper would hold the lock and make every pass exit doing nothing. A second instance exits 0 doing nothing.
- `data/ship.log` **is covered** by `scripts/rotate_data.py`'s `data/*.log` rotation/prune patterns (pinned by `tests/test_ship_rows.py::test_ship_log_covered_by_rotate_data_patterns`).
- One-shot per invocation; the drain loop runs under the per-pass wall-clock budget `SHIP_BUDGET_S` (default 50 s) so passes end before the next cron tick.
- **This runbook's implementer did NOT install this line.** Installing it is a post-green owner action — do not run it against a non-green deploy.

---

## 5 — Rollback

### STEP 7 — "ROLLBACK-TO-FULL" (owner)

1. **Stop shipping:** remove (or comment out) the STEP 6 crontab line (`crontab -e`).
2. **Re-point `config/ingest.env`:** point `STOCKGAP_INGEST_URL` where you actually want shipping, or neutralize the file (delete `STOCKGAP_INGEST_TOKEN`/`STOCKGAP_INGEST_URL` — the shipper then exits `2` with a clear config error and never touches the network). The file is read on every pass; there is no other local state to undo except the (harmless, still-valid) `data/ship_meta.json`.
3. **On NF:** set **`STOCKGAP_MODE=full`** (or unset it) and restart/redeploy — the fetch loops (collector + watchdog, oracle, alerts) resume on NF, exactly the pre-hybrid behavior (the `full` path is byte-identical to the historical entrypoint).
4. **What happens to already-shipped rows: nothing — and that is safe.** Rollback deletes nothing. Re-shipping any of them is idempotent: append tables (`spread_ticks`, `cross_issuer_ticks`, `oracle_predictions`) are `INSERT OR IGNORE` with explicit ids (replays are no-ops, history is unrewritable); snapshot tables (`oracle_reputation`, `oracle_reference`, `collector_meta`) are real upsert-by-PK (a re-ship replaces/heals, e.g. the `oracle_reputation.feedback_ref` NULL-heal).
5. **Going back to hybrid later:** the old watermark is still valid for the local lineage, but once the NF fetch loops have written **their own** rows into the same tables, the NF DB holds a mixed lineage — which is exactly the §2 case again. **Re-run the §2 cutover (all three DB files) before the next first ship.**

---

## 6 — CRITICAL: what the preflight can NOT detect

Read this before overriding anything.

- The preflight only compares **`MAX(id)` values**. **A foreign lineage on NF whose `MAX(id)` values are ≤ local `MAX(id)` at fresh start PASSES `--reset`'s same-lineage rule** — its rows are indistinguishable from this host's own (a mixed lineage can hide at identical maxima; there is no content/tick_id/timestamp sniffing). Shipping alongside such rows collides by id, and `INSERT OR IGNORE` then **silently drops** the colliding rows.
- The PLAIN fresh start refuses **any** non-empty append table precisely because low maxima prove nothing — and `SHIP_FORCE=1` (the single explicit override for that one REFUSE) is just as blind; it ships into a non-empty NF whose rows cannot be proven this lineage's. On the `--reset` path the only automatic recovery, the same blind spot exists (that is why it prints a LOUD indistinguishability caveat), and its one hard stop — NF `MAX(id)` > local `MAX(id)` — does not fire when the foreign maxima are lower.

**Therefore the §2 cutover (archive the NF DB — all THREE files — before the first ship) is MANDATORY.** It is the only step that guarantees the NF side is clean; no flag, override or inference can substitute for it.

---

## Appendix — shipper knobs (all in `config/ingest.env`, real env vars override)

| Key | Default | Meaning |
|---|---|---|
| `STOCKGAP_INGEST_URL` | — (required) | NF dashboard base URL |
| `STOCKGAP_INGEST_TOKEN` | — (required, ≥ 32 chars) | Bearer token = the NF secret from STEP 2 |
| `STOCKGAP_DB` | `<repo>/data/spread_ticks.db` | local collector DB |
| `SHIP_BATCH` | 500 (max 1000) | rows per POST; on 413, halve it |
| `SHIP_BACKFILL_HOURS` | 4 (clamped 2–6) | fresh-start/`--reset` backfill window |
| `SHIP_BACKFILL_FROM_ID` | — | explicit floor: ship ids ≥ N (≥ 1) |
| `SHIP_FORCE` | 0 | overrides ONLY the plain fresh-start REFUSE (dangerous — §6) |
| `SHIP_DRY_RUN` | 0 | shadow run: no network calls, no watermark |
| `SHIP_BUDGET_S` | 50 | per-pass drain budget |
| `--reset` (argv) | — | the ONLY accepted argv; explicit same-lineage recovery |
