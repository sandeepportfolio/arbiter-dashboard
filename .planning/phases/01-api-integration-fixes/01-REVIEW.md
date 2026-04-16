---
phase: 01-api-integration-fixes
reviewed: 2026-04-16T12:45:00Z
depth: standard
files_reviewed: 11
files_reviewed_list:
  - arbiter/.env.template
  - arbiter/api.py
  - arbiter/audit/math_auditor.py
  - arbiter/audit/test_math_auditor.py
  - arbiter/collectors/polymarket.py
  - arbiter/config/settings.py
  - arbiter/execution/engine.py
  - arbiter/execution/test_engine.py
  - arbiter/main.py
  - arbiter/verify_collectors.py
  - arbiter/workflow/__init__.py
findings:
  critical: 4
  warning: 7
  info: 4
  total: 15
status: issues_found
---

# Phase 01: Code Review Report

**Reviewed:** 2026-04-16T12:45:00Z
**Depth:** standard
**Files Reviewed:** 11
**Status:** issues_found

## Summary

Reviewed 11 source files across the arbiter system covering configuration, API server, collectors, execution engine, audit system, and tests. The codebase is well-structured with good separation of concerns and a thorough shadow-audit system. However, there are several critical security issues (hardcoded credentials in the env template and source code, weak password hashing), a few bugs in fee computation and CORS configuration, and some code quality issues.

## Critical Issues

### CR-01: Hardcoded Kalshi API Key in .env.template

**File:** `arbiter/.env.template:4`
**Issue:** The `.env.template` file contains what appears to be a real Kalshi API key ID (`05037acb-3d57-42bf-a056-8c74a707adae`). Template files are committed to git and should contain only placeholder values. If this is a real key, it is exposed in version control history.
**Fix:**
```
KALSHI_API_KEY_ID=your-kalshi-api-key-id-here
```

### CR-02: Hardcoded Postgres Password in .env.template

**File:** `arbiter/.env.template:20`
**Issue:** The template includes a real-looking Postgres password (`arbiter_secret`). Anyone cloning the repo gets this credential. If a deployment uses the template defaults without changing the password, the database is trivially accessible.
**Fix:**
```
PG_PASSWORD=change-me-before-deploying
```

### CR-03: Hardcoded Default Password and Email in API Source Code

**File:** `arbiter/api.py:40-43`
**Issue:** The dashboard UI has a hardcoded default user with real email (`sparx.sandeep@gmail.com`) and a plaintext default password (`saibaba`) embedded in source code. Even though these are wrapped in `os.getenv`, the defaults are committed and anyone reading the source knows the credentials. Combined with the SHA-256 hashing (see CR-04), this is a serious authentication weakness.
**Fix:**
```python
UI_ALLOWED_USERS = {
    os.getenv("UI_USER_EMAIL", ""): _hash_password(
        os.getenv("UI_USER_PASSWORD", "")
    ),
}
```
Require credentials to be set via environment variables. Return 401 if neither is configured rather than falling back to insecure defaults.

### CR-04: Weak Password Hashing (Plain SHA-256, No Salt)

**File:** `arbiter/api.py:34-36`
**Issue:** Passwords are hashed with plain SHA-256 and no salt. SHA-256 is not designed for password hashing -- it is fast and GPU-friendly, making brute-force and rainbow table attacks trivial. This is especially dangerous since the default password is short and dictionary-attackable.
**Fix:**
```python
import bcrypt

def _hash_password(password: str) -> str:
    """Hash password with bcrypt (salt is included automatically)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())
```
Use `bcrypt`, `argon2`, or `scrypt` for password hashing. Update `login_user` to use the corresponding verify function.

## Warnings

### WR-01: CORS Allows All Origins (Access-Control-Allow-Origin: *)

**File:** `arbiter/api.py:225,235`
**Issue:** The CORS middleware sets `Access-Control-Allow-Origin: *` for all responses, including authenticated API endpoints and state-mutating POST routes. This allows any website to make cross-origin requests to the arbiter API. Since the API uses cookie-based auth (`arbiter_session`), this is a CSRF vector -- a malicious site could trigger authenticated actions (market mapping changes, incident resolution, portfolio unwinds) on behalf of a logged-in user.
**Fix:**
Restrict CORS to the actual dashboard origin, or at minimum do not use wildcard when `Access-Control-Allow-Credentials` applies:
```python
ALLOWED_ORIGINS = {os.getenv("ARBITER_DASHBOARD_ORIGIN", "http://localhost:8080")}

@web.middleware
async def _cors_middleware(self, request, handler):
    origin = request.headers.get("Origin", "")
    allowed = origin if origin in ALLOWED_ORIGINS else ""
    # ... set Access-Control-Allow-Origin to `allowed` instead of "*"
```

### WR-02: User Email Logged on Failed Login (Information Disclosure)

**File:** `arbiter/api.py:120`
**Issue:** Failed login attempts log the attempted email address at WARNING level. In a production scenario, this leaks valid/invalid email addresses into logs, which could aid targeted attacks. While an f-string with user input in log messages is not a format-string vulnerability in Python logging, it is still an information disclosure concern.
**Fix:**
```python
logger.warning("Failed login attempt from %s", request.remote)
```
Log the source IP instead of the email, or log at DEBUG level only.

### WR-03: Polymarket _polymarket_fee Multiplies and Divides by Quantity (No-op)

**File:** `arbiter/audit/math_auditor.py:65`
**Issue:** The shadow `_polymarket_fee` function computes `(rate * price * (1.0 - price) * quantity) / quantity`, which is mathematically a no-op -- the quantity cancels out. While this does not produce wrong results today, it obscures intent: is the function supposed to return per-contract fee or total fee? The primary calculator in `settings.py` (line 112) computes `resolved_rate * quantity * price * (1.0 - price)` and divides by quantity separately in `polymarket_fee()`. If the shadow is meant to match the primary, it should follow the same structure to catch rounding divergences at scale.
**Fix:**
Either simplify to `return rate * price * (1.0 - price)` with a docstring clarifying "per-contract fee", or match the primary calculator structure exactly.

### WR-04: _active_sessions Dict Grows Without Bound

**File:** `arbiter/api.py:100`
**Issue:** `_ACTIVE_SESSIONS` is a plain dict that stores every session token ever created. Tokens expire after 7 days via HMAC timestamp verification, but expired tokens are never purged from the dict. Over time (or under token-generation attacks), this dict grows without bound, consuming memory. Since `get_current_user` checks both HMAC validity AND dict membership, expired tokens stay as dead entries.
**Fix:**
Add periodic cleanup or use an LRU cache with a max size:
```python
from collections import OrderedDict

class _SessionStore:
    def __init__(self, max_size=10000):
        self._store = OrderedDict()
        self._max_size = max_size

    def add(self, token, email):
        if len(self._store) >= self._max_size:
            self._store.popitem(last=False)
        self._store[token] = email

    def get(self, token):
        return self._store.get(token)

    def remove(self, token):
        self._store.pop(token, None)
```

### WR-05: Broadcast Loop Creates Tasks Every Iteration Without Cleanup

**File:** `arbiter/api.py:608-619`
**Issue:** The `_broadcast_loop` creates new `asyncio.create_task` wrappers around queue.get() calls on every iteration. While pending tasks are cancelled, the cancelled tasks may not be garbage collected immediately if references persist. More importantly, if all four queue.get() tasks complete between iterations (unlikely but possible during bursts), tasks accumulate. The pattern is fragile -- consider using `asyncio.wait` with named tasks or a dedicated consumer per queue.
**Fix:**
Consider restructuring to use persistent consumer tasks per queue, or at minimum add error handling around task cancellation:
```python
for task in pending:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
```

### WR-06: Kalshi fill_qty Parsed as Float, Stored in int-Typed Field

**File:** `arbiter/execution/engine.py:874`
**Issue:** The Kalshi response parser extracts `fill_count_fp` as a float (`float(order_data.get("fill_count_fp", ...) or "0")`), but the `Order.fill_qty` field is typed as `int` (line 49). The float value is assigned directly without conversion to int. While Python allows this, downstream code (e.g., `min(max(leg_yes.fill_qty, 0), max(leg_no.fill_qty, 0))` on line 651) may behave unexpectedly with float values where int is expected. The test on line 569 even asserts `fill_qty == 10.0` (float), confirming the type mismatch.
**Fix:**
```python
fill_qty = int(float(order_data.get("fill_count_fp", order_data.get("count_filled", "0")) or "0"))
```

### WR-07: Missing Input Validation on Market Mapping Canonical ID

**File:** `arbiter/api.py:318-319`
**Issue:** The `handle_market_mapping_action` endpoint takes `canonical_id` from the URL path and checks if it exists in `MARKET_MAP`, but does not sanitize or validate the format of the ID. While this is not directly exploitable (dict lookup is safe), the canonical_id is later passed to `update_market_mapping` and stored in the mapping dict. If a crafted canonical_id with special characters were somehow added, it could cause issues in downstream JSON serialization or logging.
**Fix:**
Add basic format validation:
```python
if not re.match(r'^[A-Z0-9_]{1,64}$', canonical_id):
    return web.json_response({"error": "Invalid canonical_id format"}, status=400)
```

## Info

### IN-01: Unused Variable has_critical in math_auditor

**File:** `arbiter/audit/math_auditor.py:320`
**Issue:** The variable `has_critical` is computed at line 320 and used for logging at line 339, but the `passed` field on line 321 only checks `len(flags) == 0`. The `has_critical` variable is not used to differentiate the `passed` status. This means an opportunity with only "warning" flags and no "critical" flags still fails the audit, which may be intentional but the `has_critical` variable suggests there was intent to distinguish severity levels in the pass/fail decision.
**Fix:**
If warnings should not block execution, change:
```python
passed = not has_critical
```
Otherwise, remove `has_critical` computation before the `passed` assignment and compute it only in the logging section.

### IN-02: Polymarket _extract_fee_rate Method Is Never Called

**File:** `arbiter/collectors/polymarket.py:373-386`
**Issue:** The `_extract_fee_rate` method on `PolymarketCollector` is defined but never called anywhere in the file. The `discover_markets` method uses `_fetch_dynamic_fee_rate` instead (line 172). This appears to be dead code from before the dynamic fee rate feature was implemented.
**Fix:**
Remove `_extract_fee_rate` if it is confirmed unused, or add a comment explaining it is retained for a specific reason.

### IN-03: verify_collectors.py Uses ArbiterConfig() Without load_config()

**File:** `arbiter/verify_collectors.py:139`
**Issue:** The verification script creates `ArbiterConfig()` directly instead of calling `load_config()`. The `load_config()` function (settings.py:416-421) resolves relative paths for `kalshi.private_key_path` against the dotenv file location. Using `ArbiterConfig()` directly skips this resolution, which means the Kalshi private key path might not resolve correctly depending on the working directory.
**Fix:**
```python
from arbiter.config.settings import load_config
config = load_config()
```

### IN-04: workflow/__init__.py Is a Trivially Empty Module

**File:** `arbiter/workflow/__init__.py:1`
**Issue:** This file contains only a docstring (`"""ARBITER workflow package."""`). If the workflow package has no other modules, this is dead code that adds no value. No imports reference this package in the reviewed files.
**Fix:**
Verify whether any other modules exist in `arbiter/workflow/`. If this is a placeholder for future work, it is fine to keep. Otherwise consider removing.

---

_Reviewed: 2026-04-16T12:45:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
