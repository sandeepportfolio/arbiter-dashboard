---
status: partial
phase: 01-api-integration-fixes
source: [01-VERIFICATION.md]
started: 2026-04-16T00:00:00Z
updated: 2026-04-16T00:00:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. Kalshi Live Collector Schema Validation
expected: Configure `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` in `.env`, then run `python -m arbiter.verify_collectors`. PASS (markets fetched, schema matches) or SKIP if credentials unavailable.
result: [pending]

### 2. Polymarket Heartbeat Observable in Logs
expected: Configure Polymarket credentials, run `python -m arbiter.main` for 60+ seconds. Log lines from `poly-heartbeat` task appear every 5 seconds with ClobClient initializing before heartbeat starts.
result: [pending]

### 3. Polymarket Authenticated CLOB Path
expected: Run `python -m arbiter.verify_collectors` with `POLY_PRIVATE_KEY`, `POLY_SIGNATURE_TYPE`, `POLY_FUNDER` configured. Polymarket returns PASS using authenticated CLOB endpoints.
result: [pending]

## Summary

total: 3
passed: 0
issues: 0
pending: 3
skipped: 0
blocked: 0

## Gaps
