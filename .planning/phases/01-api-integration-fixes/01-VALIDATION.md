---
phase: 1
slug: api-integration-fixes
status: draft
nyquist_compliant: true
wave_0_complete: true
created: 2026-04-16
updated: 2026-04-16
---

# Phase 1 -- Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.3.4 |
| **Config file** | conftest.py (root) |
| **Quick run command** | `python -m pytest arbiter/ -x --tb=short -q` |
| **Full suite command** | `python -m pytest arbiter/ --tb=short -v` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python -m pytest arbiter/ -x --tb=short -q`
- **After every plan wave:** Run `python -m pytest arbiter/ --tb=short -v`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | Status |
|---------|------|------|-------------|-----------|-------------------|--------|
| 01-01-T1 | 01 | 1 | API-04 | unit | `python -m pytest arbiter/audit/test_math_auditor.py -x --tb=short -q` | pending |
| 01-01-T2 | 01 | 1 | API-04 | unit | `python -m pytest arbiter/audit/test_math_auditor.py -x --tb=short -v` | pending |
| 01-02-T1 | 02 | 1 | API-01 | unit | `python -m pytest arbiter/execution/test_engine.py -x --tb=short -q` | pending |
| 01-02-T2 | 02 | 1 | API-01 | unit | `python -m pytest arbiter/execution/test_engine.py -x --tb=short -v` | pending |
| 01-03-T1 | 03 | 2 | API-05 | inline | `python -c "import arbiter.workflow; print('OK')" && python -c "import importlib; m = importlib.import_module('arbiter.workflow'); assert not hasattr(m, 'PredictItWorkflowManager')"` | pending |
| 01-03-T2 | 03 | 2 | API-05 | inline | `python -c "from arbiter.main import main; print('OK')" && python -c "from arbiter.api import create_api_server; print('OK')"` | pending |
| 01-03-T3 | 03 | 2 | API-05 | unit+inline | `python -m pytest arbiter/execution/test_engine.py -x --tb=short -q && python -c "import arbiter.execution.engine; print('OK')"` | pending |
| 01-04-T1 | 04 | 3 | API-02, API-06 | inline | `python -c "from arbiter.config.settings import PolymarketConfig; c = PolymarketConfig(); assert c.signature_type == 2; print('OK')" && python -c "from arbiter.execution.engine import ExecutionEngine; print('OK')"` | pending |
| 01-04-T2 | 04 | 3 | API-03 | inline | `python -c "from arbiter.execution.engine import ExecutionEngine; assert hasattr(ExecutionEngine, 'polymarket_heartbeat_loop'); assert hasattr(ExecutionEngine, 'stop_heartbeat'); print('OK')" && python -c "from arbiter.main import main; print('OK')"` | pending |
| 01-04-T3 | 04 | 3 | API-04 | inline | `python -c "from arbiter.collectors.polymarket import PolymarketCollector; assert hasattr(PolymarketCollector, '_fetch_dynamic_fee_rate'); assert hasattr(PolymarketCollector, 'set_clob_client'); print('OK')"` | pending |
| 01-05-T1 | 05 | 4 | API-07 | integration | `python -m arbiter.verify_collectors` | pending |
| 01-05-T2 | 05 | 4 | API-07 | checkpoint | Human verification of collector output with live credentials | pending |

*Status: pending / green / red / flaky*

---

## Requirement Coverage

| Requirement | Plans | Verified By |
|-------------|-------|-------------|
| API-01 | 01-02 | 01-02-T1, 01-02-T2 (unit tests for dollar string format) |
| API-02 | 01-04 | 01-04-T1 (ClobClient init with signature_type/funder) |
| API-03 | 01-04 | 01-04-T2 (heartbeat method exists and main.py wires it) |
| API-04 | 01-01, 01-04 | 01-01-T1, 01-01-T2 (fee rate corrections), 01-04-T3 (dynamic fee fetch) |
| API-05 | 01-03 | 01-03-T1, 01-03-T2, 01-03-T3 (PredictIt code removed, imports clean) |
| API-06 | 01-04 | 01-04-T1 (PolymarketConfig fields), 01-05-T2 (live verification) |
| API-07 | 01-05 | 01-05-T1 (verify_collectors script), 01-05-T2 (human checkpoint) |

---

## Test Files Used by Plans

| Test File | Plan | Exists | Notes |
|-----------|------|--------|-------|
| `arbiter/audit/test_math_auditor.py` | 01-01 | Yes | Expectations updated by Plan 01 Task 2 |
| `arbiter/execution/test_engine.py` | 01-02, 01-03 | Yes | New Kalshi format tests added by Plan 02 Task 2; PredictIt tests renamed by Plan 03 Task 3 |
| `arbiter/verify_collectors.py` | 01-05 | No | Created by Plan 05 Task 1 (standalone verification script, not pytest) |

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Kalshi demo env order submission | API-01 | Requires live API credentials and demo account | Submit test order with yes_price_dollars format, verify no 400/422 |
| Polymarket get_api_keys() success | API-02 | Requires live wallet credentials | Init ClobClient with signature_type/funder, call get_api_keys() |
| Collector live data fetch | API-07 | Requires live API access to all 3 platforms | Run `python -m arbiter.verify_collectors`, verify parsed data matches schema |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify commands
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Per-task verification map matches actual plan `<verify>` commands
- [x] No watch-mode flags
- [x] Feedback latency < 15s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** ready
