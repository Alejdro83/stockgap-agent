# RECOVERY.md — Failure & recovery report (StockGap evidence pack)

Honest account of what each seat delivered, what went wrong, and what it cost.
Provenance tags: **measured** (command/file evidence), **doc** (recorded in
`DX-LOG.md`/git/repo files), **operator-attested** (asserted by the operator, no
DX-LOG/git basis).

## 1. Deliverables by seat

> **Seat attribution tag: operator-attested** (from the run's roster; only the
> file→module mapping below is measured from the repo and `DX-LOG.md`). Beyond the
> dashboard (Claude) and the spread engine (MiMo subagent), the exact per-module
> seat split is **not recorded** — modules are listed with their real files and
> tests instead of an invented attribution.

| Deliverable | Files | Tests | Seat (operator-attested) |
|---|---|---|---|
| HMAC signer + client + fixtures (Day-0, git `075c3d0`) | `stockgap/signer.py`, `stockgap/client.py`, `scripts/fetch_fixtures.py`, `scripts/fetch_underlying_market.py`, `fixtures/` | `tests/test_signer.py` (15) | not recorded |
| Spread engine (`spread_bps`, `spread_bps_real`) | `stockgap/spread.py` | `tests/test_spread.py` (30), `tests/test_spread_real.py` (24) | MiMo subagent |
| Collector + watchdog | `stockgap/collector.py`, `scripts/run_collector.py`, `scripts/ensure_collector.sh` | `tests/test_collector.py` (14) | not recorded |
| Alerts + summary CLI | `stockgap/alerts.py`, `scripts/alerts.py`, `scripts/summary.py` | `tests/test_alerts.py` (28) | not recorded |
| Weekend oracle + scoring | `stockgap/oracle.py`, `stockgap/oracle_score.py` | `tests/test_oracle.py` (27) | not recorded |
| Executor pipeline (sign/simulate/broadcast, balance) | `stockgap/executor.py`, `stockgap/balance.py`, `scripts/demo_trade_flash.py`, `scripts/probe_quotes.py` | `tests/test_executor.py` (16) | not recorded |
| Dashboard (FastAPI + Jinja2 + HTMX) | `stockgap/dashboard.py`, `stockgap/templates/`, `scripts/dashboard.py` | `tests/test_dashboard.py` (13) | Claude |
| MCP layer + pay gate | `stockgap/mcp_server.py`, `stockgap/b402_gate.py`, `config/mcp.env` | `tests/test_mcp.py` (14), `tests/test_b402.py` (23) | not recorded |
| Coordination, plans, verification, DX-LOG upkeep | `DX-LOG.md` entries, plan/prompt files (outside repo) | — | L.I.S.A. (strategist) |

Suite total: 15+28+23+14+13+16+14+27+30+24+3+4+2 = **213 passed** (measured:
`grep -c 'def test_' tests/*.py` + `python3 -m pytest -q`; the +3 is
`tests/test_telemetry.py` — Sentry `SENTRY_DSN` toggle tests; the +4/+2 are
`tests/test_db.py` and `tests/test_oracle_reliability.py` — SQLite reliability
fix, and test_collector grew 9 → 14 with the resilient tick-loop regression
tests).

## 2. Starting tokens / budget

**not recorded.** No starting token count or budget figure was logged anywhere
(DX-LOG, git, or repo files) and none is estimated here.

## 3. Failures & recovery actions

### 3.1 Wrong-diagnosis incident (flagship) — min-return blamed for what was a 2s route deadline

- **What happened:** the first NVDAB buy broadcast (0.003 BNB,
  `0xfdc9806cd4791e10ca806075b2dfb84d040d414d55cab37ea08f943cf8f32021`,
  block 125205351, gasUsed 519570) reverted on-chain (`status 0x0`); the value was
  returned, only gas was burned (0.00002763 BNB). It was diagnosed as a
  "quoteId-TTL min-return race".
- **The diagnosis was WRONG** and is explicitly retracted in DX-LOG (anchor:
  `Route: expired`, DX-LOG "Quote-and-swap Flash API… TRUE root cause").
- **Recovery:** `eth_call` replay of the exact tx calldata of the two later txs
  (`0x9ca3…`, `0x7c33…`) reverted with
  `execution reverted: Route: expired`; decoding the calldata's uint64 BE
  timestamps (build at byte offset 1696; `valid-until` at 3424/3968 = build + 2s)
  proved a **2-second in-calldata route deadline** for those two txs. For this first
  tx (`0xfdc9…`) the same cause is **INFERRED** (identical pattern — same execution
  depth, auto-slippage — not replayed). Inclusion landed +0.8s / +0.1s
  past the deadline on the two replayed runs; near-identical `gasUsed` (519968 /
  519967) across those two routes = the
  same deterministic `require`. Quote→broadcast alone takes ~2s over the public BSC
  relay — structurally past `valid-until`. Min-return was not the cause of the three
  broadcast reverts (a real simulate failure `Min return not reached` occurred at 1%
  slippage on the first demo attempt; the 8% band was unreachable at this size). The
  wrong diagnosis was retracted by an appended correction; the original text remains
  in DX-LOG.
- **Fixes for a future attempt (none taken — no further sends):** a route with a
  longer deadline; or inclusion inside 2s (private/low-latency relay); or
  executor-side deadline-word detection that aborts cheaply BEFORE broadcast when
  `build_age + expected_inclusion > deadline`.
- Provenance: doc (DX-LOG receipts + decoded evidence chain).

### 3.2 Double-launch incident (operator error) — two extra broadcasts of one approved trade

- **What happened:** the live driver was accidentally launched twice in one
  harness batch (operator error; DX-LOG anchor: `accidentally launched`), so the
  same approved 0.003 BNB NVDAB trade was broadcast twice more:
  `0x9ca395c7db3b277fe3317c45be4ddc7bf1e09755241dff32911a88c42c7c7dfc`
  (nonce 2, block 125207506, gasUsed 519968) and
  `0x7c3379a18636c68c3bb0dc23dbcee6fefcff916baf74c1302ae4a88c4817446f`
  (nonce 3, block 125207520, gasUsed 519967). Both reverted (`status 0x0`), both
  values returned exactly, extra gas burned: 27,948,280,000,000 +
  28,951,469,818,579 = **56,899,749,818,579 wei** (measured balance delta matches
  receipts to the wei).
- **Recovery:** reported in DX-LOG with full receipts and hashes (judges can verify
  both on-chain); wallet state after: 0.011585857234590841 BNB native, 0 NVDAB.
- **Lesson:** one approval = one launch; verify process state before batching live
  commands.

### 3.3 Structural spread-metric bug — 488/488 tokens ≈ 0 bps

- **What happened:** `spread_bps` was structurally ≈0.0 bps across all 488/488
  tokens (~122,000 rows) because `rwa/price.referencePrice` is derived from the
  token price itself (`tokenPrice / tokenToShareRatio`) — a spread computed from
  one endpoint's own pair is ~0 by construction (DX-LOG anchor: `MÉTRICA REAL`).
- **Recovery:** caught by live verification (02:37–02:40 UTC, market open), fixed
  with `spread_bps_real` against the independent close
  (`rwa/underlying-market.referencePrice × tokenToShareRatio`, single normalization
  in `stockgap/spread.py::independent_share_close`). New ticks (251+) carry real
  spreads (e.g. ABNBon +62.59 bps, VRTVon −353.7, ATGEon −186.7 — doc: DX-LOG).
  Historical rows honestly labeled `reference_kind='derived'` with
  `spread_bps_real=NULL` — **no backfill** (the independent close was never
  fetched; honesty over filling in). Alerts measure on the real metric only.

### 3.4 Telegram alert token absent — file-log fallback

- **What happened:** no `TELEGRAM_BOT_TOKEN` was found (DX-LOG anchor: `Telegram`),
  so alert delivery could not use the Telegram transport.
- **Recovery:** delivery runs file-log-only (`FileLogTransport`, always on); the
  Telegram transport is pluggable and goes live with zero code changes once the
  token is dropped into `config/alerts.env` (chat id already defaulted).

### 3.5 Coordinator context loss mid-build *(operator-attested)*

- **What happened:** the coordinator (L.I.S.A.) lost conversation context
  mid-build *(operator-attested; source: operator handoff recap + Hermes
  conversation records, 2026-10-02 — NOT in DX-LOG/git)*.
- **Recovery:** handoff recap + conversation-history search; work resumed at the
  next queued item without redoing completed pieces (the MCP+b402 layer was found
  already delivered on re-check).

## 4. Cost breakdown

| Item | Cost | Status | Source |
|---|---|---|---|
| Strategist seat (L.I.S.A.) tokens/USD | — | **not tracked** | no per-seat metering during the run |
| Builder seat (Claude) tokens/USD | — | **not tracked** | no per-seat metering during the run |
| Executor seat (MiMo subagent) tokens/USD | — | **not tracked** | no per-seat metering during the run |
| On-chain: demo broadcast 1 (tx `0xfdc9…`) | 0.00002763 BNB | measured | DX-LOG receipt (gasUsed 519570 × effectiveGasPrice 53184394 wei) |
| On-chain: demo broadcasts 2+3 (txs `0x9ca3…` + `0x7c33…`) | 56,899,749,818,579 wei | measured | DX-LOG receipts + measured balance delta |
| **On-chain total** | **≈ 0.0000845 BNB** | measured | sum of the above; all spent on reverted attempts — every 0.003 BNB trade value was returned by the reverts |
