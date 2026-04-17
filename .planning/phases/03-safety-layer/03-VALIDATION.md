---
phase: 3
slug: safety-layer
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-04-16
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 5.x (Python) + vitest 4.x (JS view-model) |
| **Config file** | `conftest.py`, `pytest.ini` (if present); `vitest.config.ts` |
| **Quick run command** | `pytest arbiter/safety/ arbiter/test_api_safety.py -x --tb=short` |
| **Full suite command** | `pytest arbiter/safety/ arbiter/execution/ arbiter/test_api_safety.py arbiter/test_api_integration.py arbiter/test_config_loading.py arbiter/test_main_shutdown.py -x --tb=short && npx vitest run arbiter/web/dashboard-view-model.test.js` |
| **Estimated runtime** | ~60 seconds |

Test paths are CO-LOCATED with production modules (per Phase 2 convention):
- Python safety tests: `arbiter/safety/test_*.py`
- Python engine tests: `arbiter/execution/test_engine.py`
- Python adapter tests: `arbiter/execution/adapters/test_*_adapter.py`
- Python shutdown tests: `arbiter/test_main_shutdown.py` (integration)
- Python API tests: `arbiter/test_api_safety.py`, `arbiter/test_api_integration.py`
- Python config tests: `arbiter/test_config_loading.py`
- JS view-model tests: `arbiter/web/dashboard-view-model.test.js`

There is no `arbiter/tests/` directory in Phase 3 — all test files live next to the code they exercise.

---

## Sampling Rate

- **After every task commit:** Run the task-specific command from the Per-Task Verification Map below
- **After every plan wave:** Run `pytest arbiter/safety/ arbiter/execution/ arbiter/test_api_safety.py arbiter/test_main_shutdown.py -x --tb=short`
- **Before `/gsd-verify-work`:** Full suite must be green (includes vitest view-model suite)
- **Max feedback latency:** 60 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 3-01-T0 | 01 | 1 | SAFE-01 | T-3-01 | Test scaffolding collectible | unit | `pytest arbiter/safety/ arbiter/test_api_safety.py --collect-only -q` | ✅ | ⬜ pending |
| 3-01-T1 | 01 | 1 | SAFE-01 | T-3-01 | SafetySupervisor trips in ≤5s, blocks new orders, serializes concurrent arm | unit | `pytest arbiter/safety/ -x --tb=short -q` | ✅ | ⬜ pending |
| 3-01-T2 | 01 | 1 | SAFE-01 | T-3-01 | main.py wires chained_gate (readiness → safety) without import errors | smoke | `python -c "import ast; ast.parse(open('arbiter/main.py').read())"` | ✅ | ⬜ pending |
| 3-01-T3 | 01 | 1 | SAFE-01 | T-3-01 | /api/kill-switch 401/200/400 flow + WS kill_switch event shape | integration | `pytest arbiter/test_api_safety.py arbiter/safety/ -x --tb=short -q` | ✅ | ⬜ pending |
| 3-02-T0 | 02 | 1 | SAFE-02 | T-3-02 | Per-platform + rejected-order tests collect | unit | `pytest arbiter/execution/test_engine.py -k "per_platform or rejected_order" --collect-only -q` | ✅ | ⬜ pending |
| 3-02-T1 | 02 | 1 | SAFE-02 | T-3-02 | Per-platform exposure check rejects orders + structured order_rejected incident | unit | `pytest arbiter/execution/test_engine.py -x --tb=short -q` | ✅ | ⬜ pending |
| 3-03-T0 | 03 | 2 | SAFE-03 | T-3-03 | one-leg metadata + supervisor-hook + template tests collect | unit | `pytest arbiter/safety/test_alerts.py arbiter/safety/test_supervisor.py arbiter/execution/test_engine.py -k "one_leg or NAKED" --collect-only -q` | ✅ | ⬜ pending |
| 3-03-T1 | 03 | 2 | SAFE-03 | T-3-03 | One-leg exposure emits structured metadata + Telegram + dedicated WS event | unit | `pytest arbiter/safety/test_alerts.py arbiter/safety/test_supervisor.py arbiter/execution/test_engine.py -x --tb=short -q` | ✅ | ⬜ pending |
| 3-04-T0 | 04 | 2 | SAFE-04 | T-3-04 | acquire/429/WS shape tests collect | unit | `pytest arbiter/execution/adapters/test_kalshi_adapter.py arbiter/execution/adapters/test_polymarket_adapter.py arbiter/test_api_integration.py -k "rate_limit or 429 or acquire" --collect-only -q` | ✅ | ⬜ pending |
| 3-04-T1 | 04 | 2 | SAFE-04 | T-3-04 | Adapter throttles before hitting venue limits; 429 triggers apply_retry_after; FOK does not retry | unit | `pytest arbiter/execution/adapters/ -x --tb=short -q` | ✅ | ⬜ pending |
| 3-04-T2 | 04 | 2 | SAFE-04 | T-3-04 | /api/system includes rate_limits + periodic rate_limit_state WS event fires | integration | `pytest arbiter/test_api_integration.py -k rate_limit -x --tb=short -q` | ✅ | ⬜ pending |
| 3-05-T0 | 05 | 3 | SAFE-05 | T-3-05 | Shutdown-ordering and cancel_all tests collect | integration | `pytest arbiter/test_main_shutdown.py arbiter/execution/adapters/test_kalshi_adapter.py arbiter/execution/adapters/test_polymarket_adapter.py -k "shutdown or cancel_all" --collect-only -q` | ✅ | ⬜ pending |
| 3-05-T1 | 05 | 3 | SAFE-05 | T-3-05 | SIGINT cancels orders BEFORE task cancellation; cancel_all chunks correctly; prepare_shutdown broadcasts first | integration | `pytest arbiter/test_main_shutdown.py arbiter/execution/adapters/ arbiter/safety/ -x --tb=short -q` | ✅ | ⬜ pending |
| 3-06-T0 | 06 | 1 | SAFE-06 | T-3-06 | resolution_criteria schema + mapping_state WS tests collect | unit | `pytest arbiter/test_config_loading.py arbiter/test_api_integration.py -k "resolution_criteria or mapping_state" --collect-only -q` | ✅ | ⬜ pending |
| 3-06-T1 | 06 | 1 | SAFE-06 | T-3-06 | Resolution criteria schema optional + persisted + broadcast on change | unit+integration | `pytest arbiter/test_config_loading.py arbiter/test_api_integration.py -k "resolution_criteria or mapping_state" -x --tb=short -q` | ✅ | ⬜ pending |
| 3-07-T0 | 07 | 4 | SAFE-01..06 (UI) | T-3-07 | View-model vitest stubs + Playwright smoke scaffold | unit | `npx vitest run arbiter/web/dashboard-view-model.test.js --reporter=basic` | ✅ | ⬜ pending |
| 3-07-T1 | 07 | 4 | SAFE-01..06 (UI) | T-3-07 | Dashboard renders kill-switch + rate-limit pills + one-leg alert + shutdown banner + resolution comparison | unit | `npx vitest run arbiter/web/dashboard-view-model.test.js --reporter=basic` | ✅ | ⬜ pending |
| 3-07-T2 | 07 | 4 | SAFE-01..06 (UI) | T-3-07 | Operator-verified live smoke (human checkpoint) | manual | `node output/verify_safety_ui.mjs` against running dev server | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Every plan's Task 0 creates test scaffolding at these canonical paths:

- [x] `arbiter/safety/test_supervisor.py` — SAFE-01 kill switch + subscriber fanout + concurrent-arm serialization (plan 03-01)
- [x] `arbiter/safety/test_alerts.py` — SafetyAlertTemplates HTML shape (plan 03-01; extended in 03-03 for one_leg)
- [x] `arbiter/safety/test_persistence.py` — SafetyEventStore INSERT-only guarantees (plan 03-01)
- [x] `arbiter/test_api_safety.py` — /api/kill-switch auth + arm/reset/unknown-action flow (plan 03-01)
- [x] `arbiter/execution/test_engine.py` — per-platform risk + order_rejected incident + one_leg metadata + supervisor hook (plans 03-02, 03-03)
- [x] `arbiter/execution/adapters/test_kalshi_adapter.py` — rate_limiter.acquire before HTTP + 429 retry-after + no-retry-on-FOK + cancel_all chunking (plans 03-04, 03-05)
- [x] `arbiter/execution/adapters/test_polymarket_adapter.py` — rate_limiter.acquire before SDK + 429-via-exception + cancel_all via SDK (plans 03-04, 03-05)
- [x] `arbiter/test_main_shutdown.py` — graceful_shutdown_cancels_orders_before_tasks + timeout escalation + prepare_shutdown broadcast order (plan 03-05)
- [x] `arbiter/test_api_integration.py` — rate_limit_state WS event shape + /api/system rate_limits + resolution_criteria + mapping_state WS (plans 03-04, 03-06)
- [x] `arbiter/test_config_loading.py` — MarketMappingRecord optional resolution_criteria (plan 03-06)
- [x] `arbiter/web/dashboard-view-model.test.js` — buildSafetyView + buildRateLimitView + buildMappingComparison (plan 03-07)
- [x] `arbiter/conftest.py` — shared fixtures (no overwrite; add new fixtures to `arbiter/safety/conftest.py` if needed per plan 03-01 Task 0)

Each plan's Task 0 generates the required skeleton before Task 1 implementation lands. Every automated `<verify>` command in every task references one of the paths above.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real SIGINT on running process cancels open orders at live venue | SAFE-05 | Requires live platform orders; automated version uses fake adapter | Start `python -m arbiter.main --dry-run`, place open test order, send SIGINT (Ctrl+C), verify venue shows zero open orders within 5s |
| Telegram alert on one-leg exposure actually delivers | SAFE-03 | Requires real Bot API token | Trigger simulated second-leg failure in integration session, verify Telegram bot sends message to configured chat |
| Kill-switch button in dashboard arms/disarms correctly end-to-end | SAFE-01 (UI) | WebSocket + HTTP auth round trip | Open `/index.html`, click Kill Switch, confirm modal, verify backend state via `/api/safety/status` and all adapters return order rejections |
| Rate-limit pills change color under sustained call load | SAFE-04 (UI) | Requires generating real throttle state | Script generating high-frequency collector polls, observe dashboard pill transitions (green → amber → red) |
| Shutdown banner appears before WebSocket closes | SAFE-05 (UI) | Requires running server + browser session | Run `python -m arbiter.main --dry-run`, open dashboard, Ctrl+C the server; confirm `#shutdownBanner` flashes "Server shutting down…" before the WS close event |

All manual checks are operationalized via the `checkpoint:human-verify` task in plan 03-07 Task 2 with a resume-signal gate.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (all paths are co-located; every plan Task 0 creates its files)
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-04-16
