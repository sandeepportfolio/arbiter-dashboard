---
phase: 03-safety-layer
plan: 04
subsystem: safety
tags: [rate-limiter, 429, retry-after, circuit-breaker, websocket, fok, safe-04]
requires:
  - arbiter.utils.retry.RateLimiter
  - arbiter.utils.retry.CircuitBreaker
  - arbiter.execution.adapters.kalshi.KalshiAdapter
  - arbiter.execution.adapters.polymarket.PolymarketAdapter
  - arbiter.api.ArbiterAPI
provides:
  - "KalshiAdapter: acquire-before-I/O on all 5 outbound methods + 429 branch on place_fok/cancel_order/get_order/list_open_orders_by_client_id"
  - "PolymarketAdapter: acquire-before-SDK on all 4 outbound methods + exception-based 429 detection"
  - "arbiter.api.ArbiterAPI._rate_limit_broadcast_loop — 2s periodic rate_limit_state WebSocket event"
  - "GET /api/system now includes top-level rate_limits key"
  - "arbiter.utils.retry.RateLimiter.stats exposes max_requests + time_window (plus existing fields)"
  - "WebSocket event rate_limit_state payload contract: {platform: {available_tokens, max_requests, remaining_penalty_seconds, ...}}"
affects:
  - arbiter.execution.adapters.kalshi.KalshiAdapter (5 methods wrapped with acquire + 429 handler)
  - arbiter.execution.adapters.polymarket.PolymarketAdapter (4 methods wrapped with acquire + SDK 429 detection)
  - arbiter.api.ArbiterAPI._build_system_snapshot (adds rate_limits key)
  - arbiter.api.ArbiterAPI.serve (launches + cancels the rate-limit broadcaster task)
  - arbiter.utils.retry.RateLimiter.stats (two new fields)
tech-stack:
  added: []
  patterns:
    - "Acquire-before-I/O: every adapter method calls `await self.rate_limiter.acquire()` as the first statement after input validation and before any network call — invariant enforced by unit tests"
    - "FOK no-retry after 429: a rate-limited FOK POST returns FAILED Order with 'rate_limited' in error; session.post is NEVER called a second time"
    - "Platform-agnostic 429 reason tagging: reason='kalshi_429' / reason='polymarket_429' flows through apply_retry_after into RateLimiter.last_penalty_reason for operator visibility"
    - "60-second upper bound on Retry-After: forged/excessive header values are clamped via min(delay, 60.0) to mitigate T-3-04-E"
    - "SDK 429 exception detection: _is_rate_limit_error helper scans exception messages for '429', 'rate limit', 'too many requests' — case-insensitive"
    - "Periodic broadcaster pattern: separate task from _broadcast_loop, skips emit when _ws_clients empty, cancelled on serve() shutdown"
key-files:
  created: []
  modified:
    - arbiter/execution/adapters/kalshi.py
    - arbiter/execution/adapters/polymarket.py
    - arbiter/execution/adapters/test_kalshi_adapter.py
    - arbiter/execution/adapters/test_polymarket_adapter.py
    - arbiter/api.py
    - arbiter/utils/retry.py
    - arbiter/test_api_integration.py
    - .planning/phases/03-safety-layer/deferred-items.md
decisions:
  - "Kalshi _post_order signature changed from (int, str) to (int, str, dict) so the caller can read Retry-After headers on 429; same refactor applied to _fetch_order and _list_orders for consistency (all three return headers even on success, callers ignore when not needed)"
  - "Polymarket 429 detection is string-based (_is_rate_limit_error scans exception messages) because py-clob-client does not expose a typed exception hierarchy for HTTP errors — documented marker list: '429', 'rate limit', 'rate_limit', 'too many requests' (case-insensitive)"
  - "Retry-After upper bound of 60s is applied at the adapter callsite (after apply_retry_after returns) — centralizing the cap here means RateLimiter.apply_retry_after stays semantically pure (respects whatever the caller passes) while adapters (the trust boundary to external venues) enforce the bound per T-3-04-E"
  - "RateLimiter.stats extended with max_requests and time_window (both as float) — dashboard plan 03-07 consumes 'tokens_available/max_requests' to render the token-pill without hardcoding per-platform config"
  - "Pre-existing test_fok_non_2xx_returns_failed_with_status_in_error changed from status=429 to status=500 because 429 now has dedicated handling; 500 still exercises the generic non-2xx branch that test was designed to cover"
  - "cancel_all stub still acquires a token even though it returns [] — ensures the acquire-before-I/O invariant survives plan 03-05's chunked-batch-cancel replacement without a separate refactor"

requirements-completed: [SAFE-04]

metrics:
  duration: "~10min"
  completed: 2026-04-17
---

# Phase 03 Plan 04: Per-Adapter Rate Limiting (SAFE-04) Summary

## One-liner

Every outbound adapter method now awaits `rate_limiter.acquire()` before any HTTP/SDK call and handles 429 responses via structured backoff + circuit failure + FAILED order (no-retry for FOK); a new 2-second periodic `_rate_limit_broadcast_loop` publishes per-platform `RateLimiter.stats` to the dashboard through a dedicated `rate_limit_state` WebSocket event.

## Performance

- **Duration:** ~10 min
- **Started:** 2026-04-17T00:48:29Z
- **Completed:** 2026-04-17T00:58:51Z
- **Tasks:** 3 (Task 0 red tests + Task 1 adapter wiring + Task 2 broadcaster)
- **Files modified:** 8

## Accomplishments

- **Kalshi adapter:** 5 outbound methods wrapped with `await self.rate_limiter.acquire()`. 429 branches in `place_fok`, `_delete_order`, `get_order`, and `list_open_orders_by_client_id` all invoke `apply_retry_after` with `reason="kalshi_429"`, cap the penalty at 60s, record a circuit failure, and return a rejection (FAILED Order for place_fok/get_order, `False` for cancel_order, `[]` for list/cancel_all). `_post_order`, `_fetch_order`, and `_list_orders` now return a third tuple element (response headers dict) so the outer method can read `Retry-After`.
- **Polymarket adapter:** 4 outbound methods wrapped with `await self.rate_limiter.acquire()`. A new `_is_rate_limit_error(exc)` helper detects 429 in SDK-raised exceptions (case-insensitive substring match on '429', 'rate limit', 'rate_limit', 'too many requests'). When detected, the adapter calls `apply_retry_after(reason="polymarket_429")`, caps at 60s, records circuit failure, and returns FAILED Order / `False`. FOK orders never retry after a 429.
- **arbiter/api.py:** new `async def _rate_limit_broadcast_loop(self)` task emits `{"type":"rate_limit_state","payload":{platform: RateLimiter.stats}}` every 2 seconds. Task is launched inside `serve()` and cancelled in the `finally:` block on shutdown so no pending task is left dangling. `_build_system_snapshot` now includes a top-level `rate_limits` key so `GET /api/system` and the WebSocket bootstrap carry the same payload shape.
- **arbiter/utils/retry.py:** `RateLimiter.stats` extended with `max_requests` (float) and `time_window` (float) — the three dashboard-consumable fields (available_tokens, max_requests, remaining_penalty_seconds) are now all present.
- **Dashboard JS:** unchanged. The state-capture branch `state.safety.rateLimits = message.payload` from plan 03-01 is preserved verbatim. Pill rendering lands in plan 03-07.

## Task Commits

1. **Task 0: Wave-0 red tests** — `9f6a6f4` (test)
2. **Task 1: Kalshi + Polymarket acquire/429 wiring** — `55fbe19` (feat)
3. **Task 2: Periodic broadcaster + /api/system integration + stats fields** — `9e96619` (feat)

## Adapter Methods Now Acquiring a Rate-Limit Token

| Adapter | Method | Acquire site | 429 handling |
|---------|--------|--------------|---------------|
| `KalshiAdapter` | `place_fok` | via `_post_order` (line 1 of inner method) | status==429 → apply_retry_after + FAILED Order |
| `KalshiAdapter` | `cancel_order` | explicit acquire before `_delete_order` | status==429 inside `_delete_order` → `False` |
| `KalshiAdapter` | `cancel_all` | explicit acquire (stub mode) | no-op — stub returns `[]` |
| `KalshiAdapter` | `get_order` | explicit acquire before `_fetch_order` | status==429 → FAILED with `rate_limited` in error |
| `KalshiAdapter` | `list_open_orders_by_client_id` | explicit acquire before `_list_orders` | status==429 → `[]` + circuit failure |
| `PolymarketAdapter` | `place_fok` | inside the reconcile-submit branch (pre-existing) | generic `except Exception` routes 429 markers to apply_retry_after + FAILED Order |
| `PolymarketAdapter` | `cancel_order` | explicit acquire before SDK call | 429 markers → `False` + circuit failure |
| `PolymarketAdapter` | `cancel_all` | explicit acquire (stub mode) | no-op — stub returns `[]` |
| `PolymarketAdapter` | `get_order` | explicit acquire before SDK call | 429 markers → FAILED with `rate_limited` in error |

`KalshiAdapter.check_depth` and `_fetch_depth` are public-endpoint reads (no auth header, low volume, wrapped in `@transient_retry`) and were intentionally NOT modified — they lie outside the write-rate-limit threat surface and have their own failure path (`return (False, 0.0)` on any error). Plan 03-07 / later hardening may extend coverage if needed.

## `rate_limit_state` WebSocket Event — Payload Contract (for plan 03-07 UI)

Emitted every 2 seconds by `ArbiterAPI._rate_limit_broadcast_loop` when ≥1 WS client is connected AND `engine.adapters` has at least one adapter with a `rate_limiter` attribute:

```json
{
  "type": "rate_limit_state",
  "payload": {
    "kalshi": {
      "name": "kalshi-exec",
      "available_tokens": 10,
      "max_requests": 10.0,
      "time_window": 1.0,
      "remaining_penalty_seconds": 0.0,
      "penalty_count": 0,
      "last_wait_seconds": 0.0,
      "total_wait_time": 0.0,
      "total_acquires": 0,
      "last_penalty_reason": ""
    },
    "polymarket": {
      "name": "poly-exec",
      "available_tokens": 4,
      "max_requests": 5.0,
      "time_window": 1.0,
      "remaining_penalty_seconds": 0.5,
      "penalty_count": 1,
      "last_wait_seconds": 0.5,
      "total_wait_time": 0.5,
      "total_acquires": 1,
      "last_penalty_reason": "polymarket_429"
    }
  }
}
```

Required fields for plan 03-07 pill renderer (enforced by `test_rate_limit_ws_event_shape`):
- `available_tokens: int` — current bucket fill
- `max_requests: float` — bucket cap
- `remaining_penalty_seconds: float` — >0 when in 429 backoff

Optional but useful for operator diagnostics:
- `last_penalty_reason: str` — `"kalshi_429"` / `"polymarket_429"` (lower-case tag for filtering) or `""`
- `penalty_count: int` — cumulative 429s since process start
- `total_acquires: int` — successful acquires since process start

## `/api/system` — rate_limits key

`GET /api/system` now returns (in addition to all existing keys):

```json
{
  "...": "...existing keys unchanged...",
  "safety": { "armed": false, "available": false },
  "rate_limits": {
    "kalshi": { /* same RateLimiter.stats shape as above */ },
    "polymarket": { /* same */ }
  }
}
```

Regression guarantee: the two existing integration tests (`test_api_and_dashboard_contracts`, `test_ws_bootstrap`) do NOT assert on `rate_limits` absence; the new key is additive. `test_system_endpoint_includes_rate_limits` positively asserts presence.

## Threat Mitigations Implemented

| Threat | Mitigation in this plan |
|--------|-------------------------|
| T-3-04-A — Rate limiter bypass via parallel sessions | Unchanged; adapters still receive a shared `RateLimiter` from `main.py`. No new session paths introduced. |
| T-3-04-B — Rate limiter starvation under load | Unchanged; token-bucket refill rate is controlled by SafetyConfig (set in `main.py`). |
| T-3-04-C — Venue bans via 429 repetition | `apply_retry_after` respects `Retry-After` exactly; FOK POSTs NEVER retry after 429; `circuit.record_failure` eventually opens the breaker. Enforced by `test_place_fok_429_applies_retry_after` (no second `session.post` call). |
| T-3-04-D — rate_limit_state broadcast exposes activity | Accepted per plan; broadcast is public read-only (no auth); no secrets in payload. |
| T-3-04-E — Forged Retry-After tricks us into long backoff | `delay = min(delay, 60.0)` applied at every adapter callsite after `apply_retry_after` returns. 8 callsites, grep-verified. |
| T-3-04-F — Periodic broadcast floods slow WS clients | `_broadcast_json` already handles slow clients via `try/except` + remove-from-list pattern (inherited). 2s interval is conservative. |

## Dashboard JS — deliberately unchanged

`arbiter/web/dashboard.js` was NOT modified. Grep confirms the plan-03-01 tolerance branch is intact:

```bash
$ grep -c 'message.type === "rate_limit_state"' arbiter/web/dashboard.js
1
```

State-only mutation: `state.safety = { ...(state.safety || {}), rateLimits: message.payload }`. No render code touched — plan 03-07 owns the pill UI.

## Tests

**New (all green):**

| Test | File | Verifies |
|------|------|----------|
| `test_place_fok_acquires_rate_token_before_http` | `arbiter/execution/adapters/test_kalshi_adapter.py` | acquire→post ordering via call-log tracking |
| `test_place_fok_429_applies_retry_after` | same | 429 → apply_retry_after("3", fallback_delay=2.0, reason="kalshi_429") + circuit.record_failure + FAILED + error contains "rate_limited" + NO retry |
| `test_cancel_order_acquires_rate_token` | same | acquire→delete ordering for cancel_order |
| `test_cancel_all_acquires_token_per_chunk` | same | cancel_all stub acquires at least once (forward-compat for plan 03-05) |
| `test_place_fok_acquires_rate_token_before_sdk` | `arbiter/execution/adapters/test_polymarket_adapter.py` | acquire precedes client.create_order AND client.post_order |
| `test_429_via_sdk_exception_applies_retry_after` | same | SDK-raised `Exception("HTTP 429 …")` → apply_retry_after(reason="polymarket_429") + circuit.record_failure + FAILED with "rate_limited" + post_order NOT called |
| `test_rate_limit_ws_event_shape` | `arbiter/test_api_integration.py` | In-process TestServer/TestClient + WS connect → `rate_limit_state` event arrives within 4.5s with platform keys each carrying {available_tokens, max_requests, remaining_penalty_seconds} |
| `test_system_endpoint_includes_rate_limits` | same | `GET /api/system` JSON body has top-level `rate_limits` dict keyed by platform |

**Regression sweep (excluding two pre-existing failures documented in `deferred-items.md`):**

- `pytest arbiter/execution/adapters/` → 64 passed (was 48 before plan; 16 new/updated assertions all green)
- `pytest arbiter/utils/test_retry.py` → 3 passed (no regression from `max_requests` + `time_window` additions)
- `pytest arbiter/test_api_integration.py -k rate_limit` → 2 passed
- Combined Phase 3 sweep `pytest arbiter/safety/ arbiter/execution/ arbiter/test_api_safety.py arbiter/utils/test_retry.py arbiter/test_api_integration.py` (deselecting the two pre-existing failures) → **130 passed, 3 skipped, 2 deselected, 0 failed**.

Pre-existing unrelated failures (documented in `deferred-items.md`):
- `test_complete_stub_satisfies_protocol` (from plan 03-02 notes; Protocol/Python 3.13 plumbing)
- `test_api_and_dashboard_contracts` (subprocess binding fails in sandbox; reproducibly fails at the 03-04 base commit)

## Acceptance-Criteria Greps (all met)

### Task 1

| Expected | Actual |
|----------|--------|
| `grep -c "await self.rate_limiter.acquire()" arbiter/execution/adapters/kalshi.py` ≥ 4 | 5 |
| `grep -c "await self.rate_limiter.acquire()" arbiter/execution/adapters/polymarket.py` ≥ 3 | 4 |
| `grep -c "apply_retry_after" arbiter/execution/adapters/kalshi.py` ≥ 1 | 4 |
| `grep -c "apply_retry_after" arbiter/execution/adapters/polymarket.py` ≥ 1 | 3 |
| `grep -c "rate_limited" arbiter/execution/adapters/kalshi.py` ≥ 1 | 6 |
| `grep -c "kalshi_429" arbiter/execution/adapters/kalshi.py` ≥ 1 | 4 |
| `grep -c "polymarket_429" arbiter/execution/adapters/polymarket.py` ≥ 1 | 3 |
| `grep -c "circuit.record_failure" kalshi.py polymarket.py` ≥ 2 combined | 12 combined |

### Task 2

| Expected | Actual |
|----------|--------|
| `grep -c "async def _rate_limit_broadcast_loop" arbiter/api.py` = 1 | 1 |
| `grep -c "rate_limit_state" arbiter/api.py` ≥ 2 | 6 |
| `grep -c '"rate_limits"' arbiter/api.py` ≥ 1 | 1 |
| `grep -c "_rate_limit_task" arbiter/api.py` ≥ 2 | 5 |
| `grep -c "available_tokens" arbiter/utils/retry.py` ≥ 1 | 2 |
| `grep -c "max_requests" arbiter/utils/retry.py` ≥ 1 | 7 |
| `grep -c "remaining_penalty_seconds" arbiter/utils/retry.py` ≥ 1 | 3 |
| `grep -c 'message.type === "rate_limit_state"' arbiter/web/dashboard.js` = 1 | 1 |

### Task 0 (collect)

| Expected | Actual |
|----------|--------|
| `pytest … -k "rate_limit or 429 or acquire" --collect-only` ≥ 6 | 8 |

## Files Created/Modified

- `arbiter/execution/adapters/kalshi.py` — all 5 outbound methods wrapped; _post_order / _fetch_order / _list_orders return headers
- `arbiter/execution/adapters/polymarket.py` — all 4 outbound methods wrapped; new `_is_rate_limit_error` helper
- `arbiter/execution/adapters/test_kalshi_adapter.py` — 4 new tests + updated `test_fok_non_2xx_returns_failed_with_status_in_error` (500 instead of 429)
- `arbiter/execution/adapters/test_polymarket_adapter.py` — 2 new tests
- `arbiter/api.py` — `_rate_limit_broadcast_loop` method, serve() launches + cancels it, `rate_limits` key in `_build_system_snapshot`
- `arbiter/utils/retry.py` — `max_requests` and `time_window` added to `RateLimiter.stats`
- `arbiter/test_api_integration.py` — 2 new in-process aiohttp TestServer-based tests
- `.planning/phases/03-safety-layer/deferred-items.md` — documented pre-existing subprocess-binding flake

## Decisions Made

See frontmatter `decisions:` for the full list. Most load-bearing:

1. **Header tuple in Kalshi helpers:** Changed `_post_order`, `_fetch_order`, `_list_orders` from `(status, body)` → `(status, body, headers_dict)` so outer methods can read `Retry-After`. Headers are copied into a plain `dict` inside the `async with` block so the caller doesn't hold a reference to the aiohttp response after context exit.
2. **Polymarket 429 detection is string-based:** py-clob-client does not expose a typed HTTP-exception hierarchy, so `_is_rate_limit_error(exc)` does case-insensitive substring matching on markers `"429"`, `"rate limit"`, `"rate_limit"`, `"too many requests"`. This is brittle by nature but documented in the helper docstring; Phase 4 sandbox testing should verify the exact exception wording and tighten if needed.
3. **60s Retry-After cap at the callsite:** `delay = min(delay, 60.0)` is applied at every adapter 429 branch (not inside `apply_retry_after`). Keeps the RateLimiter semantically pure while adapters — the trust boundary to external venues — enforce the bound per T-3-04-E.
4. **cancel_all stub still acquires:** Even though the plan-03-01 stub returns `[]` unconditionally, it acquires a token first. This is forward-compat for plan 03-05's chunked batch-cancel replacement (which acquires per chunk) and enforced by `test_cancel_all_acquires_token_per_chunk`.

## Deviations from Plan

**None** — plan executed exactly as written with two small, deliberate additions that were explicitly permitted by the plan text:

1. **Updated `test_fok_non_2xx_returns_failed_with_status_in_error` from status=429 to status=500.** The pre-existing test asserted the generic non-2xx branch using status=429 and the literal body "rate limited". With dedicated 429 handling landed per plan, 429 bypasses the generic branch, so the test now uses 500 to continue covering the generic branch. The plan's Task 1 `<behavior>` block explicitly calls for the 429 branch to short-circuit; this test update is the mechanical consequence. Committed in Task 1 (`55fbe19`).
2. **Added `time_window` to `RateLimiter.stats`** alongside the required `max_requests` field. The plan's action step says "verify (and patch if missing) that the returned dict contains … `time_window` (float)" — plan explicitly mandates it, but the field name in retry.py was `window_seconds`. Added `time_window` as an alias (both as float) so the dashboard contract matches the plan spec while preserving the internal `window_seconds` attribute name.

---

**Total deviations:** 0 (both above are plan-mandated mechanical consequences, not deviations).
**Impact on plan:** None. All acceptance criteria met; all SAFE-04 observable truths satisfied.

## Issues Encountered

- **Pre-existing subprocess flake** in `test_api_and_dashboard_contracts` — reproducibly fails at the 03-04 base commit with "Server on port N did not become ready" after the 15s subprocess bootstrap timeout. Documented in `deferred-items.md`. Unrelated to any change in this plan. My two new tests use in-process `aiohttp.test_utils.TestServer` so they don't depend on subprocess bootstrap.

## SAFE-04 Observable Truths — all met

- [x] Every outbound call from `KalshiAdapter.place_fok`, `cancel_order`, `get_order`, `list_open_orders_by_client_id`, and `cancel_all` first awaits `self.rate_limiter.acquire()` before any HTTP I/O — verified by `test_place_fok_acquires_rate_token_before_http`, `test_cancel_order_acquires_rate_token`, `test_cancel_all_acquires_token_per_chunk` + grep count = 5.
- [x] Every outbound call from `PolymarketAdapter` mirrors the same acquire() pattern before any SDK call — verified by `test_place_fok_acquires_rate_token_before_sdk` + grep count = 4.
- [x] On HTTP 429 (Kalshi) or exception-signaled 429 (Polymarket), the adapter invokes `self.rate_limiter.apply_retry_after(header, fallback_delay=2.0, reason='<platform>_429')` AND records a circuit failure AND returns FAILED Order with error containing 'rate_limited' — NEVER retries a FOK POST — verified by `test_place_fok_429_applies_retry_after` (asserts `session.post` called exactly once) and `test_429_via_sdk_exception_applies_retry_after` (asserts `post_order` never called).
- [x] `arbiter/api.py` runs a periodic `_rate_limit_broadcast_loop` that emits a `rate_limit_state` WebSocket event every 2 seconds with `{platform: stats_dict}` for every adapter — verified by `test_rate_limit_ws_event_shape` (in-process TestServer/TestClient round-trip).
- [x] The `stats_dict` exposes at minimum: `available_tokens` (int), `max_requests` (float), `remaining_penalty_seconds` (float) — verified by per-key assertion in `test_rate_limit_ws_event_shape`.
- [x] Dashboard JS tolerance branch (plan 03-01) captures `rate_limit_state` into `state.safety.rateLimits` — confirmed unchanged; existing panels render unbroken (grep = 1, `node --check arbiter/web/dashboard.js` exit 0).
- [x] Unit tests prove: `place_fok` acquires token before `session.post`; 429 triggers `apply_retry_after` with exact header value; FOK does NOT retry after 429; `/api/system` snapshot includes `rate_limits` key — all green.

## Threat Flags

No new attack surface beyond what the plan's `<threat_model>` covered. Grep of the files modified confirms:
- No new network endpoints (handle_* method count unchanged; added 3 handler routes in plan 03-01, nothing new here).
- No new auth paths.
- No new file I/O.
- No schema changes.
- One new WebSocket event type (`rate_limit_state`) — public read-only, payload is already-public rate-limit stats.

## Next Phase Readiness

- Plan 03-05 (cancel_all full implementation): the acquire-before-I/O invariant is enforced by `test_cancel_all_acquires_token_per_chunk` so the chunked replacement only needs to call acquire() per-chunk.
- Plan 03-07 (UI): the `rate_limit_state` payload contract is stable; dashboard renderer can consume `{platform: {available_tokens, max_requests, remaining_penalty_seconds, last_penalty_reason}}` directly from `state.safety.rateLimits` without transformation.

## Self-Check: PASSED

- **Files modified (8):**
  - `arbiter/execution/adapters/kalshi.py` — present, 5 acquires + 4 `kalshi_429` tags
  - `arbiter/execution/adapters/polymarket.py` — present, 4 acquires + 3 `polymarket_429` tags
  - `arbiter/execution/adapters/test_kalshi_adapter.py` — present, 4 new tests collect
  - `arbiter/execution/adapters/test_polymarket_adapter.py` — present, 2 new tests collect
  - `arbiter/api.py` — present, `_rate_limit_broadcast_loop` defined, `rate_limits` in snapshot
  - `arbiter/utils/retry.py` — present, `max_requests` + `time_window` in stats
  - `arbiter/test_api_integration.py` — present, 2 new rate_limit tests collect and pass
  - `.planning/phases/03-safety-layer/deferred-items.md` — present, entry appended for subprocess flake
- **Commits (3):** `9f6a6f4`, `55fbe19`, `9e96619` — all visible via `git log --oneline`
- **Pytest gate:** `pytest arbiter/execution/adapters/ arbiter/utils/test_retry.py arbiter/test_api_integration.py` excluding the 2 pre-existing deferred failures → **70 passed**
- **Full Phase 3 regression:** `pytest arbiter/safety/ arbiter/execution/ arbiter/test_api_safety.py arbiter/utils/test_retry.py arbiter/test_api_integration.py` excluding 2 pre-existing deferred failures → **130 passed, 3 skipped, 0 failed**
- **JS regression:** `node --check arbiter/web/dashboard.js` → exit 0 (file unchanged by this plan)
- **Import smoke:** `python -c "from arbiter.api import ArbiterAPI; assert '_rate_limit_broadcast_loop' in dir(ArbiterAPI)"` → exit 0
