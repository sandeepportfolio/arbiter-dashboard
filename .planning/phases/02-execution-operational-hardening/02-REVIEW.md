---
phase: 02-execution-operational-hardening
reviewed: 2026-04-16T00:00:00Z
depth: standard
files_reviewed: 24
files_reviewed_list:
  - .env.template
  - requirements.txt
  - arbiter/requirements.txt
  - arbiter/main.py
  - arbiter/utils/logger.py
  - arbiter/utils/test_logger.py
  - arbiter/test_sentry_integration.py
  - arbiter/sql/__init__.py
  - arbiter/sql/migrate.py
  - arbiter/sql/migrations/001_execution_persistence.sql
  - arbiter/execution/store.py
  - arbiter/execution/test_store.py
  - arbiter/execution/recovery.py
  - arbiter/execution/test_recovery.py
  - arbiter/execution/engine.py
  - arbiter/execution/test_engine.py
  - arbiter/execution/adapters/__init__.py
  - arbiter/execution/adapters/base.py
  - arbiter/execution/adapters/retry_policy.py
  - arbiter/execution/adapters/test_retry_policy.py
  - arbiter/execution/adapters/test_protocol_conformance.py
  - arbiter/execution/adapters/kalshi.py
  - arbiter/execution/adapters/test_kalshi_adapter.py
  - arbiter/execution/adapters/polymarket.py
  - arbiter/execution/adapters/test_polymarket_adapter.py
findings:
  critical: 2
  warning: 6
  info: 6
  total: 14
status: issues_found
---

# Phase 02: Code Review Report

**Reviewed:** 2026-04-16T00:00:00Z
**Depth:** standard
**Files Reviewed:** 24
**Status:** issues_found

## Summary

Phase 02 delivers the core hardening deliverables as promised: structlog JSON
logging with context propagation, Sentry integration with safe no-op when DSN
is unset, an `ExecutionStore` backed by `asyncpg` with a forward-only
migration runner, and a clean adapter layer implementing a runtime-checkable
`PlatformAdapter` Protocol. The critical safety invariants listed in the
brief are upheld:

- Kalshi `place_fok` order body includes `"time_in_force": "fill_or_kill"`
  (`arbiter/execution/adapters/kalshi.py:104`).
- Polymarket `place_fok` is NOT decorated with `@transient_retry`; a
  dedicated test (`test_polymarket_does_not_decorate_place_fok_with_transient_retry`)
  enforces this invariant by inspecting tenacity's `.retry`/`.statistics`
  attributes on the method object.
- Polymarket `_place_fok_reconciling` performs a `get_orders(market=...)`
  pre-check before every submission attempt so a timed-out prior attempt
  cannot cause a duplicate POST (Pitfall 2).
- Polymarket `check_depth` cross-checks `get_order_book` against `get_price`
  and refuses the trade when the tick falls more than 1c outside the book's
  [best_bid, best_ask] range (Pitfall 1 / `polymarket.py:386-395`).
- Both adapters pass `isinstance(adapter, PlatformAdapter)` — verified by
  protocol-conformance tests.
- Engine has no remaining `if platform == "kalshi"` / `"polymarket"`
  branches in the execution path; everything dispatches through
  `self.adapters[platform]`.

However, two correctness issues surface that can harm live trading and
should be fixed before EXEC-05 goes live against real credentials:

1. The local-timeout cancel path in `engine._place_order_for_leg` submits
   a synthetic `order_id` to the adapter's `cancel_order`, which cannot
   match any real order on Kalshi (nor is it the `client_order_id`).
2. `engine._derive_client_order_id` returns `order.order_id` post-submit,
   but by that point `order.order_id` has been replaced by Kalshi's
   platform-assigned ID — so the DB's `client_order_id` column is
   populated with the wrong value, defeating the idempotency-lookup
   use case documented in the `PlatformAdapter` protocol.

Both are fixable with small, localized changes. Everything else is minor
code quality or Python-version-modernization tidying.

## Critical Issues

### CR-01: Timeout cancel path submits synthetic order_id that cannot match any real order

**File:** `arbiter/execution/engine.py:744-770`
**Issue:** When `asyncio.wait_for(adapter.place_fok(...), timeout=...)` fires,
the engine fabricates a `partial` `Order` with
`order_id=f"{arb_id}-{side.upper()}-{platform.upper()}"` (e.g.
`ARB-000042-YES-KALSHI`) and calls `adapter.cancel_order(partial)`. For
Kalshi, `cancel_order` hits `DELETE /portfolio/orders/{order.order_id}` —
that URL will always 404 because the synthetic ID is not a Kalshi
`order_id`. The correct recovery is to look up by `client_order_id` (or
`arb_id` prefix) and cancel any matching resting/pending orders. Today,
every timeout produces an Order in `FAILED` state with
`"; cancel failed - manual reconciliation may be required"` appended —
even when the platform never actually received the request. Worse, if
the request *did* reach Kalshi and created a resting order, that order
remains live indefinitely.
**Fix:**
```python
# arbiter/execution/engine.py — replace the cancel block in _place_order_for_leg
except asyncio.TimeoutError:
    partial = Order(
        order_id=f"{arb_id}-{side.upper()}-{platform.upper()}",
        ...
    )
    # Look up any orders this arb_id may have placed, cancel each.
    cancelled_any = False
    try:
        prefix = f"{arb_id}-{side.upper()}-"
        open_orders = await adapter.list_open_orders_by_client_id(prefix)
        for real_order in open_orders:
            if await adapter.cancel_order(real_order):
                cancelled_any = True
    except Exception as exc:
        logger.warning("timeout-recovery lookup failed on %s: %s", platform, exc)
    partial.status = OrderStatus.CANCELLED if cancelled_any else OrderStatus.FAILED
    if not cancelled_any:
        partial.error += "; no matching open order found - platform may have rejected or never received"
    order = partial
```
Polymarket's reconcile-before-retry logic inside `_place_fok_reconciling`
already handles the "the request went through" case, so this timeout
branch is really only hot for Kalshi — but since `list_open_orders_by_client_id`
is already in the Protocol and Kalshi supports it, the fix is straightforward.

---

### CR-02: `_derive_client_order_id` stores Kalshi's platform order_id in the `client_order_id` column

**File:** `arbiter/execution/engine.py:784-792` (in conjunction with `kalshi.py:174-186`)
**Issue:** After `kalshi.place_fok` succeeds, `Order.order_id` is set to
`str(order_data.get("order_id", client_order_id))` — i.e. Kalshi's
platform-assigned ID (the client_order_id is only the fallback on
missing response field). The engine then calls
`_derive_client_order_id(order)` which returns `order.order_id` for
any kalshi order with a `-` in it — and that value is the Kalshi
platform id, NOT the original `ARB-XXXXXX-YES-xxxxxxxx`
client_order_id. This is persisted to `execution_orders.client_order_id`
via `store.upsert_order(..., client_order_id=...)`. Downstream lookups
that rely on `client_order_id` to recover orphaned orders (e.g.
`list_open_orders_by_client_id`) will never match, and the uniqueness
guarantee of the partial index `idx_execution_orders_client_id` is weakened
(because the "client_order_id" is really the server order_id). Blast radius:
the key idempotency mechanism the design relies on is not actually
recorded in the DB.
**Fix:** Capture the original client_order_id on creation and thread it
back through the return value, or expose it as a field on `Order`.
Minimal diff:
```python
# kalshi.py — extend Order return to carry the original client_order_id
# Option A: add a new Order field (breaks dataclass, but is cleanest)
# Option B: make the adapter persist the client_order_id itself via a
#           side-channel the engine can pick up.

# Option B (preferred, no schema changes):
#   Return (Order, client_order_id) tuple from place_fok, OR
#   stash it on the Order's `error` metadata before success. Cleanest:
class Order:
    ...
    external_client_order_id: Optional[str] = None  # set by adapter on submit

# kalshi.py:174 — populate it
return Order(
    order_id=str(order_data.get("order_id", client_order_id)),
    ...
    external_client_order_id=client_order_id,
)

# engine.py:785 — use the new field
@staticmethod
def _derive_client_order_id(order: Order) -> Optional[str]:
    return getattr(order, "external_client_order_id", None)
```
Alternatively, change `Order.order_id` semantics so that it holds
`client_order_id` for Kalshi (and let Kalshi's platform ID live in
a separate `external_order_id` field). The current scheme mixes the two
in the same field which is the root cause.

## Warnings

### WR-01: `asyncio.get_event_loop()` is deprecated on Python 3.12 — use `get_running_loop()`

**File:** `arbiter/main.py:302`, `arbiter/execution/engine.py:954`,
`arbiter/execution/adapters/polymarket.py:107, 330, 359, 441`
**Issue:** `asyncio.get_event_loop()` emits `DeprecationWarning` on
Python 3.12 when called outside a running loop, and is scheduled for
removal in future Python versions. Inside a running coroutine (the
Polymarket adapter calls) it currently works but still warns. The main.py
call at line 302 is the risky one — it's called while the `asyncio.run(...)`
loop is active, so `get_running_loop()` is the correct, non-deprecated API.
**Fix:**
```python
# arbiter/main.py:302
-    for sig in (signal.SIGINT, signal.SIGTERM):
-        asyncio.get_event_loop().add_signal_handler(sig, handle_shutdown, sig)
+    loop = asyncio.get_running_loop()
+    for sig in (signal.SIGINT, signal.SIGTERM):
+        loop.add_signal_handler(sig, handle_shutdown, sig)

# polymarket.py:107 (and each other site)
-    loop = asyncio.get_event_loop()
+    loop = asyncio.get_running_loop()
```

---

### WR-02: Kalshi auth signing path may include query string for `_list_orders` and orderbook endpoints

**File:** `arbiter/execution/adapters/kalshi.py:356-360, 244-247`
**Issue:** Kalshi's HMAC signature spec (depending on SDK version) normally
signs the *path* of the URL, excluding the query string. `_list_orders`
passes `path = f"/trade-api/v2/portfolio/orders?status={status}"` into
`self.auth.get_headers("GET", path)` — if Kalshi's canonicalization
strips query params, this still works; if it signs the raw string, it
still works; but if the signer strips and the server validates the raw,
a mismatch is possible. `_fetch_depth` calls an unauthenticated endpoint
with `?depth=100` but doesn't pass `path` to `get_headers` at all (no
auth header), so that one is fine. Please verify the actual
`KalshiAuth.get_headers` implementation against the Kalshi signing
docs; if it signs raw, standardize on path-without-query or move the
query params into the URL only.
**Fix:**
```python
# kalshi.py:356 — strip the query from the signing path
path = "/trade-api/v2/portfolio/orders"
url = f"{self.config.kalshi.base_url}/portfolio/orders?status={status}"
headers = self.auth.get_headers("GET", path)  # sign the path, not the query
```
(The current test uses `auth = MagicMock()` so the signing path is never
exercised. Once a sandbox run happens, this is the first place to look
if auth fails.)

---

### WR-03: Polymarket `_match_existing` can match a different arb's prior order

**File:** `arbiter/execution/adapters/polymarket.py:204-226`
**Issue:** The reconcile pre-check matches open orders on
`abs(price - 0.01) < 0.01`, exact `size`, and `side == "BUY"`. If two
concurrent arbs on the same token_id happen to propose the same (price,
size, side), the pre-check could consume arb-B's still-open order as
if it were arb-A's prior-attempt artifact. Arb-A then skips submission
and hijacks arb-B's order into its own ArbExecution record. This is
rare under the current trading cadence (small capital, ~1 arb at a time)
but not impossible, and the consequence is a book-keeping mismatch
(recorded P&L differs from realized P&L on both sides).
**Fix:** Include a per-attempt nonce in the match key. Polymarket orders
accept an arbitrary `metadata` field on `OrderArgs` in some SDK versions;
if not, the cleanest workaround is to include a micro-offset in the
price (e.g. round to 4 decimals with the last digit derived from the
arb_id hash), though this may be rejected by the tick-size validator.
Short-term mitigation: log a warning whenever the reconcile matches so
operators can spot-check. Long-term: track the order IDs we submit
client-side and only match against those.

---

### WR-04: `asyncpg.Pool` declared as class-level attribute, not per-instance

**File:** `arbiter/execution/store.py:53`
**Issue:** `_pool: Optional[asyncpg.Pool] = None` at class scope makes
`ExecutionStore._pool` a shared default. If the first instance calls
`connect()` it sets `self._pool = pool` — an instance attribute shadows
the class default. But if a test creates two `ExecutionStore` instances
and the second never calls `connect()`, reads of `_pool` would resolve to
the (instance-shadowed on instance 1, still None on class). This isn't a
bug in the single-store production path but is a latent footgun for
tests and future multi-store scenarios. Moving it into `__init__` makes
intent explicit and avoids a class-vs-instance attribute surprise.
**Fix:**
```python
# store.py
class ExecutionStore:
    def __init__(self, database_url: str):
        self.database_url = database_url
-    _pool: Optional[asyncpg.Pool] = None
+        self._pool: Optional[asyncpg.Pool] = None
```

---

### WR-05: `_poly_side` silently coerces unknown side strings to "BUY"

**File:** `arbiter/execution/adapters/polymarket.py:186-202`
**Issue:** If a future code path passes `side="sell"` or a typo like
`side="yes "` (trailing space), the function falls through to
`return "BUY"` — a silent, wrong directive. For an arbitrage engine
that only buys today this is latent, but if/when hedging closes are
added (selling a token), this would silently route closes as opens.
**Fix:**
```python
@staticmethod
def _poly_side(side: str) -> str:
    s = str(side).strip().upper()
    if s in ("BUY", "SELL"):
        return s
    if s in ("YES", "NO"):
        return "BUY"
    raise ValueError(f"PolymarketAdapter: unsupported side {side!r}")
```
The caller (`place_fok`) catches all non-TimeoutError exceptions and
returns FAILED, so raising here is safe.

---

### WR-06: `Order.fill_qty` type drift — declared `int`, stored/returned as `float`

**File:** `arbiter/execution/engine.py:54`, `adapters/kalshi.py:159-161, 303-309`,
`adapters/polymarket.py:266-271, 474-479`, `store.py:272-273`
**Issue:** `Order` dataclass declares `fill_qty: int = 0` and
`quantity: int`, but multiple sites assign floats: Kalshi reads
`fill_count_fp` (Kalshi's fractional-precision fills); Polymarket reads
`size_matched` as a float; `store._row_to_order` casts `fill_qty=float(row["fill_qty"])`.
The dataclass isn't validated at runtime so this "works", but consumers
that assume `fill_qty` is an `int` (arithmetic with `quantity` in
`_live_execution` line 683: `min(max(leg_yes.fill_qty, 0), max(leg_no.fill_qty, 0))`)
can silently produce float P&L on an int field, and any downstream
`isinstance(v, int)` check breaks. Either the annotation is wrong (should
be `float`), or every assignment site should round/int-cast.
**Fix:** Change annotation to match reality — prediction-market contracts
are now fractional:
```python
# engine.py:54
@dataclass
class Order:
    ...
-    fill_qty: int = 0
+    fill_qty: float = 0.0
```
And update any `int`-dependent consumer (there are few; the risk manager
uses `suggested_qty * (yes_price + no_price)` which is already float).

## Info

### IN-01: Duplicate `_derive_arb_id` helper between store.py and recovery.py

**File:** `arbiter/execution/store.py:280-287` and `arbiter/execution/recovery.py:118-129`
**Issue:** Same logic, two copies; the recovery.py version returns the
input unchanged for unrecognized formats while the store.py version
returns None. The subtle difference (unchanged vs None) is intentional
(store raises on None, recovery passes-through to let store validate)
but that coupling is better expressed as one function with a flag.
**Fix:** Move to `arbiter/execution/_arb_id.py`:
```python
def derive_arb_id(order_id: str, *, strict: bool = False) -> Optional[str]:
    if not order_id or not order_id.startswith("ARB-"):
        return None if strict else order_id
    parts = order_id.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return None if strict else order_id
```

---

### IN-02: `_init_sentry` comment is misleading

**File:** `arbiter/main.py:479`
**Issue:** The comment `"must be before setup_logging so LoggingIntegration
sees the JSON formatter"` implies LoggingIntegration reads the formatter,
which it does not — it hooks `logging.Logger.handle` to capture records
regardless of how they'll be rendered. The real reason ordering matters
is to install Sentry's error hooks before the root logger is reconfigured,
so that any error during logging setup is captured.
**Fix:** Rewrite the comment:
```python
-    _init_sentry()              # must be before setup_logging so LoggingIntegration sees the JSON formatter
+    _init_sentry()              # install Sentry hooks before reconfiguring stdlib logging
     setup_logging(args.log_level, args.log_file)
```

---

### IN-03: `_get_poly_clob_client` is not re-entry-safe

**File:** `arbiter/execution/engine.py:901-926`
**Issue:** Two concurrent callers (heartbeat loop + adapter first use)
may both pass the `is not None` check and both construct a new
`ClobClient`. The second one discards the first. The ClobClient
constructor is cheap and idempotent so this is harmless today, but if
`create_or_derive_api_creds()` does heavy I/O, the race wastes an
API call. Trivial to fix with an `asyncio.Lock`.
**Fix:**
```python
# engine.py __init__
self._poly_clob_lock = asyncio.Lock()

def _get_poly_clob_client(self):  # consider async, but most callers sync today
    if self._poly_clob_client is not None:
        return self._poly_clob_client
    # guard once — if truly concurrent, an async variant with asyncio.Lock is cleaner
    ...
```

---

### IN-04: `MockPool.acquire()` vs real pool returns different objects — test fixture drifts

**File:** `arbiter/execution/test_store.py:69-80`
**Issue:** `MockPool.acquire()` returns a synchronous object with an
async context manager; real `asyncpg.Pool.acquire()` returns an awaitable
that *also* functions as an async context manager. Tests work because
production code uses `async with pool.acquire() as conn:` everywhere.
But `ExecutionStore.acquire()` returns `await self._pool.acquire()`
(bare await) — if any future code used that helper against the mock,
it would break. Consider removing `ExecutionStore.acquire()` (it's
not used internally) or aligning the mock.

---

### IN-05: `test_sentry_init_noop_when_dsn_unset` passes but only proves "no raise" — could assert behavior

**File:** `arbiter/test_sentry_integration.py:87-92`
**Issue:** The test docstring says "No assertion — success is 'no
exception raised'", but we can additionally verify the client is in a
safe no-op state:
```python
def test_sentry_init_noop_when_dsn_unset(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    sentry_sdk.init(dsn=None, traces_sample_rate=0.0, sample_rate=1.0)
    client = sentry_sdk.Hub.current.client
    assert client is None or client.dsn is None
```
The positive assertion matches the brief's "Sentry init() is a no-op when SENTRY_DSN env var is unset" invariant more directly.

---

### IN-06: Hardcoded `max_daily_loss=-50.0` / `max_daily_trades=100` / `max_total_exposure=500.0` in RiskManager

**File:** `arbiter/execution/engine.py:199-201`
**Issue:** These constants govern live-trade safety but aren't sourced
from config/env — they can only be changed by editing the file. Tests
override via `engine.risk._max_daily_trades = 250` (private-attribute
access), which shows the constants are tuned per-context. For live
trading these should flow from `ArbiterConfig` so operators can tune
them without a code change.
**Fix:**
```python
# config/settings.py — add to ScannerConfig or a new RiskConfig dataclass
max_daily_trades: int = 100
max_daily_loss_usd: float = -50.0
max_total_exposure_usd: float = 500.0

# engine.py
self._max_daily_trades = config.scanner.max_daily_trades
self._max_daily_loss = config.scanner.max_daily_loss_usd
self._max_total_exposure = config.scanner.max_total_exposure_usd
```

---

_Reviewed: 2026-04-16T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
