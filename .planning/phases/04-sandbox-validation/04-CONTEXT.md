# Phase 4: Sandbox Validation - Context

**Gathered:** 2026-04-17
**Status:** Ready for planning

<domain>
## Phase Boundary

Validate the full pipeline (collect → scan → execute → monitor → reconcile) end-to-end against real platform APIs in sandbox/demo mode with no cross-platform arbitrage execution. Kalshi runs against its demo environment; Polymarket has no sandbox, so minimum-size ($1–5) real orders are required per TEST-02. Single-platform lifecycles only — the first live cross-platform arb trade is Phase 5. Success is measured by TEST-01..04 passing under a structured validation harness with evidence archived.

</domain>

<decisions>
## Implementation Decisions

### Carried Forward from Earlier Phases
- **D-CF-01:** Kalshi dollar-string pricing (`yes_price_dollars`, `count_fp`) — Phase 1 D-15, D-16
- **D-CF-02:** Polymarket via `py-clob-client` with `signature_type` and `funder` — Phase 1 D-02, D-03
- **D-CF-03:** PredictIt execution is out — read-only collector only — Phase 1 D-12..D-14
- **D-CF-04:** FOK enforced at both adapter and engine layers on Kalshi + Polymarket — Phase 2 D-01, D-15
- **D-CF-05:** Orders / fills / incidents persisted to PostgreSQL with full state-transition audit trail — Phase 2 D-02, D-16
- **D-CF-06:** Structured JSON logging via structlog with bound context (`arb_id`, `order_id`, `platform`, `canonical_id`) — Phase 2 D-06, D-19
- **D-CF-07:** Tenacity retry for transient API failures; CircuitBreaker preserved for sustained outages — Phase 2 D-08, D-18
- **D-CF-08:** Kill-switch `/api/kill-switch`, per-market and per-platform exposure limits, per-adapter rate limiting, graceful shutdown cancel-all — Phase 3 SAFE-01..05
- **D-CF-09:** Client order ID (`ARB-{n}-{SIDE}-{hex}`) persisted and threaded through timeout-CANCELLED path — Phase 2.1 CR-01 / CR-02

### Environment & Credential Isolation
- **D-01:** Kalshi demo vs production selected at runtime via `KALSHI_BASE_URL` env var override. Default stays production. Phase 4 `.env.sandbox` sets the demo-api URL. Touches `arbiter/config/settings.py:365` (current hardcoded default).
- **D-02:** Polymarket $1–5 testing blast-radius contained by **both** (a) dedicated test wallet (separate `POLY_PRIVATE_KEY`) funded with ~$10 USDC — hardware cap of $10 if compromised — and (b) adapter-layer config hard-lock `PHASE4_MAX_ORDER_USD=5` enforced before every Polymarket submit. Belt-and-suspenders; both layers must be present during Phase 4 runs.
- **D-03:** Sandbox state lives in a separate Postgres database. Phase 4 `.env.sandbox` points `DATABASE_URL` (or `PG_DATABASE`) at `arbiter_sandbox`. Prod `execution_orders`, `execution_fills`, `execution_incidents` are never polluted with test rows. `docker-compose.yml` adds (or reuses) a second database on the same Postgres instance.
- **D-04:** Sandbox credentials bootstrapped via `.env.sandbox.template` + README section. No interactive setup script for this phase — operator reads template, fills values, runs. Keeps Phase 4 scope tight.

### Scenario Coverage
- **D-05:** Happy-path Kalshi demo lifecycle (submit → fill → record) — TEST-01 core.
- **D-06:** Happy-path Polymarket real-$1 lifecycle (submit → fill → record) — TEST-02 core.
- **D-07:** FOK rejection on thin-liquidity market verified live on both Kalshi demo and Polymarket — proves EXEC-01 no-partial-fills invariant against real exchanges.
- **D-08:** Execution-timeout + cancel-on-timeout verified live on Kalshi demo — the one remaining safety invariant (Phase 2.1 CR-01 remediation) not yet validated against a real exchange. Place demo order with aggressive limit, let timeout fire, assert `list_open_orders_by_client_id` lookup succeeds and the resting order is cancelled.
- **D-09:** All four Phase 3 safety-layer scenarios exercised live: kill-switch trip cancels open orders within 5s (SAFE-01), SIGINT/SIGTERM graceful shutdown cancels open orders before exit (SAFE-05), per-adapter rate-limit backoff under burst load with `rate_limit_state` WS event reflection (SAFE-04), one-leg exposure detection firing structured event + Telegram alert (SAFE-03).
- **D-10:** **Single-platform lifecycles only.** No cross-platform arb execution in Phase 4. Scanner may detect cross-platform opportunities during runs, but engine does not fire both legs. First cross-platform execution belongs to Phase 5.
- **D-11:** Failure triggering is a mix — real conditions where practical (FOK reject via illiquid demo market selection, timeout cancel via aggressive limit price, kill-switch triggered manually during an open demo order, graceful shutdown via SIGINT), fault-injected where impractical (one-leg second-leg failure via adapter mock/patch raising on second call, rate-limit burst via harness that floods the `RateLimiter`). Each scenario in `04-VALIDATION.md` is tagged `real` or `injected`.

### Test Harness & Artifacts
- **D-12:** Primary harness is a dedicated pytest suite with `@pytest.mark.live`. Skipped by default; opt-in via `pytest -m live`. Matches existing project test layout. Each scenario is one test; fixtures handle sandbox DB bootstrap, demo Kalshi client, Polymarket test wallet. Scenarios needing human observation (kill-switch UI behavior, shutdown banner visibility) reuse the Phase 3 UAT-style manual checklist model only as a supplement — pytest remains primary.
- **D-13:** Acceptance artifact is `04-VALIDATION.md` with per-scenario pass/fail + evidence links, structured like Phase 3's `03-VERIFICATION.md`. One row per scenario: linked requirement (TEST-01..04, EXEC-01, EXEC-05, SAFE-01, SAFE-03..05), scenario name, expected, actual, pass/fail, `real` or `injected` tag, evidence path.
- **D-14:** Evidence per scenario: (a) structlog JSON run logs (existing from Phase 2), (b) `execution_orders` / `execution_fills` / `execution_incidents` DB row dumps post-run, (c) platform balance snapshots pre/post (`balances.json`). All stored under `evidence/04/<scenario>/`. No adapter-level HTTP recorder and no VCR cassettes.
- **D-15:** Harness code lives in a new `arbiter/sandbox/` package. Conventional alongside `arbiter/safety/`, `arbiter/audit/`. Contains `conftest.py` (fixtures for sandbox DB, demo Kalshi client, Polymarket test wallet, pre/post balance capture), `test_*.py` per scenario, and any minimal `runbook.py` helpers for manual-observation scenarios.

### Reconciliation Methodology (TEST-03 PnL, TEST-04 Fees)
- **D-16:** Balance snapshots captured pre and post each test scenario via existing `arbiter/monitor/balance.py:BalanceMonitor.fetch_balance()`. Delta computed and compared against recorded PnL from `execution_orders`. Both snapshots persisted as `evidence/04/<scenario>/balances.json`. Existing `arbiter/audit/pnl_reconciler.py` consumes the snapshots.
- **D-17:** PnL tolerance is **±1 cent absolute**. Prediction markets price in cents; sub-cent is rounding. Any discrepancy ≥2¢ is a real bug and fails the scenario.
- **D-18:** Platform-reported fees extracted from Kalshi fill response (`realized_fee`) and Polymarket CLOB order/trade (`fee` field per CLOB docs). Compared against `arbiter/config/settings.py` fee functions (`kalshi_order_fee()`, `polymarket_order_fee()`). Equality asserted within ±1¢ tolerance. Any discrepancy logged as a structured `ExecutionIncident` and surfaced in `04-VALIDATION.md`.
- **D-19:** **Hard gate.** If any Phase 4 scenario exceeds PnL or fee tolerance, `04-VALIDATION.md` marks the phase incomplete and Phase 5 live trading is blocked until the discrepancy is diagnosed. Aligns with PROJECT.md "cannot afford to lose capital to bugs" / "safety > speed."

### Claude's Discretion
- Exact pytest fixture internals for sandbox DB bootstrap and teardown between scenarios
- Thin-liquidity market selection logic for FOK rejection scenarios (which markets to target in Kalshi demo vs Polymarket)
- Structured incident payload shape for fee-discrepancy logs (reusing existing `ExecutionIncident` dataclass)
- `evidence/04/<scenario>/` directory schema and naming conventions
- Implementation mechanics of fault injection for one-leg and rate-limit-burst scenarios (likely `pytest-asyncio` + `unittest.mock.patch` against adapter methods)
- Exact aggressive-limit price strategy for the timeout-cancel scenario (must be realistic enough not to hit but not so far off it trips risk limits)
- docker-compose layout for the sandbox Postgres database (second DB vs second service)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase Scope
- `.planning/ROADMAP.md` §Phase 4 — goal statement + 4 success criteria
- `.planning/REQUIREMENTS.md` §Validation & Testing — TEST-01, TEST-02, TEST-03, TEST-04
- `.planning/PROJECT.md` — core value, constraints (capital <$1K/platform, risk tolerance LOW, timeline ASAP), current state

### Prior Phase Decisions
- `.planning/phases/01-api-integration-fixes/01-CONTEXT.md` — Kalshi dollar-string, Polymarket `py-clob-client` auth, PredictIt removal
- `.planning/phases/02-execution-operational-hardening/02-CONTEXT.md` — FOK, persistence, structlog, tenacity, adapter extraction
- `.planning/phases/02.1-remediate-cr-01-cancel-on-timeout-and-cr-02-client-order-id-/02.1-01-PLAN.md` — CR-01 cancel-on-timeout + CR-02 `client_order_id` persistence (the invariants D-08 validates live)

### Phase 3 Safety Artifacts (what Phase 4 live-fires)
- `.planning/phases/03-safety-layer/03-VERIFICATION.md` — SAFE-01..06 verification status
- `.planning/phases/03-safety-layer/03-HUMAN-UAT.md` — already-identified UAT items blocked on running server (kill-switch ARM/RESET, shutdown banner, rate-limit pills) — Phase 4 closes these on real infrastructure
- `.planning/phases/03-safety-layer/03-PATTERNS.md` — safety-layer implementation patterns
- `.planning/phases/03-safety-layer/03-01-PLAN.md` through `03-08-PLAN.md` — what each safety mechanism does and where it plugs in

### Platform API Docs (researcher to consult)
- Kalshi demo environment URL + auth + demo-account funding procedure (current as of 2026)
- Polymarket CLOB API `fee` field shape in order/trade responses
- Kalshi fill response `realized_fee` field shape
- Polymarket minimum-order-size + USDC funding requirements for test wallet

No external ADRs exist for this project — decisions above are the source of truth.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `arbiter/monitor/balance.py:BalanceMonitor` — already fetches per-platform balances; used directly for pre/post snapshots (D-16)
- `arbiter/audit/pnl_reconciler.py` — existing reconciliation math consumes persisted orders + balance deltas
- `arbiter/audit/math_auditor.py` — shadow fee calculator; useful cross-check for D-18 fee verification
- `arbiter/execution/store.py:ExecutionStore` — exposes `execution_orders`/`execution_fills`/`execution_incidents` access for DB dumps (D-14)
- `arbiter/safety/supervisor.py` — kill-switch trigger surface used by D-09 SAFE-01 scenario
- `arbiter/execution/adapters/kalshi.py`, `arbiter/execution/adapters/polymarket.py` — adapters where D-01 URL swap and D-02 max-order-USD hard-lock are enforced
- `arbiter/config/settings.py:365` (Kalshi `base_url`), `arbiter/config/settings.py:376` (Polymarket `clob_url`) — base URL defaults requiring env-var sourcing per D-01
- `arbiter/utils/logger.py` + Phase 2 structlog migration — produces JSON logs consumed as D-14 evidence
- `arbiter/verify_collectors.py` — existing collector verification script; pattern reference for sandbox scripts

### Established Patterns
- Tests co-located next to modules (`arbiter/<module>/test_*.py`)
- `@pytest.mark` selection for opt-in suites (pattern exists in `conftest.py`)
- structlog JSON output with bound context (`arb_id`, `order_id`, `platform`, `canonical_id`)
- FOK enforced at both adapter and engine layers (invariant Phase 4 validates live)
- Platform adapters as the single surface for platform-specific logic — engine.py stays platform-agnostic
- Phase verification artifacts follow `.planning/phases/<NN>/<NN>-VERIFICATION.md` + evidence files

### Integration Points
- `arbiter/config/settings.py:365, 376` — base URL defaults that Phase 4 swaps via env var (D-01)
- `arbiter/main.py` — DB pool init reads `DATABASE_URL`; sandbox DB swap (D-03) lands here via `.env.sandbox`
- `arbiter/execution/engine.py:1289` — ClobClient `host` parameter already wired through config
- `arbiter/safety/supervisor.py` — SafetySupervisor + `/api/kill-switch` endpoint (SAFE-01) — Phase 4 D-09 fires it live
- `arbiter/execution/adapters/*.py` — where D-02 `PHASE4_MAX_ORDER_USD=5` hard-lock layers on top of existing SAFE-02 RiskManager
- `docker-compose.yml` — requires a second Postgres database / service (or named DB) for sandbox isolation (D-03)

</code_context>

<specifics>
## Specific Ideas

- User consistently chose "Recommended" defaults where options were codebase-informed — signals trust in evidence-backed scoring; keep that style for Phase 5 discussion as well.
- "Hard gate" preferred for reconciliation tolerance — aligns with the explicit PROJECT.md stance that capital loss to bugs is the existential risk, not slow execution.
- Live-fire of CR-01 cancel-on-timeout was initially missed in the scenario selection and was confirmed as in-scope on recheck — the last unverified safety invariant on a real exchange.
- Single-platform scope in Phase 4 is deliberate: avoids Phase 5's concurrent-leg code path sneaking in under a "validation" label; keeps Phase 5 as a clean, explicit go-live gate.

</specifics>

<deferred>
## Deferred Ideas

- **Cross-platform arb execution (real data)** — belongs to Phase 5. Scanner may observe opportunities but engine does not fire both legs.
- **Simulated cross-platform arb on Kalshi demo alone** — not adopted; adds coverage without proving the real cross-venue path.
- **Adapter-level HTTP response recorder middleware** — not adopted; structlog + DB dumps + balance snapshots provide enough audit trail for Phase 4. Revisit if post-mortems need raw request bodies.
- **VCR-style cassette replay for regression** — incompatible with a live-only phase; cassettes go stale the moment a market moves.
- **pytest-html formal CI gating** — OK for later; Phase 4 artifact is `04-VALIDATION.md` narrative first. CI integration is a v2 concern.
- **Interactive `scripts/setup_sandbox.py`** — not this phase. `.env.sandbox.template` + README is enough; revisit if credential setup becomes a recurring friction point.
- **Polling-every-N-seconds balance timeline** — overkill for discrete order lifecycles; revisit if drift debugging emerges in Phase 5.
- **Tiered pass/soft-flag/hard-fail reconciliation gating** — not adopted; hard gate at ±1¢ is simpler and defensible.

</deferred>

---

*Phase: 04-sandbox-validation*
*Context gathered: 2026-04-17*
