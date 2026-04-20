---
phase: 04-09-gap-closure
reviewed: 2026-04-20T00:00:00Z
depth: standard
files_reviewed: 9
files_reviewed_list:
  - arbiter/execution/adapters/kalshi.py
  - arbiter/execution/adapters/test_kalshi_list_open_orders_signing.py
  - arbiter/sandbox/test_kalshi_timeout_cancel.py
  - arbiter/sandbox/test_graceful_shutdown.py
  - arbiter/sandbox/test_kalshi_fok_rejection.py
  - conftest.py
  - index.html
  - arbiter/web/dashboard-view-model.js
  - arbiter/web/dashboard-view-model.test.js
findings:
  critical: 0
  warning: 1
  info: 5
  total: 6
status: issues_found
---

# Phase 04-09: Code Review Report

**Reviewed:** 2026-04-20
**Depth:** standard
**Files Reviewed:** 9
**Status:** issues_found
**Scope:** Gap-closure commits 44cd93a..f84ad81 (G-1 through G-5) only. Pre-existing
Phase 4 findings already covered in `04-REVIEW.md` (2026-04-17) are out of scope.

## Summary

The 04-09 gap-closure set is tightly scoped, well-commented, and the primary
production fix (G-1: Kalshi PSS querystring-free signing path) is correctly
applied and has solid regression coverage in the new
`test_kalshi_list_open_orders_signing.py`. Cross-referencing every other
`auth.get_headers(...)` call site in `arbiter/execution/adapters/kalshi.py`
(`_post_order`, `_delete_order`, batched `cancel_all`, `_fetch_order`) confirms
none still sign a querystring-bearing path — G-1 is complete.

G-2 (SimpleNamespace wrapping for `place_resting_limit` return shape), G-3
(async-generator fixture resolution in root `conftest.py`), G-4 (widened
thin-market FOK rejection assertion), and G-5 (circuit-open `crit` tone in
`buildRateLimitView`) are all behaviorally correct and well-tested.

One **warning**-level item: the teardown-exception swallow in the new
`conftest.py` async-generator path masks fixture teardown failures silently,
which can hide real resource-leak bugs in sandbox fixtures (asyncpg pools,
aiohttp sessions, BalanceMonitor). Five **info** items cover minor fragility
and coverage gaps that are acceptable to defer.

No critical security or correctness issues were found in the diff under review.

## Warnings

### WR-01: Async-generator fixture teardown exceptions are silently swallowed

**File:** `conftest.py:48-56`
**Issue:** In the G-3 fix, teardown calls `await gen.__anext__()` inside a
bare `except Exception: pass` block (lines 52-56). The comment justifies this
as "pytest's built-in async-fixture runner behaves the same way", but this is
not strictly true — pytest-asyncio's `wrap_in_sync` re-raises teardown
exceptions by default (it only suppresses them if the test body already
raised). Swallowing all teardown exceptions unconditionally will hide:

- `asyncpg.create_pool(...).close()` failures in `sandbox_db_pool` (e.g.,
  connection-leak asserts from asyncpg) — masks test DB state bugs.
- `aiohttp.ClientSession.close()` failures / unclosed-session warnings from
  `balance_snapshot` → `BalanceMonitor.stop()` → TelegramNotifier session
  close — masks the exact class of resource-leak regression that Phase 3
  already debugged.
- `evidence_dir`'s `FileHandler.close()` edge-cases (Windows file-lock during
  structlog teardown).

For a repo whose `CLAUDE.md` states "Risk tolerance: Low — cannot afford to
lose capital to bugs. Safety > speed", silently swallowing teardown failures
in the root async dispatcher is a footgun. The real pytest fixture runner
distinguishes "test already failed" from "teardown failed alone"; this hook
treats both identically.

**Fix:** At minimum, log teardown exceptions so they surface in CI output
instead of disappearing. Preferably, preserve the primary test outcome but
re-raise the first teardown exception when the test body succeeded:

```python
finally:
    teardown_exc = None
    for name, gen in reversed(active_generators):
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass
        except Exception as exc:
            # Log to stderr so teardown failures surface in CI.
            import sys
            print(
                f"[conftest] fixture {name!r} teardown raised: {exc!r}",
                file=sys.stderr,
            )
            if teardown_exc is None:
                teardown_exc = exc
    # Re-raise teardown exception only if the test body succeeded;
    # otherwise the test's own exception (already propagating) wins.
    if teardown_exc is not None:
        raise teardown_exc
```

Note: the `raise teardown_exc` at the tail runs inside the `finally` block,
so it only surfaces when the `try` block completed normally — matching
pytest's own "don't mask a test failure with a teardown error" semantics.

## Info

### IN-01: G-3 comment references "pytest-asyncio STRICT mode" but the project does not depend on pytest-asyncio

**File:** `conftest.py:13-18`
**Issue:** The docstring says "pytest-asyncio STRICT mode does not unwrap
`async def` + `yield` fixtures for us when this custom hook is active". I
checked `requirements.txt` and no pytest.ini/pyproject.toml — `pytest-asyncio`
is not a direct dependency and the custom `pytest_pyfunc_call` is the ONLY
async dispatcher. The "STRICT mode" reference is misleading: the real reason
async-generator fixtures aren't unwrapped is that there's no async fixture
resolver at all, not a mode-configuration issue.
**Fix:** Reword the docstring to "Because this repo has no pytest-asyncio
plugin, `async def` + `yield` fixtures are not resolved by any upstream
machinery — we must drive them through `__anext__` here." This keeps future
maintainers from hunting for a `asyncio_mode` config that doesn't exist.

### IN-02: `dir()` / `locals().get(...)` late-binding pattern is fragile

**File:** `arbiter/sandbox/test_graceful_shutdown.py:585, 591`
**Issue:** Manifest write uses
`locals().get("effective_client_order_id") or client_order_id` and
`sorted(phases_seen) if "phases_seen" in dir() else []`. This is a pre-
existing pattern but G-2 touched it (added `effective_client_order_id`
lookup). If a future refactor moves either variable further inside the
`try:` block, the `locals()/dir()` lookup quietly returns fallback values
without any static analysis warning. Also: this write is OUTSIDE the
`finally:` block, so if any assertion raises, the manifest is never written
at all — G-2 did not fix that gap either.
**Fix:** Initialize both sentinel variables to `None` / `set()` at the top
of the test function, then the bodies just reassign them. Move the manifest
write INSIDE the `finally:` so evidence is produced even on assertion
failure (Plan 04-08 aggregator is likely more useful with manifests from
failed runs too).

### IN-03: `test_post_order_still_signs_bare_orders_path` is sensitive to ambient `PHASE4_MAX_ORDER_USD` / `PHASE5_MAX_ORDER_USD` env vars

**File:** `arbiter/execution/adapters/test_kalshi_list_open_orders_signing.py:170-207`
**Issue:** The test calls `place_fok(..., price=0.55, qty=3)` → notional
$1.65. `KalshiAdapter.place_fok` now reads `PHASE4_MAX_ORDER_USD` and
`PHASE5_MAX_ORDER_USD` (kalshi.py:110, 135) and returns `_failed_order(...)`
without calling `auth.get_headers` if the cap is exceeded. If a contributor
runs `pytest` in a shell where either env var is set to a value < $1.65
(e.g., a Phase 4 sandbox session), this test fails via its
`adapter.auth.get_headers.called` assertion with a misleading error that
has nothing to do with G-1 signature regression.
**Fix:** Use `monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)`
and the same for `PHASE5_MAX_ORDER_USD` at the top of the test (and in the
other two tests in the file for consistency; they don't call `place_fok`
but the defensive pattern is cheap).

### IN-04: G-5 `buildRateLimitView` tone precedence is not covered when cooldown AND circuit-open co-occur

**File:** `arbiter/web/dashboard-view-model.js:244-250` + tests
**Issue:** The implementation correctly promotes `crit` over `warn` when
both `remainingPenalty > 0` AND `circuitState === "open"` (because the
circuit-open check is the LAST write to `tone`). But none of the three new
vitest cases exercise that combination, so a future refactor that flips the
order of the `if` blocks would pass the existing tests while silently
demoting circuit-open to `warn`.
**Fix:** Add one more test case: `{rateLimits: {kalshi: {remaining_penalty_seconds: 3.2, ...}}, collectors: {kalshi: {circuit: {state: "open"}}}}` and assert
`tone === "crit"`. Covers the composition rule explicitly.

### IN-05: `_FOK_STATUS_MAP` does not include a FAILED mapping, so G-4's widened assertion leans on `_failed_order(...)` for the FAILED case

**File:** `arbiter/execution/adapters/kalshi.py:28-34` (context) and
`arbiter/sandbox/test_kalshi_fok_rejection.py:83-89`
**Issue:** G-4 accepts either `OrderStatus.CANCELLED` (HTTP 201 +
body.status=canceled path, handled by `_FOK_STATUS_MAP`) or
`OrderStatus.FAILED` (HTTP 409 path, handled by `_failed_order(...)` at
kalshi.py:207-218). The second path is ONLY reached when the API returns a
non-2xx status. If Kalshi demo ever ships a third response shape — e.g.,
HTTP 200 + body.status="failed" or HTTP 200 + body.error={code:...} — the
adapter's `_FOK_STATUS_MAP.get(api_status, OrderStatus.SUBMITTED)` fallback
would map to SUBMITTED (not CANCELLED/FAILED), and G-4's assertion would
fail even though EXEC-01 semantically holds. Not a regression G-4
introduced, but G-4's broader acceptance widens the invariant surface.
**Fix:** Either (a) add `"failed": OrderStatus.FAILED` and
`"rejected": OrderStatus.FAILED` to `_FOK_STATUS_MAP` so the body-based
failure cases collapse to FAILED, or (b) in the test, assert
`order.fill_qty == 0` as the PRIMARY invariant (already done at line 90)
and demote the status assertion to informational (log, don't assert) to
make the test resilient to platform response-shape drift. The `fill_qty`
check is the real EXEC-01 guarantee.

---

_Reviewed: 2026-04-20_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
