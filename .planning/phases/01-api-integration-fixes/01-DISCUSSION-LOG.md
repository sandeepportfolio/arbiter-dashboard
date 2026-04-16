# Phase 1: API Integration Fixes - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md -- this log preserves the alternatives considered.

**Date:** 2026-04-16
**Phase:** 01-api-integration-fixes
**Areas discussed:** Polymarket platform, Collector verification, Fee rate sourcing, PredictIt scoping

---

## Polymarket Platform

| Option | Description | Selected |
|--------|-------------|----------|
| International (global) | Uses py-clob-client SDK with Ethereum wallet signing. Polygon chain. | |
| US (regulated) | Uses different auth and compliance flow. More restricted but US-legal. | |
| Not sure yet | Need to check account status and decide based on what's available. | (initial) |

**Follow-up: Location**

| Option | Description | Selected |
|--------|-------------|----------|
| US-based | Use regulated US Polymarket with KYC. Different API surface. | ✓ |
| International | Use global Polymarket. py-clob-client SDK with Ethereum wallet. | |
| Let Claude decide | Code for international CLOB -- swap later if wrong. | |

**Follow-up: Account status**

| Option | Description | Selected |
|--------|-------------|----------|
| Active account with API access | Can use API keys and place orders programmatically. | ✓ |
| Web-only account | Can trade on website but no API access yet. | |
| No account yet | Haven't signed up. | |
| Defer Polymarket | Focus on Kalshi first. | |

**User's choice:** US-based with active Polymarket account and API access. Use py-clob-client with updated ClobClient params.
**Notes:** Resolved API-06 -- target CLOB API the user's account has access to.

---

## Collector Verification

| Option | Description | Selected |
|--------|-------------|----------|
| Live API with real credentials | Read-only calls are safe. Fastest path. | |
| Kalshi demo env first | Sandbox for Kalshi, live read-only for others. | |
| You decide | Claude picks safest approach. | |

**User's choice (free text):** Monitor symbols second-by-second, fastest way possible, account for rate limits and max API calls.
**Confirmed as:** Live read-only calls at max safe frequency, WebSocket where available, respecting per-platform rate limits.
**Notes:** User wants real-time monitoring as the verification target, not just one-off test calls.

---

## Fee Rate Sourcing

| Option | Description | Selected |
|--------|-------------|----------|
| Hardcode documented rates | Per-category config table. Simple, update manually. | |
| Fetch from SDK/API | Query fee rates at startup. Future-proof. | ✓ |
| You decide | Claude picks simplest approach for API-04. | |

**Follow-up: Fallback behavior**

| Option | Description | Selected |
|--------|-------------|----------|
| Fall back to hardcoded defaults | Use documented rates as fallback with warning log. | ✓ |
| Block trading until resolved | Don't place orders if fees can't be verified. | |

**User's choice:** Dynamic fee fetching from SDK/API with hardcoded fallback on failure.
**Notes:** None.

---

## PredictIt Scoping

| Option | Description | Selected |
|--------|-------------|----------|
| Strip execution entirely | Remove all order/execution code. Keep collector only. | ✓ |
| Disable with flag | Keep code but gate behind disabled config flag. | |
| You decide | Claude picks cleanest approach. | |

**User's choice:** Strip all execution code. Keep only collector for read-only price signals.
**Notes:** Clean break, no dead code, no disabled flags.

---

## Claude's Discretion

- Error handling approach for failed API calls during verification
- Fee function signature refactoring for dynamic rate injection
- Test structure for fee calculation unit tests

## Deferred Ideas

None -- discussion stayed within phase scope
