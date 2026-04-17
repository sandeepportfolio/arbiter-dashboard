---
phase: 03-safety-layer
reviewed: 2026-04-16T22:30:00Z
depth: standard
files_reviewed: 20
files_reviewed_list:
  - arbiter/api.py
  - arbiter/execution/adapters/kalshi.py
  - arbiter/execution/adapters/polymarket.py
  - arbiter/execution/adapters/test_kalshi_adapter.py
  - arbiter/execution/adapters/test_polymarket_adapter.py
  - arbiter/execution/engine.py
  - arbiter/execution/test_engine.py
  - arbiter/safety/supervisor.py
  - arbiter/safety/test_alerts.py
  - arbiter/safety/test_supervisor.py
  - arbiter/test_api_integration.py
  - arbiter/utils/retry.py
  - arbiter/web/dashboard-view-model.js
  - arbiter/web/dashboard-view-model.test.js
  - arbiter/web/dashboard.html
  - arbiter/web/dashboard.js
  - arbiter/web/styles.css
  - index.html
  - output/verify_safety_ui.mjs
findings:
  critical: 2
  warning: 9
  info: 8
  total: 19
status: issues_found
---

# Phase 3: Code Review Report

**Reviewed:** 2026-04-16T22:30:00Z
**Depth:** standard
**Files Reviewed:** 20
**Status:** issues_found

## Summary

The Phase 3 safety layer is functionally comprehensive: SafetySupervisor correctly
serializes kill-switch state through `asyncio.Lock`, adapter 429 handling caps
Retry-After delays at 60s, per-platform exposure accounting follows the
Option A pattern, and one-leg exposure surfacing is wired through three
independent channels (incident queue, Telegram, dedicated WS event).

However, there are **two critical security findings** that must be addressed
before live trading:

1. A plaintext default password (`"saibaba"`) ships hardcoded in `arbiter/api.py`
   as a fallback for `UI_USER_PASSWORD`, and the user's personal email is
   hardcoded as the default operator.
2. The session secret falls back to a publicly visible constant
   (`"INSECURE_DEFAULT_CHANGE_ME"`) when `UI_SESSION_SECRET` is unset — anyone
   with source access can forge operator session tokens.

Nine warnings concern logic/consistency issues: a broken `except (CancelledError,
BaseException)` pattern that swallows `KeyboardInterrupt`/`SystemExit`, tests
asserting metric-card labels that no longer match the implementation,
`renderRateLimitBadges` writing to a DOM element ID (`#rateLimitIndicators`)
that does not exist in either HTML file, `verify_safety_ui.mjs` asserting
selectors (`#safetySection`, `#rateLimitIndicators`) that are never rendered,
deprecated `asyncio.get_event_loop()` usage in the Polymarket adapter, and
several parse/type-conversion fragilities.

---

## Critical Issues

### CR-01: Hardcoded default operator password and email in `arbiter/api.py`

**File:** `arbiter/api.py:40-44`
**Issue:** The `UI_ALLOWED_USERS` dict is built at module-import time using
`os.getenv(..., DEFAULT)` with a real-looking password (`"saibaba"`) and the
repository owner's personal email (`"sparx.sandeep@gmail.com"`) as defaults.
If `UI_USER_EMAIL` or `UI_USER_PASSWORD` is unset in production, the server
silently accepts the hardcoded credentials — no warning, no startup abort.
This is a credential-leak risk (the value is visible in the public repo) and
an auth-bypass risk (a misconfigured deploy inherits these credentials). The
integration test at `arbiter/test_api_integration.py:133-134` confirms the
defaults work out-of-the-box.

**Fix:**
```python
UI_USER_EMAIL = os.getenv("UI_USER_EMAIL")
UI_USER_PASSWORD = os.getenv("UI_USER_PASSWORD")
if not UI_USER_EMAIL or not UI_USER_PASSWORD:
    # Fail closed: in production, refuse to start without operator credentials.
    # Tests can monkeypatch UI_ALLOWED_USERS (as test_api_integration.py already does).
    UI_ALLOWED_USERS: Dict[str, str] = {}
    logger.error(
        "UI_USER_EMAIL / UI_USER_PASSWORD not set. Operator login is disabled. "
        "Set both env vars before running in production."
    )
else:
    UI_ALLOWED_USERS = {UI_USER_EMAIL: _hash_password(UI_USER_PASSWORD)}
```
Also purge the committed defaults from git history once the fix lands.

### CR-02: Session secret falls back to a publicly known constant

**File:** `arbiter/api.py:47-54`
**Issue:** `_get_secret()` returns the literal string `"INSECURE_DEFAULT_CHANGE_ME"`
when `UI_SESSION_SECRET` is not set, logging only a warning. Every session
token signed with this secret is trivially forgeable by any attacker with
access to the source code — they can mint a valid `_generate_token(any_email)`
and bypass authentication on every auth-gated endpoint (`/api/kill-switch`,
`/api/market-mappings/...`, `/api/errors/...`, `/api/manual-positions/...`,
`/api/portfolio/unwind/...`). Combined with CR-01 this gives full operator
control of the system.

**Fix:**
```python
def _get_secret() -> str:
    secret = os.getenv("UI_SESSION_SECRET", "")
    if not secret:
        raise RuntimeError(
            "UI_SESSION_SECRET is not set. Refusing to sign session tokens "
            "with a predictable fallback. Generate with `openssl rand -hex 32` "
            "and export UI_SESSION_SECRET before starting the server."
        )
    return secret
```
Call this once at startup (not per-request) so the failure surfaces before
`site.start()` rather than on the first login. Tests should set the env var
via fixture/monkeypatch.

---

## Warnings

### WR-01: `except (CancelledError, BaseException)` defeats `KeyboardInterrupt`/`SystemExit` propagation

**File:** `arbiter/api.py:242` and `arbiter/test_api_integration.py:337`
**Issue:** `except (asyncio.CancelledError, BaseException)` is redundant and
dangerous: `BaseException` already subsumes `CancelledError`, so the tuple is
equivalent to `except BaseException`. This swallows `KeyboardInterrupt`,
`SystemExit`, and `GeneratorExit` — the three signals that should normally
propagate out of cleanup handlers. In the `serve()` finally block this means
pressing Ctrl-C during shutdown silently eats the interrupt, and in the test
it can mask legitimate test failures.

**Fix:**
```python
try:
    await self._rate_limit_task
except asyncio.CancelledError:
    pass
```
Apply identical fix at `test_api_integration.py:337`.

### WR-02: `renderRateLimitBadges` targets a DOM node that doesn't exist

**File:** `arbiter/web/dashboard.js:1388-1400`
**Issue:** The function looks up `document.getElementById("rateLimitIndicators")`,
but grepping both `arbiter/web/dashboard.html` and `index.html` shows no
element with that ID. The function early-returns silently when `host` is null,
so the rate-limit pill UI (one of the Phase 3 dashboard surfaces) is never
rendered despite the backend broadcasting `rate_limit_state` events every 2s
and the view-model building the pill data correctly.

**Fix:** Add the mount point to `dashboard.html` near the kill-switch toolbar:
```html
<div id="rateLimitIndicators" class="rate-limit-indicators" data-ops-only="true"></div>
```
and confirm it's present in both HTML variants. Alternatively, relocate the
render target to an existing element and update the verify script.

### WR-03: `verify_safety_ui.mjs` expects selectors that never render

**File:** `output/verify_safety_ui.mjs:45-56`
**Issue:** The `SAFETY_SELECTORS` list asserts presence of `#safetySection`
and `#rateLimitIndicators`. Neither selector exists in `arbiter/web/dashboard.html`
or `index.html`. The verification script will therefore always fail on a
real browser run — the passing path depends on `reachable: false` short-
circuiting the aggregation. This makes the smoke acceptance criterion
misleading: operators who run the script on a live dashboard will see failure
even though the safety UI is otherwise functional.

**Fix:** Either add the two IDs to the HTML (see WR-02 for `#rateLimitIndicators`)
or remove them from `SAFETY_SELECTORS` and replace with IDs that actually
exist (`#killSwitchToolbar`, `#killSwitchBadge`, `#oneLegAlertPanel`,
`#shutdownBanner`, etc.). Prefer adding the IDs — the script's intent is
clearly to assert the safety-layer UI surface.

### WR-04: `dashboard-view-model.test.js` asserts metric-card labels that no longer match the implementation

**File:** `arbiter/web/dashboard-view-model.test.js:123-124`
**Issue:** The test asserts `cards[2].label === "Validator progress"` and
`cards[3].label === "Trade throughput"`, but `buildMetricCards` in
`dashboard-view-model.js:191,199` actually returns `"Validator state"` and
`"Execution flow"`. These assertions will fail on every test run.

**Fix:** Either update the test to match the implementation:
```javascript
expect(cards[2].label).toBe("Validator state");
expect(cards[3].label).toBe("Execution flow");
```
or update the implementation to match the intended labels. Pick whichever
matches the design intent — the test and code must agree.

### WR-05: `_FOK_STATUS_MAP["resting"] = OrderStatus.SUBMITTED` on a FOK order can mis-classify fills

**File:** `arbiter/execution/adapters/kalshi.py:27-33, 170-177`
**Issue:** A FOK (fill-or-kill) order by definition does not rest — it either
fills immediately or is cancelled. The adapter maps the `"resting"` string to
`OrderStatus.SUBMITTED` and logs a warning. Downstream, the engine's
`_live_execution` status classifier treats `SUBMITTED` as a "surviving status"
that triggers per-platform exposure recording and the one-leg recovery path
(`engine.py:789-798`). If Kalshi ever returns `"resting"` for a FOK (API bug
or edge case), the engine will book real exposure for an order that was
never actually placed on the book. Since FOK semantics mean the order
already either filled or was killed, this is a defensive-programming issue.

**Fix:** Treat `"resting"` as anomalous and return `FAILED` so the engine's
one-leg recovery pipeline triggers explicit reconciliation instead of booking
phantom exposure:
```python
if api_status == "resting":
    log.error(
        "kalshi.fok.resting_impossible",
        client_order_id=client_order_id,
        note="Kalshi returned 'resting' for a FOK; treating as reconcile-required",
    )
    return self._failed_order(
        arb_id, market_id, canonical_id, side, price, qty, now,
        "Kalshi FOK returned resting — reconcile required",
    )
```

### WR-06: Polymarket adapter uses deprecated `asyncio.get_event_loop()` repeatedly

**File:** `arbiter/execution/adapters/polymarket.py:107, 371, 420, 473, 557`
**Issue:** `asyncio.get_event_loop()` is deprecated in Python 3.10+ and
emits a `DeprecationWarning` when called outside a running loop; in 3.12
(the project runtime per CLAUDE.md) it will start raising `DeprecationWarning`
more aggressively and is slated for removal. Use `asyncio.get_running_loop()`
inside coroutines — these methods are always called from `async def` so a
running loop exists.

**Fix:** Replace every occurrence:
```python
loop = asyncio.get_running_loop()
```

### WR-07: `_match_existing` uses exact float equality on `size`

**File:** `arbiter/execution/adapters/polymarket.py:255-260`
**Issue:** The reconcile match logic uses `o_size == float(qty)` — exact
float equality. If the SDK ever returns size as `10.00000001` or any non-
exact float representation, a legitimate reconcile match will be missed and
the retry path will submit a duplicate order (the exact Pitfall 2 scenario
the reconcile loop exists to prevent).

**Fix:**
```python
if (
    abs(o_price - price) < 0.01
    and abs(o_size - float(qty)) < 0.5  # whole-contract tolerance
    and o_side == side_normalized
):
    return o_dict
```
A 0.5-contract tolerance is safe because Polymarket sizes are whole
integers; the tolerance just absorbs floating-point round-trip error.

### WR-08: Kalshi adapter parses orderbook level `lvl[0]` (price_cents) without bounds check

**File:** `arbiter/execution/adapters/kalshi.py:494-507`
**Issue:** `sorted_levels = sorted(levels, key=lambda lvl: lvl[0])` followed
by `best_price_cents = float(sorted_levels[0][0])` will raise `IndexError` on
any level that is an empty list `[]` or a single-element list. The surrounding
try/except catches `IndexError, TypeError, KeyError`, but the error path
returns `(False, 0.0)` which silently fails the depth check with no warning
visible to operators. A malformed orderbook would look indistinguishable
from an empty book.

**Fix:** Validate level shape first:
```python
valid_levels = [lvl for lvl in levels if isinstance(lvl, (list, tuple)) and len(lvl) >= 2]
if not valid_levels:
    log.warning("kalshi.depth.malformed_levels", market_id=market_id)
    return (False, 0.0)
sorted_levels = sorted(valid_levels, key=lambda lvl: lvl[0])
```

### WR-09: `Order.fill_qty` typed as `int` but stores floats in Kalshi path

**File:** `arbiter/execution/engine.py:46-56, 179-181` and
`arbiter/execution/adapters/kalshi.py:179-181`
**Issue:** `Order.fill_qty` is declared `int` in the dataclass (`engine.py:56`),
but `KalshiAdapter.place_fok` assigns `float(order_data.get("fill_count_fp", ...))`
(adapter line 179), producing a float. Downstream, `_resolved_fill_qty`
(`api.py:1025`) coerces with `int(order.fill_qty)` which silently truncates
any fractional fills. The engine's exposure math (`engine.py:1059`:
`float(filled_leg.fill_qty) * float(filled_leg.fill_price)`) uses the float
value directly, so the UI and the engine can disagree by up to one contract
on partial-fill edge cases.

**Fix:** Decide on a single numeric type. Since Kalshi supports fractional
contracts (FP means "floating point"), update the dataclass:
```python
fill_qty: float = 0.0  # Kalshi supports fractional fills via count_fp
```
Then audit `_resolved_fill_qty` and any other `int(order.fill_qty)` sites to
drop the cast where fractional precision matters.

---

## Info

### IN-01: `except Exception` in `supervisor.py` `handle_one_leg_exposure` hides `to_dict` bugs

**File:** `arbiter/safety/supervisor.py:376-389`
**Issue:** `incident.to_dict()` is wrapped in a bare `except Exception` that
silently falls back to a minimal dict. If a refactor breaks `to_dict` (e.g.,
adds a required parameter, changes return type), the supervisor will quietly
emit degraded payloads to subscribers instead of failing loudly.

**Fix:** Log the exception at `warning` level so operators see the fallback:
```python
try:
    payload = incident.to_dict()
except Exception as exc:
    logger.warning("one_leg_exposure: incident.to_dict failed: %s", exc)
    payload = {...}
```

### IN-02: `ManualPosition.quantity` comparison uses falsy-zero pattern

**File:** `arbiter/web/dashboard-view-model.js:60, 126`
**Issue:** `Number(collector?.rate_limiter?.remaining_penalty_seconds || 0) > 0`
uses `|| 0` which coerces `false`, `""`, `null`, `undefined`, AND `0` to
fallback. Harmless here because all paths then compare `> 0`, but this
pattern hides intent. Prefer `?? 0` for consistency with the safety view
(`buildSafetyView` at line 215 already uses `??`).

**Fix:** Use `??` throughout for numeric coercion; reserve `||` for string
fallbacks.

### IN-03: `login_user` returns `None` on failure; callers should avoid logging password

**File:** `arbiter/api.py:117-126`
**Issue:** The failed-login log line `logger.warning(f"Failed login attempt: {email}")`
includes the email but not the password (good). Consider adding a rate-limit
counter per IP / email to mitigate credential stuffing — currently an
attacker can hammer `/api/auth/login` with unlimited attempts. Not strictly
a bug, but worth a TODO.

**Fix:** Track per-source login attempt counts in an in-memory dict with
time-windowed keys, or delegate to a middleware. Out of scope for v1 but
valuable before live trading.

### IN-04: `response.set_cookie(..., secure=_request_is_secure(request), ...)` missing `domain`/`path`

**File:** `arbiter/api.py:553-562`
**Issue:** The session cookie lacks explicit `path` and `domain` attributes.
Browsers default to path of the request and the exact host, which is fine
for single-origin deploys. When the dashboard is served behind a reverse
proxy with path prefixing (e.g., `/arbiter/api/...`), the cookie may
accidentally leak to sibling apps. Low risk for the current deploy shape
but worth a note.

**Fix:** Add `path="/"` explicitly:
```python
response.set_cookie(
    "arbiter_session", token,
    httponly=True,
    secure=_request_is_secure(request),
    max_age=7 * 86400,
    samesite="lax",
    path="/",
)
```

### IN-05: `_ACTIVE_SESSIONS` is an unbounded in-memory dict

**File:** `arbiter/api.py:101`
**Issue:** `_ACTIVE_SESSIONS: Dict[str, str] = {}` grows for every login and
only shrinks on explicit `/api/auth/logout`. A long-running server with
session rotation (7-day tokens) accumulates entries indefinitely; expired
tokens are never evicted from this dict, only from the token verification
(via timestamp). Low risk given the v1 operator count (1-2 people), but
worth wiring a periodic eviction task or delegating to Redis when it's
available.

**Fix:** Add a periodic eviction sweep — iterate the dict every hour and
remove entries whose tokens fail `_verify_token`.

### IN-06: `escapeHtml` is defined twice in the dashboard JS bundle

**File:** `arbiter/web/dashboard.js:417-424` and (likely) inherited helpers
**Issue:** The dashboard ships both a local `escapeHtml` in `dashboard.js`
and plausibly inherited helpers. Not a bug, but leaves room for drift if the
future refactor touches only one. Consolidate into a shared helper module
imported from `dashboard-view-model.js` or a new `dashboard-utils.js`.

### IN-07: `OrderStatus.SIMULATED` is a status but not in the `_FOK_STATUS_MAP` / `status_map` dicts

**File:** `arbiter/execution/engine.py:42` and
`arbiter/execution/adapters/polymarket.py:290-297`
**Issue:** The `SIMULATED` enum value is used in `_simulate_execution` but
never appears in any API→OrderStatus map. This is correct (simulated orders
don't come from the platform), but the dead-path symmetry can confuse future
readers. Add a short comment on the enum noting which statuses are reachable
from the adapter path vs. engine-only.

### IN-08: Duplicate `asyncio.run(runner())` call in `test_rejected_order_incident_per_platform`

**File:** `arbiter/execution/test_engine.py:1118-1120`
**Issue:** The test body runs `asyncio.run(runner())` twice — line 1118 and
line 1120. The second call re-runs the test assertions but is wasted work
(~2x the runtime for this test). Not a correctness bug because each call
creates a fresh event loop and `runner()` is idempotent, but it doubles
test latency unnecessarily.

**Fix:** Delete the duplicate `asyncio.run(runner())` on line 1120.

---

_Reviewed: 2026-04-16T22:30:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
