# Arbiter Dashboard

## What This Is

A cross-platform prediction market arbitrage system that detects price discrepancies across Kalshi and Polymarket, then executes trades to capture the spread. It includes a real-time WebSocket dashboard for monitoring prices, opportunities, positions, and execution. The system is built but untested against live APIs.

## Core Value

Execute live arbitrage trades across both platforms without losing money to bugs, stale prices, or partial fills.

## Requirements

### Validated

<!-- Inferred from existing codebase. These capabilities exist in code. -->

- Multi-platform price collection (Kalshi, Polymarket collectors) -- existing
- Redis-backed quote cache with 30s TTL and pub/sub subscriptions -- existing
- Cross-platform market mapping with scoring and status workflow -- existing
- Fee-aware arbitrage detection (Kalshi quadratic, Polymarket market-specific) -- existing
- Persistence gating (opportunity must appear N consecutive scans) -- existing
- Execution engine with order lifecycle management (pending -> submitted -> filled -> settled) -- existing
- Concurrent leg execution (buy + sell in parallel) with re-quote checks -- existing
- Balance monitoring per platform -- existing
- Portfolio tracking (exposure, realized/unrealized PnL, drift detection) -- existing
- P&L reconciliation and math auditing -- existing
- WebSocket-driven real-time dashboard with REST API -- existing
- Docker containerization (PostgreSQL, Redis, API server) -- existing
- Dry-run CLI pipeline (TypeScript) -- existing
- Session-based dashboard authentication (HMAC-SHA256) -- existing
- Readiness gating system (auth, API connectivity, capital checks) -- existing
- Telegram alerts for portfolio violations -- existing
- Circuit breaker pattern on collector failures -- existing

### Active

<!-- What needs to happen to go from current state -> live profitable trades -->

- [x] Kill switch / emergency stop mechanism -- Validated in Phase 3 (SAFE-01, supervisor + ARM/RESET UI + operator auth)
- [x] Position limits enforcement verified against live data -- Validated in Phase 3 (SAFE-02, per-market + per-platform exposure limits, closed-loop on submitted/recovering/filled)
- [x] Rate limiting compliance per platform API docs -- Validated in Phase 3 (SAFE-04, RateLimiter wrapped at every adapter call site)
- [ ] Deep production-readiness audit of all platform integrations
- [ ] Live API testing against each platform (sandbox then production) -- Phase 4 (sandbox), Phase 5 (live)
- [ ] Verify order placement actually works per platform API -- Phase 4
- [ ] Verify order cancellation and partial fill handling -- partial in Phase 3 (SAFE-05 shutdown cancel), full in Phase 4
- [ ] End-to-end integration testing (collector -> scanner -> executor -> monitor)
- [ ] Reconciliation verified against actual platform balances -- Phase 4
- [ ] Error handling for production edge cases (network drops, API changes, maintenance windows)
- [ ] Research production arbitrage systems for reference patterns and pitfalls
- [ ] Gap analysis ranked by criticality (blocker -> nice-to-have)
- [ ] Test coverage assessment (what's tested vs. what only looks tested)

### Out of Scope

- Mobile app -- web dashboard sufficient for monitoring
- Multi-user support -- single operator system
- Backtesting engine -- forward-testing with small capital instead
- Additional platforms beyond Kalshi/Polymarket -- stabilize both first
- PredictIt support -- removed in Phase 4.1 (dead weight, never went live)
- Automated scaling of position sizes -- manual capital allocation initially

## Context

- **Existing code**: Substantial Python backend with TypeScript CLI. Architecture is layered (collectors -> price store -> scanner -> execution -> monitoring -> API). Event-driven with async pub/sub.
- **Testing state**: Test files exist alongside modules but nothing has been verified against real APIs. The system may have logic bugs that only surface with live data.
- **Polymarket complexity**: Requires Ethereum wallet signing via py-clob-client. Most complex integration.
- **Kalshi auth**: RSA private key signing for orders. API key + private key from env vars.
- **Infrastructure**: Docker Compose stack with PostgreSQL 16, Redis 7, Python 3.12 API server on port 8090.
- **Dashboard**: Real-time WebSocket dashboard with auth, prices, opportunities, trades, portfolio views, market mapping curation, and manual position management.

## Constraints

- **Capital**: Under $1K per platform initially -- system must handle small position sizes
- **Timeline**: ASAP -- get to live trades as fast as possible, even with manual monitoring
- **Risk tolerance**: Low -- cannot afford to lose capital to bugs. Safety > speed.
- **Platform APIs**: Must comply with rate limits and terms of service for both platforms
- **Auth credentials**: API keys stored in .env file, RSA keys in arbiter/keys/ (git-ignored)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Two platforms in scope (Kalshi, Polymarket) | PredictIt removed in Phase 4.1 as dead weight | 2026-04-17 |
| Small capital first ($1K/platform) | Prove system works before scaling | -- Pending |
| Python backend for live trading | Already built, async-first, mature | -- Pending |
| Deep audit before any live trades | Risk of losing money to untested code | -- Pending |
| Research similar production systems | Learn from others' mistakes and patterns | -- Pending |

## Current State

- **Phases complete:** 1 (API Integration Fixes), 2 (Execution Hardening), 2.1 (cancel-on-timeout + client-order-id), 3 (Safety Layer)
- **Phase 3 delivered:** kill-switch supervisor + UI, per-platform/per-market exposure limits (closed-loop on submitted+recovering+filled), one-leg recovery with structured alerts, per-adapter rate limiting, graceful shutdown with cancel-all, resolution-criteria mapping schema, consolidated operator dashboard UI
- **Next phase:** 4 Sandbox Validation — requires Kalshi demo credentials, Polymarket test wallet, Docker-based Postgres+Redis, `.env` populated
- **Known deferred items (tracked in 03-HUMAN-UAT.md):** operator kill-switch ARM/RESET browser session, shutdown banner visibility, rate-limit pill color transitions — blocked on running server + browser, not on code
- **Known cross-phase drift:** `test_api_integration.py::test_api_and_dashboard_contracts` line 82 asserts a pre-03-07 heading string; 1-line test fix tracked

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? -> Move to Out of Scope with reason
2. Requirements validated? -> Move to Validated with phase reference
3. New requirements emerged? -> Add to Active
4. Decisions to log? -> Add to Key Decisions
5. "What This Is" still accurate? -> Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check -- still the right priority?
3. Audit Out of Scope -- reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-17 after Phase 3 (Safety Layer) completion*
