# Arbiter Dashboard

## What This Is

A cross-platform prediction market arbitrage system that detects price discrepancies across Kalshi, Polymarket, and PredictIt, then executes trades to capture the spread. It includes a real-time WebSocket dashboard for monitoring prices, opportunities, positions, and execution. The system is built but untested against live APIs.

## Core Value

Execute live arbitrage trades across all three platforms without losing money to bugs, stale prices, or partial fills.

## Requirements

### Validated

<!-- Inferred from existing codebase. These capabilities exist in code. -->

- Multi-platform price collection (Kalshi, Polymarket, PredictIt collectors) -- existing
- Redis-backed quote cache with 30s TTL and pub/sub subscriptions -- existing
- Cross-platform market mapping with scoring and status workflow -- existing
- Fee-aware arbitrage detection (Kalshi quadratic, Polymarket market-specific, PredictIt profit/withdrawal) -- existing
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

- [ ] Deep production-readiness audit of all platform integrations
- [ ] Live API testing against each platform (sandbox then production)
- [ ] Verify order placement actually works per platform API
- [ ] Verify order cancellation and partial fill handling
- [ ] Kill switch / emergency stop mechanism
- [ ] Position limits enforcement verified against live data
- [ ] End-to-end integration testing (collector -> scanner -> executor -> monitor)
- [ ] Reconciliation verified against actual platform balances
- [ ] Rate limiting compliance per platform API docs
- [ ] Error handling for production edge cases (network drops, API changes, maintenance windows)
- [ ] Research production arbitrage systems for reference patterns and pitfalls
- [ ] Gap analysis ranked by criticality (blocker -> nice-to-have)
- [ ] Test coverage assessment (what's tested vs. what only looks tested)

### Out of Scope

- Mobile app -- web dashboard sufficient for monitoring
- Multi-user support -- single operator system
- Backtesting engine -- forward-testing with small capital instead
- Additional platforms beyond Kalshi/Polymarket/PredictIt -- stabilize three first
- Automated scaling of position sizes -- manual capital allocation initially

## Context

- **Existing code**: Substantial Python backend with TypeScript CLI. Architecture is layered (collectors -> price store -> scanner -> execution -> monitoring -> API). Event-driven with async pub/sub.
- **Testing state**: Test files exist alongside modules but nothing has been verified against real APIs. The system may have logic bugs that only surface with live data.
- **PredictIt status**: Platform is winding down but user's account is still active. Include but don't let it block progress on Kalshi/Polymarket.
- **Polymarket complexity**: Requires Ethereum wallet signing via py-clob-client. Most complex integration.
- **Kalshi auth**: RSA private key signing for orders. API key + private key from env vars.
- **Infrastructure**: Docker Compose stack with PostgreSQL 16, Redis 7, Python 3.12 API server on port 8090.
- **Dashboard**: Real-time WebSocket dashboard with auth, prices, opportunities, trades, portfolio views, market mapping curation, and manual position management.

## Constraints

- **Capital**: Under $1K per platform initially -- system must handle small position sizes
- **Timeline**: ASAP -- get to live trades as fast as possible, even with manual monitoring
- **Risk tolerance**: Low -- cannot afford to lose capital to bugs. Safety > speed.
- **Platform APIs**: Must comply with rate limits and terms of service for all three platforms
- **Auth credentials**: API keys stored in .env file, RSA keys in arbiter/keys/ (git-ignored)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| All three platforms in scope | PredictIt still works for user's account | -- Pending |
| Small capital first ($1K/platform) | Prove system works before scaling | -- Pending |
| Python backend for live trading | Already built, async-first, mature | -- Pending |
| Deep audit before any live trades | Risk of losing money to untested code | -- Pending |
| Research similar production systems | Learn from others' mistakes and patterns | -- Pending |

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
*Last updated: 2026-04-16 after initialization*
