---
phase: 04-sandbox-validation
plan: 08
subsystem: testing
tags: [pytest, aggregator, reconciliation, validation-artifact, phase-gate, d-17, d-19, pending-live-fire]

# Dependency graph
requires:
  - phase: 04-sandbox-validation
    plan: 01
    provides: reconcile.RECONCILE_TOLERANCE_USD (D-17), evidence dir convention
  - phase: 04-sandbox-validation
    plans: [03, 04, 05, 06, 07]
    provides: scenario_manifest.json schema across 9 live-fire scenarios
provides:
  - "arbiter/sandbox/aggregator.py library (collect_scenario_manifests, reconcile_pnl_across_manifests, render_validation_markdown, write_validation_markdown)"
  - "arbiter/sandbox/test_aggregator.py: 13 offline unit tests covering collection, reconciliation, and rendering"
  - "arbiter/sandbox/test_phase_reconciliation.py: terminal @pytest.mark.live driver with D-19 hard-gate enforcement"
  - ".planning/phases/04-sandbox-validation/04-VALIDATION.md: authoritative Phase 5 input (populated pending-live-fire; re-generated on live-fire completion)"
affects: [05-live-trading]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Pending-live-fire VALIDATION.md mode (status=pending_live_fire, phase_gate_status=PENDING) so the artifact exists and documents the acceptance contract before operator provisions .env.sandbox"
    - "D-19 hard-gate via pytest.fail() (not pytest.skip) with breach diagnostics in failure message"
    - "Aggregator is OFFLINE-testable: synthesizes scenario_manifest.json fixtures under tmp_path; no live API or DB needed for 13/13 green"
    - "realized_pnl primary + signed-notional fallback in _compute_recorded_pnl (Phase 2 execution_orders has no realized_pnl column)"
    - "Scenario-name heuristic for platform selection in balance-delta PnL check ('kalshi' / 'polymarket' / 'poly' substrings)"

key-files:
  created:
    - arbiter/sandbox/aggregator.py
    - arbiter/sandbox/test_aggregator.py
    - arbiter/sandbox/test_phase_reconciliation.py
  modified:
    - .planning/phases/04-sandbox-validation/04-VALIDATION.md

key-decisions:
  - "Operate in pending-live-fire mode when evidence/04/ is empty: VALIDATION.md gets status=pending_live_fire + phase_gate_status=PENDING rather than a fake-PASS or missing-file state. Re-running the aggregator after live-fire produces the authoritative PASS/BLOCKED verdict."
  - "realized_pnl-primary with signed-notional fallback in _compute_recorded_pnl: Phase 2 execution_orders schema does not directly store realized_pnl; the aggregator first looks for an explicit column (if a scenario harness computed it), then falls back to a buy/sell-aware cash-flow delta minus fee."
  - "Injected-tagged scenarios (SAFE-03, SAFE-04) bypass balance/fee reconciliation per D-11: they have no real balance deltas (mock adapters) but assert their own invariants. Aggregator trusts manifest self-claims (exec_01_invariant_holds, cancel_succeeded, etc.) for the overall_passed verdict when no balance/fee check is applicable."
  - "Hard-gate uses pytest.fail() (NOT pytest.skip()) per D-19: skips do not fail CI, but the gate MUST fail the suite on tolerance breach so operators cannot accidentally ship Phase 5."
  - "Augmented VALIDATION.md with explicit ## Phase Gate Status + ## Operator Workflow sections beyond the plan pseudocode to improve legibility (plan's must_haves.artifacts lists 'contains: ## Phase Gate Status' as an explicit requirement)."

patterns-established:
  - "Phase-acceptance aggregator shape: collect -> reconcile -> render -> write (library) + terminal live-test driver"
  - "Offline-testable aggregator: synthesize scenario_manifest.json under tmp_path for unit tests; no live-fixture dependency"
  - "Dual-mode VALIDATION.md: pending-live-fire (expected-scenarios table, PENDING status) vs populated (observed-scenarios table, PASS/BLOCKED)"
  - "D-19 enforcement via pytest.fail with breach-detail message: operator sees non-zero exit + per-scenario discrepancy values pointing at evidence directories"

requirements-completed: []
requirements-scaffolded-awaiting-live-run: [TEST-01, TEST-02, TEST-03, TEST-04]

# Metrics
duration: 12min
completed: 2026-04-17
---

# Phase 4 Plan 08: Phase Reconciliation Aggregator + 04-VALIDATION.md Summary

**Offline-testable aggregator library + terminal @pytest.mark.live driver + populated (pending-live-fire) 04-VALIDATION.md acceptance artifact. D-19 hard-gate enforced via pytest.fail on any real-tagged reconciliation breach; phase gate BLOCKS Phase 5 when the tolerance breach exists. Because .env.sandbox is not provisioned on this host, the authoritative artifact is in pending_live_fire state with a 9-row expected-scenarios table and a 19-row Per-Task Verification Map ready for operator-triggered re-generation.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-04-17T08:30:00Z (approx)
- **Completed:** 2026-04-17T08:43:00Z
- **Tasks:** 2
- **Files created:** 3 (1,183 new lines)
- **Files modified:** 1 (04-VALIDATION.md — 112 lines, full rewrite from 82-line draft)
- **Commits:** 4 (RED + GREEN + live-test + docs)

## Task Commits

| Commit | Type | Scope | Summary |
|--------|------|-------|---------|
| `be2e038` | test | 04-08 | Task 1 RED: failing aggregator unit tests (13 tests) |
| `996b521` | feat | 04-08 | Task 1 GREEN: aggregator library (763 lines) |
| `7bcf64f` | feat | 04-08 | Task 2a: terminal phase-reconciliation live test |
| `d6fc7ad` | docs | 04-08 | Task 2b: populate 04-VALIDATION.md (pending live-fire) |

## Files Created

- `arbiter/sandbox/aggregator.py` (820 lines) -- `collect_scenario_manifests`, `reconcile_pnl_across_manifests`, `render_validation_markdown`, `write_validation_markdown`, `ReconcileReport`, `ScenarioReconcileResult`, CLI entrypoint
- `arbiter/sandbox/test_aggregator.py` (273 lines) -- 13 offline unit tests
- `arbiter/sandbox/test_phase_reconciliation.py` (90 lines) -- terminal `@pytest.mark.live` test

## Files Modified

- `.planning/phases/04-sandbox-validation/04-VALIDATION.md` -- rewritten from 82-line draft to 112-line populated (pending live-fire) artifact via `python -m arbiter.sandbox.aggregator`

## Aggregator Library API Surface

```python
from arbiter.sandbox.aggregator import (
    # Constants
    DEFAULT_EVIDENCE_ROOT,                 # pathlib.Path("evidence/04")
    VALIDATION_MD_PATH,                    # .planning/.../04-VALIDATION.md

    # Dataclasses
    ScenarioReconcileResult,               # per-scenario verdict + discrepancies
    ReconcileReport,                        # aggregate (any_real_breach, phase_gate_status)

    # Primary API
    collect_scenario_manifests,             # (evidence_root=...) -> list[dict]
    reconcile_pnl_across_manifests,         # (manifests, tolerance=0.01) -> ReconcileReport
    render_validation_markdown,             # (manifests, report) -> str
    write_validation_markdown,              # (manifests, report, target_path=...) -> None
)
```

CLI entrypoint: `python -m arbiter.sandbox.aggregator` writes VALIDATION.md from whatever manifests currently exist under `evidence/04/` — usable for operator dry-runs between live-fire attempts.

## Live-Fire Status: PENDING

**Why pending:** `.env.sandbox` is not provisioned on this host (only `.env.sandbox.template` exists per Plan 04-02). Without it, none of the 9 live scenario tests can execute, so `evidence/04/*/scenario_manifest.json` does not exist. The aggregator correctly detects this and emits `status: pending_live_fire`, `phase_gate_status: PENDING` in the VALIDATION.md frontmatter.

**Verified live-run behavior on empty evidence:**
- `pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py` fails fast with the explicit message "No scenario manifests found under evidence/04/... Source .env.sandbox and run the full live suite, then re-run this test." — no vacuous PASS, no silent skip.

**Phase gate on live-fire completion:** When operator runs the full live suite (`pytest -m live --live arbiter/sandbox/` across all 9 scenario tests) and then re-runs `test_phase_reconciliation_and_validation_report`:
- All 9 `scenario_manifest.json` files are collected from `evidence/04/<scenario>_<UTC ts>/`.
- TEST-03 (PnL) + TEST-04 (fee) reconciliation applied at +/-$0.01 per D-17.
- D-19 hard gate: `pytest.fail` on any real-tagged scenario breach.
- VALIDATION.md overwritten with `phase_gate_status: PASS` (if all pass) or `BLOCKED` + Tolerance Breach section (if any real breach).

## Scenario Count + Traceability

9 expected scenarios across 5 scenario plans (04-03 through 04-07):

| # | Scenario | Plan Task | Tag | Requirements |
|---|----------|-----------|-----|--------------|
| 1 | kalshi_happy_lifecycle | 04-03 Task 1 | real | TEST-01, TEST-04 |
| 2 | polymarket_happy_lifecycle | 04-04 Task 1 | real | TEST-02, TEST-04 |
| 3 | kalshi_fok_rejected_on_thin_market | 04-03 Task 2 | real | EXEC-01, TEST-01 |
| 4 | polymarket_fok_rejected_on_thin_market | 04-04 Task 2 | real | EXEC-01, TEST-02 |
| 5 | kalshi_timeout_triggers_cancel_via_client_order_id | 04-03 Task 3 | real | TEST-01, EXEC-05, EXEC-04 |
| 6 | kill_switch_cancels_open_kalshi_demo_order | 04-05 Task 1 | real | SAFE-01, TEST-01 |
| 7 | one_leg_recovery_injected | 04-06 Task 1 | injected | SAFE-03, TEST-01 |
| 8 | rate_limit_burst_triggers_backoff_and_ws | 04-06 Task 2 | injected | SAFE-04, TEST-01 |
| 9 | sigint_cancels_open_kalshi_demo_orders | 04-07 Task 1 | real | SAFE-05, TEST-01 |

**Observed so far: 0.** Authored-for-execution: all 9 test files exist; operator-gated on `.env.sandbox` + per-scenario env overrides.

## Recorded-PnL Column Substitution (per plan output spec)

Plan asked: "Actual execution_orders column used for recorded PnL computation (if `realized_pnl` wasn't the right column, document the substitute)."

**Implementation:** `_compute_recorded_pnl` uses a two-tier strategy:

1. **Primary:** explicit `realized_pnl` field on each row. Used when a scenario harness has computed and stored it (e.g., future EXEC-03-driven scenarios might write it directly).
2. **Fallback:** signed cash-flow notional. For each row with `fill_price` + `fill_qty` + `side` + `fee`:
   - `side=buy` contributes `-(fill_price * fill_qty) - fee` (cash out)
   - `side=sell` contributes `+(fill_price * fill_qty) - fee` (cash in minus fee)
   - Unknown side: skip

The Phase 2 `execution_orders` schema does not include a dedicated `realized_pnl` column (verified via grep on `arbiter/sql/schema/` and `arbiter/execution/store.py`). Absent a scenario harness that computes it, the fallback provides a best-effort cash-flow delta suitable for reconciliation within a single scenario's balance window.

**Caveat:** The fallback is NOT a true realized PnL (it ignores cost basis across fills). For happy-path scenarios where a single FOK buy + immediate settlement is the full lifecycle, the fallback IS exactly what balance-delta measures. For more complex scenarios (future multi-leg sandbox tests), scenario harnesses SHOULD compute `realized_pnl` explicitly in their manifest emission.

**Future work:** A Phase 5 or later plan may add `realized_pnl` as a computed column to `execution_orders` or to the `execution_arbs` table to make the primary path authoritative.

## Interesting Edge Cases Observed

1. **Malformed manifest doesn't crash the aggregator.** Unit test `test_collect_with_malformed_manifest` asserts the `parse_error` fallback: the aggregator emits a stub dict with `tag: "unknown"` and logs the JSON parse exception into the manifest itself. Rationale: one bad file shouldn't tank an 8-scenario run's aggregator pass.

2. **Injected scenarios without balance/fee data default to PASS on self-claim.** No `balances_pre.json` + no `platform_fee` field => `applicable_checks` is empty => `overall_passed` falls back to the manifest's own assertions (`exec_01_invariant_holds`, `cancel_succeeded`, etc.). This means SAFE-03 / SAFE-04 injected scenarios can pass reconciliation even though they never hit a real exchange — which is the intended D-11 contract.

3. **Empty evidence/04/ yields `phase_gate_status: PASS` in ReconcileReport BUT `PENDING` in VALIDATION.md.** The `ReconcileReport.phase_gate_status` property returns PASS when `any_real_breach` is False (no breaches in an empty set). But the Markdown renderer detects `awaiting_live_fire = (len(observed_scenarios) == 0)` and emits frontmatter `status: pending_live_fire`, `phase_gate_status: PENDING` instead. Covered by `test_reconcile_report_phase_gate_status_pass_when_empty` (empty report => PASS) and `test_render_markdown_contains_required_sections` (renders with minimal manifest).

4. **Aggregator can be re-run offline.** `python -m arbiter.sandbox.aggregator` is a pure stdlib + arbiter call; operators can regenerate VALIDATION.md between live-runs without re-invoking pytest. Useful for iteration and debugging.

## Deviations from Plan

### Intentional enhancements (beyond plan pseudocode)

**1. [Rule 2 - Missing critical functionality] Explicit `## Phase Gate Status` section + `## Operator Workflow` section in rendered VALIDATION.md**
- **Issue:** Plan pseudocode's `render_validation_markdown` emitted frontmatter `phase_gate_status:` but no dedicated `## Phase Gate Status` section in the body. Plan's `must_haves.artifacts[2].contains` explicitly lists `## Phase Gate Status` as a required token.
- **Fix:** Added `## Phase Gate Status` body section with PASS/BLOCKED/PENDING narrative text and `## Operator Workflow` section with copy-paste shell commands for .env.sandbox provisioning + scenario env overrides + pytest invocation order.
- **Files modified:** `arbiter/sandbox/aggregator.py::render_validation_markdown`
- **Committed in:** `d6fc7ad`

**2. [Rule 2 - Missing critical functionality] Pending-live-fire mode**
- **Issue:** Plan's `render_validation_markdown` assumed manifests always exist. For the current environment (no `.env.sandbox`, empty `evidence/04/`), a naive call would emit a zero-row scenario table + an ambiguous "PASS" status (no breaches in an empty set). That would falsely advertise Phase 5 unblocked.
- **Fix:** Added `awaiting_live_fire = (len(observed_scenarios) == 0)` branch. When triggered, frontmatter uses `status: pending_live_fire` + `phase_gate_status: PENDING`, the banner says "Phase 5 BLOCKED per D-19 until operator runs the full live-fire suite", and the scenario table lists the 9 expected scenarios with PENDING status.
- **Files modified:** `arbiter/sandbox/aggregator.py::render_validation_markdown`
- **Committed in:** `996b521` (GREEN; augmented further in `d6fc7ad`)

**3. [Rule 2 - Missing critical functionality] `_compute_recorded_pnl` signed-notional fallback**
- **Issue:** Plan pseudocode read `realized_pnl` directly from execution_orders rows. Phase 2 `execution_orders` schema does not carry this column (verified). Without a fallback, reconciliation would perpetually fail on "no recorded PnL to compare" even on happy-path runs.
- **Fix:** Two-tier `_compute_recorded_pnl`: primary reads `realized_pnl` if present; fallback computes signed cash-flow (buy => -notional - fee, sell => +notional - fee).
- **Files modified:** `arbiter/sandbox/aggregator.py`
- **Committed in:** `996b521`

**4. [Rule 2 - Missing critical functionality] Malformed-manifest graceful fallback**
- **Issue:** Plan pseudocode used bare `json.loads(...)` with only `json.JSONDecodeError` caught. If any scenario test crashes mid-write and leaves a partial manifest, the aggregator would hit one bad file and skip the rest of the 8+ scenarios.
- **Fix:** Wrapped in try/except producing `parse_error` stub dict with `tag: "unknown"`. Aggregator always processes all manifests even if individual ones are malformed. Covered by `test_collect_with_malformed_manifest`.
- **Files modified:** `arbiter/sandbox/aggregator.py::collect_scenario_manifests`
- **Committed in:** `996b521`

**No other deviations.** All acceptance-criteria grep patterns pass; unit-test count (13) exceeds plan minimum (6); production code outside `arbiter/sandbox/` was not touched.

## TDD Gate Compliance

- **RED gate:** `be2e038` (test) — 13 unit tests fail on `ModuleNotFoundError: No module named 'arbiter.sandbox.aggregator'` (explicit, intended first-failure signal).
- **GREEN gate:** `996b521` (feat) — aggregator implemented; 13/13 tests pass.
- **REFACTOR gate:** Not exercised (implementation was clean on first pass; no post-GREEN tidy-up needed). The `d6fc7ad` VALIDATION.md-population commit added new sections to `render_validation_markdown` but did not change existing test behavior (13/13 still pass).

## Live-Fire Readiness (Downstream Operator Actions)

The plan's operator workflow, rendered directly into `04-VALIDATION.md` for visibility:

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

## Issues Encountered

- **Worktree base drift.** Initial HEAD was `7d4bd33` (pre-04-02.1). Per `<worktree_branch_check>` the worktree was hard-reset to `d2e46bc` (expected base after all Wave 2 plans merged). Verified `git rev-parse HEAD` matches expected base before any file edits.
- **Initial file write targeted main-repo path.** First `Write` tool call created `test_aggregator.py` in the main repo (`C:/Users/sande/Documents/arbiter-dashboard/arbiter/sandbox/`), not the worktree. Fixed by moving the file to the worktree path and committing from there. No commit-history impact.
- **Pre-existing pytest-asyncio deprecation warning** about `asyncio_default_fixture_loop_scope`. Pre-dates this plan; out of scope per executor scope boundary.

## Auth Gates

None. All work was file creation + unit tests + offline aggregator runs. No API calls were made. Live-fire execution remains operator-gated outside this plan's scope.

## Threat Model Mitigation Status

All 6 threat IDs from plan's `<threat_model>` section:

| Threat ID | Disposition | Mitigation Status |
|-----------|-------------|-------------------|
| T-04-08-01 (Manifest tampering) | accept | Documented in SUMMARY; operator-edited manifests are ephemeral (aggregator overwrites on next run) |
| T-04-08-02 (VALIDATION.md edit bypass) | mitigate | Aggregator overwrites each run; operator edits are replaced |
| T-04-08-03 (Phase gate BLOCKED override) | mitigate | D-19 hard-gate via `pytest.fail` surfaces breach detail; override requires documented remediation |
| T-04-08-04 (Balance delta disclosure) | accept | Balance deltas are small ($<$5); artifact is a planning doc in git |
| T-04-08-05 (Missing manifests DoS) | mitigate | `assert manifests` at start of live test emits clear actionable error; aggregator library no-ops gracefully on empty root |
| T-04-08-06 (Breach miscount) | mitigate | 13 unit tests cover tolerance boundary, injected path, breach path, and empty-set semantics |

## Known Stubs

None. The rendered VALIDATION.md has a "pending live-fire" status that is a legitimate state (no `.env.sandbox` on this host), not a stub — re-running the aggregator after live-fire produces the authoritative PASS/BLOCKED artifact automatically. The `_compute_recorded_pnl` signed-notional fallback is an intentional graceful degradation documented in the Decisions section, not a stub.

## Threat Flags

None. No new network endpoints, auth paths, or schema changes at trust boundaries. Aggregator reads local JSON files and writes a local Markdown file.

## Next Phase Readiness

**Ready for Phase 5:**
- `04-VALIDATION.md` exists as the single Phase 5 input at the canonical path.
- While it currently reads `phase_gate_status: PENDING`, the operator path to `PASS` (or explicit BLOCKED) is automated: run the 9 live scenarios + the terminal aggregator, and the file overwrites itself with the authoritative verdict.
- Phase 5's gate-check logic (if any) can simply grep `04-VALIDATION.md` for `phase_gate_status: PASS` + `status: complete`.

**Blockers:** None from this plan's code. The Phase 5 go-live gate itself is blocked on operator provisioning of `.env.sandbox` + throwaway wallet — that is the entire Phase 4 acceptance predicate, explicitly outside this plan's scope (delegated to operator per D-19).

## Self-Check: PASSED

**Files created (verified on disk):**
- FOUND: `arbiter/sandbox/aggregator.py` (820 lines)
- FOUND: `arbiter/sandbox/test_aggregator.py` (273 lines)
- FOUND: `arbiter/sandbox/test_phase_reconciliation.py` (90 lines)
- FOUND: `.planning/phases/04-sandbox-validation/04-VALIDATION.md` (112 lines, overwritten)

**Commits (verified via `git log --oneline d2e46bc..HEAD`):**
- FOUND: `be2e038` — test(04-08): add failing aggregator unit tests (RED)
- FOUND: `996b521` — feat(04-08): implement Phase 4 acceptance aggregator (GREEN)
- FOUND: `7bcf64f` — feat(04-08): add terminal phase-reconciliation live test
- FOUND: `d6fc7ad` — docs(04-08): populate 04-VALIDATION.md (pending live-fire state)

**Acceptance-criteria greps (all pass):**
- `aggregator.py` exports: `collect_scenario_manifests`, `reconcile_pnl_across_manifests`, `render_validation_markdown`, `write_validation_markdown`, `ReconcileReport`, `ScenarioReconcileResult` — all present.
- `test_aggregator.py` has 13 tests covering: empty-root, collection, PnL pass, PnL breach (gate=BLOCKED), fee breach (gate=BLOCKED), injected pass, malformed-manifest, markdown-contains-sections, markdown-breach-section, markdown-write, default-evidence-root constant, empty-report-default, dataclass shape.
- `pytest arbiter/sandbox/test_aggregator.py -v` => **13 passed**.
- `test_phase_reconciliation.py` contains `collect_scenario_manifests`, `write_validation_markdown`, `D-19 HARD GATE`, `pytest.fail` (all grep-verified).
- `pytest --collect-only` on `test_phase_reconciliation.py` => 1 test collected; SKIPPED without `--live`.
- VALIDATION.md contains: `phase_gate_status:`, `# Phase 4: Sandbox Validation`, `## Phase Gate Status`, `## Scenario Results`, `## Per-Task Verification Map`, `## Manual-Only Verifications`.
- Breach-path rendering verified in `test_render_markdown_marks_breach_section_when_blocked` (produces `Phase 5 BLOCKED` substring).

**Full sandbox suite behavior:**
- `pytest arbiter/sandbox/` (no flag): **19 passed, 11 skipped** (was 6 passed + 3 skipped pre-04-08; delta: +13 aggregator passes, +1 phase_reconciliation skip, +7 other scenario skips previously present).
- `pytest arbiter/sandbox/ --collect-only`: **30 tests collected** (+14 net from this plan).

**Scope boundary:**
- Zero modifications outside `arbiter/sandbox/` + `.planning/phases/04-sandbox-validation/`.
- No STATE.md or ROADMAP.md changes (parallel-executor invariant honored).

---
*Phase: 04-sandbox-validation*
*Completed: 2026-04-17*
