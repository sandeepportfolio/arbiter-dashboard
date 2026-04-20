---
phase: 05-live-trading
plan: 01
subsystem: safety
tags: [phase5, live-trading, hardlock, preflight, reconcile, harness, tdd, readiness, supervisor]

# Dependency graph
requires:
  - phase: 04-sandbox-validation
    provides: PHASE4_MAX_ORDER_USD adapter pattern (Plan 04-02 D-02), arbiter/sandbox/ harness shape, reconcile.assert_pnl_within_tolerance and assert_fee_matches helpers with D-17 ±$0.01 tolerance
  - phase: 03-safety-layer
    provides: SafetySupervisor kill-switch state machine, OperationalReadiness._check_profitability gate
  - phase: 02
    provides: KalshiAdapter.place_fok, PolymarketAdapter.place_fok, KalshiAdapter.place_resting_limit
provides:
  - "PHASE5_MAX_ORDER_USD adapter-layer hard-lock on 3 call sites (Polymarket.place_fok, Kalshi.place_fok, Kalshi.place_resting_limit)"
  - "PHASE4 hard-lock added to Kalshi.place_fok (closes gap documented in Plan 04-02 SUMMARY)"
  - "arbiter/live/ pytest harness: conftest, 3 guard-railed fixtures (production_db, production_kalshi, production_polymarket), evidence.py, reconcile.py with reconcile_post_trade"
  - "arbiter/live/preflight.py — 15-item Go-Live Preflight Checklist runner, callable from CLI and pytest"
  - ".env.production.template + .gitignore entries for Phase 5 credentials and evidence/05/"
  - "arbiter/live/README.md — 255-line operator runbook (Setup, Go-Live, Abort, Rollback, Troubleshooting)"
  - "SafetySupervisor.is_armed and .armed_by public @property accessors (W-5)"
  - "PHASE5_BOOTSTRAP_TRADES readiness override in _check_profitability (B-1 Q6 chicken-and-egg bypass)"
affects: [05-02, 06]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Env-var-opt-in adapter hard-lock (mirror of Plan 04-02 D-02)"
    - "arbiter/live/ harness mirroring arbiter/sandbox/ shape"
    - "Preflight-as-code: operator checklist implemented as dataclass-returning _check_* functions unit-testable in pytest and callable as CLI"
    - "Inverse safety guards on fixtures: production fixtures REFUSE demo URLs / sandbox DBs; sandbox fixtures REFUSE production URLs / live DBs"

key-files:
  created:
    - "arbiter/execution/adapters/test_phase5_hardlock.py — 18 unit tests (5 per call site + 3 combination tests)"
    - "arbiter/live/__init__.py, arbiter/live/conftest.py, arbiter/live/fixtures/__init__.py"
    - "arbiter/live/fixtures/production_db.py — asyncpg pool gated on DATABASE_URL=arbiter_live"
    - "arbiter/live/fixtures/kalshi_production.py — KalshiAdapter refusing demo URL/key"
    - "arbiter/live/fixtures/polymarket_production.py — PolymarketAdapter gated on PHASE5_MAX_ORDER_USD<=10"
    - "arbiter/live/evidence.py — re-exports from arbiter.sandbox.evidence"
    - "arbiter/live/reconcile.py — re-exports + NEW reconcile_post_trade async helper"
    - "arbiter/live/preflight.py — PreflightReport + 15 _check_* + run_preflight + CLI main"
    - "arbiter/live/test_reconcile.py — 4 non-live unit tests"
    - "arbiter/live/test_preflight.py — 34 unit tests + 3 integration tests"
    - "arbiter/live/README.md — operator runbook (255 lines)"
    - "arbiter/tests/__init__.py, arbiter/tests/test_readiness_bootstrap.py — 9 bootstrap-mode tests"
    - ".env.production.template — operator credential template"
  modified:
    - "arbiter/execution/adapters/polymarket.py — PHASE5 block inserted after PHASE4 in place_fok"
    - "arbiter/execution/adapters/kalshi.py — BOTH PHASE4 + PHASE5 added to place_fok; PHASE5 added to place_resting_limit"
    - "arbiter/readiness.py — import os + PHASE5_BOOTSTRAP_TRADES bootstrap block at top of _check_profitability"
    - "arbiter/safety/supervisor.py — public @property is_armed + armed_by accessors"
    - ".gitignore — added .env.production + evidence/05/ under Phase 5 section"

key-decisions:
  - "PHASE5 block inserted AFTER PHASE4 block in source order (not replacing) so both belts enforce in sequence; stricter cap effectively wins."
  - "KalshiAdapter.place_fok had NO hard-lock prior to Plan 05-01 (known gap). Added BOTH PHASE4 and PHASE5 together to avoid a regression window where PHASE5 is present but PHASE4 still missing."
  - "arbiter/live/conftest.py uses try/except on parser.addoption('--live') — standalone invocation needs its own registration; combined invocation (live+sandbox) currently fails because sandbox conftest also registers without try/except, but the plan's verify block requires separate invocations only."
  - "pytest_collection_modifyitems uses get_closest_marker('live') instead of substring in item.keywords — directory name 'arbiter/live/' contributes 'live' to keywords and would skip all non-live tests otherwise."
  - "reconcile_post_trade accepts fee_fetcher callable (None-safe) rather than calling adapters directly; keeps helper pure for unit tests and lets Plan 05-02 inject the real adapter-backed implementation."
  - "Preflight check #9 (W-2 polarity fix): PHASE4 absence in production is EXPECTED (pass, not-blocking). Only PHASE4<PHASE5 inversion blocks."
  - "Preflight checks #11 and #12 are non-blocking when the arbiter.main process isn't running yet (marked manual) — allows running preflight before bootstrap without misreporting."
  - "Bootstrap short-circuits BEFORE validated_profitable and blocked branches — documented escape hatch; operator setting PHASE5_BOOTSTRAP_TRADES = accepting the override (05-RESEARCH.md Open Question #6)."

patterns-established:
  - "Env-var-opt-in adapter belt: unset = no-op, strict > comparison, unparseable = 0.0 cap (maximally restrictive). Mirror of Plan 04-02 D-02 across both Kalshi and Polymarket adapters and both FOK and resting-limit paths."
  - "arbiter/live/ mirrors arbiter/sandbox/ with inverse safety guards — a one-line shape rule that makes the two harnesses cross-comparable."
  - "Preflight as unit-testable code rather than a markdown checklist: each _check_<N>_<name> returns a PreflightItem(key, label, passed, blocking, detail) dataclass; run_preflight orchestrates all 15 concurrently; to_table() renders for CLI."

requirements-completed: []  # TEST-05 requires live-fire (Plan 05-02); Plan 05-01 is scaffolding only.

# Metrics
duration: ~75min
completed: 2026-04-20
---

# Phase 05 Plan 01: Live Trading Scaffolding Summary

**PHASE5_MAX_ORDER_USD adapter hard-lock across three call sites + complete arbiter/live/ harness (preflight, fixtures, reconcile helper, runbook) + PHASE5_BOOTSTRAP_TRADES readiness bypass + SafetySupervisor public accessors — the entire go-live layer built with unit tests only, zero live API calls, setting up Plan 05-02 for the first real trade.**

## Performance

- **Duration:** ~75 minutes
- **Started:** 2026-04-20T21:20:00Z
- **Completed:** 2026-04-20T22:37:58Z
- **Tasks:** 3 (1 TDD-RED-GREEN, 1 standard, 1 TDD-RED-GREEN)
- **Files created:** 14
- **Files modified:** 5

## Accomplishments

- **3-call-site PHASE5 hard-lock** with identical D-02 semantics (unset=no-op, strict `>`, unparseable=0.0, structlog event, `_failed_order` without HTTP). Includes closing the pre-existing PHASE4 gap on `KalshiAdapter.place_fok`.
- **Complete `arbiter/live/` harness** mirroring `arbiter/sandbox/`: package init, guard-railed fixtures that fail-fast on production credential mismatches, `evidence/05/` wiring, and a new `reconcile_post_trade` async helper.
- **15-item Go-Live Preflight Checklist** implemented as `arbiter/live/preflight.py` — runnable as `python -m arbiter.live.preflight` (exit 0/1) and unit-tested per-check.
- **Operator runbook** at `arbiter/live/README.md` (255 lines) plus `.env.production.template` and `.gitignore` updates for Phase 5 secrets.
- **B-1 Q6 chicken-and-egg resolution:** `PHASE5_BOOTSTRAP_TRADES` env var bypass of the `collecting_evidence` block for the first N live trades.
- **W-5 public API:** `SafetySupervisor.is_armed` and `.armed_by` @property accessors replacing private-attribute access in Plan 05-02's test body.
- **68 new unit tests, all passing:** 18 PHASE5 hard-lock + 41 live-harness + 9 bootstrap-mode. Zero regressions in 113 existing adapter tests + 19 existing sandbox tests.

## Task Commits

1. **Task 1 (TDD): PHASE5_MAX_ORDER_USD hard-lock on 3 call sites**
   - RED: `09b52f4` — `test(05-01): add failing PHASE5_MAX_ORDER_USD hard-lock tests`
   - GREEN: `4dacdc8` — `feat(05-01): add PHASE5_MAX_ORDER_USD hard-lock to both adapters`
   - REFACTOR: skipped (per plan action guidance — PHASE4 structure preserved).
2. **Task 2: arbiter/live/ harness scaffold + preflight + runbook** (3 commits per plan sequence)
   - `56ccca0` — `feat(05-01): scaffold arbiter/live/ harness`
   - `6b36c07` — `feat(05-01): add Phase 5 preflight runner + non-live unit tests`
   - `f60f7b6` — `docs(05-01): add Phase 5 runbook + production env template`
3. **Task 3 (TDD): SafetySupervisor.is_armed + PHASE5_BOOTSTRAP_TRADES**
   - RED: `3b2281a` — `test(05-01): add failing bootstrap-mode readiness tests`
   - GREEN: `7749dc2` — `feat(05-01): add PHASE5_BOOTSTRAP_TRADES override + SafetySupervisor is_armed property`
   - REFACTOR: skipped (both changes minimal).

## Files Created/Modified

**Created (14 files):**

- `arbiter/execution/adapters/test_phase5_hardlock.py` — 18-test suite covering 5 unit cases × 3 call sites + 3 combination tests (PHASE5-tighter-wins, PHASE4-tighter-wins, PHASE4-unset+PHASE5-set).
- `arbiter/live/__init__.py`, `arbiter/live/fixtures/__init__.py` — package markers.
- `arbiter/live/conftest.py` — `--live` opt-in gate, `evidence_dir` fixture writing to `evidence/05/`, fixture plugin loader. Uses `get_closest_marker('live')` to skip only explicitly-marked tests.
- `arbiter/live/fixtures/production_db.py` — asyncpg pool that asserts `DATABASE_URL` contains `arbiter_live` and does NOT contain `arbiter_sandbox` or `arbiter_dev`.
- `arbiter/live/fixtures/kalshi_production.py` — `KalshiAdapter` fixture that refuses base URLs containing `demo`, key paths containing `demo`, or missing key files.
- `arbiter/live/fixtures/polymarket_production.py` — `PolymarketAdapter` fixture gated on `PHASE5_MAX_ORDER_USD <= $10` and `POLY_PRIVATE_KEY` + `POLY_FUNDER` presence.
- `arbiter/live/evidence.py` — re-exports `dump_execution_tables` and `write_balances` from `arbiter.sandbox.evidence`.
- `arbiter/live/reconcile.py` — re-exports D-17 tolerance helpers + `reconcile_post_trade(execution, adapters, tolerance, fee_fetcher)` returning a list of discrepancy dicts.
- `arbiter/live/preflight.py` — 13 sync + 2 async check functions, `PreflightReport.to_table()`, CLI `main()`.
- `arbiter/live/test_reconcile.py` — 4 unit tests (all-filled match, fee drift, failed-leg skip, None fetcher).
- `arbiter/live/test_preflight.py` — 34 per-check unit tests + 3 integration tests.
- `arbiter/live/README.md` — 255-line operator runbook.
- `arbiter/tests/__init__.py`, `arbiter/tests/test_readiness_bootstrap.py` — 9 tests for PHASE5_BOOTSTRAP_TRADES override.
- `.env.production.template` — operator credential template (DRY_RUN=false, arbiter_live DB, production Kalshi URLs, `PHASE5_MAX_ORDER_USD=10`, `MAX_POSITION_USD=10` B-5, `PHASE5_BOOTSTRAP_TRADES=1` B-1 Q6).

**Modified (5 files):**

- `arbiter/execution/adapters/polymarket.py` — PHASE5 hard-lock block inserted after existing PHASE4 in `place_fok` (log event `polymarket.phase5_hardlock.rejected`).
- `arbiter/execution/adapters/kalshi.py` — adds BOTH PHASE4 and PHASE5 blocks to `place_fok` (closing Plan 04-02 gap); adds PHASE5 block to `place_resting_limit` after existing PHASE4. All log events have `op="place_fok"` / `op="place_resting_limit"` kwarg.
- `arbiter/readiness.py` — adds `import os` and PHASE5_BOOTSTRAP_TRADES bypass block at the top of `_check_profitability` (before existing branches). Out-of-range / unparseable values fall through to existing logic.
- `arbiter/safety/supervisor.py` — adds `is_armed: bool` and `armed_by: Optional[str]` public `@property` accessors after `__init__`. No behavior change; `self._state` remains the backing store.
- `.gitignore` — adds `.env.production` and `evidence/05/` under a new Phase 5 section, preserving existing Phase 4 entries.

## Decisions Made

1. **PHASE5 block AFTER PHASE4 (not replacing):** Both belts enforced in sequence so when both caps are set the stricter one effectively wins. PHASE4's source-order priority means the PHASE4 error string is returned when PHASE4 is the tighter cap; otherwise PHASE5 returns its own error string. Unit tests 16-18 lock this ordering.
2. **Close the Phase 4 gap in `KalshiAdapter.place_fok` alongside the PHASE5 addition:** Plan 04-02 added PHASE4 only to `PolymarketAdapter.place_fok`; Plan 04-02.1 added it to `KalshiAdapter.place_resting_limit`. `KalshiAdapter.place_fok` had no hard-lock at all. Adding PHASE5 without PHASE4 would have created a regression window.
3. **`--live` flag registration in `arbiter/live/conftest.py` uses try/except on `ValueError`:** Standalone invocation (`pytest arbiter/live/`) only loads live's conftest so the flag needs to be registered here. Combined invocation (`pytest arbiter/live/ arbiter/sandbox/`) loads both conftests and the second registration raises; the try/except absorbs the collision. Sandbox conftest is not modified (outside plan scope).
4. **Use `get_closest_marker('live')` instead of `'live' in item.keywords`:** The directory name `arbiter/live/` contributes `'live'` to `item.keywords`, which would skip every non-live unit test under that directory. Marker-based detection only targets tests explicitly decorated with `@pytest.mark.live`.
5. **`reconcile_post_trade` accepts a `fee_fetcher` callable (nullable):** Keeps the helper pure for unit tests and defers the real adapter-backed fetcher to Plan 05-02. `None` = no ground-truth source = empty discrepancy list; does not break.
6. **`polymarket_order_fee` called without `canonical_id` in the reconcile helper:** The `Order` dataclass does not carry the market category, and the fee function's signature is `(price, quantity, fee_rate, category)`. Default category = `"default"`. Plan 05-02 can thread category lookup through `fee_fetcher` if per-market rates are needed.
7. **W-2 polarity fix in preflight check #9:** `PHASE4_MAX_ORDER_USD` absence is EXPECTED in production (pass, not-blocking). Only the unsafe inversion `PHASE4 < PHASE5` blocks — that inversion means Phase 4's tighter cap would reject below the Phase 5 belt.
8. **Preflight checks #11 and #12 are non-blocking when dashboard is unreachable:** Operators typically run preflight before starting `arbiter.main`. Marking these "unreachable" as non-blocking (with a clear message to start the process and re-run) avoids false blockers during setup.
9. **Bootstrap short-circuits BEFORE `validated_profitable` and `blocked` branches:** Bootstrap is the only operator-opt-in escape hatch. Having it win over `blocked` is intentional — the operator accepting the override is the whole point. Documented in 05-RESEARCH.md Open Question #6 and in `test_bootstrap_wins_over_blocked_verdict`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug] `pytest_collection_modifyitems` substring-match skipped non-live tests under `arbiter/live/`**
- **Found during:** Task 2 (running `pytest arbiter/live/ -v`).
- **Issue:** The plan's `conftest.py` template used `"live" in item.keywords` (copy of sandbox), but the directory name `arbiter/live/` contributes `'live'` to `item.keywords`, causing all 41 non-live unit tests to be skipped.
- **Fix:** Changed to `item.get_closest_marker("live") is not None` so only tests explicitly decorated with `@pytest.mark.live` are skipped.
- **Files modified:** `arbiter/live/conftest.py`.
- **Verification:** `pytest arbiter/live/ -v` went from 41 skipped to 41 passed.
- **Committed in:** `56ccca0` (scaffold commit).

**2. [Rule 1 — Bug] `reconcile_post_trade` called `polymarket_order_fee` with incorrect positional args**
- **Found during:** Task 2 (running `pytest arbiter/live/test_reconcile.py`).
- **Issue:** I had written `polymarket_order_fee(leg.canonical_id, fill_price, fill_qty)`. The real signature is `(price, quantity, fee_rate=None, category="default")` — `canonical_id` is not accepted.
- **Fix:** Changed the call to `polymarket_order_fee(float(leg.fill_price), float(leg.fill_qty))` (defaults to `category="default"`), added a doc comment that Plan 05-02 can thread per-market category through `fee_fetcher`. Also updated the test's `fetcher` callback to match the real signature.
- **Files modified:** `arbiter/live/reconcile.py`, `arbiter/live/test_reconcile.py`.
- **Verification:** `test_reconcile_all_filled_matching_fees_returns_empty` passes.
- **Committed in:** `6b36c07` (preflight + tests commit).

---

**Total deviations:** 2 auto-fixed (both Rule 1 bugs caught by the test suite immediately).
**Impact on plan:** Both fixes were pure correctness — no scope change, no architectural shift. Both were caught within one RED/GREEN cycle.

## Issues Encountered

- **Combined invocation `pytest arbiter/live/ arbiter/sandbox/` raises ValueError** on second `--live` registration. Plan's verify block uses separate invocations (`pytest arbiter/live/ && pytest arbiter/sandbox/`), which pass. The combined invocation is a known limitation documented in `arbiter/live/conftest.py`. Fixing it would require modifying `arbiter/sandbox/conftest.py`, which is outside the plan's `files_modified` list. Leaving as-is per scope boundary.
- **`py_clob_client.__version__` is `None`:** Preflight check #13 reports `py_clob_client=unknown` but still gates on the `POLYMARKET_MIGRATION_ACK` env var, so the check behaves correctly; operator attestation is what it was always going to depend on anyway.

## User Setup Required

None. Plan 05-01 is scaffolding-only: no operator credentials needed, no live API calls, no DB migrations. The `.env.production.template` is a template, not a live credential bundle.

Plan 05-02 will require operator setup (fund Kalshi, fund Polymarket wallet, create `arbiter_live` DB, source `.env.production`, run preflight, attest migration + runbook).

## Next Phase Readiness

**Ready for Plan 05-02 (live-fire first trade):**
- All 13 Wave 0 deliverables from `05-VALIDATION.md` are on disk.
- Adapter hard-lock is in place — Plan 05-02's `test_first_live_trade.py` can rely on $10 notional cap enforcement.
- `SafetySupervisor.is_armed` / `.armed_by` public accessors are in place — Plan 05-02 test body does not need to reach into private attributes.
- `PHASE5_BOOTSTRAP_TRADES` override is in place — Plan 05-02's first trade can clear readiness with `BOOTSTRAP=1`.
- `reconcile_post_trade` helper is in place — Plan 05-02's auto-abort wiring consumes its return value.

**Blocked on (Plan 05-02 concern, not this plan's):**
- Phase 4 D-19 gate is still PENDING (0/9 scenarios observed). Plan 05-02 Task 1 checkpoint is the enforcement point; Plan 05-01 scaffolding is safe to land now (W-4 risk note acknowledged in plan objective).

## Self-Check

**Created files — presence verification:**

```
FOUND: arbiter/execution/adapters/test_phase5_hardlock.py
FOUND: arbiter/live/__init__.py
FOUND: arbiter/live/conftest.py
FOUND: arbiter/live/fixtures/__init__.py
FOUND: arbiter/live/fixtures/production_db.py
FOUND: arbiter/live/fixtures/kalshi_production.py
FOUND: arbiter/live/fixtures/polymarket_production.py
FOUND: arbiter/live/evidence.py
FOUND: arbiter/live/reconcile.py
FOUND: arbiter/live/preflight.py
FOUND: arbiter/live/test_reconcile.py
FOUND: arbiter/live/test_preflight.py
FOUND: arbiter/live/README.md
FOUND: arbiter/tests/__init__.py
FOUND: arbiter/tests/test_readiness_bootstrap.py
FOUND: .env.production.template
```

**Modified files — verification:**

```
polymarket.py: contains PHASE5_MAX_ORDER_USD (2 matches)
kalshi.py: contains PHASE5_MAX_ORDER_USD (3 matches) and PHASE4_MAX_ORDER_USD (5 matches, up from 3)
readiness.py: contains PHASE5_BOOTSTRAP_TRADES
supervisor.py: contains "def is_armed" and "def armed_by" as @property
.gitignore: contains .env.production and evidence/05/
```

**Commit presence (via `git log --oneline -10`):**

```
FOUND: 09b52f4 test(05-01): add failing PHASE5_MAX_ORDER_USD hard-lock tests
FOUND: 4dacdc8 feat(05-01): add PHASE5_MAX_ORDER_USD hard-lock to both adapters
FOUND: 56ccca0 feat(05-01): scaffold arbiter/live/ harness
FOUND: 6b36c07 feat(05-01): add Phase 5 preflight runner + non-live unit tests
FOUND: f60f7b6 docs(05-01): add Phase 5 runbook + production env template
FOUND: 3b2281a test(05-01): add failing bootstrap-mode readiness tests
FOUND: 7749dc2 feat(05-01): add PHASE5_BOOTSTRAP_TRADES override + SafetySupervisor is_armed property
```

**Full test suite (quick-run sampling rate):**

```
pytest arbiter/execution/adapters/test_phase5_hardlock.py arbiter/tests/test_readiness_bootstrap.py arbiter/live/ -q
-> 68 passed in 7.28s

pytest arbiter/execution/adapters/ -q (regression)
-> 113 passed in 12.26s

pytest arbiter/sandbox/ -q (regression)
-> 19 passed, 11 skipped in 0.15s
```

## TDD Gate Compliance

Plan 05-01 type is `execute` (not `tdd`), but Tasks 1 and 3 were flagged `tdd="true"` internally. Gate sequence for those tasks (from `git log`):

- **Task 1:** `test(...)` at `09b52f4` (RED) -> `feat(...)` at `4dacdc8` (GREEN). No refactor needed.
- **Task 3:** `test(...)` at `3b2281a` (RED) -> `feat(...)` at `7749dc2` (GREEN). No refactor needed.

Both gates complied with the RED -> GREEN ordering. REFACTOR skipped on both per plan action guidance.

## Self-Check: PASSED

All 16 created files present, 5 modified files contain their target symbols, 7 task commits present in git history, 68 new unit tests green + 0 regressions across 113 adapter and 19 sandbox tests. Plan done criteria in all three tasks verified.

---
*Phase: 05-live-trading*
*Plan: 01*
*Completed: 2026-04-20*
