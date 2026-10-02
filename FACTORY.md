# FACTORY.md — How the StockGap multi-agent factory worked

Evidence pack for the StockGap hackathon entry (BNB Hack: Tokenized Stocks Edition).
Every quantitative claim below is traceable to DX-LOG.md, git, and commands re-run
on 2026-10-02. Provenance tags used throughout:
**measured** (a command/file in this repo or its live service), **doc** (recorded in
`DX-LOG.md`/git/repo files), **operator-attested** (asserted by the operator, no
DX-LOG/git basis).

## 1. Seats & models

> **Tag: operator-attested.** The roster and who-built-what attribution have no
> DX-LOG or git basis; they are the operator's record of the run.

| Seat | Role | Harness | Model |
|---|---|---|---|
| L.I.S.A. | strategist / coordinator / verifier | Hermes Agent (Telegram) | `xiaomi-mimo/mimo-v2.6-pro` |
| Claude | builder (dashboard, reviews) | `claude -p` CLI | Claude (Anthropic) |
| MiMo subagent | executor (spread engine, modules) | Hermes `delegate_task` | `xiaomi-mimo/mimo-v2.6-pro` |

Who-built-what (operator-attested): Claude built the dashboard; the MiMo subagent
built the spread engine (the per-module split beyond these two attributions is not
recorded — see RECOVERY.md).

## 2. Factory design / work flow

> **Tag for this section's orchestration claims: operator-attested** (the mechanics
> of delegation were not logged in DX-LOG or git).

The coordinator seat (L.I.S.A., Hermes Agent) is the single communication bus:
it receives the plan, decomposes it into atomic tasks, delegates each to a builder
or executor seat with full context, verifies every output before accepting it, and
reports status to the user. Seats never hand work to each other directly — every
handoff goes through the coordinator (coordinator pattern per
`hackathon-factory/references/band-coordination.md`, which was read before writing
this section).

Real orchestration observed in this run:

- **Parallel tracks:** the spread-engine work (Hermes `delegate_task` subagent) ran
  in parallel with the dashboard build (`claude -p`), each on its own files
  (operator-attested attribution).
- **Serialization gate before shared-DB work:** anything touching the shared SQLite
  file was serialized through the coordinator so concurrent writers could not
  collide (operator-attested).
- **Delegation media:** `claude -p` received written prompt/plan files; subagents
  received task specs through Hermes `delegate_task`.
- **Verification loop:** outputs cross-checked by the coordinator (and, per the
  build plan's review gate, by `claude -p` review) before acceptance.

**Inter-seat handoff evidence that actually exists (stated plainly):**

1. **Written plan/prompt files** — e.g. `~/.hermes/plans/2026-10-01_144240-stockgap-tokenized-stocks.md`
   ("StockGap — Tokenized Stocks Spread Agent · Implementation Plan", including its
   `claude -p` review gate) and `~/.hermes/plans/2026-10-02-stockgap-evidence-pack.md`.
2. **`DX-LOG.md` entries** — the developer-experience log each work item appended to.

**No chat-room log exists for this build.** No Band/chat transcript is part of this
repository and none is staged or synthesized here. The plan/prompt files above live
outside the submitted repo tree; they are cited, not copied.

## 3. Verification strategy

- **pytest gate** — full suite must pass before any claim: `python3 -m pytest -q` →
  **202 passed** (measured, this session; `data/pytest_status.json` last write:
  199/0 at 2026-10-02T02:43:26Z).
- **Live demo checks** — `curl http://localhost:8098/` → **200** and
  `curl -X POST http://localhost:8098/mcp` `tools/list` → the three tools
  `get_spread`, `get_cross_issuer`, `get_oracle_latest` (measured, this session).
- **Cross-review** — plans and claims routed through `claude -p` review before
  execution/acceptance (operator-attested; visible in the build plan's review gate).
- **Simulate-before-broadcast rule for trades** — `execute_swap` aborts before
  sign/broadcast whenever a fresh `/pre-transaction/simulate` is not SUCCESS
  (doc: DX-LOG "Real demo trade + `_signable_tx` fix"). One approval = exactly one
  broadcast attempt; a retry requires a new approval (doc: DX-LOG "Lesson").

## 4. Design choices & trade-offs

- **SQLite writer discipline** — the collector writes the tick tables and the oracle
  writes its own tables (`oracle_predictions` + `oracle_reference` via
  `stockgap/oracle.py`; `oracle_reputation` + `oracle_feedback` via
  `stockgap/oracle_score.py`); every read path opens the DB
  read-only: dashboard `mode=ro&immutable=1`
  (`stockgap/dashboard.py`), MCP tools `mode=ro` (`stockgap/mcp_server.py`), summary
  CLI `mode=ro` (`scripts/summary.py`) (measured: grep of connection code). The
  alerts/MCP paths run read-only `mode=ro` except two narrow documented writes per
  DX-LOG: the alerts scanner writes only its own
  watermark rows (`alert_state`), and the MCP module's single write (`paid_calls`)
  occurs only after a verified payment. Trade-off: no concurrent writers, but no
  lock contention either.
- **One consistent price source per metric (provenance rule)** — every price field
  in `spread_ticks`/`cross_issuer_ticks` comes from `/api/v1/dex/market/rwa/price`;
  the ratio is a static property from `rwa/tokens`; every row carries
  `source_endpoint` + `fetched_at` (doc: DX-LOG "Provenance rule"). Trade-off:
  slightly fewer cross-checks per row in exchange for never-ambiguous series.
- **Honest `reference_kind='derived'` labeling instead of backfill** — historical
  rows whose independent close was never fetched are labeled
  `reference_kind='derived'` with `spread_bps_real=NULL`; the independent close was
  never invented (doc: DX-LOG "MÉTRICA REAL de spread" — translated from Spanish:
  "backfill impossible: the independent close was never fetched — honesty over
  filling in"). Trade-off: holes in history, zero fabricated data.
- **Pay-per-call gate defaulting to `free`** — `MCP_PAY_MODE=free|b402` defaults to
  `free` to avoid demo friction; the b402 path (`local_receipt|binance`) is built and
  mock-tested but the `binance` settlement was **not** tested live (merchant
  credentials unavailable) (doc: DX-LOG "Capa agente-a-agente de pago por llamada").

## 5. A bad result caught

**Flagship: the wrong min-return diagnosis of the NVDAB demo-trade reverts.** The
first NVDAB buy broadcast (`0xfdc9806cd4791e10ca806075b2dfb84d040d414d55cab37ea08f943cf8f32021`,
block 125205351) reverted and was diagnosed as a "quoteId-TTL min-return race" — that
diagnosis was **wrong** and is explicitly retracted in DX-LOG (line 258: "the
'quoteId-TTL min-return race' diagnosis was wrong" — translated anchor). Recovery
work — `eth_call` replay of the exact calldata plus decoding the embedded uint64
timestamps (build at byte offset 1696, `valid-until` at 3424/3968 = build + 2s) —
proved the true cause for `0x9ca3…` and `0x7c33…`: a **2-second in-calldata route
deadline** (`execution
reverted: Route: expired`), structurally past by the time inclusion landed
(+0.8s / +0.1s past deadline on those two replayed runs). The same cause for the
first tx (`0xfdc9…`) is **INFERRED** (identical pattern — same execution depth,
auto-slippage — not replayed). Min-return was not the cause of the three broadcast
reverts (a real simulate failure `Min return not reached` occurred at 1% slippage
on the first demo attempt).
This is exactly the factory's verification loop working: a published diagnosis was
re-derived from raw evidence and corrected before submission (doc: DX-LOG
"Quote-and-swap Flash API… TRUE root cause").

**Secondary: the structural spread-metric bug.** `spread_bps` was structurally ≈0
for all 488/488 tokens (~122,000 rows) because `rwa/price.referencePrice` is derived
from the token price itself (`tokenPrice / tokenToShareRatio`) — caught by live
verification during the 02:37–02:40 UTC check and fixed with `spread_bps_real`
against the independent close (`rwa/underlying-market`), with historical rows
honestly labeled `derived`, no backfill (doc: DX-LOG "MÉTRICA REAL de spread").

**Optional third (operator-attested):** an unverified-claims review incident — the
coordinator's cross-check flagged claims that lacked evidence before they reached
the submission docs (operator-attested; no DX-LOG anchor).

## 6. Measured resources

Only sourceable timings are cited (file mtimes, git timestamps,
and the recorded live-check window; no PIDs, no unsourced wall-times):

| Timing | Value | Source |
|---|---|---|
| Git commit `075c3d0` ("Day-0: HMAC signer, live credential verification, RWA fixtures") | 2026-10-02 00:13:06 +0000 | `git log` (measured) |
| `stockgap/dashboard.py` mtime | 2026-10-02 02:26:10 UTC | `stat` (measured) |
| `stockgap/mcp_server.py` mtime | 2026-10-02 02:26:10 UTC | `stat` (measured) |
| `stockgap/b402_gate.py` mtime | 2026-10-02 02:25:48 UTC | `stat` (measured) |
| Live metric verification window | 02:37–02:40 UTC, 2026-10-02 (market open) | DX-LOG (doc) |
| Full suite run (latest, post-Sentry-wiring) | 202 passed in 5.79s | `python3 -m pytest -q` (measured) |

- Token / LLM usage per seat: **not tracked (no per-seat metering during the run)**.
- USD cost per seat: **not tracked** — no metering was enabled and nothing was
  recorded; no estimate is offered.
- On-chain spend actually measured: ≈ **0.0000845 BNB**, all gas on reverted demo
  attempts (receipts + measured balance delta in DX-LOG; see `budgets.json` and
  `RECOVERY.md`).

## 7. Failure recovery

Five real incidents are documented with their recovery actions in
[RECOVERY.md](./RECOVERY.md): the wrong-diagnosis incident above (flagship), the
operator-error double-launch that produced two extra reverted broadcasts
(`0x9ca395c7…`, `0x7c3379a1…`), the structural spread-metric bug, the missing
Telegram alert token (file-log fallback), and a coordinator context loss mid-build
(operator-attested) recovered via handoff recap + conversation-history search with
no rework of completed pieces. Cost breakdown (honest `not tracked` rows plus the
measured on-chain gas) is there too.
