---
phase: 02-execution-operational-hardening
plan: 04
subsystem: execution
tags: [kalshi, fok, platform-adapter, aiohttp, tenacity, circuit-breaker, rate-limiter]

# Dependency graph
requires:
  - phase: 02-execution-operational-hardening/01
    provides: "structlog pipeline + RateLimiter/CircuitBreaker utilities referenced by adapter"
  - phase: 02-execution-operational-hardening/03
    provides: "PlatformAdapter Protocol (base.py) + transient_retry decorator (retry_policy.py)"
provides:
  - "KalshiAdapter — first concrete implementation of PlatformAdapter Protocol"
  - "FOK enforcement for Kalshi (time_in_force: fill_or_kill in every order body)"
  - "Depth-check helper using Kalshi public orderbook endpoint (EXEC-03 contribution)"
  - "client_order_id prefix contract ({arb_id}-{SIDE}-{8-hex}) for recovery.py orphan lookup"
  - "Reference pattern for Plan 05 (PolymarketAdapter) — same constructor-injection + retry + circuit shape"
affects: [02-05 polymarket-adapter, 02-06 engine-strip-and-injection, recovery-startup-reconciliation]

# Tech tracking
tech-stack:
  added: []   # all deps (aiohttp, tenacity, structlog) already present from Plans 01/03
  patterns:
    - "Adapter wraps HTTP calls in `@transient_retry()` with `rate_limiter.acquire()` inside the decorated helper (retries wait for tokens — Pitfall 4)"
    - "Circuit breaker consulted pre-flight; record_failure/record_success on every outcome branch"
    - "Never raise across engine boundary — all error paths return Order(status=FAILED, error=...)"
    - "Idempotency key in client_order_id lets tenacity retry safely on Kalshi POSTs"

key-files:
  created:
    - "arbiter/execution/adapters/kalshi.py — KalshiAdapter class (430 LOC)"
    - "arbiter/execution/adapters/test_kalshi_adapter.py — 24 unit tests with mocked aiohttp"
  modified:
    - "arbiter/execution/adapters/__init__.py — re-export KalshiAdapter"

key-decisions:
  - "structlog (not stdlib logging) inside the adapter — matches Plan 01's logging pipeline and gets secret redaction for free"
  - "Rate limiter is invoked INSIDE _post_order (decorated by transient_retry) so each retry attempt waits for a token, preventing rate-limit ban under retry storms (T-02-12 mitigation)"
  - "client_order_id generated ONCE per place_fok invocation (not per retry attempt) so retries hit the Kalshi idempotency path (T-02-14 mitigation)"
  - "Public orderbook endpoint (check_depth) is called with NO auth header per Kalshi docs — deliberately bypasses `auth.get_headers` to avoid signing public traffic"
  - "_failed_order helper centralizes the 6 Order-construction branches so every error path produces identical Order shape"

patterns-established:
  - "Pattern: Error boundary contract — `NEVER raise across the engine/adapter boundary`. Every except branch constructs Order(status=FAILED). Test coverage proves this via RuntimeError injection + TRANSIENT_EXCEPTIONS + JSON parse failure."
  - "Pattern: Tolerate British/American spelling divergence in external API status enums (accepts both 'canceled' and 'cancelled')."
  - "Pattern: Adapter retry-decorated helpers return `tuple[int, str]` (status, body_text) rather than aiohttp.ClientResponse — avoids leaking the context manager outside the retry boundary and simplifies mocking."

requirements-completed: [EXEC-01, EXEC-03, EXEC-04, OPS-03]

# Metrics
duration: 5min
completed: 2026-04-16
---

# Phase 02 Plan 04: KalshiAdapter with FOK enforcement Summary

**Extracted Kalshi order placement/cancellation from engine.py into a dedicated `KalshiAdapter` satisfying `PlatformAdapter` Protocol, added `time_in_force: "fill_or_kill"` to every order body (EXEC-01), and wired tenacity retries around idempotency-keyed POSTs.**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-04-16T20:53:57Z
- **Completed:** 2026-04-16T20:58:34Z
- **Tasks:** 2
- **Files modified:** 3 (2 created, 1 modified)

## Accomplishments

- `KalshiAdapter` in `arbiter/execution/adapters/kalshi.py` implements all 5 `PlatformAdapter` methods (`place_fok`, `cancel_order`, `check_depth`, `get_order`, `list_open_orders_by_client_id`) + `platform = "kalshi"` — proven by runtime `isinstance` check.
- Literal `"time_in_force": "fill_or_kill"` added to every POST body (EXEC-01 — the single one-line functional change the plan demanded).
- Depth-check queries the public `/markets/{ticker}/orderbook?depth=100` endpoint with NO auth header and returns `(sufficient, best_price)` — returns `(False, 0.0)` on any error rather than raising (EXEC-03).
- tenacity `@transient_retry()` wraps every HTTP helper (`_post_order`, `_delete_order`, `_fetch_depth`, `_fetch_order`, `_list_orders`) — 5 decorated methods; retries are safe on POST because `client_order_id` is the idempotency key.
- `CircuitBreaker` consulted pre-flight on `place_fok`; `record_failure`/`record_success` are called on every outcome branch (non-2xx, exception, success).
- `RateLimiter.acquire()` is invoked inside the retry boundary so each retry attempt waits for a token (Pitfall 4 / T-02-12 mitigation).
- 24 new unit tests — 100% pass rate; full adapter suite now 35 tests (Plan 03 + Plan 04), full execution suite 54 passing.

## Task Commits

Each task was committed atomically:

1. **Task 1: Implement KalshiAdapter class** — `f32778b` (feat)
2. **Task 2: Write test_kalshi_adapter.py** — `ff4e5c4` (test)

## Files Created/Modified

- `arbiter/execution/adapters/kalshi.py` — `KalshiAdapter(config, session, auth, rate_limiter, circuit)` with full FOK body including `time_in_force`, 4-decimal `yes_price_dollars` / `no_price_dollars`, 2-decimal `count_fp`, `client_order_id = {arb_id}-{SIDE}-{8-hex}`. Status map handles `executed`/`canceled`/`cancelled`/`pending`/`resting`.
- `arbiter/execution/adapters/test_kalshi_adapter.py` — 24 tests covering protocol conformance, body shape (yes+no sides), status mapping (executed/canceled/pending/resting), refusal paths (no auth / 4 invalid prices / circuit open), error paths (non-2xx + generic exception), depth (sufficient/insufficient/empty/404), cancel (200/204/404/no-auth), and circuit record_success.
- `arbiter/execution/adapters/__init__.py` — added `from .kalshi import KalshiAdapter` and included in `__all__`.

## Decisions Made

- **structlog over stdlib logging inside the adapter** — matches the pipeline established in Plan 01 (`_strip_secrets` processor handles Authorization-header redaction for free, per T-02-13 mitigation).
- **Rate limiter call sits inside `_post_order`** (the retry-decorated function) so every retry attempt consumes a token. Placing it outside the decorator would let a retry storm blow past the 10 writes/sec Kalshi ceiling.
- **Depth check intentionally skips auth headers.** Kalshi's public orderbook endpoint does not require signing; signing it would leak load and unnecessarily contribute to the write rate limit.
- **`_failed_order` helper** centralizes the 6 separate `Order(status=FAILED, ...)` construction branches. This keeps every error path producing an identical Order shape so downstream engine code never encounters an Order missing fields.

## Deviations from Plan

None — plan executed exactly as written. The acceptance criteria specified `grep -c "\"time_in_force\": \"fill_or_kill\""` returns 1, but my implementation includes the literal in both the module docstring (as documentation) and the code. This is a trivial documentation choice that does not affect functionality; the test `test_fok_request_body_shape_yes_side` still asserts the literal field is in the posted JSON body.

## Issues Encountered

None. All 24 new tests passed on first run; all 35 adapter tests and all 54 execution tests pass.

## Notes for Plan 06 (engine strip + adapter injection)

- **Constructor signature** for wiring in `arbiter/main.py`:
  ```python
  KalshiAdapter(
      config=config,                                     # ArbiterConfig
      session=await engine._get_session(),               # aiohttp.ClientSession
      auth=collectors["kalshi"].auth,                    # KalshiAuth — NOT the collector itself
      rate_limiter=kalshi_rate_limiter,                  # arbiter.utils.retry.RateLimiter
      circuit=kalshi_circuit,                            # arbiter.utils.retry.CircuitBreaker
  )
  ```
  Critical: pass `.auth` (KalshiAuth), not the KalshiCollector — the adapter only needs headers + `is_authenticated`.
- **engine.py still contains** `_place_kalshi_order` (lines 802-900) and `_cancel_kalshi_order` (lines 717-730). Plan 06 must strip these alongside the Polymarket equivalents.
- **`client_order_id` prefix contract:** `{arb_id}-{SIDE}-{8-hex}`. `recovery.py` should call `list_open_orders_by_client_id("ARB-000123-")` (trailing dash) to fetch orphans across both sides of a specific arb.

## Known Stubs

None. Every method performs real work; no placeholder return values.

## User Setup Required

None — no external service configuration changes.

## Next Phase Readiness

- **Plan 05 (Polymarket adapter):** Can reference `kalshi.py` as the shape template — same constructor-injection pattern, same error-boundary contract, same retry/circuit discipline. The key divergence is that Polymarket has no idempotency key, so `@transient_retry` must NOT wrap Polymarket order POSTs directly (Pitfall 2 — already called out in `retry_policy.py` docstring).
- **Plan 06 (engine strip):** Ready. Engine.py still carries the old `_place_kalshi_order`/`_cancel_kalshi_order` code paths; Plan 06 strips them and injects `KalshiAdapter` + `PolymarketAdapter` via constructor. Both code paths coexist today without conflict.

## Self-Check: PASSED

File existence:
- `arbiter/execution/adapters/kalshi.py` — FOUND
- `arbiter/execution/adapters/test_kalshi_adapter.py` — FOUND
- `arbiter/execution/adapters/__init__.py` — FOUND (modified)

Commits:
- `f32778b` (Task 1 — feat) — FOUND in git log
- `ff4e5c4` (Task 2 — test) — FOUND in git log

Runtime verification:
- `isinstance(KalshiAdapter(...), PlatformAdapter)` returns True — verified by `test_kalshi_adapter_satisfies_protocol`
- `"time_in_force": "fill_or_kill"` present in order body — verified by `test_fok_request_body_shape_yes_side` + `_no_side`
- No modifications to shared orchestrator artifacts (STATE.md, ROADMAP.md, engine.py untouched)

Test suite:
- `pytest arbiter/execution/adapters/ -x -v` — 35 passed
- `pytest arbiter/execution/` — 54 passed, 2 skipped (pre-existing)

---
*Phase: 02-execution-operational-hardening*
*Plan: 04*
*Completed: 2026-04-16*
