---
phase: 02-execution-operational-hardening
plan: 01
subsystem: infra
tags: [structlog, tenacity, sentry-sdk, cryptography, logging, observability, python]

# Dependency graph
requires:
  - phase: 01-api-integration-fixes
    provides: working Kalshi/Polymarket clients that depend on cryptography (RSA signing)
provides:
  - structlog JSON logging via ProcessorFormatter stdlib bridge
  - contextvars propagation across asyncio awaits (arb_id, order_id, canonical_id)
  - secret redaction processor (_KEY/_SECRET/_DSN/Authorization)
  - Sentry SDK init at main() entry point with Asyncio/AioHttp/Logging integrations
  - cryptography 46.x upgrade (OPS-04) verified against Kalshi collector tests
  - tenacity 9.1.4 and sentry-sdk 2.58.0 pinned in requirements
affects:
  - 02-02 (execution store) — uses structlog bound contextvars for arb_id in logs
  - 02-03 (pre-trade depth) — uses tenacity retry decorators on adapter HTTP calls
  - 02-04 (adapters) — uses structlog bound contextvars + sentry capture on errors
  - 02-05 (timeouts) — uses structlog for cancellation events
  - 02-06 (engine integration) — wires setup_logging and _init_sentry side effects

# Tech tracking
tech-stack:
  added:
    - structlog 25.5.0 (JSON logging + contextvars)
    - tenacity 9.1.4 (retry with exponential jitter)
    - sentry-sdk 2.58.0 (error tracking with asyncio/aiohttp/logging integrations)
  patterns:
    - structlog + stdlib ProcessorFormatter bridge (zero-touch of existing logger.info calls)
    - structlog.stdlib.ExtraAdder processor to flow stdlib extra={} into event_dict
    - structlog.contextvars.bind_contextvars for per-Task context propagation
    - Secret-stripping structlog processor regex (_KEY$|_SECRET$|_DSN$|^Authorization$)
    - sentry_sdk.init(dsn=None) no-op default when SENTRY_DSN unset
    - sentry-sdk 2.x Transport subclass (not instance) for test injection

key-files:
  created:
    - arbiter/utils/test_logger.py (4 tests: JSON shape, contextvars, secrets, signature)
    - arbiter/test_sentry_integration.py (2 tests: async exception capture, dsn=None no-op)
  modified:
    - requirements.txt (add structlog/tenacity/sentry-sdk, bump cryptography + py-clob-client)
    - arbiter/requirements.txt (mirror of repo-root)
    - arbiter/utils/logger.py (full rewrite: structlog + ProcessorFormatter + secret redactor)
    - arbiter/main.py (add sentry imports + _init_sentry() + call before setup_logging)
    - .env.template (document SENTRY_DSN, ARBITER_ENV, ARBITER_RELEASE)

key-decisions:
  - "Use structlog stdlib bridge (ProcessorFormatter) not full migration — keeps existing logger.info calls working unchanged"
  - "Add ExtraAdder to foreign_pre_chain so logger.info(msg, extra={k: v}) flows k into JSON event dict"
  - "Secret redactor runs AFTER ExtraAdder so extras from stdlib callers are also redacted"
  - "_init_sentry() called BEFORE setup_logging() so LoggingIntegration sees the final JSON formatter"
  - "dsn=None on unset SENTRY_DSN is the safe default — sentry-sdk makes this a no-op"
  - "Test uses sentry-sdk 2.x Transport subclass (not instance) — API shift from 1.x"

patterns-established:
  - "Pattern: structlog SHARED_PROCESSORS list reused by both structlog.configure (processors) and ProcessorFormatter (foreign_pre_chain) so native structlog calls and stdlib calls produce identical JSON"
  - "Pattern: contextvars.bind_contextvars(arb_id=...) at engine scope → every downstream log line inherits arb_id until clear_contextvars()"
  - "Pattern: module-level buffer + Transport subclass for test injection (sentry-sdk 2.x ABI)"

requirements-completed: [OPS-01, OPS-02, OPS-04]

# Metrics
duration: 8 min
completed: 2026-04-16
---

# Phase 2 Plan 01: Observability Foundation Summary

**structlog ProcessorFormatter bridge emits JSON via existing logger.info calls, contextvars propagate across asyncio, Sentry captures unhandled async exceptions — cryptography upgraded to 46.0.7 without breaking Kalshi RSA signing.**

## Performance

- **Duration:** 8 min
- **Started:** 2026-04-16T20:37:50Z
- **Completed:** 2026-04-16T20:45:35Z
- **Tasks:** 3
- **Files modified:** 5 (+ 2 new test files)

## Accomplishments

- Pinned and installed `structlog==25.5.0`, `tenacity==9.1.4`, `sentry-sdk==2.58.0`; upgraded `cryptography` from 44.0.0 to 46.0.7 — Kalshi RSA signing path still clean (kalshi_collector tests pass)
- Rewrote `arbiter/utils/logger.py` around `structlog.stdlib.ProcessorFormatter` — all existing `logging.getLogger("arbiter.X").info(...)` calls across the codebase now emit valid JSON to stdout without needing to change call sites
- Added `structlog.contextvars.merge_contextvars` to the processor chain: bound `arb_id`, `canonical_id`, `order_id` will propagate across asyncio await boundaries (per-Task contextvar semantics)
- Added `_strip_secrets` processor that redacts any event_dict key matching `_KEY$|_SECRET$|_DSN$|^Authorization$` (Pitfall 7 / T-02-01 threat register)
- Wired `sentry_sdk.init(...)` as the first action inside `arbiter/main.py:main()` (before `setup_logging`), with `AsyncioIntegration`, `AioHttpIntegration`, and `LoggingIntegration(level=INFO, event_level=ERROR)`. `send_default_pii=False` enforces T-02-02 mitigation.
- Verified `dsn=None` (when `SENTRY_DSN` unset) is a genuine no-op — safe for local dev / CI
- 6 new tests pass: 4 for logger (JSON parseable, contextvars propagate, secret stripping, signature preserved) + 2 for Sentry (async exception capture via fake Transport, dsn=None no-op)

## Task Commits

Each task was committed atomically:

1. **Task 1: Update requirements.txt files and verify install** — `2e2a873` (chore)
2. **Task 2: Rewrite arbiter/utils/logger.py + unit tests** — `76ebbfe` (feat)
3. **Task 3: Initialize Sentry SDK + .env.template + async test** — `72b0f5d` (feat)

## Files Created/Modified

- `requirements.txt` — added structlog/tenacity/sentry-sdk, bumped cryptography to 46.x, py-clob-client to 0.34.x
- `arbiter/requirements.txt` — mirror of repo-root (kept identical per plan instruction)
- `arbiter/utils/logger.py` — replaced stdlib-only logging with structlog + ProcessorFormatter; preserved `setup_logging(level, log_file)` signature, `prepare_console_stream`, and `TradeLogger` class
- `arbiter/utils/test_logger.py` (NEW) — 4 unit tests validating JSON output, contextvars, secret redaction, signature preservation
- `arbiter/main.py` — added 3 sentry_sdk integration imports + `_init_sentry()` + call before `setup_logging()` at the top of `main()`
- `arbiter/test_sentry_integration.py` (NEW) — 2 unit tests: async exception capture + dsn=None no-op
- `.env.template` — appended Sentry section with SENTRY_DSN, ARBITER_ENV, ARBITER_RELEASE

## Decisions Made

- **Stdlib bridge over full structlog migration.** Matches "safety > speed" — avoids touching every collector/audit/scanner module in this PR. Existing `logger.info(...)` calls continue to work unchanged and emit JSON.
- **ExtraAdder added to processor chain.** Without it, stdlib callers' `logger.info(msg, extra={k: v})` keys don't appear in the JSON event dict. This was discovered during test runs (Rule 1 bug fix — see Deviations).
- **Secret redactor runs AFTER ExtraAdder.** Ensures secrets passed via stdlib `extra={}` get redacted (not just secrets from native structlog calls).
- **_init_sentry() before setup_logging().** Per plan requirement — LoggingIntegration attaches handlers to root logger, and setup_logging clears them. Order ensures the formatter is installed AFTER sentry's LoggingIntegration so Sentry's capture doesn't interfere with JSON output.
- **Sentry Transport injection via subclass (not instance).** sentry-sdk 2.x API shift — `transport=` must be a `Transport` subclass that the client instantiates with options. Test uses a module-level list as buffer.
- **dsn=None is the safe default.** Unset SENTRY_DSN means sentry-sdk init is a no-op; prevents accidental leakage in local/CI.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] stdlib `extra={}` keys weren't flowing into JSON event dict**
- **Found during:** Task 2 — first pytest run of `test_output_is_json_parseable` failed with `KeyError: 'k'` on `parsed["k"]`
- **Issue:** The plan's action block specified the SHARED_PROCESSORS list without `structlog.stdlib.ExtraAdder`. Without it, when a stdlib caller does `logger.info("e", extra={"k": "v"})`, the `k` field lands on the `LogRecord` but never makes it into the structlog `event_dict`, so it's missing from the JSON output.
- **Fix:** Added `from structlog.stdlib import ExtraAdder` and inserted `ExtraAdder()` into `SHARED_PROCESSORS` immediately after `merge_contextvars` (so secrets passed via `extra={}` also flow through the downstream `_strip_secrets` redactor).
- **Files modified:** `arbiter/utils/logger.py`
- **Verification:** All 4 test_logger.py tests pass after the fix; output contains `k`, `POLY_PRIVATE_KEY` (redacted), `arb_id`/`canonical_id` (contextvars).
- **Committed in:** `76ebbfe` (Task 2 commit)

**2. [Rule 1 - Bug] sentry-sdk 2.x rejects Transport instance in `transport=` kwarg**
- **Found during:** Task 3 — first pytest run of `test_async_exception_captured` failed: `captured envelopes: 0`, urllib3 tried to resolve `example.invalid` (the SDK ignored the fake transport instance and fell back to HTTP).
- **Issue:** The plan's draft test passed `transport=transport` where `transport` was an instance of a plain class (not a subclass of `sentry_sdk.transport.Transport`). In sentry-sdk 2.x, `transport=` must be a **subclass** of `Transport` that the client instantiates itself with options. Passing an instance silently falls back to `HttpTransport`.
- **Fix:** Rewrote the fake transport as `class _FakeTransport(Transport)` and pass the class (not an instance) to `sentry_sdk.init(transport=_FakeTransport, ...)`. Use a module-level list `_CAPTURED_ENVELOPES` as the buffer (cleared at the start of each test) since the class is instantiated by the SDK.
- **Files modified:** `arbiter/test_sentry_integration.py`
- **Verification:** Both tests pass. `_CAPTURED_ENVELOPES` has 1 envelope containing `RuntimeError("boom")` after `asyncio.run(runner())` + `sentry_sdk.flush`.
- **Committed in:** `72b0f5d` (Task 3 commit)

---

**Total deviations:** 2 auto-fixed (2 Rule 1 bugs — both in test scaffolding referenced by the plan)
**Impact on plan:** Both fixes necessary for tests to pass. No scope creep. ExtraAdder is a direct correctness requirement (without it the logger drops caller data). The Transport subclass fix documents an SDK ABI shift future TDD writers should know about.

## Issues Encountered

- `prepare_console_stream` was present in the plan's "keep verbatim" instructions but NOT present in the worktree's `arbiter/utils/logger.py` (the base commit `374cc96` predates a later edit that added it on `main`). I implemented `prepare_console_stream` per the plan's literal code block — net effect: the function exists in the rewritten logger.py regardless of what the base had.
- Initial Write calls to `requirements.txt` inadvertently hit the main repo at `C:/Users/sande/Documents/arbiter-dashboard/` (an "additional working directory") instead of the worktree path. Caught via `git status` showing no worktree changes and main repo showing modified files. Reverted the main repo writes (`git checkout --`) and re-wrote to the explicit worktree paths. No commits landed in the main repo.
- sentry-sdk 2.x transport API shift (Transport subclass, not instance) is documented in the new test file's docstring for future reference.

## User Setup Required

None — Sentry is opt-in via `SENTRY_DSN` env var. If the operator wants Sentry error capture, they set `SENTRY_DSN`, `ARBITER_ENV`, `ARBITER_RELEASE` in `.env`. Otherwise `sentry_sdk.init(dsn=None)` is a no-op.

## Operator Notes

- **Docker rebuild:** `cd arbiter && docker compose build arbiter` picks up the new deps via the existing `COPY requirements.txt . && RUN pip install --no-cache-dir -r requirements.txt` step. No Dockerfile changes were made in this plan.
- **Sentry setup:** Optional. If desired, create a Sentry project, copy the DSN, and set `SENTRY_DSN=<dsn>` in `.env`. Optionally set `ARBITER_ENV=production` and `ARBITER_RELEASE=<git-sha>` before launch to tag events properly.
- **cryptography upgrade verification:** `arbiter/collectors/test_kalshi_collector.py` (2 tests) still passes post-upgrade — Kalshi RSA-PSS signing path is intact. No rollback needed for the `cryptography>=46.0.0` pin.
- **Live log format:** Log lines now look like `{"event": "order.submitted", "level": "info", "logger": "arbiter.execution", "timestamp": "2026-04-16T20:45:35.123456Z", "arb_id": "ARB-000123", ...}`. Any downstream log shipper (Datadog/CloudWatch/Loki) should treat `event` as the primary message key.

## Next Phase Readiness

- Plan 02-02 (execution store) can now import `structlog` and bind contextvars for `arb_id`.
- Plan 02-03 (pre-trade depth) can now use `tenacity` retry decorators on adapter HTTP calls.
- Plan 02-06 (engine integration) will inherit `_init_sentry()` and `setup_logging()` side effects automatically — no additional wiring needed.

## Self-Check: PASSED

- [x] `arbiter/utils/logger.py` exists (modified; contains JSONRenderer, merge_contextvars, prepare_console_stream, class TradeLogger, _strip_secrets)
- [x] `arbiter/utils/test_logger.py` exists (4 tests all passing)
- [x] `arbiter/main.py` contains `sentry_sdk.init`, `AsyncioIntegration`, `AioHttpIntegration`, `LoggingIntegration`, `send_default_pii=False`
- [x] `arbiter/test_sentry_integration.py` exists (2 tests all passing)
- [x] `.env.template` documents `SENTRY_DSN`, `ARBITER_ENV`, `ARBITER_RELEASE`
- [x] `requirements.txt` contains `structlog>=25.5.0`, `tenacity>=9.1.4`, `sentry-sdk>=2.58.0`, `cryptography>=46.0.0`; no remaining `cryptography>=41`
- [x] `arbiter/requirements.txt` identical to root `requirements.txt` (diff returns empty)
- [x] Commits found in `git log --oneline`: `2e2a873` (chore), `76ebbfe` (feat), `72b0f5d` (feat)
- [x] `pytest arbiter/utils/test_logger.py arbiter/test_sentry_integration.py -x -v` → 6 passed
- [x] `pytest arbiter/utils/test_retry.py arbiter/collectors/test_kalshi_collector.py -x` → 5 passed (cryptography 46.x non-regression)
- [x] `python -c "import arbiter.main; import arbiter.utils.logger"` exits 0

---
*Phase: 02-execution-operational-hardening*
*Completed: 2026-04-16*
