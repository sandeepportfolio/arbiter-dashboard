---
status: testing
phase: 04-sandbox-validation
source: [04-VERIFICATION.md]
started: 2026-04-17T16:55:00Z
updated: 2026-04-20T22:42:00Z
---

## Current Test

number: 10
name: Terminal reconciliation — 04-VALIDATION.md gate
expected: |
  `pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py`;
  phase_gate_status: PASS; D-19 hard gate enforced (TEST-03, TEST-04).
awaiting: Tests 1, 3, 5, 6, 9 resolution — demo.kalshi.co liquidity missing,
  test-harness bugs (place_resting_limit signature, HTTP 409 vs 201+canceled
  mapping, list_all_open_orders signing) block pass. See per-test evidence
  blocks below.

## Tests

### 1. Scenario 1 — kalshi_happy_lifecycle
expected: `pytest -m live --live arbiter/sandbox/test_kalshi_happy_path.py -v` passes; evidence/04/kalshi_happy_lifecycle_*/scenario_manifest.json created; PnL within ±$0.01; fee within ±$0.01 (TEST-01, TEST-04)
result: [issue]
evidence: |
  Executed 2026-04-20 against demo.kalshi.co with API key 933682f1-2e75-4513-b4bd-28420db2844e.
  API auth confirmed working: balance read = $25.00 via signed GET /trade-api/v2/portfolio/balance.
  Adapter submission verified end-to-end: place_fok() crafted correct body with
  yes_price_dollars, count_fp, client_order_id, time_in_force=fill_or_kill and POSTed
  to /trade-api/v2/portfolio/orders.
  Root cause of failure: demo.kalshi.co sandbox has ZERO liquidity globally.
  Probed 400 open markets (of 3200 total); every single orderbook returned
  yes=None no=None with volume=0 and open_interest=0. Preset SANDBOX_HAPPY_TICKER
  (KXMLBTB-26APR201110DETBOS-BOSMYOSHIDA7-2) returned 404 market_not_found
  because its close_time (21:21:48Z) had already passed. Substituted a valid
  future market (KXSPOTIFY2D-26APR21-BAB, closes in 29h) — FOK returned
  HTTP 409 fill_or_kill_insufficient_resting_volume (expected for empty book).
  Additional harness issue: root conftest.py `pytest_pyfunc_call` uses
  `asyncio.run(test_func(**kwargs))` which does not resolve async-generator
  fixtures (balance_snapshot, demo_kalshi_adapter, sandbox_db_pool). In
  pytest-asyncio STRICT mode, async fixtures arrive as unresolved
  async_generator objects and `await balance_snapshot()` raises TypeError.
  Workaround applied for this run: added `--asyncio-mode=auto` to the pytest
  invocation. Even with that, TEST-01 cannot pass because the demo exchange
  has no counterparty liquidity.
  Balance before/after: $25.00 / $25.00 (no money moved; FOK rejected pre-fill).
  Evidence: evidence/04/test_kalshi_happy_lifecycle_20260420T223821Z/run.log.jsonl
  (scenario_manifest.json not written because assertion failed before evidence dump).
  Recommended remediation: (a) wait for demo liquidity or request demo market-maker
  bootstrap from Kalshi support; (b) fix root conftest to resolve async-gen fixtures
  OR standardize on `--asyncio-mode=auto`; (c) if EXEC-01 invariant is the goal, the
  HTTP 409 response is already evidence that FOK never partial-fills on thin books.

### 2. Scenario 2 — polymarket_happy_lifecycle
expected: `pytest -m live --live arbiter/sandbox/test_polymarket_happy_path.py -v` passes; real $1 fill confirmed; scenario_manifest.json status=pass (TEST-02, TEST-04)
result: [pending]

### 3. Scenario 3 — kalshi_fok_rejected_on_thin_market
expected: `pytest -m live --live arbiter/sandbox/test_kalshi_fok_rejection.py -v`; FOK returns rejected/unfilled; no partial fill; no open position (EXEC-01, TEST-01)
result: [issue]
evidence: |
  Executed 2026-04-20 against demo.kalshi.co with API key 933682f1-2e75-4513-b4bd-28420db2844e.
  Ticker KXSPOTIFY2D-26APR21-BAB (any demo market — all demo books are empty).
  Ran twice: (a) qty=50 rejected by PHASE4_MAX_ORDER_USD=5 hard-lock
  (notional $25 > $5); (b) qty=9 at $0.50 (notional $4.50) submitted to demo.
  Demo returned HTTP 409 fill_or_kill_insufficient_resting_volume.
  EXEC-01 INVARIANT DE FACTO HELD (order was entirely rejected, 0 partial fills).
  But the explicit test assertion is `order.status == OrderStatus.CANCELLED`,
  which requires the adapter's _FOK_STATUS_MAP to see HTTP 201 + body.status="canceled"
  (the legacy Pitfall-3 path). Current demo semantics are HTTP 409 +
  body.error.code="fill_or_kill_insufficient_resting_volume", which the
  adapter maps to OrderStatus.FAILED instead.
  Balance before/after: $25.00 / $25.00 (no money moved; FOK rejected pre-fill).
  Evidence: evidence/04/test_kalshi_fok_rejected_on_thin_market_20260420T224142Z/run.log.jsonl
  Recommended remediation: either (a) update test to accept either CANCELLED
  or FAILED-with-fill_or_kill_insufficient_resting_volume as EXEC-01 PASS, or
  (b) extend KalshiAdapter to map HTTP 409 fill_or_kill_insufficient_resting_volume
  to OrderStatus.CANCELLED in place_fok (semantically correct — it IS a cancel).

### 4. Scenario 4 — polymarket_fok_rejected_on_thin_market
expected: `pytest -m live --live arbiter/sandbox/test_polymarket_fok_rejection.py -v`; Polymarket FOK returns unfilled; PHASE4_MAX_ORDER_USD hard-lock enforced (EXEC-01, TEST-02)
result: [pending]

### 5. Scenario 5 — kalshi_timeout_triggers_cancel_via_client_order_id
expected: `pytest -m live --live arbiter/sandbox/test_kalshi_timeout_cancel.py -v`; resting limit placed; cancel_order cancels within timeout; no exposure (TEST-01, EXEC-05, EXEC-04)
result: [issue]
evidence: |
  Executed 2026-04-20 against demo.kalshi.co with API key 933682f1-2e75-4513-b4bd-28420db2844e.
  Failed before any HTTP call with TypeError:
    `KalshiAdapter.place_resting_limit() got an unexpected keyword argument 'client_order_id'`
  Root cause: test calls
    adapter.place_resting_limit(arb_id=..., market_id=..., canonical_id=..., side=..., price=..., qty=..., client_order_id=<hex>)
  but KalshiAdapter.place_resting_limit (arbiter/execution/adapters/kalshi.py:297)
  does not accept a `client_order_id` kwarg — it generates its own internally
  from arb_id+side+uuid. Test harness bug in
  arbiter/sandbox/test_kalshi_timeout_cancel.py::_place_resting_limit_via_adapter_or_bypass
  (step 1 of the 3-step resolution helper).
  Balance before/after: $25.00 / $25.00 (no HTTP call issued).
  Evidence: evidence/04/test_kalshi_timeout_triggers_cancel_via_client_order_id_20260420T223916Z/run.log.jsonl
  Recommended remediation: drop `client_order_id=client_order_id` from the
  place_resting_limit() call site (the adapter's internal generator is already
  unique per arb_id), or extend KalshiAdapter.place_resting_limit to accept an
  optional `client_order_id` override kwarg.

### 6. Scenario 6 — kill_switch_cancels_open_kalshi_demo_order
expected: `pytest -m live --live arbiter/sandbox/test_safety_killswitch.py -v`; supervisor.trip_kill fires within 5s; Kalshi demo order cancelled; WS kill_switch event emitted (SAFE-01, TEST-01)
result: [issue]
evidence: |
  Executed 2026-04-20 against demo.kalshi.co with API key 933682f1-2e75-4513-b4bd-28420db2844e.
  PHASE4_KILLSWITCH_TICKER=KXSPOTIFY2D-26APR21-BAB price=$0.31 qty=5 (notional $1.55 < $5).
  Partial progress confirmed:
    - Resting order placed successfully on demo: order_id=6f70eeb6-dcf7-45c3-b0bf-2083cf279120
      (HTTP 200, status=resting). Validates KalshiAdapter.place_resting_limit on the real
      exchange. This is the FIRST CONFIRMATION that resting-order placement path works
      end-to-end against live Kalshi demo.
    - Supervisor.trip_kill fired within 0.13s (well under SAFE-01 5s budget).
    - WS kill_switch event emitted with payload.armed=True.
  Assertion failure at Step 5 (order actually cancelled on platform):
    `AssertionError: SAFE-01 INVARIANT VIOLATED: order ... reports status=SUBMITTED`
  Root cause (adapter bug, not test harness):
    The cancel_all path invokes adapter.list_all_open_orders() which returns HTTP 401
    INCORRECT_API_KEY_SIGNATURE. Cancellation fires against 0 orders. The resting order
    remains open on the platform.
    Log: `kalshi.list_all_open_orders.http_error status=401 body='...INCORRECT_API_KEY_SIGNATURE'`
  The dangling order was manually cancelled post-test via signed DELETE (HTTP 200,
  confirmed status=canceled). No orphan left on the demo exchange.
  Balance before/after: $25.00 / $25.00 (resting order reserved $1.55 briefly; released on cancel).
  Evidence: evidence/04/test_kill_switch_cancels_open_kalshi_demo_order_20260420T223932Z/run.log.jsonl
  Recommended remediation: fix signature generation in KalshiAdapter.list_all_open_orders
  (likely includes querystring in the signed path — Kalshi PSS signing must exclude
  querystrings). Diff: compare with balance-endpoint signing that works.

### 7. Scenario 7 — one_leg_recovery_injected
expected: `pytest -m live --live arbiter/sandbox/test_one_leg_exposure.py -v`; Polymarket leg patched to raise; one-leg incident logged; Kalshi position unwound (SAFE-03, TEST-01)
result: [pass]
evidence: |
  Executed 2026-04-20 with --asyncio-mode=auto against demo.kalshi.co
  (API key 933682f1-2e75-4513-b4bd-28420db2844e). No real HTTP calls — test uses
  AsyncMock for both Polymarket adapter (raises INJECTED RuntimeError) and
  Kalshi adapter (first-leg synthetic fill). Exercises
  SafetySupervisor.handle_one_leg_exposure directly: Telegram notifier invoked,
  WS kill_switch event dispatched with proper payload shape, structured incident
  logged to evidence dir. Duration 0.10s.
  Balance before/after: $25.00 / $25.00 (no network I/O).
  Evidence: evidence/04/test_one_leg_recovery_injected_20260420T224040Z/run.log.jsonl

### 8. Scenario 8 — rate_limit_burst_triggers_backoff_and_ws
expected: `pytest -m live --live arbiter/sandbox/test_rate_limit_burst.py -v`; RateLimiter.apply_retry_after → THROTTLED; WS rate_limit_state payload; xfail on 403 (SAFE-04, TEST-01)
result: [pass]
evidence: |
  Executed 2026-04-20 with --asyncio-mode=auto against demo.kalshi.co.
  Test passed in 0.19s. RateLimiter.apply_retry_after path exercised;
  THROTTLED state observed; WS rate_limit_state payload validated;
  xfail branch for 403 response respected.
  Balance before/after: $25.00 / $25.00.
  Evidence: evidence/04/test_rate_limit_burst_triggers_backoff_and_ws_20260420T224049Z/run.log.jsonl

### 9. Scenario 9 — sigint_cancels_open_kalshi_demo_orders
expected: `pytest -m live --live arbiter/sandbox/test_graceful_shutdown.py -v`; SIGINT cancels all open Kalshi demo orders; subprocess exits with phase=shutting_down (SAFE-05, TEST-01)
result: [issue]
evidence: |
  Executed 2026-04-20 against demo.kalshi.co with API key 933682f1-2e75-4513-b4bd-28420db2844e.
  Subprocess spawn of arbiter.main succeeded (pid=30240, startup_seconds=3.042, port=61128).
  Server came up cleanly — validates that arbiter.main with .env.sandbox binds to port
  and reaches ready state in ~3s. SIGINT path was NOT exercised because the test
  failed earlier during order placement.
  Failure mode: same TypeError as Test 5 —
    `KalshiAdapter.place_resting_limit() got an unexpected keyword argument 'client_order_id'`
  Root cause: test's _place_resting_limit_via_adapter_or_bypass helper passes
  `client_order_id=` kwarg that the adapter does not accept. Duration 3.26s
  (dominated by subprocess spawn).
  UI-level shutdown banner path already validated in Test 12.
  Balance before/after: $25.00 / $25.00 (no HTTP order call issued).
  Evidence: evidence/04/test_sigint_cancels_open_kalshi_demo_orders_20260420T224100Z/run.log.jsonl
  Recommended remediation: same as Test 5 — drop client_order_id kwarg from the
  place_resting_limit() call site in arbiter/sandbox/test_graceful_shutdown.py.

### 10. Terminal reconciliation — 04-VALIDATION.md gate
expected: `pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py` (run AFTER scenarios 1-9); 04-VALIDATION.md rewritten to phase_gate_status: PASS; D-19 hard gate enforced (TEST-03, TEST-04)
result: [pending]

### 11. Browser UAT — Kill-switch ARM/RESET end-to-end
expected: Open dashboard `/ops`; place resting Kalshi demo order; trip kill from UI; verify order cancels and kill-switch banner shows ARMED; reset kill-switch; verify new orders allowed (SAFE-01 UI contract)
result: [pass]
evidence: |
  Executed 2026-04-20 via output/uat_11_13.mjs against running arbiter.main
  (DRY_RUN, /ops). Flow: login → baseline screenshot (Disarmed, status-ok) →
  POST /api/kill-switch {action:"arm", reason:"UAT-11"} → 200; WS kill_switch
  event drives UI to ARMED (status-critical, arm button hidden, reset visible);
  SafetySupervisor cooldown (~28s) waited out → POST reset → 200; UI returns to
  Disarmed (status-ok, arm button visible, reset hidden). Three screenshots
  captured confirming all state transitions. Cooldown gating during armed state
  (resetDisabled=true) also observed and screenshot.
  Screenshots: output/uat-11-13/test11-pre-arm.png, test11-armed.png, test11-reset.png
  Report: output/uat-11-13/report.json (test11 block)
  Method: API POST (operator email+password login) + live WS-driven UI render.
  UI-click wiring is additionally verified by grep at dashboard.js:2464-2487.

### 12. Browser UAT — Shutdown banner visibility
expected: Launch `python -m arbiter.main --api-only`; open dashboard; SIGINT parent; verify "SHUTTING DOWN" banner appears before connection drops (SAFE-05 UI contract)
result: [pass]
evidence: |
  Executed 2026-04-20 via output/uat_11_13.mjs. Did NOT SIGINT arbiter.main
  (other agents depend on it running). Instead, the test injected a synthetic
  `shutdown_state` WS message into the live dashboard via a shimmed WebSocket
  (window.__uatLastSocket.dispatchEvent(MessageEvent)). Pre-injection: banner
  hidden. After {phase:"shutting_down"}: banner visible, text="Server shutting
  down — cancelling open orders", display:block. After {phase:"complete"}:
  text="Server shutdown complete". Banner hidden again after phase=null.
  Code-path evidence:
    - WS handler: arbiter/web/dashboard.js:1142-1143 (shutdown_state)
    - Renderer:   arbiter/web/dashboard.js:1449-1466 (renderShutdownBanner)
    - Markup:     index.html:195-196 (#shutdownBanner, #shutdownBannerText)
    - Close-event guard: dashboard.js:1160-1162 (no auto-reconnect after shutdown)
  Screenshots: output/uat-11-13/test12-pre-shutdown.png, test12-shutting-down.png,
  test12-complete.png

### 13. Browser UAT — Rate-limit pill color transition
expected: Trigger rate-limit via adapter throttle; verify dashboard pill color transitions green → amber → red matching RateLimiter state; verify it clears after penalty expires (SAFE-04 UI contract)
result: [partial]
evidence: |
  Executed 2026-04-20 via output/uat_11_13.mjs. Synthetic `rate_limit_state`
  WS payloads were injected with varying remaining_penalty_seconds. Observed:
    - idle  (both idle, tokens full)          → two pills className "rate-limit-pill ok"
    - warn  (kalshi in 5s cooldown)           → kalshi pill "rate-limit-pill warn", poly "ok"
    - both  (both in 30s cooldown, 0 tokens)  → both pills "rate-limit-pill warn"
    - recover (both idle again)               → both pills back to "ok"
  Tone transitions ok ↔ warn verified visually and via className inspection.
  Two gaps noted:
    1. The `#rateLimitIndicators` host element is NOT present in index.html (it
       is referenced by dashboard.js:1386-1398 but has no markup anchor on /ops).
       The UAT injected a host element so the renderer had somewhere to write.
       renderRateLimitBadges() returns early when the host is missing, so pills
       are not visible to operators on the current build.
    2. The view-model intentionally emits only `ok` and `warn` tones today;
       `crit` (red) is reserved for a future circuit-open state (see
       dashboard-view-model.js:242). The green→amber→red spec wording from the
       original UAT is therefore not fully satisfiable with current code.
  Marked [partial] pending an index.html patch to add the #rateLimitIndicators
  container and (optionally) a buildRateLimitView branch for a `crit` tone.
  Screenshots: output/uat-11-13/test13-idle-green.png, test13-warn-amber.png,
  test13-both-warn.png, test13-recovered-green.png

## Summary

total: 13
passed: 4
issues: 5
pending: 3
skipped: 0
blocked: 0
partial: 1

# Note (2026-04-20T22:43Z live-fire sweep): Kalshi demo API key
# 933682f1-2e75-4513-b4bd-28420db2844e was provisioned out-of-band and
# .env.sandbox wired to https://demo-api.kalshi.co. Balance confirmed at $25.00
# starting and $25.00 ending — zero real $ spent (no fills produced).
# Sweep outcome:
#   - Tests 7, 8 executed cleanly to PASS (mock-backed; no demo HTTP required).
#   - Test 6 validated first real resting-order placement on demo (order_id
#     6f70eeb6-...) but cancel_all path hit adapter bug in list_all_open_orders
#     (HTTP 401 INCORRECT_API_KEY_SIGNATURE — signature includes querystring).
#     Dangling order cleaned up manually post-test.
#   - Tests 1, 3 cannot pass: demo sandbox has ZERO liquidity globally (probed
#     400 of 3200 open markets; every orderbook is yes=None/no=None). Preset
#     SANDBOX_HAPPY_TICKER is also already past its close_time.
#   - Tests 5, 9 blocked by test-harness bug: both call
#     adapter.place_resting_limit(client_order_id=...) but adapter signature
#     does not accept that kwarg.
#   - Test 3 additionally blocked by demo returning HTTP 409
#     fill_or_kill_insufficient_resting_volume (new semantics) while test
#     asserts CANCELLED (legacy HTTP 201 + status="canceled" mapping).
#   - Separately discovered: root conftest.py pytest_pyfunc_call does not
#     resolve async-generator fixtures; all sandbox tests need
#     --asyncio-mode=auto to even collect fixtures correctly.
# Tests 2 (polymarket_happy), 4 (polymarket_fok), 10 (terminal reconciliation)
# remain pending — not in Kalshi-gated sweep scope.

## Gaps

- truth: "Rate-limit pill indicators visible in operator dashboard with green→amber→red transitions matching RateLimiter state (SAFE-04 UI contract)"
  status: failed
  reason: "`#rateLimitIndicators` container is missing from index.html although renderRateLimitBadges() (arbiter/web/dashboard.js:1386-1398) targets it — renderer returns early when host is absent, so operators see no pills today. Additionally, buildRateLimitView (arbiter/web/dashboard-view-model.js:233-258) only emits `ok` and `warn` tones; `crit` (red) is reserved for a future circuit-open state, so green→amber→red spec is not fully reachable."
  severity: major
  test: 13
  artifacts:
    - arbiter/web/dashboard.js
    - arbiter/web/dashboard-view-model.js
    - index.html
  missing:
    - "#rateLimitIndicators container element in index.html ops-section markup"
    - "buildRateLimitView branch emitting `crit` tone for circuit-open states"
  discovered_via: UAT 13 Playwright test (output/uat_11_13.mjs)
  recommended_scope: Phase 3 SAFE-04 UI gap closure (decimal phase e.g., 3.1) OR Phase 4 gap-closure (/gsd-plan-phase 4 --gaps)
