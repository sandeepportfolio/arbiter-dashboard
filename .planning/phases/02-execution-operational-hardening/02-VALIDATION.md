---
phase: 2
slug: execution-operational-hardening
status: ready
nyquist_compliant: true
wave_0_complete: false
created: 2026-04-16
updated: 2026-04-16
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 5.x (existing, per requirements.txt) |
| **Config file** | conftest.py (existing at repo root) |
| **Quick run command** | `pytest arbiter/execution arbiter/utils -q -x` |
| **Full suite command** | `pytest arbiter -x` |
| **Estimated runtime** | ~30 seconds (quick), ~90 seconds (full, excluding integration) |

---

## Sampling Rate

- **After every task commit:** Run `pytest arbiter/execution arbiter/utils -q -x`
- **After every plan wave:** Run full suite `pytest arbiter -x`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 90 seconds

---

## Per-Task Verification Map

One row per task across the 6 phase plans. Source-of-truth is each PLAN.md's `<verify><automated>` block.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 2-01-1 | 01 | 1 | OPS-04 | T-02-03 | cryptography 46.x non-regression of Kalshi RSA-PSS signing | smoke | `pip install -r requirements.txt && python -c "import structlog, tenacity, sentry_sdk; import cryptography; assert cryptography.__version__.startswith('46.')"` | ❌ W0 → exists post-task | ⬜ pending |
| 2-01-2 | 01 | 1 | OPS-01 | T-02-01 | structlog JSON output + contextvars + secret stripping | unit | `pytest arbiter/utils/test_logger.py -x -v` | ❌ W0 → created in Task | ⬜ pending |
| 2-01-3 | 01 | 1 | OPS-02 | T-02-02 | sentry async exception capture + dsn-unset no-op | unit (fake transport) | `pytest arbiter/test_sentry_integration.py -x -v` | ❌ W0 → created in Task | ⬜ pending |
| 2-02-1 | 02 | 1 | EXEC-02 | T-02-06 | Migration DDL is correct + idempotent runner | static | `python -c "from pathlib import Path; assert 'CREATE TABLE IF NOT EXISTS execution_orders' in Path('arbiter/sql/migrations/001_execution_persistence.sql').read_text()"` + `python -c "from arbiter.sql.migrate import apply_pending"` | ❌ W0 | ⬜ pending |
| 2-02-2 | 02 | 1 | EXEC-02 | T-02-05 | ExecutionStore class + asyncpg pool config | static | `python -c "from arbiter.execution.store import ExecutionStore, _derive_arb_id"` | ❌ W0 | ⬜ pending |
| 2-02-3 | 02 | 1 | EXEC-02 | T-02-05/T-02-08 | Mock-pool unit tests for upsert_order / insert_incident / list_non_terminal_orders | unit (mock asyncpg) | `pytest arbiter/execution/test_store.py -x -v -k "not integration"` | ❌ W0 | ⬜ pending |
| 2-03-1 | 03 | 1 | EXEC-04 | — | PlatformAdapter Protocol importable + runtime_checkable | static | `python -c "from arbiter.execution.adapters import PlatformAdapter; assert PlatformAdapter._is_runtime_protocol"` | ❌ W0 | ⬜ pending |
| 2-03-2 | 03 | 1 | OPS-03 | T-02-09 | tenacity transient_retry classifies transient vs permanent correctly | unit | `pytest arbiter/execution/adapters/test_retry_policy.py -x -v` | ❌ W0 | ⬜ pending |
| 2-03-3 | 03 | 1 | EXEC-04 | — | Protocol conformance via stub adapter (positive + negative) | unit | `pytest arbiter/execution/adapters/test_protocol_conformance.py -x -v` | ❌ W0 | ⬜ pending |
| 2-04-1 | 04 | 2 | EXEC-01, EXEC-03, EXEC-04, OPS-03 | T-02-11/T-02-12/T-02-13/T-02-14 | KalshiAdapter implements 5 PlatformAdapter methods + FOK time_in_force + idempotent retry + circuit/rate-limit gating | static | `python -c "from arbiter.execution.adapters import KalshiAdapter; assert KalshiAdapter.platform == 'kalshi'"` | ❌ W0 | ⬜ pending |
| 2-04-2 | 04 | 2 | EXEC-01, EXEC-03 | T-02-11 | FOK body shape on yes+no, status mapping, refusal paths, depth check, cancel, conformance | unit (mock aiohttp) | `pytest arbiter/execution/adapters/test_kalshi_adapter.py -x -v` | ❌ W0 | ⬜ pending |
| 2-05-1 | 05 | 2 | EXEC-01, EXEC-03, EXEC-04 | T-02-15/T-02-16/T-02-17/T-02-18/T-02-19 | PolymarketAdapter two-phase create+post(FOK) + reconcile-before-retry + stale-book guard + heartbeat invariant preserved | static | `python -c "from arbiter.execution.adapters import PolymarketAdapter; assert PolymarketAdapter.platform == 'polymarket'"` | ❌ W0 | ⬜ pending |
| 2-05-2 | 05 | 2 | EXEC-01, EXEC-03 | T-02-15/T-02-16/T-02-18 | Two-phase FOK + reconcile-on-timeout + stale-book guard tests + tenacity-not-applied invariant | unit (mock ClobClient) | `pytest arbiter/execution/adapters/test_polymarket_adapter.py -x -v` | ❌ W0 | ⬜ pending |
| 2-06-1 | 06 | 3 | EXEC-04, EXEC-05, EXEC-02, OPS-01 | T-02-20/T-02-21/T-02-22 | Engine stripped of platform code + adapter dispatch + asyncio.wait_for + contextvars + store persists | static + import | `python -c "from arbiter.execution.engine import ExecutionEngine, Order, OrderStatus, ExecutionIncident, ArbExecution; print('ok')"` then `python -c "src=open('arbiter/execution/engine.py').read(); banned=['_place_kalshi_order','_place_polymarket_order','_cancel_kalshi_order','_cancel_polymarket_order']; [exit(f'still contains {b}') for b in banned if b in src]"` | ❌ W0 | ⬜ pending |
| 2-06-2 | 06 | 3 | EXEC-02 | T-02-24 | reconcile_non_terminal_orders + 7 unit tests covering empty / changed / orphaned-via-exc / orphaned-via-not-found / no-adapter / unchanged / list-raises | unit (mock store + mock adapters) | `pytest arbiter/execution/test_recovery.py -x -v` | ❌ W0 | ⬜ pending |
| 2-06-3 | 06 | 3 | EXEC-02, EXEC-04 | T-02-22/T-02-23 | main.py wires ExecutionStore + KalshiAdapter + PolymarketAdapter + reconcile-on-startup + heartbeat task untouched (D-13) | unit + import | `pytest arbiter/execution/test_engine.py arbiter/execution/test_recovery.py arbiter/execution/test_store.py -x -v -k "not integration"` and `python -c "import arbiter.main"` | ❌ W0 | ⬜ pending |

*Status legend: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Per RESEARCH.md §Validation Architecture (Wave 0 gaps). All Wave-0 work is performed inside Plan 01 / Plan 02 / Plan 03 tasks themselves — there is no separate Wave-0 plan because every dependency a later wave needs is created within Wave 1.

- [ ] `arbiter/execution/adapters/__init__.py` — created in Plan 03 Task 1
- [ ] `arbiter/execution/adapters/base.py` — Plan 03 Task 1 (PlatformAdapter Protocol)
- [ ] `arbiter/execution/adapters/retry_policy.py` — Plan 03 Task 2 (tenacity decorator)
- [ ] `arbiter/execution/adapters/test_retry_policy.py` — Plan 03 Task 2
- [ ] `arbiter/execution/adapters/test_protocol_conformance.py` — Plan 03 Task 3
- [ ] `arbiter/execution/adapters/kalshi.py` — Plan 04 Task 1 (Wave 2)
- [ ] `arbiter/execution/adapters/test_kalshi_adapter.py` — Plan 04 Task 2 (Wave 2)
- [ ] `arbiter/execution/adapters/polymarket.py` — Plan 05 Task 1 (Wave 2)
- [ ] `arbiter/execution/adapters/test_polymarket_adapter.py` — Plan 05 Task 2 (Wave 2)
- [ ] `arbiter/execution/store.py` — Plan 02 Task 2
- [ ] `arbiter/execution/test_store.py` — Plan 02 Task 3
- [ ] `arbiter/execution/recovery.py` — Plan 06 Task 2 (Wave 3)
- [ ] `arbiter/execution/test_recovery.py` — Plan 06 Task 2 (Wave 3)
- [ ] `arbiter/sql/migrations/001_execution_persistence.sql` — Plan 02 Task 1
- [ ] `arbiter/sql/migrate.py` — Plan 02 Task 1
- [ ] `arbiter/utils/test_logger.py` — Plan 01 Task 2
- [ ] `arbiter/test_sentry_integration.py` — Plan 01 Task 3
- [ ] `requirements.txt` + `arbiter/requirements.txt` — Plan 01 Task 1 (structlog, tenacity, sentry-sdk added; cryptography bumped to 46.x)
- [ ] Install verified: `python -c "import structlog, tenacity, sentry_sdk"` (Plan 01 Task 1 verify command)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| FOK rejects entire order on insufficient depth (Kalshi sandbox) | EXEC-01 | Needs live sandbox API credentials; cannot run in CI | Phase 4 sandbox task — place FOK for qty > book depth, confirm status=cancelled with 0 fills |
| FOK rejects entire order on insufficient depth (Polymarket) | EXEC-01 | Needs live API + USDC test wallet | Phase 4 sandbox task — same as above |
| Process restart recovers open orders from DB | EXEC-02 | Requires kill -9 mid-execution; not automatable in CI without fragile timing | Start service, place order, kill -9, restart, confirm `await reconcile_non_terminal_orders(...)` reconciles via adapter.get_order; orphaned orders surface as warning incidents in dashboard |
| Polymarket two-phase FOK actually rejects on insufficient liquidity (live) | EXEC-01 | Real API call cost + test wallet | Phase 4 sandbox task — submit ‘ridiculous’ FOK qty and confirm response.success=false |
| Polymarket stale-book guard correctly trips when book diverges from get_price (live) | EXEC-03 / Pitfall 1 | Need a live market with the known Issue #180 stale-data behavior | Observe in Phase 4 sandbox; document divergence threshold tuning if needed |
| Sentry capture under network outage scenario (no DSN set) | OPS-02 | Requires running the live service | Smoke: launch with SENTRY_DSN unset; confirm sentry_sdk.init does not raise; smoke logs are still JSON |

---

## Validation Sign-Off

- [x] All tasks have `<verify><automated>` blocks (verified by `gsd-tools verify plan-structure` across all 6 plans)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify (every task has at least one automated command)
- [x] Wave 0 covers all MISSING references (deps install, migrations dir, test stubs all created in Wave 1 plans)
- [x] No watch-mode flags
- [x] Feedback latency < 90s (quick subset runs in ~30s; full suite in ~90s excluding integration tier)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** ready
