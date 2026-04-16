# Phase 2: Execution & Operational Hardening - Context

**Gathered:** 2026-04-16
**Status:** Ready for planning

<domain>
## Phase Boundary

Make the execution engine production-ready for live trading: fill-or-kill orders on Kalshi and Polymarket, PostgreSQL persistence for orders/fills/incidents surviving restart, structured JSON logging via structlog, retry logic via tenacity for transient failures, and per-platform execution adapters extracted from the monolithic engine.py. PredictIt execution is already out of scope (removed in Phase 1).

</domain>

<decisions>
## Implementation Decisions

### Locked Constraints (from REQUIREMENTS.md)
- **D-01:** FOK (fill-or-kill) order types for both legs on both platforms — no partial fills allowed (EXEC-01)
- **D-02:** Execution state (orders, fills, incidents) persisted to PostgreSQL and survives process restart (EXEC-02)
- **D-03:** Pre-trade order book depth verification before submission — confirm sufficient liquidity (EXEC-03)
- **D-04:** Per-platform execution adapters extracted from engine.py into `arbiter/execution/adapters/` — no platform-specific logic remains in engine.py (EXEC-04)
- **D-05:** Execution timeout with automatic cancellation if fill not received within threshold (EXEC-05)
- **D-06:** Structured JSON logging via structlog for all trading operations (OPS-01)
- **D-07:** Sentry error tracking for unhandled exceptions and execution failures (OPS-02)
- **D-08:** Retry logic via tenacity for transient API failures with appropriate backoff (OPS-03)
- **D-09:** Dependency versions upgraded: `py-clob-client` to 0.34.x, `cryptography` to 46.x (OPS-04)

### Carried Forward from Phase 1
- **D-10:** Kalshi uses dollar string format (`yes_price_dollars`, `count_fp`) per Phase 1 D-15, D-16
- **D-11:** Polymarket uses `py-clob-client` SDK with `signature_type` and `funder` params per Phase 1 D-02, D-03
- **D-12:** PredictIt execution code already removed per Phase 1 D-12–D-14 — only Kalshi and Polymarket adapters needed
- **D-13:** Polymarket heartbeat already runs as dedicated async task per Phase 1 D-04 — adapter extraction must not disturb it

### Claude's Discretion (explicitly delegated)

User delegated all four implementation pattern choices to Claude. Decisions must align with the project's "low risk tolerance" and "safety > speed" constraints from CLAUDE.md.

#### Adapter Extraction Pattern
- **D-14:** Claude picks between Protocol/ABC vs thin-wrapper approaches based on engine.py's existing shape. Goal: zero platform-specific logic in engine.py, platform adapters live under `arbiter/execution/adapters/`, engine remains the orchestrator of order lifecycle (state machine, retry, timeout). Interface must support both Kalshi (dollar strings, `count_fp`) and Polymarket (CLOB via py-clob-client) without leaking types.

#### FOK Enforcement
- **D-15:** Claude picks whether FOK is enforced at the adapter layer, engine layer, or both. Must guarantee no partial fills can occur on either platform — this is the core safety invariant.

#### State Persistence
- **D-16:** Claude picks between write-every-state-transition vs write-on-terminal-states. Given "cannot afford to lose capital to bugs" in CLAUDE.md, Claude should bias toward full audit trail on every state change.
- **D-17:** Claude picks restart recovery behavior — either (a) query platforms on startup to reconcile open orders, or (b) flag DB orders in non-terminal state as orphaned and alert operator. Bias toward whichever preserves safety better for the initial release.

#### Retry and Circuit Breaker Layering
- **D-18:** Existing `arbiter/utils/retry.py` `CircuitBreaker` handles sustained outages with 5-failure threshold and 30s recovery — used by collectors. Claude picks whether to keep CircuitBreaker alongside tenacity (layered: tenacity retries transient, CircuitBreaker stops on sustained) or replace CircuitBreaker with tenacity's stop/wait primitives. Must still satisfy OPS-03 (tenacity present for retries).

#### Logging Migration
- **D-19:** Claude picks between full structlog migration (replace stdlib logging in all modules) or structlog-as-processor-chain (stdlib logger calls still work, output becomes JSON). Must produce parseable JSON per OPS-01. Every trading operation must include bound context: `arb_id`, `order_id`, `platform`, `canonical_id` where applicable.

### Claude's Discretion — Broader
- Exact PostgreSQL schema for `orders`, `fills`, `incidents` tables
- asyncpg connection pool sizing and lifecycle
- Backoff strategy tuning per platform (respecting Kalshi 10 writes/sec rate limit per SAFE-04)
- Execution timeout threshold values (starting point, tunable in config)
- Sentry DSN configuration (env var name, sampling rate)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

No external ADRs or design docs exist for this project — requirements are fully captured above and in REQUIREMENTS.md. Platform API docs and library docs should be consulted during the research phase for:

### Platform APIs
- Kalshi API v2 order submission (FOK `time_in_force`, order lifecycle)
- Polymarket CLOB client `py-clob-client` 0.34.x order types and FOK equivalent

### Libraries
- `structlog` 24.x processor chain design, JSON renderer, context binding
- `tenacity` retry primitives (stop, wait, retry_if), async support
- `sentry-sdk` Python 2.x async/Sentry integration, exception capture
- `asyncpg` connection pooling and transaction patterns

### Prior Phase Artifacts
- `.planning/phases/01-api-integration-fixes/01-CONTEXT.md` — Phase 1 decisions locked here (Kalshi dollar strings, Polymarket auth, PredictIt removal)
- `.planning/phases/01-api-integration-fixes/01-01-SUMMARY.md` through `01-05-SUMMARY.md` — what Phase 1 built

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `arbiter/utils/retry.py` — `CircuitBreaker` dataclass already used by all collectors (5-failure threshold, 30s recovery, half-open testing). Can stay alongside tenacity or be replaced.
- `arbiter/utils/logger.py` — `setup_logging()` with stdlib formatter. Migration target for structlog.
- `arbiter/execution/engine.py:235` — `ExecutionEngine` class with good lifecycle logic: order state machine (`OrderStatus` enum with 8 states), `_place_order_for_leg`, `_cancel_order`, re-quote checks, concurrent legs via asyncio. Platform-specific code: `_place_kalshi_order` at line 802 (~170 lines after Phase 1 changes), `_place_polymarket_order` at line 974 (~80 lines).
- `arbiter/execution/engine.py:27-35` — `OrderStatus` enum (PENDING, SUBMITTED, FILLED, PARTIAL, CANCELLED, FAILED, ABORTED, SIMULATED) — already rich enough for state machine persistence.
- `arbiter/collectors/polymarket.py` — ClobClient integration from Phase 1, including heartbeat loop launched from `arbiter/main.py`. Adapter extraction must not break this.

### Established Patterns
- Async-first: aiohttp, asyncpg, asyncio throughout (CLAUDE.md)
- Event-driven: subscribers receive price updates, opportunities, fills, incidents
- Fee-aware: fee functions centralized in `arbiter/config/settings.py` and shadow calculator in `arbiter/audit/math_auditor.py` (both fixed in Phase 1)
- `arbiter/sql/` already exists for PostgreSQL schema per ARCHITECTURE.md

### Integration Points
- `arbiter/main.py` — orchestrator that launches collectors, scanner, execution engine, heartbeat, reconciliation loop. New DB connection pool and Sentry init land here.
- `arbiter/api.py` — dashboard WebSocket/HTTP server subscribes to engine events. Persistence layer must not block subscriber fanout.
- `arbiter/audit/pnl_reconciler.py` — existing reconciliation component; will consume persisted state.
- `arbiter/monitor/balance.py` — existing balance monitor; its data should also be included in the persistence design if relevant to reconciliation.

</code_context>

<specifics>
## Specific Ideas

- User consistently picked "You decide" across all four gray areas — signals high trust in Claude's judgment for this phase. Apply the project's safety-over-speed constraint when making implementation choices.
- No specific product references or "I want it like X" moments came up during discussion.

</specifics>

<deferred>
## Deferred Ideas

- WebSocket price feeds replacing REST polling — v2 requirement OPT-01, not this phase
- Automated kill switch triggers (daily loss threshold, error rate ceiling) — v2 requirement OPT-04, not this phase
- Dynamic fee rate fetching via SDK — already partially addressed in Phase 1 D-09/D-10, remaining work is v2 MON-02
- Settlement divergence monitoring across platforms — v2 requirement MON-01
- Telegram kill/alert integration — SAFE-01 belongs to the Safety & Risk phase, not execution hardening

</deferred>

---

*Phase: 02-execution-operational-hardening*
*Context gathered: 2026-04-16*
