# Roadmap: Arbiter Dashboard

## Overview

Take the existing untested arbitrage system from "code that compiles" to "system that trades live without losing money." The path is: fix broken API integrations that block all trading, harden execution and operational infrastructure, layer on safety mechanisms, validate everything in sandbox environments, then execute the first live arbitrage trade under operator supervision.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: API Integration Fixes** - Fix hard-blocker API issues (Kalshi pricing, Polymarket auth, PredictIt scoping, fee math, collector verification)
- [ ] **Phase 2: Execution & Operational Hardening** - Make the execution engine production-grade with FOK orders, state persistence, structured logging, and retry logic
- [ ] **Phase 3: Safety Layer** - Build the safety net: kill switch, position limits, one-leg recovery, rate limiting, graceful shutdown
- [ ] **Phase 4: Sandbox Validation** - Validate the full pipeline against sandbox/demo APIs with real API calls and small orders
- [ ] **Phase 5: Live Trading** - Execute the first live arbitrage trade with real money under operator supervision

## Phase Details

### Phase 1: API Integration Fixes
**Goal**: All platform API calls succeed -- collectors return real data, order submission formats are correct, authentication works end-to-end
**Depends on**: Nothing (first phase)
**Requirements**: API-01, API-02, API-03, API-04, API-05, API-06, API-07
**Success Criteria** (what must be TRUE):
  1. Kalshi order payload uses `yes_price_dollars` string format and `count_fp` for fractional markets -- a test order to demo env does not return 400/422
  2. Polymarket ClobClient initializes with correct `signature_type` and `funder` -- `client.get_api_keys()` succeeds without 401 errors
  3. Polymarket heartbeat manager runs as a dedicated async task sending keepalive every 5 seconds -- observable in logs during a 60-second session
  4. Fee calculations for all platforms match documented rates (Polymarket per-category, Kalshi per current schedule) -- unit tests pass with real rate values
  5. All three collectors successfully fetch and parse current market data from their respective live APIs without errors
**Plans:** 5 plans

Plans:
- [x] 01-01-PLAN.md -- Fix Polymarket fee rate constants and update tests
- [x] 01-02-PLAN.md -- Migrate Kalshi order format to dollar string pricing
- [x] 01-03-PLAN.md -- Remove PredictIt execution code (keep collector)
- [x] 01-04-PLAN.md -- Fix Polymarket ClobClient auth and add heartbeat task
- [ ] 01-05-PLAN.md -- Verify all collectors against live APIs

### Phase 2: Execution & Operational Hardening
**Goal**: The execution engine reliably places, monitors, and records orders with proper error handling, logging, and recovery from transient failures
**Depends on**: Phase 1
**Requirements**: EXEC-01, EXEC-02, EXEC-03, EXEC-04, EXEC-05, OPS-01, OPS-02, OPS-03, OPS-04
**Success Criteria** (what must be TRUE):
  1. Orders are submitted as fill-or-kill (FOK) on both Kalshi and Polymarket -- no partial fills can occur
  2. All execution state (orders, fills, incidents) is persisted to PostgreSQL and survives a process restart -- restarting the service shows previous orders intact
  3. Structured JSON logs (via structlog) are emitted for every trading operation -- log output is parseable by standard JSON tools
  4. Transient API failures (timeout, 503) trigger automatic retry with exponential backoff -- observable in logs during a simulated failure
  5. Per-platform execution adapters exist as separate modules under execution/adapters/ -- no platform-specific logic remains in engine.py
**Plans**: TBD

Plans:
- [ ] 02-01: TBD
- [ ] 02-02: TBD
- [ ] 02-03: TBD

### Phase 3: Safety Layer
**Goal**: The system cannot lose money due to runaway execution, naked positions, rate limit bans, or uncontrolled shutdown -- every dangerous scenario has a safety mechanism
**Depends on**: Phase 2
**Requirements**: SAFE-01, SAFE-02, SAFE-03, SAFE-04, SAFE-05, SAFE-06
**Success Criteria** (what must be TRUE):
  1. Operator can trigger kill switch from dashboard UI or programmatic threshold -- all open orders are cancelled within 5 seconds and no new orders are accepted until manually reset
  2. Position limits are enforced before every order submission -- attempting to exceed per-platform or per-market limits results in order rejection with a clear log message
  3. One-leg exposure is detected within one scan cycle after a failed second leg -- operator is alerted via Telegram and an unwind recommendation is logged
  4. API rate limits are enforced per-platform with configurable thresholds -- sustained high-frequency calls are throttled before hitting platform limits
  5. SIGINT/SIGTERM triggers graceful shutdown that cancels all open orders before process exit -- verified by sending signal during active session
**Plans**: TBD

Plans:
- [ ] 03-01: TBD
- [ ] 03-02: TBD
- [ ] 03-03: TBD

### Phase 4: Sandbox Validation
**Goal**: The full pipeline (collect -> scan -> execute -> monitor -> reconcile) is validated end-to-end against real platform APIs in sandbox/demo mode with no real money at risk
**Depends on**: Phase 3
**Requirements**: TEST-01, TEST-02, TEST-03, TEST-04
**Success Criteria** (what must be TRUE):
  1. A complete order lifecycle (submit -> fill/cancel -> record) succeeds on Kalshi demo environment with real API calls
  2. A minimum-size order ($1-5) on Polymarket completes the full lifecycle -- order placed, fill confirmed, position reflected in dashboard
  3. Recorded PnL for test trades matches actual platform balance changes within acceptable rounding tolerance
  4. Fee amounts charged by platforms on real trades match the system's fee calculations -- discrepancies are zero or explained
**Plans**: TBD

Plans:
- [ ] 04-01: TBD
- [ ] 04-02: TBD

### Phase 5: Live Trading
**Goal**: The first real cross-platform arbitrage trade executes successfully with small capital under operator supervision, proving the system works end-to-end with real money
**Depends on**: Phase 4
**Requirements**: TEST-05
**Success Criteria** (what must be TRUE):
  1. A real arbitrage opportunity is detected, both legs are executed across two platforms, and both orders are filled -- visible in dashboard and platform accounts
  2. The trade's actual profit (after all fees) is positive and matches the system's predicted edge within reasonable tolerance
  3. Operator supervised the entire trade lifecycle from detection to settlement without needing to intervene for bugs or errors
**Plans**: TBD

Plans:
- [ ] 05-01: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4 -> 5

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. API Integration Fixes | 0/5 | Planned | - |
| 2. Execution & Operational Hardening | 0/3 | Not started | - |
| 3. Safety Layer | 0/3 | Not started | - |
| 4. Sandbox Validation | 0/2 | Not started | - |
| 5. Live Trading | 0/1 | Not started | - |
