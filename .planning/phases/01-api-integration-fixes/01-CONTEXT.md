# Phase 1: API Integration Fixes - Context

**Gathered:** 2026-04-16
**Status:** Ready for planning

<domain>
## Phase Boundary

Fix all hard-blocker API issues so platform integrations work against live APIs: Kalshi pricing format migration, Polymarket auth and heartbeat, PredictIt read-only scoping, fee calculation accuracy, and collector schema verification. No new features -- fix what exists.

</domain>

<decisions>
## Implementation Decisions

### Polymarket Platform (API-02, API-06)
- **D-01:** User is US-based with an active Polymarket account that has API access
- **D-02:** Use `py-clob-client` SDK with updated ClobClient init -- add `signature_type` and `funder` parameters
- **D-03:** Target the CLOB API endpoint the user's account has access to -- verify correct base URL, chain_id, and auth method against live credentials
- **D-04:** Polymarket heartbeat manager (API-03) must run as a dedicated async task sending keepalive every 5 seconds to the CLOB connection -- separate from the dashboard WebSocket heartbeat in `api.py:595`

### Collector Verification (API-07)
- **D-05:** Verify all three collectors with live API calls using real credentials -- read-only calls (price/market fetching) are safe, no money at risk
- **D-06:** Push for fastest refresh possible within each platform's rate limits -- second-by-second monitoring is the goal
- **D-07:** Use WebSocket feeds where available (Polymarket WS already has circuit breaker in `polymarket.py:49`), fall back to fast REST polling within rate limits
- **D-08:** Validate response schema and field names match what the code expects -- any mismatch is a fix target

### Fee Rate Sourcing (API-04)
- **D-09:** Fetch fee rates dynamically from each platform's SDK/API at startup -- not hardcoded
- **D-10:** Fall back to hardcoded documented rates if dynamic fetch fails, with a warning log so stale fees are visible
- **D-11:** Hardcoded fallback values: Polymarket per-category (crypto 0.072, sports 0.03, politics 0.04, geopolitics 0.0), Kalshi per current schedule, PredictIt profit fee 10% + withdrawal fee 5%

### PredictIt Scoping (API-05)
- **D-12:** Strip all PredictIt execution code entirely -- remove order submission, fill handling, and workflow stubs from `engine.py` and `workflow/predictit_workflow.py`
- **D-13:** Keep only the PredictIt collector (`arbiter/collectors/predictit.py`) for read-only price signals
- **D-14:** Clean break with no dead code -- no disabled flags, no execution stubs

### Kalshi Pricing Migration (API-01)
- **D-15:** Migrate Kalshi order payload from integer cents `yes_price` (current: `engine.py:829`) to `yes_price_dollars` string format per March 2026 API migration
- **D-16:** Add `count_fp` support for fractional markets

### Claude's Discretion
- Exact error handling approach for failed API calls during verification
- Internal refactoring of fee function signatures to support dynamic rate injection
- Test structure for unit tests verifying fee calculations against real rate values

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

No external specs -- requirements fully captured in decisions above. Platform API docs should be consulted via web research during the research phase for:
- Kalshi API v2 order format (yes_price_dollars, count_fp)
- Polymarket CLOB client py-clob-client SDK (signature_type, funder params)
- Polymarket fee schedule by market category
- PredictIt public API endpoint schema

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `arbiter/collectors/` -- existing collector framework with CircuitBreaker, RateLimiter, retry_with_backoff patterns
- `arbiter/config/settings.py` -- fee functions (`kalshi_order_fee`, `polymarket_order_fee`, `predictit_order_fee`) already structured per-platform
- `arbiter/utils/retry.py` -- CircuitBreaker, RateLimiter, retry_with_backoff utilities
- `arbiter/utils/price_store.py` -- PriceStore with subscribe pattern and 30s TTL

### Established Patterns
- Async-first: all I/O non-blocking (aiohttp, asyncpg, asyncio)
- Circuit breaker on collector failures with configurable thresholds
- Rate limiter per collector with configurable max_requests and window
- PricePoint dataclass with to_dict() for API serialization

### Integration Points
- ClobClient init: `arbiter/execution/engine.py:896-900` -- missing signature_type/funder
- Kalshi order body: `arbiter/execution/engine.py:820-831` -- uses integer cents, needs string dollars
- Fee functions imported in: `arbiter/scanner/arbitrage.py` from `arbiter/config/settings.py`
- PredictIt execution: scattered across `engine.py` and `workflow/predictit_workflow.py`
- Polymarket heartbeat: only exists for dashboard WS (`api.py:595`), not CLOB client

</code_context>

<specifics>
## Specific Ideas

- Second-by-second price monitoring is the target refresh rate -- as fast as rate limits allow
- WebSocket-first for Polymarket, fastest-safe-polling for others
- Fee fetch at startup with hardcoded fallback keeps the system operational even if fee APIs are down

</specifics>

<deferred>
## Deferred Ideas

None -- discussion stayed within phase scope

</deferred>

---

*Phase: 01-api-integration-fixes*
*Context gathered: 2026-04-16*
