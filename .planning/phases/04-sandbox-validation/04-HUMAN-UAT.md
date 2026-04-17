---
status: partial
phase: 04-sandbox-validation
source: [04-VERIFICATION.md]
started: 2026-04-17T16:55:00Z
updated: 2026-04-17T16:55:00Z
---

## Current Test

[awaiting operator `.env.sandbox` provisioning]

## Tests

### 1. Scenario 1 — kalshi_happy_lifecycle
expected: `pytest -m live --live arbiter/sandbox/test_kalshi_happy_path.py -v` passes; evidence/04/kalshi_happy_lifecycle_*/scenario_manifest.json created; PnL within ±$0.01; fee within ±$0.01 (TEST-01, TEST-04)
result: [pending]

### 2. Scenario 2 — polymarket_happy_lifecycle
expected: `pytest -m live --live arbiter/sandbox/test_polymarket_happy_path.py -v` passes; real $1 fill confirmed; scenario_manifest.json status=pass (TEST-02, TEST-04)
result: [pending]

### 3. Scenario 3 — kalshi_fok_rejected_on_thin_market
expected: `pytest -m live --live arbiter/sandbox/test_kalshi_fok_rejection.py -v`; FOK returns rejected/unfilled; no partial fill; no open position (EXEC-01, TEST-01)
result: [pending]

### 4. Scenario 4 — polymarket_fok_rejected_on_thin_market
expected: `pytest -m live --live arbiter/sandbox/test_polymarket_fok_rejection.py -v`; Polymarket FOK returns unfilled; PHASE4_MAX_ORDER_USD hard-lock enforced (EXEC-01, TEST-02)
result: [pending]

### 5. Scenario 5 — kalshi_timeout_triggers_cancel_via_client_order_id
expected: `pytest -m live --live arbiter/sandbox/test_kalshi_timeout_cancel.py -v`; resting limit placed; cancel_order cancels within timeout; no exposure (TEST-01, EXEC-05, EXEC-04)
result: [pending]

### 6. Scenario 6 — kill_switch_cancels_open_kalshi_demo_order
expected: `pytest -m live --live arbiter/sandbox/test_safety_killswitch.py -v`; supervisor.trip_kill fires within 5s; Kalshi demo order cancelled; WS kill_switch event emitted (SAFE-01, TEST-01)
result: [pending]

### 7. Scenario 7 — one_leg_recovery_injected
expected: `pytest -m live --live arbiter/sandbox/test_one_leg_exposure.py -v`; Polymarket leg patched to raise; one-leg incident logged; Kalshi position unwound (SAFE-03, TEST-01)
result: [pending]

### 8. Scenario 8 — rate_limit_burst_triggers_backoff_and_ws
expected: `pytest -m live --live arbiter/sandbox/test_rate_limit_burst.py -v`; RateLimiter.apply_retry_after → THROTTLED; WS rate_limit_state payload; xfail on 403 (SAFE-04, TEST-01)
result: [pending]

### 9. Scenario 9 — sigint_cancels_open_kalshi_demo_orders
expected: `pytest -m live --live arbiter/sandbox/test_graceful_shutdown.py -v`; SIGINT cancels all open Kalshi demo orders; subprocess exits with phase=shutting_down (SAFE-05, TEST-01)
result: [pending]

### 10. Terminal reconciliation — 04-VALIDATION.md gate
expected: `pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py` (run AFTER scenarios 1-9); 04-VALIDATION.md rewritten to phase_gate_status: PASS; D-19 hard gate enforced (TEST-03, TEST-04)
result: [pending]

### 11. Browser UAT — Kill-switch ARM/RESET end-to-end
expected: Open dashboard `/ops`; place resting Kalshi demo order; trip kill from UI; verify order cancels and kill-switch banner shows ARMED; reset kill-switch; verify new orders allowed (SAFE-01 UI contract)
result: [pending]

### 12. Browser UAT — Shutdown banner visibility
expected: Launch `python -m arbiter.main --api-only`; open dashboard; SIGINT parent; verify "SHUTTING DOWN" banner appears before connection drops (SAFE-05 UI contract)
result: [pending]

### 13. Browser UAT — Rate-limit pill color transition
expected: Trigger rate-limit via adapter throttle; verify dashboard pill color transitions green → amber → red matching RateLimiter state; verify it clears after penalty expires (SAFE-04 UI contract)
result: [pending]

## Summary

total: 13
passed: 0
issues: 0
pending: 13
skipped: 0
blocked: 0

## Gaps
