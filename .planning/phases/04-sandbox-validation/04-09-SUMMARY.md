---
phase: 04-sandbox-validation
plan: 09
subsystem: gap-closure
tags:
  - gap-closure
  - kalshi-demo-remediation
  - ui-wiring-fix
requirements:
  - TEST-01
  - SAFE-01
  - SAFE-04
  - SAFE-05
  - EXEC-01
  - EXEC-05
dependency_graph:
  requires:
    - 04-HUMAN-UAT.md (G-1..G-5 evidence)
    - KalshiAdapter.place_resting_limit (Phase 4.1 frozen signature)
    - SafetySupervisor trip_kill / cancel_all (Phase 3)
    - buildRateLimitView (Phase 3)
  provides:
    - Querystring-free Kalshi signed path for GET /portfolio/orders (unblocks cancel_all enumeration)
    - Adapter-generated client_order_id consumption path in timeout + shutdown tests
    - Async-generator-aware pytest_pyfunc_call (unblocks balance_snapshot in STRICT mode)
    - Widened EXEC-01 assertion (CANCELLED or FAILED) + fill_qty==0 invariant
    - #rateLimitIndicators DOM host + crit tone for circuit-open collectors
  affects:
    - arbiter/execution/adapters/kalshi.py (_list_orders signed path)
    - arbiter/sandbox/test_kalshi_timeout_cancel.py (Test 5)
    - arbiter/sandbox/test_graceful_shutdown.py (Test 9)
    - arbiter/sandbox/test_kalshi_fok_rejection.py (Test 3)
    - conftest.py (async dispatch)
    - index.html (#opsSection)
    - arbiter/web/dashboard-view-model.js (buildRateLimitView)
    - arbiter/web/dashboard-view-model.test.js (3 new cases)
tech_stack:
  added: []
  patterns:
    - "Kalshi PSS signature path stripping (querystring-in-URL, not-in-signed-message)"
    - "Adapter-generated client_order_id surfaced via Order.external_client_order_id"
    - "async-generator fixture lifecycle mirroring via __anext__/__anext__"
    - "Collector circuit state feeds UI tone ladder (ok -> warn -> crit)"
key_files:
  created:
    - arbiter/execution/adapters/test_kalshi_list_open_orders_signing.py
    - .planning/phases/04-sandbox-validation/04-09-SUMMARY.md
  modified:
    - arbiter/execution/adapters/kalshi.py
    - arbiter/sandbox/test_kalshi_timeout_cancel.py
    - arbiter/sandbox/test_graceful_shutdown.py
    - arbiter/sandbox/test_kalshi_fok_rejection.py
    - conftest.py
    - index.html
    - arbiter/web/dashboard-view-model.js
    - arbiter/web/dashboard-view-model.test.js
decisions:
  - "G-1 fix localized to _list_orders; _post_order/_fetch_order/_delete_order were already correct so no audit-wide refactor needed."
  - "G-2 test harness consumes adapter-generated client_order_id via Order.external_client_order_id; Phase 4.1 adapter signature stays frozen."
  - "G-4 assertion widens to OrderStatus in (CANCELLED, FAILED) AND fill_qty == 0. Status alone was brittle against demo Kalshi shape drift; fill_qty == 0 is the platform-independent EXEC-01 guarantee."
  - "G-3 hook resolves async-generators via __anext__ setup + __anext__ teardown; sync and already-resolved async fixtures pass through unchanged (backward-compatible)."
  - "G-5 emits circuitState on view-model output for test consumers + downstream enrichment without requiring renderer changes."
metrics:
  duration_minutes: 72
  completed_date: 2026-04-20
  tasks_completed: 5
  commits: 6
  files_created: 1
  files_modified: 8
---

# Phase 4 Plan 09: Kalshi Live-Fire Gap Closure Summary

Surgical closure of five gaps surfaced by the 2026-04-20 Kalshi demo UAT sweep: one production signature bug (G-1), two test-harness corrections (G-2, G-4), one async-fixture scaffolding fix (G-3), and one UI wiring regression (G-5) — all inside the Phase 4 scope envelope, zero new abstractions, eight files touched.

## Execution

- **Duration:** ~72 minutes
- **Commits:** 6 (1 RED + 5 task commits), all with `--no-verify` per parallel-executor protocol
- **Files touched:** 1 created, 8 modified
- **Deviations:** None — plan executed exactly as written. No Rule 1/2/3 auto-fixes required. No Rule 4 architectural checkpoints triggered.

### Commit Trail

| Task | Type | Commit | Message |
|------|------|--------|---------|
| 1 (RED) | test | `44cd93a` | test(04-09): add failing signature-message test for list_orders querystring bug |
| 1 (GREEN) | fix | `7989972` | fix(04-09): strip querystring from Kalshi signed path in _list_orders (G-1) |
| 2 | fix | `1e21684` | fix(04-09): drop client_order_id kwarg from adapter.place_resting_limit calls (G-2) |
| 3 | fix | `245319c` | fix(04-09): accept CANCELLED OR FAILED on FOK rejection; enforce fill_qty==0 (G-4) |
| 4 | fix | `134a685` | fix(04-09): resolve async-generator fixtures in pytest_pyfunc_call (G-3) |
| 5 | fix | `5f3787f` | fix(04-09): wire #rateLimitIndicators host + crit-tone for circuit-open (G-5) |

## G-1 Before/After Evidence (the one production bug)

**Before (the broken signed message):**
```python
# arbiter/execution/adapters/kalshi.py:895-902 (pre-fix)
@transient_retry()
async def _list_orders(self, status: str) -> tuple[int, str, dict]:
    path = f"/trade-api/v2/portfolio/orders?status={status}"     # ← querystring in signed path
    url = f"{self.config.kalshi.base_url}/portfolio/orders?status={status}"
    headers = self.auth.get_headers("GET", path)                 # ← HTTP 401 INCORRECT_API_KEY_SIGNATURE
```

**After:**
```python
# arbiter/execution/adapters/kalshi.py:895-913 (post-fix)
@transient_retry()
async def _list_orders(self, status: str) -> tuple[int, str, dict]:
    # G-1 fix (Plan 04-09, 2026-04-20): Kalshi PSS signing requires a
    # querystring-free path in the signed message. Querystring is appended
    # to the REQUEST URL (so Kalshi routes the filter) but is stripped from
    # the SIGNED path.
    path = "/trade-api/v2/portfolio/orders"                      # ← bare path
    url = f"{self.config.kalshi.base_url}/portfolio/orders?status={status}"
    headers = self.auth.get_headers("GET", path)
```

**Evidence that the fix is correct:**
- RED commit: new test `test_list_all_open_orders_signs_without_querystring` asserted `get_headers` was called with `('GET', '/trade-api/v2/portfolio/orders?status=resting')` — FAILED pre-fix exactly on that bug shape.
- GREEN commit: same test now asserts path == `/trade-api/v2/portfolio/orders` — PASSES.
- Sibling regression guard `test_post_order_still_signs_bare_orders_path` confirms `_post_order` was already correct (this fix brings `_list_orders` into parity, not the other way around).

**Downstream operational impact (out-of-scope for code in this plan — surfaces after operator re-sweep):**
- SAFE-01 kill-switch against real demo/prod: `cancel_all` now enumerates resting orders correctly. Test 6 evidence from 04-HUMAN-UAT showed order `6f70eeb6-dcf7-45c3-b0bf-2083cf279120` survived `trip_kill` because `_list_all_open_orders()` returned `[]` on the 401. After fix, enumeration should return the resting order and cancel_all proceeds.

## G-2..G-5 File Diff Hints

| Gap | File | Grep-able literal(s) added | Lines changed |
|-----|------|---------------------------|---------------|
| G-2 | `arbiter/sandbox/test_kalshi_timeout_cancel.py` | `order.external_client_order_id`, `effective_client_order_id` | +29 / -11 |
| G-2 | `arbiter/sandbox/test_graceful_shutdown.py` | `order.external_client_order_id`, `effective_client_order_id` | +25 / -4 |
| G-4 | `arbiter/sandbox/test_kalshi_fok_rejection.py` | `OrderStatus.FAILED`, `order.fill_qty == 0` | +26 / -11 |
| G-3 | `conftest.py` | `inspect.isasyncgen`, `active_generators` | +45 / -5 |
| G-5 | `index.html` | `id="rateLimitIndicators"` (line 286) | +11 / -0 |
| G-5 | `arbiter/web/dashboard-view-model.js` | `if (circuitState === "open") tone = "crit";` | +10 / -1 |
| G-5 | `arbiter/web/dashboard-view-model.test.js` | `"circuit-open collector promotes tone to crit"` + 2 siblings | +46 / -0 |
| G-1 | `arbiter/execution/adapters/test_kalshi_list_open_orders_signing.py` | 3 tests; signed-path asserts | new file (207 LOC) |
| G-1 | `arbiter/execution/adapters/kalshi.py` | `path = "/trade-api/v2/portfolio/orders"` (line 907) | +11 / -1 |

## Verification Results

### Automated (ran in-plan)

- `pytest arbiter/execution/adapters/ -q` → **116 passed** (new signing suite 3/3 + full adapter regression clean)
- `pytest arbiter/sandbox/test_kalshi_timeout_cancel.py arbiter/sandbox/test_graceful_shutdown.py --collect-only -q` → **2 tests collected** (no TypeError)
- `pytest arbiter/sandbox/test_kalshi_fok_rejection.py --collect-only -q` → **1 test collected**
- `pytest arbiter/sandbox/test_aggregator.py arbiter/execution/adapters/test_kalshi_place_resting_limit.py arbiter/execution/adapters/test_polymarket_phase4_hardlock.py -q` → **39 passed** (conftest G-3 fix non-regressive)
- `pytest arbiter/sandbox/ --collect-only -q` → **30 tests collected**, no collection errors
- `npx vitest run arbiter/web/dashboard-view-model.test.js` → **15/15 passed** (12 pre-existing + 3 new crit-tone cases)

### Grep-level invariants (from plan verification block)

| Check | Expected | Result |
|-------|----------|--------|
| `grep -n 'path = "/trade-api/v2/portfolio/orders"' arbiter/execution/adapters/kalshi.py` | ≥ 2 hits | **2 hits** (lines 285, 907) |
| `grep -n 'path = f"/trade-api/v2/portfolio/orders?status' arbiter/execution/adapters/kalshi.py` | 0 hits | **0 hits** |
| `grep -n "external_client_order_id" arbiter/sandbox/test_kalshi_timeout_cancel.py arbiter/sandbox/test_graceful_shutdown.py` | ≥ 1 hit each | **2 hits each** |
| `grep -n '"crit"' arbiter/web/dashboard-view-model.js` inside buildRateLimitView | ≥ 1 hit | **1 hit** (line 250) |
| `grep -n 'id="rateLimitIndicators"' index.html` | exactly 1 hit | **1 hit** (line 286) |
| `grep -n 'isasyncgen' conftest.py` | 1 hit | **1 hit** (line 37) |
| `grep -n "OrderStatus.CANCELLED\|OrderStatus.FAILED\|fill_qty == 0" arbiter/sandbox/test_kalshi_fok_rejection.py` | all 3 literals on the assertion | **3 distinct assertions present** |

## Threat-Model Disposition

All 6 mitigation-class threats from the plan `<threat_model>` are closed by the landed code:

- **T-04-09-01 (Tampering — signed-message shape):** `_list_orders` now signs `/trade-api/v2/portfolio/orders` (verified by `test_list_all_open_orders_signs_without_querystring`).
- **T-04-09-02 (DoS — SAFE-01 self-DoS):** G-1 fix unblocks enumeration; operator re-sweep will validate end-to-end.
- **T-04-09-05 (Repudiation — FOK rejection masking):** `fill_qty == 0` assertion added; EXEC-01 violation cannot hide behind a status-string mismatch.
- **T-04-09-06 (Information Disclosure — SAFE-04 operator blindness):** `#rateLimitIndicators` host wired + `crit` tone emits on circuit-open.
- **T-04-09-03 (accept), T-04-09-04 (accept), T-04-09-07 (accept):** unchanged.

No new threat-flags introduced. No new network endpoints, auth paths, file access patterns, or schema changes.

## Operator Re-Sweep Next

This plan landing is **necessary but not sufficient** for `04-VALIDATION.md phase_gate_status: PASS`. After this lands on main, the operator must re-run the Kalshi-gated live-fire sweep with real demo credentials:

```bash
set -a; source .env.sandbox; set +a
pytest -m live --live arbiter/sandbox/test_kalshi_timeout_cancel.py -v      # Test 5 (G-2 unblocked)
pytest -m live --live arbiter/sandbox/test_safety_killswitch.py -v          # Test 6 (G-1 unblocks cancel_all)
pytest -m live --live arbiter/sandbox/test_graceful_shutdown.py -v          # Test 9 (G-2 unblocked)
pytest -m live --live arbiter/sandbox/test_kalshi_fok_rejection.py -v       # Test 3 (G-4 widened assertion)
```

**Also:** UAT 13 browser check — load the dashboard in an operator session and confirm rate-limit pills render inside the ops section with ok/warn/crit tone transitions as collector circuit state changes.

**Remaining operator-side blockers outside this plan's scope:**
- Test 1 (Kalshi happy path): blocked on demo liquidity, not code.
- Tests 2, 4, 10: pending outside the Kalshi-gated scope.

## Deviations from Plan

None — plan executed exactly as written. All 5 tasks landed with their specified literals, grep invariants, and verification targets. No architectural checkpoints triggered.

## Known Stubs

None — all code changes wire real data paths (no hardcoded empty values, placeholders, or TODO stubs introduced by this plan).

## Incidental Notes

- `.rate-limit-pill.crit` CSS already existed in `arbiter/web/styles.css:4449` — no CSS additions required, plan anticipated this possibility correctly.
- Windows line-ending warnings (`LF will be replaced by CRLF`) on `conftest.py` and the new test file are cosmetic (`.gitattributes` unchanged); not a regression.

## Self-Check: PASSED

**Created files verified:**
- `.planning/phases/04-sandbox-validation/04-09-SUMMARY.md` — FOUND (this file)
- `arbiter/execution/adapters/test_kalshi_list_open_orders_signing.py` — FOUND (committed in `44cd93a`)

**Commits verified:**
- `44cd93a` (test RED) — FOUND
- `7989972` (fix G-1 GREEN) — FOUND
- `1e21684` (fix G-2) — FOUND
- `245319c` (fix G-4) — FOUND
- `134a685` (fix G-3) — FOUND
- `5f3787f` (fix G-5) — FOUND
