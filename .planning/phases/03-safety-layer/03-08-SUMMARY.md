---
phase: 03-safety-layer
plan: 08
subsystem: safety
tags: [risk-manager, per-platform-exposure, execution-engine, safe-02, gap-closure]

# Dependency graph
requires:
  - phase: 03-safety-layer
    provides: "RiskManager per-platform accounting scaffolding (Plan 03-02 SAFE-02) and _recover_one_leg_risk structured metadata (Plan 03-03 SAFE-03)"
provides:
  - "Live-mode per-platform exposure tracking that fires on submitted status (not just filled) — closes SAFE-02 burst-window gap"
  - "Symmetric release_trade hook on recovery cancellation — frees per-platform reservations when a previously-submitted leg is successfully cancelled"
  - "Recovering-status asymmetric accounting — only the surviving leg's exposure is recorded (rejected leg never had real exposure)"
affects: [phase-04-sandbox-validation, safety-verification-re-run]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Record-before-recover ordering: _live_execution now records per-platform exposure BEFORE calling _recover_one_leg_risk, so the release_trade hook inside recovery can free the reservation that was just booked."
    - "Single-platform record_trade call form for asymmetric states: when only one leg has real exposure (recovering status), use record_trade(..., platform=X) instead of the full yes/no split to avoid double-counting the rejected side."
    - "Pre-recovery leg-status snapshot: _recover_one_leg_risk snapshots leg.status BEFORE _cancel_order mutates it to CANCELLED, so the release_trade decision can distinguish 'was SUBMITTED/PARTIAL (has reservation to free)' from 'was PENDING (nothing recorded)'."

key-files:
  created: []
  modified:
    - "arbiter/execution/engine.py — _live_execution status+record reordered; recovering branch added; _recover_one_leg_risk gains release_trade hook."
    - "arbiter/execution/test_engine.py — 4 new tests covering burst rejection, recovering survivor-only recording, cancel release, and dry-run parity."

key-decisions:
  - "Option A (record on submitted) chosen over Option B (reserve_exposure/release_exposure reservation model) — 1-line guard removal + explicit recovery symmetry has ~3× smaller blast radius, mirrors _simulate_execution verbatim, and keeps the one-shot lifecycle intact within a single _live_execution call."
  - "Record-BEFORE-recover ordering required a deviation from the plan's Task 2 design (plan assumed recovery fired before record; actual flow is the reverse). Snapshotted survivor detection + reordered needs_recovery flag keeps the fix surgical while making the release_trade hook meaningful."

patterns-established:
  - "Pattern: snapshot leg.status before _cancel_order mutates it, so release-trade decisions can distinguish 'was recorded' from 'never recorded'."
  - "Pattern: when recording asymmetric (single-platform) exposure in _live_execution, use record_trade(canonical_id, exposure, pnl, platform=X) to avoid the yes/no split double-counting a rejected venue."

requirements-completed: [SAFE-02]

# Metrics
duration: 11min
completed: 2026-04-17
---

# Phase 03 Plan 08: SAFE-02 Gap Closure Summary

**Live-mode per-platform exposure tracking now fires on submitted status (not just filled) with symmetric release_trade on recovery cancellation — closes the Phase 03 VERIFICATION SAFE-02 PARTIAL gap.**

## Performance

- **Duration:** 11 min
- **Started:** 2026-04-17T02:42:38Z
- **Completed:** 2026-04-17T02:53:42Z
- **Tasks:** 4 (3 code/test + 1 verification-only)
- **Files modified:** 2 (`arbiter/execution/engine.py`, `arbiter/execution/test_engine.py`)

## Accomplishments

- **Burst-window closed:** Two back-to-back live arb opportunities on the same platform whose combined legs exceed `SafetyConfig.max_platform_exposure_usd=$300` — the second is now rejected by `check_trade` BEFORE `place_fok` dispatch (not merely after both legs fill).
- **Recovering accounting is now asymmetric:** When one leg FILLS and the other is rejected by the venue, only the surviving leg's exposure is recorded against `_platform_exposures`. The rejected leg is never recorded (it never had real exposure).
- **Submit-cancel symmetry:** When `_recover_one_leg_risk` successfully cancels a previously-SUBMITTED leg, `release_trade` frees the per-platform reservation that was just booked, keeping `_platform_exposures` coherent under recovery.
- **Dry-run parity intact:** `_simulate_execution` is unchanged; its existing `record_trade` call site stays verbatim. All prior engine and adapter tests still pass.

## Task Commits

1. **Task 0: Wave-0 failing tests for the SAFE-02 live-mode gap** — `8c0cd88` (test)
2. **Task 1: Fix _live_execution per-platform accounting** — `577bd97` (fix, TDD GREEN)
3. **Task 2: Add release_trade hook in _recover_one_leg_risk** — `57a6234` (fix, TDD GREEN)
4. **Task 3: Full-suite regression sweep** — no commit (verification-only)

## Why Option A Over Option B

The plan explicitly chose Option A (record on submitted) over Option B (new `reserve_exposure` / `release_exposure` reservation primitives in `RiskManager`) for four reasons:

1. **Lower blast radius.** Option A is a surgical guard removal + a small recovering branch + one new `release_trade` call inside an existing loop. Option B would require new public `RiskManager` methods, a new dispatcher surface in `_place_order_for_leg`, and rewiring of every test that uses `_place_order_for_leg` directly — ~3× the change surface.
2. **Mirrors the dry-run path verbatim.** `_simulate_execution` already calls `record_trade(canonical_id, total, pnl, yes_platform=..., no_platform=..., yes_exposure=..., no_exposure=...)` unconditionally. Option A makes the live path use the IDENTICAL call shape — preserving the dry-run/live parity invariant the VERIFICATION report cites.
3. **Symmetric within the current one-shot lifecycle.** `_live_execution` returns once with a final status. Recovery happens inline via `_recover_one_leg_risk`, so we have full information at the record-trade decision point.
4. **Test additionality is small.** Four focused tests (three red gates + one parity guard) cover all three observable truths plus symmetry. No new test infrastructure required.

## Code Changes

### 1. `arbiter/execution/engine.py::_live_execution`

**Before (lines 780-824):**

```python
status = "submitted"
notes: List[str] = []
if leg_yes.status in {OrderStatus.FAILED, OrderStatus.CANCELLED, OrderStatus.ABORTED} or leg_no.status in {...}:
    if leg_yes.status in {OrderStatus.FILLED, OrderStatus.PARTIAL, OrderStatus.SUBMITTED} or leg_no.status in {...}:
        status = "recovering"
        notes.extend(await self._recover_one_leg_risk(arb_id, opp, leg_yes, leg_no))  # ← cancels legs INLINE
    else:
        status = "failed"
elif ...:
    status = "recovering"
    notes.extend(await self._recover_one_leg_risk(arb_id, opp, leg_yes, leg_no))
elif leg_yes.status == OrderStatus.FILLED and leg_no.status == OrderStatus.FILLED:
    status = "filled"

# ... execution dataclass built ...

if status in {"submitted", "filled"}:
    if status == "filled":                         # ← THE BUG GUARD
        self.risk.record_trade(opp.canonical_id, ...)
```

**After:**

```python
# Status decision first (snapshots pre-recovery leg statuses so survivor
# detection below sees SUBMITTED/FILLED/PARTIAL, not CANCELLED).
needs_recovery = False
if ...FAILED/CANCELLED/ABORTED...:
    if ...survivor exists...:
        status = "recovering"
        needs_recovery = True  # ← defer recovery until AFTER recording
    else:
        status = "failed"
elif ...PARTIAL...:
    status = "recovering"
    needs_recovery = True
elif ...both FILLED...:
    status = "filled"

# ... execution dataclass built ...

if status in {"submitted", "filled"}:
    self.risk.record_trade(opp.canonical_id, ..., yes_platform=..., no_platform=..., yes_exposure=..., no_exposure=...)
elif status == "recovering":
    # Record ONLY the surviving leg using single-platform form.
    surviving_platform, surviving_exposure = _detect_survivor(leg_yes, leg_no, opp)
    if surviving_platform:
        self.risk.record_trade(opp.canonical_id, surviving_exposure, pnl, platform=surviving_platform)
    else:
        # Edge case: both surviving (e.g. both PARTIAL) — record full split;
        # recovery's release_trade hook will rebalance per cancel confirm.
        self.risk.record_trade(opp.canonical_id, ..., yes_platform=..., no_platform=..., ...)

# Recovery runs AFTER recording so release_trade can free the reservation.
if needs_recovery:
    notes.extend(await self._recover_one_leg_risk(arb_id, opp, leg_yes, leg_no))
```

### 2. `arbiter/execution/engine.py::_recover_one_leg_risk`

**Before (lines 1045-1049):**

```python
for leg in (leg_yes, leg_no):
    if leg.status in {OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIAL}:
        cancelled = await self._cancel_order(leg)
        notes.append(f"cancel-{leg.side}:{'ok' if cancelled else 'failed'}")
return notes
```

**After:**

```python
for leg in (leg_yes, leg_no):
    if leg.status in {OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIAL}:
        original_status = leg.status  # ← snapshot before _cancel_order mutates to CANCELLED
        cancelled = await self._cancel_order(leg)
        notes.append(f"cancel-{leg.side}:{'ok' if cancelled else 'failed'}")
        # If the leg was SUBMITTED/PARTIAL (i.e. Task 1 booked a reservation
        # for it) and the cancel succeeded, release that reservation.
        # PENDING legs were never recorded; failed cancels leave exposure real.
        if cancelled and original_status in {OrderStatus.SUBMITTED, OrderStatus.PARTIAL}:
            unfilled_qty = max(leg.quantity - leg.fill_qty, 0)
            if unfilled_qty > 0:
                self.risk.release_trade(
                    opp.canonical_id,
                    unfilled_qty * leg.price,
                    platform=leg.platform,
                )
return notes
```

## Test Results

### New Tests (Plan 03-08 gap closure)

| Test | Status | Proves |
|------|--------|--------|
| `test_live_burst_submitted_rejected_at_per_platform_ceiling` | PASS | Burst of two submitted on same platform — second rejected by check_trade; `_platform_exposures` accumulates on submit; place_fok NOT called for rejected opp. |
| `test_live_recovering_records_only_surviving_leg` | PASS | Recovering status records ONLY the filled leg; rejected leg not in `_platform_exposures`. |
| `test_live_recovery_cancellation_releases_reservation` | PASS | Cancel of a previously-SUBMITTED leg invokes release_trade; pre-seeded unrelated exposure on the same platform remains intact ($100 + $60 record - $60 release = $100). |
| `test_dry_run_record_trade_unchanged_after_fix` | PASS | Dry-run path produces identical per-platform and per-market accounting as before; guards against dry-run regression during live-path surgery. |

### Full Regression Suite

| Scope | Before | After | Delta |
|-------|--------|-------|-------|
| `arbiter/execution/test_engine.py` | 27 passed | 31 passed | +4 (new tests) |
| `arbiter/execution/` (adapters + engine) | 113 passed + 2 skipped | 117 passed + 2 skipped | +4 |
| `arbiter/safety/` + `arbiter/test_api_safety.py` | 17 passed + 1 skipped | 17 passed + 1 skipped | 0 |
| `arbiter/test_main_shutdown.py` | 3 passed | 3 passed | 0 |
| **Canonical Phase 3 regression (combined)** | 133 passed + 3 skipped | **137 passed + 3 skipped** | **+4** |

All static acceptance checks:

- `grep -E 'if status == "filled":' arbiter/execution/engine.py` → 0 matches (bug guard removed).
- `grep -E 'elif status == "recovering":' arbiter/execution/engine.py` → 1 match (recovering branch present).
- `grep -E 'self\.risk\.release_trade' arbiter/execution/engine.py` → 3 matches (1 new in _recover_one_leg_risk + 2 existing in update_manual_position).
- `grep -c 'Plan 03-08' arbiter/execution/engine.py` → 3 (edit annotations at both sites).
- `node --check arbiter/web/dashboard.js` → exit 0 (dashboard untouched).
- Module imports clean (`from arbiter.safety import SafetySupervisor; from arbiter.execution.engine import ExecutionEngine; print('OK')`).

## Decisions Made

- **Option A (record on submitted) over Option B (reservation model)** — smaller blast radius, dry-run parity preserved, fits one-shot lifecycle. See "Why Option A Over Option B" above.
- **Record-BEFORE-recover ordering** — deviation from the plan's Task 2 design (see "Deviations from Plan" below). Required because `_recover_one_leg_risk` mutates leg state; if we recorded after recovery the release would fire against an empty reservation and record would follow, producing net mis-accounting.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Record-trade / recover-one-leg ordering was inverted in the plan**

- **Found during:** Task 2 (after adding release_trade inside `_recover_one_leg_risk`).
- **Issue:** The plan's Task 2 assumed `_recover_one_leg_risk` fired BEFORE the `record_trade` block in `_live_execution`, so the new release_trade call would free a reservation that had just been booked. The actual control flow is the reverse: `_recover_one_leg_risk` was called INSIDE the status-decision tree at engine.py:793/798, well before the record_trade block at line 814+. As a result, after Task 2 landed, the flow was: (1) recovery fires, leg.status mutates to CANCELLED, release_trade pops the (empty) reservation, (2) record_trade fires with CANCELLED leg statuses — falling into the edge-case full-split branch and adding exposure back. Net result: `_platform_exposures["kalshi"]` was accidentally correct by coincidence, but `_open_positions["MKT_CNC"]` was wrong ($60 leftover after cancel).
- **Fix:** Restructured `_live_execution` to (a) decide status without calling `_recover_one_leg_risk`, setting a `needs_recovery` flag instead; (b) record per-platform exposure; (c) THEN call `_recover_one_leg_risk` if `needs_recovery` is set. Survivor detection in the "recovering" branch now sees pre-recovery leg statuses, and Task 2's release_trade hook in `_recover_one_leg_risk` now fires against a real reservation booked moments earlier.
- **Files modified:** `arbiter/execution/engine.py` (Task 2 commit `57a6234`).
- **Verification:** `test_live_recovery_cancellation_releases_reservation` now asserts that a pre-seeded $100 Kalshi exposure on an UNRELATED market is unchanged after the new-arb submit+cancel cycle (i.e. release freed exactly the new leg's $60, not more or less). Full regression at 137 passed + 3 skipped.
- **Committed in:** `57a6234` (Task 2 commit — the restructure was folded into Task 2 because the buggy ordering only surfaced when Task 2's release call was wired in).

**2. [Rule 2 - Missing critical] MathAuditor bypass helper for gap-closure tests**

- **Found during:** Task 0 (initial test run).
- **Issue:** The sibling helper `_make_safety_opp()` (from Plan 03-02) hand-sets `gross_edge=0.10, total_fees=0.03, net_edge=0.07` defaults that don't match MathAuditor's shadow recomputation for the Plan 03-08 test price combinations. The existing Plan 03-02 tests side-stepped this by calling `RiskManager.check_trade` directly, not `execute_opportunity`. The Plan 03-08 tests need to go through `execute_opportunity` to exercise `_live_execution`, which means passing through `_audit_opportunity` — which rejects every `_make_safety_opp()` as "Shadow math audit critical".
- **Fix:** Added a small test-local helper `_bypass_math_auditor(engine)` that replaces `engine._auditor.audit_opportunity` and `engine._auditor.audit_execution` with stubs returning `AuditResult(passed=True)`. The MathAuditor itself is already covered exhaustively by `arbiter/audit/test_math_auditor.py`; skipping it in the Plan 03-08 tests keeps the fixture focus on RiskManager per-platform accounting.
- **Files modified:** `arbiter/execution/test_engine.py` (Task 0 commit `8c0cd88`).
- **Verification:** `test_dry_run_record_trade_unchanged_after_fix` (the parity test) passes both before and after the engine.py fixes, confirming the bypass is sound.
- **Committed in:** `8c0cd88` (Task 0 commit).

---

**Total deviations:** 2 auto-fixed (1 bug in plan's ordering assumption, 1 missing test infrastructure).
**Impact on plan:** Both auto-fixes were essential to make the planned tests and fixes work. The ordering fix actually made the solution cleaner (explicit record-before-recover rather than relying on the plan's mistaken assumption). No scope creep.

## Issues Encountered

Initial Task 3 test-3 false-pass: pre-Task-1, the test `test_live_recovery_cancellation_releases_reservation` was trivially passing because the current bug guarded `record_trade` on filled only, so `_platform_exposures["kalshi"]` was never populated in the first place. Strengthened the test during Task 0 to pre-seed a $100 Kalshi exposure on an unrelated market, so the test now genuinely proves release_trade fired for exactly the new leg's $60 (not more, not less).

## Deferred Operator UAT (unchanged)

The 3 deferred operator UAT items from `.planning/phases/03-safety-layer/03-VERIFICATION.md` remain deferred regardless of this plan:

1. Operator kill-switch ARM + RESET end-to-end (requires browser UI + WS auth).
2. Shutdown banner visibility before WebSocket close (requires running server + open browser session).
3. Rate-limit pills color transition under load (requires real throttle state in a browser session).

These will be walked through by `/gsd-verify-work` against a running server before Phase 4 ships. This gap-closure plan does not affect them.

## Re-verification Expectation

Re-running `/gsd-verify-work` on Phase 03 should now show:

- **SAFE-02 row:** PARTIAL → **SATISFIED** (burst-window closed, submitted-status accounting correct, release symmetry confirmed).
- **Phase 03 score:** 17/18 → **18/18** (the one PARTIAL row now fully passes).

## Next Phase Readiness

- Phase 03 safety layer is now fully closed per VERIFICATION's automated checks.
- Phase 04 (sandbox validation) can consume an ExecutionEngine whose `RiskManager._platform_exposures` is coherent under the full live order lifecycle (submit, fill, partial, recover-and-cancel).
- No new blockers introduced.

## Self-Check

Files created/modified (verified to exist on disk):

- `arbiter/execution/engine.py` — FOUND
- `arbiter/execution/test_engine.py` — FOUND
- `.planning/phases/03-safety-layer/03-08-SUMMARY.md` — FOUND (this file)

Commits (verified in `git log`):

- `8c0cd88` — FOUND (Task 0: test commit)
- `577bd97` — FOUND (Task 1: fix commit)
- `57a6234` — FOUND (Task 2: fix commit, includes ordering deviation)

## Self-Check: PASSED

---
*Phase: 03-safety-layer*
*Completed: 2026-04-17*
