---
status: partial
phase: 03-safety-layer
source: [03-VERIFICATION.md]
started: 2026-04-16T23:05:00Z
updated: 2026-04-16T23:05:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. Operator kill-switch ARM + RESET end-to-end
expected: ARM button triggers window.confirm + prompt; badge flips to ARMED (red); Reset appears with 30s cooldown; after cooldown Reset resets to Disarmed. Backend logs show trip_kill + adapter.cancel_all.
result: [pending]

### 2. Shutdown banner visibility before WebSocket close
expected: Ctrl+C on running server causes #shutdownBanner to show "Server shutting down — cancelling open orders" before the WS close event; after phase=complete no auto-reconnect.
result: [pending]

### 3. Rate-limit pills color transition under load
expected: Dashboard pills transition from green (.ok) to amber (.warn) when adapters are throttled.
result: [pending]

## Summary

total: 3
passed: 0
issues: 0
pending: 3
skipped: 0
blocked: 0

## Gaps
