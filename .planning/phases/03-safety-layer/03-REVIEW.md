---
phase: 03-safety-layer
reviewed: 2026-04-17T02:10:16Z
depth: standard
files_reviewed: 35
files_reviewed_list:
  - arbiter/api.py
  - arbiter/config/settings.py
  - arbiter/execution/adapters/base.py
  - arbiter/execution/adapters/kalshi.py
  - arbiter/execution/adapters/polymarket.py
  - arbiter/execution/adapters/test_kalshi_adapter.py
  - arbiter/execution/adapters/test_polymarket_adapter.py
  - arbiter/execution/adapters/test_protocol_conformance.py
  - arbiter/execution/engine.py
  - arbiter/execution/test_engine.py
  - arbiter/main.py
  - arbiter/mapping/market_map.py
  - arbiter/safety/__init__.py
  - arbiter/safety/alerts.py
  - arbiter/safety/conftest.py
  - arbiter/safety/persistence.py
  - arbiter/safety/supervisor.py
  - arbiter/safety/test_alerts.py
  - arbiter/safety/test_persistence.py
  - arbiter/safety/test_supervisor.py
  - arbiter/sql/init.sql
  - arbiter/sql/safety_events.sql
  - arbiter/test_api_integration.py
  - arbiter/test_api_safety.py
  - arbiter/test_config_loading.py
  - arbiter/test_main_shutdown.py
  - arbiter/utils/retry.py
  - arbiter/web/dashboard-view-model.js
  - arbiter/web/dashboard-view-model.test.js
  - arbiter/web/dashboard.html
  - arbiter/web/dashboard.js
  - arbiter/web/styles.css
  - index.html
  - output/verify_dashboard_polish.mjs
  - output/verify_safety_ui.mjs
findings:
  critical: 3
  warning: 6
  info: 5
  total: 14
status: issues_found
---

# Phase 3: Code Review Report

**Reviewed:** 2026-04-17T02:10:16Z
**Depth:** standard
**Files Reviewed:** 35
**Status:** issues_found

## Summary

Phase 3 (Safety Layer) implements the kill-switch state machine, per-platform
exposure ceiling, one-leg exposure detection, rate-limit supervision, graceful
shutdown, market-mapping resolution criteria, and the operator-facing safety UI.

The kill-switch core (`SafetySupervisor`) is well-designed: arm/reset are
serialized through `_state_lock`, Telegram + Postgres failures are swallowed
so they cannot abort a trip, `cancel_all` is fanned out through
`asyncio.gather` with a per-adapter 5s timeout, and graceful shutdown
correctly broadcasts `shutdown_state` BEFORE trip_kill. Dashboard rendering
correctly uses `textContent` for operator-entered resolution-criteria text
(T-3-06-C mitigated), and all JSONB writes are parameterized via `$N::jsonb`
casts so SQL injection is not present. Thirty-five files reviewed at standard
depth turned up three Critical findings, six Warnings, and five Info items.

The Critical findings fall into three categories:
1. **Hardcoded default operator credentials and HMAC secret** in
   `arbiter/api.py` (the sole guard on `POST /api/kill-switch`).
2. **Per-platform exposure accounting is silently skipped when a live
   arb reaches `submitted` but not `filled`** — orders placed on the
   platform never count against `max_platform_exposure_usd`, defeating
   SAFE-02 in the common slow-fill path.
3. **Adapter 60s cap on forged `Retry-After` headers is applied only in
   the returned value, not in the rate limiter's `_penalty_until`** — a
   hostile or buggy server's `Retry-After: 999999` freezes the adapter's
   token bucket for 11+ days.

The Warnings cover a CORS wildcard on a cookie/token-auth API, the
`cancel_all` response-shape fallback that treats an unparseable body as
"all cancelled," the `Forwarded` header parser that only handles the first
segment, empty-block catch-`BaseException` cleanup, rejection-reason string
parsing for per-platform platform extraction, and the absence of startup
failure when `UI_SESSION_SECRET` is unset.

## Critical Issues

### CR-01: Hardcoded default operator credentials and HMAC session secret

**File:** `arbiter/api.py:34-44, 47-54`
**Issue:**
Three security-critical defaults live in module-level code:

1. `UI_USER_EMAIL` defaults to `"sparx.sandeep@gmail.com"`.
2. `UI_USER_PASSWORD` defaults to `"saibaba"`.
3. `UI_SESSION_SECRET` unset → `_get_secret()` returns the literal string
   `"INSECURE_DEFAULT_CHANGE_ME"` with only a `logger.warning`.

These defaults are the SOLE authentication gate on `POST /api/kill-switch`
(arm + reset), `POST /api/market-mappings/{id}` (resolution-criteria updates),
`POST /api/errors/{incident_id}` (incident resolution), and every
`/api/manual-positions/{position_id}` action. For a system whose stated
constraint is "cannot afford to lose capital to bugs," shipping a binary
that is operable with publicly-known creds is unacceptable. The HMAC secret
default is worse — an attacker who knows it can forge valid 7-day session
tokens for any email.

A secondary sub-issue: passwords are stored as unsalted single-round SHA-256
(`_hash_password`). For a v1 trading system with a single operator this is
a lower-severity concern than the hardcoded default, but should be migrated
to `bcrypt` / `argon2id` when the auth layer is revisited.

**Fix:**
Fail closed at startup. Refuse to serve traffic — especially the `/api/kill-
switch` and `/api/market-mappings/*` handlers — if either `UI_USER_PASSWORD`
or `UI_SESSION_SECRET` is unset OR equal to the documented default strings.

```python
# arbiter/api.py — module-level, replacing current UI_ALLOWED_USERS and
# _get_secret() defaults:
def _load_ui_auth_or_exit() -> tuple[Dict[str, str], str]:
    email = os.getenv("UI_USER_EMAIL", "").strip().lower()
    password = os.getenv("UI_USER_PASSWORD", "")
    secret = os.getenv("UI_SESSION_SECRET", "")
    problems: list[str] = []
    if not email:
        problems.append("UI_USER_EMAIL is not set")
    if not password or password == "saibaba":
        problems.append("UI_USER_PASSWORD is unset or still at the default")
    if not secret or secret == "INSECURE_DEFAULT_CHANGE_ME":
        problems.append("UI_SESSION_SECRET is unset or still at the default")
    if problems:
        # For api-only dev, this must still hard-fail when --live is in
        # effect; main.py should bail BEFORE ArbiterAPI is instantiated.
        raise RuntimeError(
            "Refusing to start with insecure operator auth: "
            + "; ".join(problems)
        )
    return {email: _hash_password(password)}, secret

UI_ALLOWED_USERS, UI_SESSION_SECRET = _load_ui_auth_or_exit()
```

Thread this into `arbiter/main.py::main()` so `--live` aborts via
`sys.exit(2)` when these env vars are missing, matching the existing
readiness startup-failure pattern.

---

### CR-02: SAFE-02 per-platform exposure not recorded on `submitted` status

**File:** `arbiter/execution/engine.py:814-824`
**Issue:**
After `_live_execution` determines the final execution status, per-platform
exposure is recorded only when `status == "filled"`:

```python
if status in {"submitted", "filled"}:
    if status == "filled":
        self.risk.record_trade(
            opp.canonical_id,
            opp.suggested_qty * (opp.yes_price + opp.no_price),
            execution.realized_pnl,
            yes_platform=opp.yes_platform,
            no_platform=opp.no_platform,
            yes_exposure=opp.suggested_qty * opp.yes_price,
            no_exposure=opp.suggested_qty * opp.no_price,
        )
```

The outer `if status in {"submitted", "filled"}:` is effectively dead (it
wraps only the inner filled branch), and the `submitted` case — legs that
reached the platform but haven't terminated — never calls `record_trade`.
That means `_platform_exposures` is stale for every open order. A second
arb dispatched against the same platform will pass `RiskManager.check_trade`
using the stale exposure and blow through `SafetyConfig.max_platform_expo\
sure_usd`. This defeats SAFE-02 in the exact scenario it was designed for
(slow-fill / live-order queueing).

**Fix:**
Record exposure on `submitted` OR `filled`, and release it in the recovery
path when a leg moves to CANCELLED/FAILED. Use the existing
`release_trade` helper so the accounting stays in sync.

```python
if status in {"submitted", "filled"}:
    self.risk.record_trade(
        opp.canonical_id,
        opp.suggested_qty * (opp.yes_price + opp.no_price),
        execution.realized_pnl if status == "filled" else 0.0,
        yes_platform=opp.yes_platform,
        no_platform=opp.no_platform,
        yes_exposure=opp.suggested_qty * opp.yes_price,
        no_exposure=opp.suggested_qty * opp.no_price,
    )
```

Then in `_recover_one_leg_risk` (engine.py:974), after a leg is cancelled,
call `self.risk.release_trade` with the matching per-platform split so the
ceiling recovers when an order is killed.

---

### CR-03: Forged `Retry-After` freezes adapter rate limiter (cap applied in wrong layer)

**File:** `arbiter/execution/adapters/kalshi.py:129-134, 255-263, 336-340, 430-434, 527-531, 590-594`; `arbiter/utils/retry.py:283-298`
**Issue:**
Every adapter 429 path does:

```python
retry_after = response_headers.get("Retry-After", "1")
delay = self.rate_limiter.apply_retry_after(
    retry_after, fallback_delay=2.0, reason="kalshi_429",
)
# T-3-04-E: cap forged Retry-After headers at 60 seconds.
delay = min(float(delay or 0.0), 60.0)
```

The 60s cap is applied AFTER `apply_retry_after` returns, and is used only
for the log message and the returned-`Order.error` string. Inside
`RateLimiter.apply_retry_after` → `penalize`, the un-capped value is
written to `self._penalty_until`:

```python
# arbiter/utils/retry.py:287-288
self._penalty_until = max(self._penalty_until, now + delay_seconds)
```

A server sending `Retry-After: 999999` — either hostile, or a
misconfigured CDN returning seconds-to-datetime in a malformed form, or
an upstream bug that the Kalshi/Polymarket team hasn't noticed — parks the
adapter's rate limiter for 11+ days. `RateLimiter.acquire()` then sleeps
for that full window on every acquire attempt, so any subsequent
`cancel_all`, `place_fok`, or `cancel_order` call hangs.

The supervisor's `asyncio.wait_for(adapter.cancel_all(), timeout=5.0)`
escapes the hang for kill-switch trips, but every other callsite
(`_live_execution`, `_recover_one_leg_risk._cancel_order`,
`_place_order_for_leg`) inherits the frozen limiter. For a low-risk-
tolerance trading system, this is a one-bad-header-away-from-DoS surface.

**Fix:**
Cap the delay inside `RateLimiter` where `_penalty_until` is actually
written — not in the adapter post-processing. Add a module-level or
per-limiter max_penalty and enforce it in `_parse_retry_after` or
`apply_retry_after`:

```python
# arbiter/utils/retry.py
@dataclass
class RateLimiter:
    name: str
    max_requests: int = 10
    window_seconds: float = 1.0
    max_penalty_seconds: float = 60.0   # NEW — T-3-04-E invariant
    # ... existing fields ...

    def apply_retry_after(
        self, retry_after: Any, fallback_delay: float,
        reason: str = "rate_limited",
    ) -> float:
        delay = self._parse_retry_after(retry_after)
        if delay is None:
            delay = max(float(fallback_delay or 0.0), 0.0)
        delay = min(delay, self.max_penalty_seconds)   # ← CAP HERE
        return self.penalize(delay, reason=reason)
```

After this the adapter-side `min(delay, 60.0)` post-process becomes
defense-in-depth but is no longer load-bearing. Add a unit test in
`arbiter/utils/test_retry.py` that feeds `Retry-After: 999999` and asserts
`remaining_penalty_seconds <= 60.0`.

## Warnings

### WR-01: `Access-Control-Allow-Origin: *` on API that serves cookie + Bearer auth

**File:** `arbiter/api.py:249, 260`
**Issue:**
The middleware unconditionally sets `Access-Control-Allow-Origin: *` on
every response including preflights. The login flow stores the session
token in a `SameSite=Lax` cookie (good) AND returns it in the JSON body
for Bearer use. A cross-origin page cannot read the Bearer token (CORS
read blocked) and cannot send the cookie (wildcard + no Allow-Credentials),
so classic CSRF is mitigated — but the wildcard is still overbroad for a
production trading dashboard. The `/api/safety/status`, `/api/safety/events`,
and `/api/market-mappings` responses are also readable by any origin, which
leaks operator audit trail and mapping metadata.

**Fix:**
Reflect a configured allowlist (`UI_ALLOWED_ORIGINS` env var, comma-
separated) instead of `*`. Fall back to blocking CORS entirely (no header)
in production; only reflect during local dev.

```python
_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("UI_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]

def _cors_origin(request: web.Request) -> str | None:
    origin = request.headers.get("Origin", "")
    if not origin:
        return None
    if origin in _ALLOWED_ORIGINS:
        return origin
    return None
```

Use it in the middleware instead of the static `"*"`.

---

### WR-02: `cancel_all` treats unparseable body as "entire chunk cancelled"

**File:** `arbiter/execution/adapters/kalshi.py:362-396`
**Issue:**
When the Kalshi batched-DELETE response parses to something the loop
doesn't recognise (not `dict`, not `list`), `parsed_ids` stays empty and
the else-branch assumes every submitted id succeeded:

```python
if parsed_ids:
    cancelled_ids.extend(parsed_ids)
else:
    # No structured response — assume chunk succeeded and
    # record the ids we submitted. This matches the 204
    # (no body) pattern common for batched DELETE endpoints.
    cancelled_ids.extend(str(cid) for cid in chunk_ids)
```

This mixes two genuinely different cases: `204 No Content` (empty body,
everything cancelled) and `200 OK` with a malformed/truncated body (we
don't actually know what happened). The latter is silently treated as
success. The kill-switch audit log (`cancelled_counts`) will over-report,
operators will see "cleared" when some orders are still open, and the
kill-switch invariant "armed = no naked exposure on platform" leaks.

**Fix:**
Distinguish 204/empty from 200/truthy-but-malformed. Only assume success
when `status == 204` or `body_text.strip() == ""`; otherwise log a warning
and omit the chunk from `cancelled_ids` so Postgres reflects reality.

```python
elif status == 204 or not body_text.strip():
    cancelled_ids.extend(str(cid) for cid in chunk_ids)
else:
    log.warning(
        "kalshi.cancel_all.ambiguous_body",
        status=status, body_sample=body_text[:200],
        chunk_index=i // CHUNK_SIZE,
    )
    # Do NOT claim cancelled — operator must manually reconcile.
```

---

### WR-03: `_request_is_secure` mis-parses multi-element `Forwarded` header

**File:** `arbiter/api.py:63-69`
**Issue:**
The code splits `Forwarded` by `;` and checks each segment for `proto=`:

```python
forwarded = request.headers.get("Forwarded", "")
for segment in forwarded.split(";"):
    key, _, value = segment.partition("=")
    if key.strip().lower() == "proto":
        return value.strip().strip('"').lower() == "https"
```

RFC 7239 uses `;` as the intra-element separator and `,` between elements.
A header like `Forwarded: for=1.2.3.4, for=5.6.7.8;proto=https` gets split
into `["for=1.2.3.4, for=5.6.7.8", "proto=https"]` — so the second element's
`proto` happens to be read correctly here, but `Forwarded: for=1.2.3.4;\
proto=https, for=5.6.7.8;proto=http` ends up with the first `proto=https,
for=5.6.7.8` value, and `.split(",", 1)[0]` logic is not present. Session
cookies then incorrectly set `Secure` based on a later-hop protocol, not
the initial client hop. For a TLS-terminating proxy chain this can make
cookies either too permissive (HTTP→browser) or too strict (never Secure).

**Fix:**
Parse the first element only (RFC 7239 §5.2 says the first element is the
client-nearest hop):

```python
forwarded = request.headers.get("Forwarded", "")
if forwarded:
    first_element = forwarded.split(",", 1)[0]
    for segment in first_element.split(";"):
        key, _, value = segment.partition("=")
        if key.strip().lower() == "proto":
            return value.strip().strip('"').lower() == "https"
```

---

### WR-04: `_emit_rejection_incident` extracts platform via loose string suffix parsing

**File:** `arbiter/execution/engine.py:1140-1148`
**Issue:**
SAFE-02 emits a structured `order_rejected` incident when RiskManager
denies a trade, and the dashboard keys off `metadata.platform` for the
per-platform filter. The platform name is reconstructed from the reason
string by splitting on `" on "`:

```python
rejection_type = "per_platform"
# Reason format: "Per-platform exposure limit exceeded on {platform}"
if " on " in reason:
    platform = reason.rsplit(" on ", 1)[-1].strip() or None
```

This is fragile: the reason string is authored in one place (line 267:
`f"Per-platform exposure limit exceeded on {platform}"`), but a later
edit that changes the wording (e.g. localization or adding a period) will
silently break metadata.platform without failing any test. The rejection
flow loses its platform attribution.

**Fix:**
Change `RiskManager.check_trade` to return a structured denial (platform
and rejection-type fields alongside the human string) so the API/incident
layer doesn't have to reverse-parse prose:

```python
# RiskManager.check_trade — return (bool, str, dict) instead of (bool, str):
return (
    False,
    f"Per-platform exposure limit exceeded on {platform}",
    {"rejection_type": "per_platform", "platform": platform},
)
```

Update callers (`execute_opportunity`, `_emit_rejection_incident`) to read
from the dict. The existing test
`test_market_mapping_update_rejects_invalid_criteria_match` already proves
the rejection path; extend it to assert the new structured fields.

---

### WR-05: `except (asyncio.CancelledError, BaseException)` swallows every interrupt

**File:** `arbiter/api.py:242`
**Issue:**
The finally-block that drains `_rate_limit_task` catches everything —
`BaseException` covers `KeyboardInterrupt`, `SystemExit`, and every other
exception. The intent is "cancellation during shutdown is fine," but the
current form hides real bugs (e.g. `MemoryError` or a programmer error
raising `Exception` from the loop) and prevents operators from seeing the
stack trace on shutdown.

```python
try:
    await self._rate_limit_task
except (asyncio.CancelledError, BaseException):
    pass
```

**Fix:**
Catch only `asyncio.CancelledError`; let everything else propagate so it
reaches Sentry and the shutdown log.

```python
try:
    await self._rate_limit_task
except asyncio.CancelledError:
    pass
```

---

### WR-06: `await asyncio.sleep(wait)` inside `RateLimiter.acquire` cannot be interrupted by cancel during penalty

**File:** `arbiter/utils/retry.py:247-265`
**Issue:**
`RateLimiter.acquire` loops while a penalty is active:

```python
async def acquire(self):
    while True:
        wait = self.remaining_penalty_seconds
        if wait > 0:
            self._last_wait_seconds = wait
            self._total_wait_time += wait
            await asyncio.sleep(wait)
            continue
        # ... token acquisition ...
```

With CR-03 unfixed, a forged `Retry-After: 999999` makes `wait` enormous
and `asyncio.sleep(wait)` will park the caller for that duration. Graceful
shutdown signals to the engine's task via `task.cancel()`, which DOES
interrupt `asyncio.sleep` — but the shutdown sequence explicitly calls
cancel_all BEFORE cancelling tasks, so the adapter's `await
self.rate_limiter.acquire()` on the cancel-all path can block for the full
penalty before the supervisor's 5s timeout kicks in. The supervisor's
wait_for saves shutdown, but only by abandoning the cancel.

Once CR-03 caps the penalty at 60s, this becomes a bounded hang at worst.
Still, acquire should re-read `remaining_penalty_seconds` after a partial
sleep so a shortened penalty (e.g., operator clears the limiter) unblocks
the loop.

**Fix:**
Sleep in small chunks so the loop can re-evaluate cancellation and
penalty-reset events:

```python
if wait > 0:
    self._last_wait_seconds = wait
    self._total_wait_time += wait
    await asyncio.sleep(min(wait, 1.0))
    continue
```

This also improves fairness — fast tokens released during a long penalty
window are picked up within 1s instead of at the end of the penalty.

## Info

### IN-01: Dead `if status in {"submitted", "filled"}:` outer wrapper

**File:** `arbiter/execution/engine.py:814-824`
**Issue:**
The outer `if status in {"submitted", "filled"}:` wraps only an inner
`if status == "filled":` — CR-02 above documents the resulting missed-
exposure bug. Once CR-02 is fixed the outer `if` can be simplified to a
single condition; leaving it in as a nested guard is a code smell that
suggests a refactor was half-finished.

**Fix:**
After applying CR-02's fix, remove the nested `if`:

```python
if status in {"submitted", "filled"}:
    self.risk.record_trade(...)
```

---

### IN-02: `_ACTIVE_SESSIONS` is unbounded in-memory dict

**File:** `arbiter/api.py:101, 124`
**Issue:**
`_ACTIVE_SESSIONS[token] = email` adds a new entry on every login without
any cap or TTL. A single operator generating many logins over 7 days
(token TTL) grows the dict. There is no reaper. Minor DoS surface, and
expired tokens persist until a restart. `logout_user` pops correctly but
clients that close the browser without hitting /logout leak entries.

**Fix:**
Either (a) skip `_ACTIVE_SESSIONS` entirely and rely on HMAC signature +
timestamp (the current HMAC already covers expiry), or (b) prune entries
in `get_current_user` whose embedded timestamp is >7 days old before
returning.

---

### IN-03: `logger.warning(f"Failed login attempt: {email}")` log injection

**File:** `arbiter/api.py:121`
**Issue:**
The operator-supplied `email` field is interpolated into the log line
without normalization. An attacker can inject newlines / ANSI codes into
log sinks that don't escape control characters. Low severity (single-user
auth, no log-forwarding pipeline documented), but worth fixing before any
SIEM integration.

**Fix:**
`repr(email)` or explicit sanitization:

```python
logger.warning("Failed login attempt: %r", email[:200])
```

---

### IN-04: Broad `except Exception` in DDL migration silently skips schema

**File:** `arbiter/main.py:277-285`
**Issue:**
Startup runs `safety_events.sql` and `init.sql` inside a `try/except
Exception` that demotes any failure to a warning log line. If the
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS resolution_criteria JSONB`
statement fails (e.g. user lacks `ALTER` privilege), the system starts
with a partial schema and every later `INSERT` into `safety_events` or
`UPDATE` on `market_mappings.resolution_criteria` fails. For a
safety-critical audit trail, silent schema drift is worse than a hard
startup abort.

**Fix:**
On `--live`, treat DDL failure as a startup failure. Keep the soft-fail
only when `dry_run=true`:

```python
except Exception as exc:
    if config.scanner.dry_run:
        logger.warning("%s migration skipped: %s", sql_name, exc)
    else:
        logger.critical("%s migration failed under --live: %s", sql_name, exc)
        sys.exit(2)
```

---

### IN-05: `eval(`(${rectDataFn})`)` inside Playwright verifier

**File:** `output/verify_dashboard_polish.mjs:70`
**Issue:**
The audit script serialises a JS function to the browser context and
re-hydrates it with `eval`. The function body is static code, not user
input, so this is not a live vulnerability — but `eval` triggers
security-linting tooling and is the kind of pattern that can silently
grow unsafe if someone later passes dynamic strings through. A safer
idiom is `new Function(body)` (still dynamic, but flagged separately)
or to pass the rectData helper into `page.evaluate` via the second
argument.

**Fix:**
Playwright's `page.evaluate` supports passing functions directly:

```js
const result = await page.evaluate(() => {
  const rectData = (node) => { /* ... */ };
  // ... rest of audit block
});
```

or pass rectData as a stringified handle via `addInitScript`.

---

_Reviewed: 2026-04-17T02:10:16Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
