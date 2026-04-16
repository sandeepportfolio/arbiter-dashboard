---
phase: 1
slug: api-integration-fixes
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-16
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 5.0+ |
| **Config file** | conftest.py |
| **Quick run command** | `python -m pytest arbiter/tests/ -x -q` |
| **Full suite command** | `python -m pytest arbiter/tests/ -v` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python -m pytest arbiter/tests/ -x -q`
- **After every plan wave:** Run `python -m pytest arbiter/tests/ -v`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 01-01-01 | 01 | 1 | API-01 | — | N/A | unit | `python -m pytest arbiter/tests/test_kalshi_format.py -v` | ❌ W0 | ⬜ pending |
| 01-02-01 | 02 | 1 | API-02 | — | N/A | unit | `python -m pytest arbiter/tests/test_polymarket_auth.py -v` | ❌ W0 | ⬜ pending |
| 01-03-01 | 03 | 1 | API-03 | — | N/A | unit | `python -m pytest arbiter/tests/test_heartbeat.py -v` | ❌ W0 | ⬜ pending |
| 01-04-01 | 04 | 1 | API-04 | — | N/A | unit | `python -m pytest arbiter/tests/test_fee_calculations.py -v` | ❌ W0 | ⬜ pending |
| 01-05-01 | 05 | 1 | API-05 | — | N/A | unit | `python -m pytest arbiter/tests/test_predictit_scoping.py -v` | ❌ W0 | ⬜ pending |
| 01-06-01 | 06 | 2 | API-06, API-07 | — | N/A | integration | `python -m pytest arbiter/tests/test_collectors_live.py -v` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `arbiter/tests/test_kalshi_format.py` — stubs for API-01 (dollar format, count_fp)
- [ ] `arbiter/tests/test_polymarket_auth.py` — stubs for API-02 (ClobClient init params)
- [ ] `arbiter/tests/test_heartbeat.py` — stubs for API-03 (heartbeat async task)
- [ ] `arbiter/tests/test_fee_calculations.py` — stubs for API-04 (fee rate accuracy)
- [ ] `arbiter/tests/test_predictit_scoping.py` — stubs for API-05 (execution code removal)
- [ ] `arbiter/tests/test_collectors_live.py` — stubs for API-06, API-07 (collector verification)
- [ ] `conftest.py` — shared fixtures for API credentials, mock responses

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Kalshi demo env order submission | API-01 | Requires live API credentials and demo account | Submit test order with yes_price_dollars format, verify no 400/422 |
| Polymarket get_api_keys() success | API-02 | Requires live wallet credentials | Init ClobClient with signature_type/funder, call get_api_keys() |
| Collector live data fetch | API-07 | Requires live API access to all 3 platforms | Run collectors against live APIs, verify parsed data matches schema |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
