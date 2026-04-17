---
phase: 04-sandbox-validation
plan: 01
subsystem: testing
tags: [pytest, structlog, asyncpg, kalshi-demo, polymarket, live-fire, evidence-capture]

# Dependency graph
requires:
  - phase: 03-safety-layer
    provides: BalanceMonitor, KalshiAdapter, PolymarketAdapter, structlog SHARED_PROCESSORS
provides:
  - arbiter/sandbox/ pytest package with @pytest.mark.live opt-in
  - sandbox_db_pool, demo_kalshi_adapter, poly_test_adapter fixtures with env-var guard-rails
  - evidence_dir fixture (per-scenario structlog JSONL FileHandler)
  - balance_snapshot factory fixture (real KalshiCollector + PolymarketCollector)
  - evidence.dump_execution_tables + evidence.write_balances helpers
  - reconcile.assert_pnl_within_tolerance + reconcile.assert_fee_matches (±$0.01 hard gate)
  - Operator-facing README for credential + DB bootstrap
affects: [04-03, 04-04, 04-05, 04-06, 04-07]

# Tech tracking
tech-stack:
  added: [pytest opt-in --live marker, structlog ProcessorFormatter per-test file handler]
  patterns:
    - "Fixture-built-at-runtime guard-rail assertions (fail before opening any resource)"
    - "pytest_plugins for fixture module re-export"
    - "Per-scenario evidence directory under evidence/04/<scenario>_<UTC ts>/"

key-files:
  created:
    - arbiter/sandbox/__init__.py
    - arbiter/sandbox/conftest.py
    - arbiter/sandbox/evidence.py
    - arbiter/sandbox/reconcile.py
    - arbiter/sandbox/fixtures/__init__.py
    - arbiter/sandbox/fixtures/sandbox_db.py
    - arbiter/sandbox/fixtures/kalshi_demo.py
    - arbiter/sandbox/fixtures/polymarket_test.py
    - arbiter/sandbox/README.md
    - arbiter/sandbox/test_smoke.py
  modified: []

key-decisions:
  - "Adopted production adapter constructor signatures (KalshiAdapter accepts (config, session, auth, rate_limiter, circuit); PolymarketAdapter accepts (config, clob_client_factory, rate_limiter, circuit)) in fixtures instead of the plan's placeholder single-arg form, so scenarios exercise the same wiring as arbiter/main.py:218-231."
  - "Per-scenario structlog FileHandler attached to the 'arbiter' logger namespace (not root) to preserve secret redaction via SHARED_PROCESSORS while leaving non-arbiter loggers untouched."
  - "balance_snapshot fixture builds REAL KalshiCollector + PolymarketCollector and AssertionErrors on construction failure — no object() substitutes, consistent with plan fix rule #2."

patterns-established:
  - "Opt-in live marker pattern: pytest_addoption(--live) + pytest_configure (marker registration) + pytest_collection_modifyitems auto-skip unless -m live or --live"
  - "Per-test structlog JSONL redirection via ProcessorFormatter(foreign_pre_chain=SHARED_PROCESSORS) mounted as a FileHandler on 'arbiter' logger with teardown-restored level"
  - "Env-var guard-rail fixtures that assert-before-yield (DATABASE_URL, PHASE4_MAX_ORDER_USD, KALSHI_BASE_URL) so misconfigured sandboxes fail-fast at fixture build time, not mid-scenario"

requirements-completed: [TEST-01, TEST-02, TEST-03, TEST-04]

# Metrics
duration: 35min
completed: 2026-04-17
---

# Phase 4 Plan 01: Sandbox Harness Scaffolding Summary

**arbiter/sandbox/ pytest package with @pytest.mark.live opt-in, env-var guard-rails (sandbox DB + demo Kalshi + PHASE4_MAX_ORDER_USD), per-scenario structlog JSONL evidence capture, real-collector balance snapshotting, and ±$0.01 reconciliation helpers.**

## Performance

- **Duration:** 35 min
- **Started:** 2026-04-17T06:52:00Z (approx)
- **Completed:** 2026-04-17T07:27:28Z
- **Tasks:** 3
- **Files created:** 10 (654 total lines)

## Accomplishments

- `@pytest.mark.live` opt-in gate enforced at collection time — sandbox tests SKIP unless `-m live` or `--live` is passed
- Three env-var-guarded fixtures (`sandbox_db_pool`, `demo_kalshi_adapter`, `poly_test_adapter`) fail-fast before opening any DB connection, aiohttp session, or ClobClient when `DATABASE_URL`, `KALSHI_BASE_URL`, or `PHASE4_MAX_ORDER_USD` is misconfigured
- `evidence_dir` fixture creates `evidence/04/<scenario>_<UTC ts>/` AND attaches a per-test structlog JSONL FileHandler (via ProcessorFormatter over SHARED_PROCESSORS) on the `arbiter` logger namespace, tearing down cleanly
- `balance_snapshot` fixture wires real `KalshiCollector` + `PolymarketCollector` into `BalanceMonitor` (no `object()` placeholders) so TEST-03 reconciliation has actual pre/post data
- `evidence.dump_execution_tables()` writes execution_orders / execution_fills / execution_incidents / execution_arbs JSON per scenario
- `reconcile.RECONCILE_TOLERANCE_USD = 0.01` (D-17) with `assert_pnl_within_tolerance` + `assert_fee_matches` helpers that produce structured AssertionError messages citing D-19
- Operator README (158 lines) covers Kalshi demo key generation, Polymarket test-wallet bootstrap, USDC bridging, DB setup, run commands, and a 6-entry Troubleshooting section referencing Pitfalls 5/6/7
- 7 smoke tests (6 pass, 1 skipped without flag; all 7 pass with `--live`) validate the wiring without hitting any real API

## Task Commits

Each task was committed atomically (TDD RED/GREEN cycle on Tasks 1-2):

1. **Task 1 RED: failing smoke test for live marker** — `e2e48fb` (test)
2. **Task 1 GREEN: implement @pytest.mark.live opt-in gate** — `a2ef897` (feat)
3. **Task 2 RED: extend smoke tests for evidence/reconcile/fixtures** — `b7279f6` (test)
4. **Task 2 GREEN: guard-railed fixtures + evidence/reconcile helpers + structlog JSONL** — `df5b7f8` (feat)
5. **Task 3: operator README for sandbox bootstrap** — `976e65c` (docs)

No separate REFACTOR commit — the GREEN implementations were already clean and matched the plan's style.

## Files Created

- `arbiter/sandbox/__init__.py` (1 line) — package marker
- `arbiter/sandbox/conftest.py` (173 lines) — opt-in marker + `pytest_plugins` + `evidence_dir` + `balance_snapshot` fixtures
- `arbiter/sandbox/evidence.py` (43 lines) — `dump_execution_tables()` + `write_balances()`
- `arbiter/sandbox/reconcile.py` (38 lines) — `RECONCILE_TOLERANCE_USD` + assertion helpers
- `arbiter/sandbox/fixtures/__init__.py` (1 line) — subpackage marker
- `arbiter/sandbox/fixtures/sandbox_db.py` (22 lines) — `sandbox_db_pool` asyncpg fixture with DATABASE_URL guard
- `arbiter/sandbox/fixtures/kalshi_demo.py` (48 lines) — `demo_kalshi_adapter` with KALSHI_BASE_URL guard, real production wiring
- `arbiter/sandbox/fixtures/polymarket_test.py` (88 lines) — `poly_test_adapter` with PHASE4_MAX_ORDER_USD guard, lazy ClobClient factory
- `arbiter/sandbox/README.md` (158 lines) — operator bootstrap + troubleshooting
- `arbiter/sandbox/test_smoke.py` (82 lines) — 7 smoke tests

Total: 654 lines across 10 files. No files modified outside `arbiter/sandbox/`.

## Interface Contracts Published (for downstream 04-03 through 04-07)

**Fixtures available via `conftest.py` (automatically injected when a scenario test imports them):**

```python
# Fails before opening any connection if DATABASE_URL lacks 'arbiter_sandbox'
async def sandbox_db_pool() -> asyncpg.Pool

# Fails before building if KALSHI_BASE_URL lacks 'demo-api.kalshi.co'
# Depends on sandbox_db_pool. Real KalshiAdapter; yields adapter; closes aiohttp session on teardown.
async def demo_kalshi_adapter(sandbox_db_pool) -> KalshiAdapter

# Fails before building if PHASE4_MAX_ORDER_USD unset. Depends on sandbox_db_pool.
# Real PolymarketAdapter with lazy ClobClient factory (None if py-clob-client missing or key unset).
async def poly_test_adapter(sandbox_db_pool) -> PolymarketAdapter

# Creates evidence/04/<scenario>_<UTC ts>/ and installs structlog JSONL FileHandler
# on 'arbiter' logger. Removes handler + restores level on teardown.
def evidence_dir(request) -> pathlib.Path

# Factory: await snapshot() -> dict[platform, {'balance': float|None, 'timestamp': float}]
# Built with REAL KalshiCollector + PolymarketCollector; fail-fast if either fails to construct.
async def balance_snapshot(sandbox_db_pool) -> Callable[[], Awaitable[dict]]
```

**Helpers available via `from arbiter.sandbox import evidence, reconcile`:**

```python
async def evidence.dump_execution_tables(pool: asyncpg.Pool, directory: pathlib.Path) -> None
def     evidence.write_balances(directory, pre, post) -> None
def     reconcile.assert_pnl_within_tolerance(platform, pre_balance, post_balance, recorded_pnl, tolerance=0.01) -> None
def     reconcile.assert_fee_matches(platform, platform_fee, computed_fee, tolerance=0.01) -> None
reconcile.RECONCILE_TOLERANCE_USD = 0.01
```

## Observed Production Signatures (verified during execution)

- `BalanceMonitor.__init__(config: AlertConfig, collectors: dict)` — matched the plan exactly; `check_balances()` returns `Dict[str, BalanceSnapshot]` where `BalanceSnapshot(platform, balance, timestamp, is_low)`
- `KalshiCollector.__init__(config: KalshiConfig, price_store: PriceStore)` — 2-arg signature, matched expected
- `PolymarketCollector.__init__(config: PolymarketConfig, price_store: PriceStore)` — 2-arg signature, matched expected
- `KalshiAdapter.__init__(config, session, auth, rate_limiter, circuit)` — 5-arg signature (DEVIATES from plan pseudocode `KalshiAdapter(cfg)`); matched production usage in `arbiter/main.py:218-224`
- `PolymarketAdapter.__init__(config, clob_client_factory, rate_limiter, circuit)` — 4-arg signature (DEVIATES from plan pseudocode `PolymarketAdapter(cfg)`); matched production usage in `arbiter/main.py:225-231`
- `KalshiAuth(api_key_id, private_key_path)` — lifted from `arbiter.collectors.kalshi` so the adapter fixture can wire auth without a collector instance

## Decisions Made

1. **Real production wiring in adapter fixtures:** Plan pseudocode used `KalshiAdapter(cfg)` / `PolymarketAdapter(cfg)`; real constructors take 4-5 args (config, session, auth, rate_limiter, circuit for Kalshi; config, clob_client_factory, rate_limiter, circuit for Polymarket). Fixtures mirror `arbiter/main.py` wiring verbatim so scenarios exercise the identical code path. Rationale: plan explicitly warned "If `KalshiAdapter` does not accept a single `cfg` argument or requires a session/session factory, inspect `arbiter/execution/adapters/kalshi.py:__init__` and adapt the call. Do not invent a new constructor — match what is there."

2. **Lazy ClobClient factory in `poly_test_adapter`:** No engine exists at fixture build time, so there is no cached ClobClient to share via closure (as in `arbiter/main.py:228`). The fixture instead provides a local lazy factory that instantiates a ClobClient on first call, gracefully returning None if `py-clob-client` is not installed or `POLY_PRIVATE_KEY` is unset. This matches the engine's own `_get_poly_clob_client` None-handling semantics.

3. **Teardown restores `arbiter` logger level:** `evidence_dir` always restores the prior level on teardown (even if the fixture raised `None → DEBUG` promotion). Prevents leaked DEBUG level from affecting unrelated tests in the same session.

4. **Adapter close detection:** Kalshi adapter has no `close()`/`aclose()` method — the fixture explicitly closes the shared `aiohttp.ClientSession` instead. Polymarket adapter has no owning resources (ClobClient is GC'd or reused across calls) — nothing to close. Both branches documented in fixture docstrings.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Adapter constructor signature mismatch**
- **Found during:** Task 2 (`kalshi_demo_adapter` + `poly_test_adapter` fixtures)
- **Issue:** Plan pseudocode used `KalshiAdapter(cfg)` / `PolymarketAdapter(cfg)` single-arg form. Actual production constructors (verified in `arbiter/execution/adapters/kalshi.py:46` and `arbiter/execution/adapters/polymarket.py:40`) take 4-5 positional dependencies.
- **Fix:** Wired fixtures with the real constructor signatures, constructing `KalshiAuth`, `aiohttp.ClientSession`, `RateLimiter("kalshi-exec", 10, 1.0)`, `CircuitBreaker("kalshi-exec", 5, 30.0)` for Kalshi and `RateLimiter("poly-exec", 5, 1.0)`, `CircuitBreaker("poly-exec", 5, 30.0)` plus a lazy `ClobClient` factory for Polymarket. Rate-limit + circuit parameters mirror `arbiter/main.py:205-216` exactly.
- **Files modified:** `arbiter/sandbox/fixtures/kalshi_demo.py`, `arbiter/sandbox/fixtures/polymarket_test.py`
- **Verification:** `pytest arbiter/sandbox/test_smoke.py::test_fixture_modules_importable` PASSES; import chain `from arbiter.sandbox.fixtures import kalshi_demo, polymarket_test` resolves cleanly.
- **Committed in:** `df5b7f8`

---

**Total deviations:** 1 auto-fixed (1 blocking). Plan explicitly anticipated this and instructed the executor to adapt; no scope creep — fixtures match production wiring rather than inventing a new surface.

## Issues Encountered

None — the plan was precise and the RED/GREEN cycle on each task surfaced exactly the expected gaps.

## TDD Gate Compliance

Tasks 1 and 2 followed the plan's `tdd="true"` directive:

- **Task 1 RED:** `e2e48fb` (test) — smoke test confirming `test_live_marker_runs` passed unconditionally (wrong — expected SKIP without flag)
- **Task 1 GREEN:** `a2ef897` (feat) — opt-in gate; smoke now SKIPS without flag, PASSES with `--live`
- **Task 2 RED:** `b7279f6` (test) — extended smoke with evidence/reconcile/fixture imports (fail because modules missing)
- **Task 2 GREEN:** `df5b7f8` (feat) — fixtures + helpers implemented; all 6 non-live tests PASS

Task 3 is `type="auto"` (not TDD) — single `docs` commit `976e65c`.

## User Setup Required

Operators must complete the credential bootstrap in `arbiter/sandbox/README.md` before running any `pytest -m live --live` invocation:

- `./keys/kalshi_demo_private.pem` (separate from prod key)
- Funded Polygon test wallet (~$10 USDC)
- `.env.sandbox` with `DATABASE_URL` pointing at `arbiter_sandbox`, `KALSHI_BASE_URL` pointing at `demo-api.kalshi.co`, and `PHASE4_MAX_ORDER_USD=5`
- `docker-compose up -d` followed by `arbiter_sandbox` schema migration

The `.env.sandbox.template` itself is built in Plan 04-02, not this plan.

## Next Phase Readiness

**Ready for downstream waves:**
- 04-02 can build on `arbiter/sandbox/` and add the `.env.sandbox.template` + `init-sandbox.sh` + Polymarket adapter hard-lock patch
- 04-03 through 04-07 can import `demo_kalshi_adapter`, `poly_test_adapter`, `sandbox_db_pool`, `evidence_dir`, `balance_snapshot`, `evidence.dump_execution_tables`, `evidence.write_balances`, `reconcile.assert_pnl_within_tolerance`, `reconcile.assert_fee_matches` with no further scaffolding

**Blockers:** None. All guard-rails in place; the only remaining safety layer (adapter-level `PHASE4_MAX_ORDER_USD` check inside `polymarket.py::place_fok`) is explicitly Plan 04-02's scope.

## Self-Check: PASSED

- `arbiter/sandbox/__init__.py` exists (verified)
- `arbiter/sandbox/fixtures/__init__.py` exists (verified)
- `arbiter/sandbox/conftest.py` contains `pytest_addoption`, `pytest_configure`, `pytest_collection_modifyitems`, `pytest_plugins`, `evidence_dir`, `balance_snapshot`, `run.log.jsonl`, `ProcessorFormatter`, `SHARED_PROCESSORS`, `JSONRenderer`, `FileHandler`, `addHandler`, `removeHandler` (13 grep matches)
- `arbiter/sandbox/conftest.py` does NOT contain `pytest_pyfunc_call` (verified — 0 matches in arbiter/sandbox/)
- `arbiter/sandbox/conftest.py` does NOT contain `object()` (verified — 0 matches)
- `arbiter/sandbox/conftest.py` imports `KalshiCollector` + `PolymarketCollector` (verified — 3 grep matches)
- `arbiter/sandbox/evidence.py` iterates over all 4 execution tables (verified — 4 grep matches)
- `arbiter/sandbox/reconcile.py` contains `RECONCILE_TOLERANCE_USD = 0.01` + both assert helpers
- `arbiter/sandbox/README.md` has 158 lines (≥60) and contains all 6 required tokens
- `pytest arbiter/sandbox/` (no flag): 6 passed, 1 skipped (verified)
- `pytest -m live --live arbiter/sandbox/`: 1 passed, 6 deselected (verified)
- No `@pytest.mark.asyncio` anywhere in `arbiter/sandbox/` (verified — 0 matches)
- Commit hashes all present in git log: `e2e48fb`, `a2ef897`, `b7279f6`, `df5b7f8`, `976e65c`

---
*Phase: 04-sandbox-validation*
*Completed: 2026-04-17*
