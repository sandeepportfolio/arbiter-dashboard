# Requirements: Arbiter Dashboard

**Defined:** 2026-04-16
**Core Value:** Execute live arbitrage trades across both platforms (Kalshi, Polymarket) without losing money to bugs, stale prices, or partial fills.

## v1 Requirements

Requirements for production-ready live trading. Each maps to roadmap phases.

### API Integration

- [ ] **API-01**: Kalshi order submission uses fixed-point dollar string pricing (`yes_price_dollars: "0.56"`) per March 2026 API migration
- [ ] **API-02**: Polymarket ClobClient initialized with correct `signature_type` and `funder` parameters for authenticated order placement
- [ ] **API-03**: Polymarket heartbeat manager sends keepalive every 5 seconds to prevent open order auto-cancellation
- [ ] **API-04**: Fee calculations use correct platform-specific rates (Polymarket category rates: crypto 0.072, sports 0.03, politics 0.04, geopolitics 0.0)
- [x] **API-05**: Supported platforms constrained to Kalshi + Polymarket (PredictIt removed in Phase 4.1)
- [ ] **API-06**: Polymarket platform decision resolved (international vs US) with correct SDK, endpoints, and auth method
- [ ] **API-07**: All platform collectors verified against current API responses (schema, field names, auth flow)

### Safety & Risk

- [ ] **SAFE-01**: Kill switch cancels all open/pending orders, halts new execution, alerts via Telegram, and is triggerable from dashboard and programmatic thresholds
- [ ] **SAFE-02**: Position limits enforced per-platform and per-market before order submission
- [ ] **SAFE-03**: One-leg recovery detects naked directional positions and executes automated or operator-assisted unwind
- [ ] **SAFE-04**: Per-platform API rate limiting prevents throttling/bans (Kalshi 10 writes/sec, Polymarket limits per docs)
- [ ] **SAFE-05**: Graceful shutdown cancels all open orders before process exit (SIGINT/SIGTERM)
- [ ] **SAFE-06**: Market mapping includes resolution criteria comparison -- operator must verify both platforms resolve identically before approving pairs

### Execution

- [ ] **EXEC-01**: FOK (fill-or-kill) order types used for both legs to eliminate partial fill risk
- [ ] **EXEC-02**: Execution state (orders, fills, incidents) persisted to PostgreSQL -- survives process restart
- [ ] **EXEC-03**: Pre-trade order book depth verification confirms sufficient liquidity before submission
- [x] **EXEC-04**: Per-platform execution adapters extracted from monolithic engine.py into execution/adapters/
- [x] **EXEC-05**: Execution timeout with automatic cancellation if fill not received within threshold

### Validation & Testing

- [ ] **TEST-01**: End-to-end pipeline validated on Kalshi demo environment (sandbox) with real API calls
- [ ] **TEST-02**: Polymarket order lifecycle validated with minimum-size real orders ($1-5)
- [ ] **TEST-03**: Balance reconciliation verified -- recorded PnL matches actual platform balance changes
- [ ] **TEST-04**: All fee calculations verified against actual platform-reported fees on real trades
- [ ] **TEST-05**: First live arbitrage trade executed successfully with small capital ($10-50) under operator supervision

### Operational

- [ ] **OPS-01**: Structured logging via structlog with JSON output for all trading operations
- [ ] **OPS-02**: Error tracking via Sentry for unhandled exceptions and execution failures
- [ ] **OPS-03**: Retry logic via tenacity for transient API failures (with appropriate backoff)
- [ ] **OPS-04**: Dependency versions upgraded (py-clob-client to 0.34.x, cryptography to 46.x)

## v2 Requirements

Deferred to after live trading is proven profitable.

### Optimization

- **OPT-01**: WebSocket price feeds replacing REST polling for Kalshi and Polymarket
- **OPT-02**: Liquidity-aware position sizing based on order book depth
- **OPT-03**: Annualized return scoring for opportunity prioritization
- **OPT-04**: Automated kill switch triggers (daily loss threshold, error rate ceiling)
- **OPT-05**: Telegram `/kill` command for remote emergency stop

### Monitoring

- **MON-01**: Settlement divergence monitoring across platforms
- **MON-02**: Dynamic fee rate fetching via SDK (replace hardcoded rates)
- **MON-03**: Automated reconciliation scheduling
- **MON-04**: Performance dashboards with latency percentiles

## Out of Scope

| Feature | Reason |
|---------|--------|
| PredictIt integration (any form) | Removed in Phase 4.1 -- dead weight, never went live |
| Multi-user dashboard | Single operator system, adds unnecessary complexity |
| Mobile app | Web dashboard sufficient for monitoring |
| Backtesting engine | Forward-testing with small capital is more reliable for this domain |
| Additional platforms | Stabilize both before expanding |
| High-frequency strategies | Prediction markets have wide spreads and slow settlement -- HFT doesn't apply |
| Automated position scaling | Manual capital allocation until system is proven profitable |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| API-01 | Phase 1 | Pending |
| API-02 | Phase 1 | Pending |
| API-03 | Phase 1 | Pending |
| API-04 | Phase 1 | Pending |
| API-05 | Phase 4.1 | Complete |
| API-06 | Phase 1 | Pending |
| API-07 | Phase 1 | Pending |
| SAFE-01 | Phase 3 | Pending |
| SAFE-02 | Phase 3 | Pending |
| SAFE-03 | Phase 3 | Pending |
| SAFE-04 | Phase 3 | Pending |
| SAFE-05 | Phase 3 | Pending |
| SAFE-06 | Phase 3 | Pending |
| EXEC-01 | Phase 2 | Pending |
| EXEC-02 | Phase 2 | Pending |
| EXEC-03 | Phase 2 | Pending |
| EXEC-04 | Phase 2 | Complete |
| EXEC-05 | Phase 2 | Complete |
| TEST-01 | Phase 4 | Pending |
| TEST-02 | Phase 4 | Pending |
| TEST-03 | Phase 4 | Pending |
| TEST-04 | Phase 4 | Pending |
| TEST-05 | Phase 5 | Pending |
| OPS-01 | Phase 2 | Pending |
| OPS-02 | Phase 2 | Pending |
| OPS-03 | Phase 2 | Pending |
| OPS-04 | Phase 2 | Pending |

**Coverage:**
- v1 requirements: 27 total
- Mapped to phases: 27
- Unmapped: 0

---
*Requirements defined: 2026-04-16*
*Last updated: 2026-04-16 after roadmap phase mapping*
