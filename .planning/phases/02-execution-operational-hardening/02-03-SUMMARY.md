---
phase: 02-execution-operational-hardening
plan: 03
subsystem: execution
tags: [protocol, runtime_checkable, tenacity, retry, aiohttp, adapters, python]

# Dependency graph
requires:
  - phase: 01-api-integration-fixes
    provides: Kalshi/Polymarket collector clients whose adapter counterparts will consume this Protocol
provides:
  - arbiter.execution.adapters package with PlatformAdapter Protocol + transient_retry decorator
  - Structural contract (5 async methods + platform attribute) Plans 04/05 implement for KalshiAdapter/PolymarketAdapter
  - Tenacity-backed transient_retry decorator factory classifying aiohttp/asyncio transient errors
  - TRANSIENT_EXCEPTIONS tuple — the canonical transient-vs-permanent classifier used across adapters
  - Protocol conformance test pattern (isinstance-based) for adapter consumers to reuse
affects:
  - 02-04-PLAN.md (Kalshi adapter — implements PlatformAdapter, uses transient_retry on idempotent POSTs)
  - 02-05-PLAN.md (Polymarket adapter — implements PlatformAdapter; must NOT use transient_retry on order POSTs)
  - 02-06-PLAN.md (engine refactor — depends on this Protocol to inject adapters)

# Tech tracking
tech-stack:
  added:
    - tenacity (retry) — note: tenacity 9.0.0 already installed; Plan 01 will pin in requirements.txt
  patterns:
    - runtime_checkable Protocol for structural typing (no inheritance required)
    - Decorator factory returning tenacity retry predicate (max_attempts as parameter)
    - Separation of transient (retry) vs permanent (fail-fast) exception classification

key-files:
  created:
    - arbiter/execution/adapters/__init__.py
    - arbiter/execution/adapters/base.py
    - arbiter/execution/adapters/retry_policy.py
    - arbiter/execution/adapters/test_retry_policy.py
    - arbiter/execution/adapters/test_protocol_conformance.py
  modified: []

key-decisions:
  - "Used stdlib logging.getLogger for retry-policy module logger instead of structlog (structlog not yet installed; Plan 01 installs it). before_sleep_log accepts stdlib logger so retry behavior is unchanged."
  - "Committed retry_policy.py foundation with Task 1 (not Task 2) because adapters/__init__.py re-exports transient_retry. Task 2 added only the test file."
  - "runtime_checkable Protocol with class attribute `platform: str` documented as structurally weaker — Python does not enforce attribute presence for isinstance checks. Test_missing_attribute_stub_fails_protocol falls back to verifying AttributeError on direct read."

patterns-established:
  - "Pattern: runtime_checkable Protocol — engine depends only on Protocol, concrete adapters inject via constructor (EXEC-04 foundation)"
  - "Pattern: transient_retry decorator factory — callers specify max_attempts; default (3, 0.5s→10s jitter) used across adapters"
  - "Pattern: TRANSIENT_EXCEPTIONS tuple — aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError only; all other exceptions fail fast"
  - "Pattern: adapter conformance test — isinstance(<Adapter>(...), PlatformAdapter); Plans 04/05 reuse this in their own test modules"

requirements-completed: [EXEC-04, OPS-03]

# Metrics
duration: ~18min
completed: 2026-04-16
---

# Phase 02 Plan 03: Adapters Package Foundation Summary

**PlatformAdapter runtime_checkable Protocol (5 async methods + platform attr) and tenacity-backed transient_retry decorator with TRANSIENT_EXCEPTIONS classifier — foundation for Plans 04/05 adapter extraction.**

## Performance

- **Duration:** ~18 min
- **Started:** 2026-04-16T19:06Z (phase 2 wave 1 spawn)
- **Completed:** 2026-04-16T19:24Z
- **Tasks:** 3 completed / 3 planned
- **Files created:** 5 (3 source + 2 test)
- **Files modified:** 0

## Accomplishments

- **`PlatformAdapter` Protocol** defined with 5 async methods (`check_depth`, `place_fok`, `cancel_order`, `get_order`, `list_open_orders_by_client_id`) and the `platform: str` class attribute. `@runtime_checkable` enables structural `isinstance(...)` checks without inheritance.
- **`transient_retry` decorator factory** built from tenacity primitives (`stop_after_attempt`, `wait_exponential_jitter(0.5→10s)`, `retry_if_exception_type`, `before_sleep_log`, `reraise=True`). Caller can override `max_attempts`.
- **`TRANSIENT_EXCEPTIONS` tuple** exposes the canonical transient-vs-permanent classifier: `aiohttp.ClientConnectionError`, `aiohttp.ServerTimeoutError`, `asyncio.TimeoutError`. All other exceptions (ValueError, RuntimeError, platform-specific errors) bypass retry and fail fast.
- **11 tests passing** (7 retry_policy + 4 protocol_conformance) proving the classifier is correct and the Protocol is enforced structurally.
- **Package-level re-exports** — `from arbiter.execution.adapters import PlatformAdapter, transient_retry, TRANSIENT_EXCEPTIONS` works out of the box.
- **No circular import** between `arbiter.execution.adapters` and `arbiter.execution.engine`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Create adapters package + PlatformAdapter Protocol** — `7e11480` (feat)
2. **Task 2: Implement transient_retry tests** — `6d7be7a` (test)
3. **Task 3: Protocol conformance tests** — `0e86402` (test)

**Plan metadata commit:** will be created after this SUMMARY is written.

## Files Created/Modified

- `arbiter/execution/adapters/__init__.py` — package init re-exporting `PlatformAdapter`, `TRANSIENT_EXCEPTIONS`, `transient_retry`
- `arbiter/execution/adapters/base.py` — `@runtime_checkable PlatformAdapter(Protocol)` with 5 abstract async methods + `platform: str`
- `arbiter/execution/adapters/retry_policy.py` — `transient_retry(*, max_attempts=3)` factory + `TRANSIENT_EXCEPTIONS` tuple, module logger via stdlib `logging`
- `arbiter/execution/adapters/test_retry_policy.py` — 7 tests covering transient success/exhaustion, permanent fail-fast (ValueError, RuntimeError), asyncio.TimeoutError retry, and max_attempts=1 no-retry behavior
- `arbiter/execution/adapters/test_protocol_conformance.py` — 4 tests: complete stub passes Protocol, missing-method stub fails, missing-attribute documented weakness, Protocol method-surface sanity check

## Decisions Made

- **Use stdlib `logging.getLogger` instead of `structlog.get_logger` for retry_policy module logger.** `structlog` is not yet installed in this environment (Plan 01 bootstraps OPS-01 and adds it to requirements). `before_sleep_log` from tenacity accepts a stdlib logger, so retry behavior is identical. When Plan 01 lands structlog, a 1-line swap is possible but not required.
- **Ship `retry_policy.py` with Task 1, not Task 2.** The plan's acceptance criteria for Task 1 require `from arbiter.execution.adapters import transient_retry` to succeed (re-export check). That requires `retry_policy.py` to exist at import time. Committing the full module in Task 1 (file listed in Task 2's `<files>`) was the minimum change to keep the package importable between tasks; Task 2 added only the test file.
- **Document the `platform: str` attribute limitation.** `runtime_checkable` Protocol `isinstance()` checks enforce method presence reliably but not attribute presence (CPython behavior). The conformance test for missing-attribute falls back to verifying `AttributeError` on direct read — this matches Python semantics and documents the limitation for Plans 04/05.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Substituted stdlib `logging.getLogger` for `structlog.get_logger` in retry_policy.py**
- **Found during:** Task 1 (retry_policy.py module creation — required for Task 1 import chain)
- **Issue:** Plan specifies `log = structlog.get_logger("arbiter.adapters.retry")` at module load. `structlog` is not installed (Plan 01 is scheduled to install it as part of OPS-01). Attempting the import would break the entire adapters package before any test runs, blocking all 3 tasks.
- **Fix:** Replaced with `log = logging.getLogger("arbiter.adapters.retry")`. Tenacity's `before_sleep_log` accepts a stdlib logger, so retry log emission behaviour is preserved. A follow-up in Plan 01 (or any plan that installs structlog) can swap the import back if desired — no semantic change.
- **Files modified:** `arbiter/execution/adapters/retry_policy.py` (module-level logger assignment and `before_sleep_log` call reference)
- **Verification:** `python -c "from arbiter.execution.adapters import PlatformAdapter, transient_retry"` succeeds; all 11 tests pass.
- **Committed in:** `7e11480` (Task 1 commit)

**2. [Rule 3 - Blocking] Created `retry_policy.py` within Task 1 commit (file originally planned in Task 2)**
- **Found during:** Task 1 (package `__init__.py` re-exports)
- **Issue:** Task 1's acceptance criteria require `__init__.py` to re-export `transient_retry` and the verification step runs `from arbiter.execution.adapters import PlatformAdapter, transient_retry`. That import fails unless `retry_policy.py` exists. Task 2 owns that file in the plan, but the Task 1 verification would fail before Task 2 runs.
- **Fix:** Committed the full `retry_policy.py` content in Task 1's commit. Task 2 added only the test file `test_retry_policy.py`. The module contents match the plan's Task 2 specification exactly — no rework needed.
- **Files modified:** `arbiter/execution/adapters/retry_policy.py`
- **Verification:** Task 1 import verification and Task 2 pytest both pass without modification.
- **Committed in:** `7e11480` (Task 1 commit)

---

**Total deviations:** 2 auto-fixed (both Rule 3 — blocking issues caused by plan inter-task ordering and missing optional dependency).
**Impact on plan:** Zero scope creep. Both deviations preserve the plan's intended behavior; only ordering and logger backend shifted. Plans 04/05 consume exactly the interface the plan promised.

## Issues Encountered

- `structlog` is not installed in the active Python environment — handled via Rule 3 deviation (see above).
- `tenacity 9.0.0` (not 9.1.4 as Plan 01 planned) is installed. Verified that `wait_exponential_jitter`, `stop_after_attempt`, `retry_if_exception_type`, and `before_sleep_log` all exist in 9.0.0 — no API gap for this plan.

## Protocol Reference for Plans 04/05

Plans 04 (Kalshi) and 05 (Polymarket) must implement all 5 methods of the Protocol on their concrete adapter class:

```python
class KalshiAdapter:  # note: no explicit inheritance required
    platform: str = "kalshi"

    async def check_depth(self, market_id, side, required_qty): ...
    async def place_fok(self, arb_id, market_id, canonical_id, side, price, qty): ...
    async def cancel_order(self, order): ...
    async def get_order(self, order): ...
    async def list_open_orders_by_client_id(self, client_order_id_prefix): ...
```

Each adapter test module should add this canonical conformance check:

```python
from arbiter.execution.adapters import PlatformAdapter

def test_kalshi_adapter_satisfies_protocol():
    adapter = KalshiAdapter(...)
    assert isinstance(adapter, PlatformAdapter)
```

## `transient_retry` Usage Policy

**SAFE:** Kalshi adapter — `client_order_id` is the idempotency key. A retried POST with the same `client_order_id` returns the existing record rather than creating a duplicate.

**UNSAFE:** Polymarket adapter order POSTs — no idempotency key. A retried POST after a network timeout can create a duplicate order. Plan 05 MUST implement `_place_fok_reconciling` with a pre-check via `get_open_orders(market=token_id)` before each retry attempt (see 02-RESEARCH.md Pitfall 2, lines 582-587). **Do not decorate Polymarket's `post_order` with `@transient_retry`.** Use `@transient_retry` only for read-only queries like `get_open_orders` and `get_order`.

Kalshi rate-limit caveat (RESEARCH Pitfall 4): Plan 04 MUST call `await self.rate_limiter.acquire()` inside the retry-decorated function body so each retry attempt waits for a token (10 writes/sec ceiling). The tenacity `wait_exponential_jitter(0.5, 10)` spreads retries temporally; combined with the rate limiter token bucket, this prevents retry storms from exhausting Kalshi's burst allowance.

## User Setup Required

None — no external service configuration required. All work is in-process Python code.

## Next Phase Readiness

- **Wave 2 (Plans 04, 05) unblocked** — Both adapter plans can now import `PlatformAdapter` and `transient_retry` from `arbiter.execution.adapters`.
- **Plan 06 (engine refactor) unblocked** on the Protocol side — the engine can now depend on `PlatformAdapter` without seeing concrete adapter classes.
- **No residual risk:** All 11 tests green in 6.30s. No circular imports. No heavy SDK imports triggered by loading the adapters package.
- **Follow-up (optional, not blocking):** When Plan 01 installs `structlog`, swap the module logger in `retry_policy.py` back to `structlog.get_logger(...)` if a single logging backend is desired. No functional change.

## TDD Gate Compliance

Plan type is `execute` (not `tdd`) — plan-level TDD gate sequence not required. Per-task `tdd="true"` flags were honored:

- **Task 1 (tdd):** Verification is an import/attribute check rather than a pytest test (acceptance criteria lists grep + `python -c` checks). Module created, verification passes — TDD spirit preserved (no untested code shipped; Task 3 adds conformance tests against the Protocol).
- **Task 2 (tdd):** RED would normally precede GREEN, but the decorator module was created in Task 1 to unblock the package import chain. Tests were added in Task 2 and passed immediately against the Task 1 module. Documented as Rule 3 deviation above.
- **Task 3 (tdd):** Conformance tests written and passing in a single commit; the Protocol (Task 1) already existed, so tests ratify the earlier work.

Gate-sequence commits in git log:
- `7e11480` — feat (Task 1 module)
- `6d7be7a` — test (Task 2 decorator tests)
- `0e86402` — test (Task 3 conformance tests)

## Self-Check: PASSED

- `arbiter/execution/adapters/__init__.py` — FOUND
- `arbiter/execution/adapters/base.py` — FOUND
- `arbiter/execution/adapters/retry_policy.py` — FOUND
- `arbiter/execution/adapters/test_retry_policy.py` — FOUND
- `arbiter/execution/adapters/test_protocol_conformance.py` — FOUND
- Commit `7e11480` — FOUND in git log
- Commit `6d7be7a` — FOUND in git log
- Commit `0e86402` — FOUND in git log
- pytest arbiter/execution/adapters/: 11 passed
- Package-level imports: ok
- PlatformAdapter runtime_checkable: ok
- No circular import with arbiter.execution.engine: ok

---
*Phase: 02-execution-operational-hardening*
*Plan: 03*
*Completed: 2026-04-16*
