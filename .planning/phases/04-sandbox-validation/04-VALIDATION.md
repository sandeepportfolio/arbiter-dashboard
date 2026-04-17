---
phase: 4
slug: sandbox-validation
status: pending_live_fire
phase_gate_status: PENDING
nyquist_compliant: true
wave_0_complete: true  # Wave 0 scaffolding done; live-fire deferred
generated: 2026-04-17T08:42:05Z
tolerance_usd: 0.01
total_scenarios_expected: 9
total_scenarios_observed: 0
scenarios_passed: 0
scenarios_failed: 0
scenarios_missing: 9
---

# Phase 4: Sandbox Validation - Acceptance Report

**Phase Goal:** The full pipeline (collect -> scan -> execute -> monitor -> reconcile) is validated end-to-end against real platform APIs in sandbox/demo mode with no real money at risk.
**Generated:** 2026-04-17T08:42:05Z
**Phase Gate Status:** **PENDING** -- 0 of 9 scenarios observed. Phase 5 BLOCKED per D-19 until operator runs the full live-fire suite.
**Tolerance:** +/-$0.01 absolute (D-17, both TEST-03 PnL and TEST-04 fee)
**Hard Gate Rule:** D-19 -- any real-tagged scenario with a tolerance breach blocks Phase 5

## Phase Gate Status

**PENDING** -- Phase 5 BLOCKED. The 9 scenario live-fire runs have not been executed yet (evidence/04/ empty). Operator action required: see the **Operator Workflow** section below.

## Operator Workflow

To populate or refresh this file with real scenario results, run the full Phase 4 live suite from a host with `.env.sandbox` provisioned:

```bash
# 1. One-time setup (see arbiter/sandbox/README.md)
cp .env.sandbox.template .env.sandbox
# Fill in KALSHI_DEMO_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,
# POLY_PRIVATE_KEY (throwaway wallet), POLY_FUNDER, DATABASE_URL
# pointing at arbiter_sandbox, PHASE4_MAX_ORDER_USD=5, etc.

# 2. Source environment + export scenario-specific overrides
set -a; source .env.sandbox; set +a
export SANDBOX_HAPPY_TICKER=<liquid-kalshi-demo-market>
export SANDBOX_FOK_TICKER=<thin-kalshi-demo-market>
export PHASE4_KILLSWITCH_TICKER=<resting-capable-kalshi-market>
export PHASE4_SHUTDOWN_TICKER=<same-as-killswitch>

# 3. Run all 9 scenario tests
pytest -m live --live arbiter/sandbox/ -v

# 4. Run the terminal aggregator (rewrites this file)
pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py -v
```

The aggregator can also be run offline (after manifests exist) via: `python -m arbiter.sandbox.aggregator`.

## Scenario Results

_No scenario manifests found under `evidence/04/`. The 9 live-fire scenarios below are expected but **not yet executed** (requires operator to provision `.env.sandbox` and run `pytest -m live --live arbiter/sandbox/`)._

| # | Expected Scenario | Requirements | Plan Ref | Tag | Status |
|---|-------------------|--------------|----------|-----|--------|
| 1 | kalshi_happy_lifecycle | TEST-01, TEST-04 | 04-03 Task 1 | real | **PENDING** (awaiting live-fire) |
| 2 | polymarket_happy_lifecycle | TEST-02, TEST-04 | 04-04 Task 1 | real | **PENDING** (awaiting live-fire) |
| 3 | kalshi_fok_rejected_on_thin_market | EXEC-01, TEST-01 | 04-03 Task 2 | real | **PENDING** (awaiting live-fire) |
| 4 | polymarket_fok_rejected_on_thin_market | EXEC-01, TEST-02 | 04-04 Task 2 | real | **PENDING** (awaiting live-fire) |
| 5 | kalshi_timeout_triggers_cancel_via_client_order_id | TEST-01, EXEC-05, EXEC-04 | 04-03 Task 3 | real | **PENDING** (awaiting live-fire) |
| 6 | kill_switch_cancels_open_kalshi_demo_order | SAFE-01, TEST-01 | 04-05 Task 1 | real | **PENDING** (awaiting live-fire) |
| 7 | one_leg_recovery_injected | SAFE-03, TEST-01 | 04-06 Task 1 | injected | **PENDING** (awaiting live-fire) |
| 8 | rate_limit_burst_triggers_backoff_and_ws | SAFE-04, TEST-01 | 04-06 Task 2 | injected | **PENDING** (awaiting live-fire) |
| 9 | sigint_cancels_open_kalshi_demo_orders | SAFE-05, TEST-01 | 04-07 Task 1 | real | **PENDING** (awaiting live-fire) |

## Per-Task Verification Map

Every task across Plans 04-01 through 04-08, with automated command + status.

| Task | Plan | Wave | Requirement | Automated Command | Status |
|------|------|------|-------------|-------------------|--------|
| 04-01 Task 1 | 04-01 | 1 | TEST-01..04 | `pytest arbiter/sandbox/test_smoke.py -v` | complete (Wave 1 scaffolding) |
| 04-01 Task 2 | 04-01 | 1 | TEST-01..04 | `pytest arbiter/sandbox/test_smoke.py -v` | complete (Wave 1 scaffolding) |
| 04-01 Task 3 | 04-01 | 1 | TEST-01..04 | `wc -l arbiter/sandbox/README.md` | complete (Wave 1 scaffolding) |
| 04-02 Task 1 | 04-02 | 1 | TEST-01, TEST-02 | `pytest arbiter/execution/adapters/test_polymarket_phase4_hardlock.py -v` | complete (Wave 1 scaffolding) |
| 04-02 Task 2 | 04-02 | 1 | TEST-01, TEST-02 | `python -c sanity checks on settings.py defaults` | complete (Wave 1 scaffolding) |
| 04-02 Task 3 | 04-02 | 1 | TEST-01, TEST-02 | `docker-compose config && bash -n arbiter/sql/init-sandbox.sh` | complete (Wave 1 scaffolding) |
| 04-02.1 Tasks 1-2 | 04-02.1 | 1 | SAFE-01 enabler | `pytest arbiter/execution/adapters/test_kalshi_place_resting_limit.py -v` | complete (Wave 1 scaffolding) |
| 04-03 Task 1 | 04-03 | 2 | TEST-01, TEST-04 | `pytest -m live --live arbiter/sandbox/test_kalshi_happy_path.py -v` | pending live-fire |
| 04-03 Task 2 | 04-03 | 2 | EXEC-01, TEST-01 | `pytest -m live --live arbiter/sandbox/test_kalshi_fok_rejection.py -v` | pending live-fire |
| 04-03 Task 3 | 04-03 | 2 | TEST-01, EXEC-05, EXEC-04 | `pytest -m live --live arbiter/sandbox/test_kalshi_timeout_cancel.py -v` | pending live-fire |
| 04-04 Task 1 | 04-04 | 2 | TEST-02, TEST-04 | `pytest -m live --live arbiter/sandbox/test_polymarket_happy_path.py -v` | pending live-fire |
| 04-04 Task 2 | 04-04 | 2 | EXEC-01, TEST-02 | `pytest -m live --live arbiter/sandbox/test_polymarket_fok_rejection.py -v` | pending live-fire |
| 04-05 Task 1 | 04-05 | 2 | SAFE-01, TEST-01 | `pytest -m live --live arbiter/sandbox/test_safety_killswitch.py -v` | pending live-fire |
| 04-06 Task 1 | 04-06 | 2 | SAFE-03, TEST-01 | `pytest -m live --live arbiter/sandbox/test_one_leg_exposure.py -v` | pending live-fire |
| 04-06 Task 2 | 04-06 | 2 | SAFE-04, TEST-01 | `pytest -m live --live arbiter/sandbox/test_rate_limit_burst.py -v` | pending live-fire |
| 04-07 Task 1 | 04-07 | 2 | SAFE-05, TEST-01 | `pytest -m live --live arbiter/sandbox/test_graceful_shutdown.py -v` | pending live-fire |
| 04-08 Task 1 | 04-08 | 3 | TEST-03, TEST-04 | `pytest arbiter/sandbox/test_aggregator.py -v` | complete (Wave 1 scaffolding) |
| 04-08 Task 2 | 04-08 | 3 | TEST-03, TEST-04 | `pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py -v` | pending live-fire |

## Manual-Only Verifications (Deferred from Phase 3 HUMAN-UAT)

| Behavior | Requirement | Backend Verified | UI Verification |
|----------|-------------|------------------|-----------------|
| Kill-switch ARM/RESET end-to-end | SAFE-01 | Scenario 6 (backend; WS event + platform cancel) | Deferred to operator browser UAT |
| Shutdown banner visibility | SAFE-05 | Scenario 9 (backend; phase=shutting_down log + platform cancel) | Deferred to operator browser UAT |
| Rate-limit pill color transition | SAFE-04 | Scenario 8 (backend; rate_limit_state payload) | Deferred to operator browser UAT |

## Notes

- Real-tagged scenarios observed: 0
- Injected-tagged scenarios observed: 0
- Expected total: 9 (7 real, 2 injected)
- Tolerance: +/-$0.01 (D-17)
- Hard-gate rule: D-19 -- any real breach blocks Phase 5
- To refresh this file after a live-fire run: `pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py`
