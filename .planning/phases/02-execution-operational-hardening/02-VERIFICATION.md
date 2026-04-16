---
phase: 02-execution-operational-hardening
verified: 2026-04-16T00:00:00Z
status: gaps_found
score: 7/9 requirements satisfied (9 scoped: EXEC-01..05, OPS-01..04; 2 partials from REVIEW CR-01/CR-02)
overrides_applied: 0
requirements_coverage:
  - id: EXEC-01
    status: SATISFIED
    evidence: "Kalshi `time_in_force: \"fill_or_kill\"` present at kalshi.py:104; Polymarket two-phase create_order + post_order(OrderType.FOK) at polymarket.py:146-149"
  - id: EXEC-02
    status: PARTIAL
    evidence: "ExecutionStore + 001_execution_persistence.sql + migrate.py all present and pass unit tests; engine and main.py wire persistence end-to-end; recovery.py reconciles non-terminal orders. HOWEVER: CR-02 (engine.py:785-792) breaks client_order_id storage — the DB's client_order_id column is populated with Kalshi's platform-assigned order_id, not the original client_order_id, defeating the idempotency-lookup path used by `list_open_orders_by_client_id`."
  - id: EXEC-03
    status: SATISFIED
    evidence: "KalshiAdapter.check_depth queries public orderbook endpoint (kalshi.py:240-273); PolymarketAdapter.check_depth cross-checks get_order_book vs get_price with 1¢ stale-book guard (polymarket.py:346-404). Both return (False, 0.0) on error without raising."
  - id: EXEC-04
    status: SATISFIED
    evidence: "Engine.py has zero `_place_kalshi_order/_place_polymarket_order/_cancel_kalshi_order/_cancel_polymarket_order` symbols (grep returns 0); KalshiAdapter + PolymarketAdapter implement PlatformAdapter Protocol; engine dispatches through `self.adapters[platform]` at engine.py:720; only remaining `from py_clob_client.client import ClobClient` is inside `_get_poly_clob_client` which is kept verbatim for D-13 heartbeat invariant."
  - id: EXEC-05
    status: PARTIAL
    evidence: "`asyncio.wait_for(adapter.place_fok(...), timeout=self.execution_timeout_s)` at engine.py:736-739; timeout path attempts adapter.cancel_order; stale-book guard and reconcile-before-retry are in adapters. HOWEVER: CR-01 (engine.py:744-770) — the timeout cancel path fabricates a synthetic order_id `ARB-XXX-YES-KALSHI` and calls adapter.cancel_order(synthetic), which on Kalshi hits DELETE /portfolio/orders/{synthetic_id} and 404s. Timeouts where the order actually reached Kalshi leave that order LIVE on the platform. Polymarket's own reconcile-before-retry saves it on that side, so the gap is primarily Kalshi-facing."
  - id: OPS-01
    status: SATISFIED
    evidence: "arbiter/utils/logger.py configures structlog + ProcessorFormatter with JSONRenderer; SHARED_PROCESSORS includes merge_contextvars + _strip_secrets; engine.execute_opportunity binds arb_id/canonical_id at engine.py:312-318 and clears in finally at engine.py:360. 4 test_logger.py tests pass."
  - id: OPS-02
    status: SATISFIED
    evidence: "arbiter/main.py:45-60 defines _init_sentry() with AsyncioIntegration, AioHttpIntegration, LoggingIntegration, send_default_pii=False, traces_sample_rate=0.0, dsn=os.getenv('SENTRY_DSN') or None (no-op when unset). 2 test_sentry_integration.py tests pass including `test_async_exception_captured` (fake Transport subclass)."
  - id: OPS-03
    status: SATISFIED
    evidence: "arbiter/execution/adapters/retry_policy.py exposes transient_retry decorator built from tenacity (stop_after_attempt, wait_exponential_jitter(0.5,10), retry_if_exception_type(TRANSIENT_EXCEPTIONS), reraise=True). TRANSIENT_EXCEPTIONS = (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError). Kalshi adapter wraps POST/DELETE/GET-order/GET-orderbook/GET-list in @transient_retry. Polymarket deliberately does NOT (Pitfall 2 safety) — proven by test_polymarket_does_not_decorate_place_fok_with_transient_retry."
  - id: OPS-04
    status: SATISFIED
    evidence: "requirements.txt line 3: `cryptography>=46.0.0`; installed version 46.0.7; Kalshi collector tests still pass post-upgrade per 02-01-SUMMARY.md. py-clob-client bumped to >=0.34.0."
gaps_count: 2
gaps:
  - truth: "Execution timeout path correctly cancels live orders on Kalshi (EXEC-05)"
    status: partial
    reason: "CR-01: engine._place_order_for_leg fabricates a synthetic order_id on timeout and calls adapter.cancel_order with it. KalshiAdapter.cancel_order hits DELETE /portfolio/orders/{synthetic} which always 404s. An order that reached Kalshi after a local timeout remains live indefinitely."
    artifacts:
      - path: "arbiter/execution/engine.py"
        issue: "Lines 744-770 — synthetic Order(order_id=f\"{arb_id}-{side.upper()}-{platform.upper()}\") is fed to adapter.cancel_order; the synthetic id is not a Kalshi platform order_id nor the client_order_id."
    missing:
      - "Timeout recovery should use `adapter.list_open_orders_by_client_id(prefix=f\"{arb_id}-{side.upper()}-\")` to look up any real orders, then cancel each returned Order by its platform order_id."
      - "On cancel failure with zero matching orders, flag `FAILED` with `error='no matching open order found - platform may have rejected or never received'` so operators can distinguish \"never received\" from \"received but stuck\"."
  - truth: "client_order_id idempotency key is persisted to DB for Kalshi orders (EXEC-02 idempotency path)"
    status: partial
    reason: "CR-02: After kalshi.place_fok succeeds, Order.order_id is overwritten with Kalshi's platform-assigned id. engine._derive_client_order_id then returns `order.order_id` (now the platform id) for any Kalshi order containing '-', and that value is stored in execution_orders.client_order_id. Downstream `list_open_orders_by_client_id(prefix=...)` can never match because the recorded `client_order_id` is really the server order_id."
    artifacts:
      - path: "arbiter/execution/engine.py"
        issue: "Lines 784-792 — _derive_client_order_id() returns order.order_id (by that point Kalshi's platform id) instead of the original `ARB-{n}-{SIDE}-{hex}` client_order_id generated by KalshiAdapter.place_fok."
      - path: "arbiter/execution/adapters/kalshi.py"
        issue: "Line 174-175 — `Order(order_id=str(order_data.get(\"order_id\", client_order_id)), ...)` only falls back to client_order_id when the response lacks an id. No separate `external_client_order_id` field is carried forward."
    missing:
      - "Either add a new field to Order (e.g. `external_client_order_id: Optional[str] = None`) that KalshiAdapter populates with the original client_order_id, and have engine._derive_client_order_id return that field."
      - "Or change Order.order_id semantics for Kalshi so it holds client_order_id (and put the platform id in a separate external_order_id field)."
human_verification:
  - test: "Live-sandbox FOK rejection on insufficient depth — Kalshi"
    expected: "Place FOK for qty > visible book depth; Kalshi returns status=canceled with 0 fills"
    why_human: "Needs live sandbox credentials; EXEC-01 behavior against real API can only be validated in Phase 4 sandbox run"
  - test: "Live-sandbox FOK rejection on insufficient depth — Polymarket"
    expected: "Polymarket post_order returns success=false / rejected response on under-liquid market"
    why_human: "Real Polymarket API + USDC test wallet required"
  - test: "Polymarket two-phase FOK response shape validation"
    expected: "Response parser in _order_from_response correctly maps real Polymarket response to OrderStatus.FILLED/CANCELLED/FAILED"
    why_human: "Exact response shape from py-clob-client 0.34.x is documented only approximately; status_map coverage needs live validation. Noted in 02-05-SUMMARY.md."
  - test: "Polymarket stale-book guard trips against live Issue #180 behavior"
    expected: "When Polymarket get_order_book returns stale/cached data, get_price cross-check refuses trade"
    why_human: "Requires observing the known SDK bug against live market data"
  - test: "Process kill -9 mid-execution then restart recovers open orders via reconciliation"
    expected: "After restart, reconcile_non_terminal_orders queries each non-terminal DB order via adapter.get_order, updates DB state, and emits warning incidents for orphaned orders"
    why_human: "Requires live kill -9 timing; not reliably automatable in CI"
  - test: "Sentry capture in live-service path with SENTRY_DSN unset"
    expected: "sentry_sdk.init does not raise; structured logs remain valid JSON; no envelopes sent"
    why_human: "Requires launching the real service to observe runtime behavior"
  - test: "Kalshi HMAC signing under cryptography 46.x against live API"
    expected: "Authenticated endpoints (portfolio/orders POST/DELETE/GET) return 200/201, not 401"
    why_human: "WR-02 in REVIEW notes potential path-vs-query signing mismatch; only a live sandbox call can confirm"
review_findings_cross_reference:
  critical:
    - id: CR-01
      impacts: EXEC-05
      status: blocks live Kalshi timeout recovery — recorded as gap above
    - id: CR-02
      impacts: EXEC-02 (idempotency lookup path)
      status: breaks startup recovery via list_open_orders_by_client_id — recorded as gap above
  warning:
    - id: WR-01
      impacts: OPS-01 (none — cosmetic) / future Python compat
      status: asyncio.get_event_loop() deprecated on 3.12 — does not block satisfaction but should be scheduled
    - id: WR-02
      impacts: EXEC-01/EXEC-04 (Kalshi auth signing)
      status: potential path-vs-query signing mismatch; flagged in human_verification above (live sandbox test)
    - id: WR-03
      impacts: EXEC-01 (Polymarket reconcile correctness)
      status: _match_existing could match a different arb's order under concurrency; accept for current low-volume capital, revisit before multi-arb-concurrent trading
    - id: WR-04
      impacts: none functional — latent test risk
      status: informational
    - id: WR-05
      impacts: EXEC-01 (Polymarket side handling)
      status: silent BUY coercion on unknown side; low risk today (buy-only engine), blocker if/when hedging closes are added
    - id: WR-06
      impacts: EXEC-02 (fill_qty storage precision)
      status: annotation drift (int vs float); Kalshi count_fp and Polymarket size_matched are already floats — annotation is wrong, but storage path is float-tolerant (DECIMAL(12,2) in DDL)
---

# Phase 2: Execution & Operational Hardening — Verification Report

**Phase Goal:** Harden the execution path so live trading is safe. Enforce FOK on both platforms, persist execution state to PostgreSQL, add restart-recovery for orphaned orders, extract per-platform logic behind a `PlatformAdapter` Protocol, add observability (structlog + Sentry + tenacity), and upgrade cryptography.

**Verified:** 2026-04-16
**Status:** gaps_found (7/9 requirements fully satisfied; EXEC-02 and EXEC-05 partial due to CR-01 + CR-02)
**Re-verification:** No — initial verification

---

## Goal Achievement Summary

Phase 02 delivers the structural architecture the roadmap promised. All six plans produced the expected artifacts:

- Dependency upgrades landed (`cryptography 46.0.7`, `structlog 25.5.0`, `tenacity 9.1.4`, `sentry-sdk 2.58.0`, `py-clob-client 0.34.x`)
- Structured JSON logging via `ProcessorFormatter` bridge is live; contextvars propagate; secrets are redacted
- Sentry init gracefully no-ops when `SENTRY_DSN` is unset, captures async exceptions when set
- A forward-only migration runner + 4-table execution schema + `ExecutionStore` CRUD with asyncpg pool
- `PlatformAdapter` Protocol is runtime-checkable; `KalshiAdapter` and `PolymarketAdapter` both satisfy it
- `KalshiAdapter.place_fok` sets `time_in_force: "fill_or_kill"`
- `PolymarketAdapter.place_fok` uses two-phase `create_order` + `post_order(signed, OrderType.FOK)` with reconcile-before-retry on timeout and a stale-book guard on `check_depth`
- `engine.py` has zero references to the old platform-specific methods; dispatch flows through `self.adapters[platform]`
- `engine._live_execution` wraps each leg in `asyncio.wait_for` and persists every state transition through `self.store`
- `recovery.py:reconcile_non_terminal_orders` provides the startup reconciliation hook
- `main.py` wires `ExecutionStore` + both adapters + reconcile-on-startup; the Polymarket heartbeat task is untouched (D-13 preserved)

**161 unit tests pass, 2 DB-integration tests skip (no `DATABASE_URL`), 1 pre-existing Windows signal handler failure unrelated to this phase.**

However, two correctness issues surface in the REVIEW that materially weaken the live-trading story: CR-01 (timeout cancel path can't match real Kalshi orders) and CR-02 (the wrong string ends up in the `client_order_id` DB column). Both are fixable with small localized changes; both should land before EXEC-05 and recovery are trusted against real Kalshi credentials.

---

## Per-Requirement Verdicts

### EXEC-01 — Fill-or-Kill enforcement (Kalshi + Polymarket)

**Status:** SATISFIED

| Check | Evidence |
|-------|----------|
| Kalshi FOK literal | `"time_in_force": "fill_or_kill"` present at `arbiter/execution/adapters/kalshi.py:104` |
| Polymarket two-phase | `client.create_order(order_args)` (`polymarket.py:146`) + `client.post_order(signed, OrderType.FOK)` (`polymarket.py:149`) |
| Legacy one-shot gone | `grep create_and_post_order arbiter/execution/adapters/polymarket.py` returns 0 |
| Test coverage | `test_fok_request_body_shape_yes_side`/`_no_side`, `test_place_fok_uses_two_phase_create_then_post`, `test_place_fok_post_order_called_with_fok_order_type`, `test_place_fok_create_and_post_NOT_used` — all passing |
| Never-raise invariant | Every error path in both adapters constructs `Order(status=FAILED, error=...)`; no raise escapes `place_fok` |

### EXEC-02 — Execution state persistence

**Status:** PARTIAL (weakened by CR-02)

| Check | Evidence |
|-------|----------|
| Tables created | `001_execution_persistence.sql` contains CREATE TABLE IF NOT EXISTS for `execution_arbs`, `execution_orders`, `execution_fills`, `execution_incidents` + partial indexes |
| Migration runner | `arbiter/sql/migrate.py` with `schema_migrations` tracking, idempotent; `apply_pending`/`status` importable |
| Store lifecycle | `ExecutionStore.connect/disconnect/acquire/init_schema/upsert_order/insert_fill/insert_incident/record_arb/list_non_terminal_orders/get_order` all present; pool `min_size=2 max_size=10 command_timeout=30` |
| SQL safety | All statements parameterized `$1..$N`; single dynamic clause is selected from 2 fixed strings |
| Engine integration | `engine._place_order_for_leg` writes via `store.upsert_order` (engine.py:777); `_live_execution` writes via `store.record_arb` (engine.py:700); `_record_incident` writes via `store.insert_incident` (engine.py:494) |
| Restart recovery | `recovery.reconcile_non_terminal_orders` queries DB, asks adapter per order, updates DB with fresh state, returns orphaned list; 7 unit tests cover the branches |
| **Gap (CR-02)** | `engine._derive_client_order_id` returns `order.order_id` for Kalshi; by then, `order.order_id` = Kalshi's platform id, not the original `ARB-{n}-{SIDE}-{hex}` client id. DB's `client_order_id` column is populated with the wrong value. `list_open_orders_by_client_id(prefix="ARB-000042-YES-")` will never match. |

### EXEC-03 — Pre-trade depth verification

**Status:** SATISFIED

| Check | Evidence |
|-------|----------|
| Kalshi depth | `KalshiAdapter.check_depth` → GET `/markets/{ticker}/orderbook?depth=100` (public, no auth); sums levels on requested side |
| Polymarket depth | `PolymarketAdapter.check_depth` calls `get_order_book(market_id)` AND `get_price(market_id, side.upper())` concurrently |
| Stale-book guard (Pitfall 1) | If `tick > best_ask + 0.01 OR tick < best_bid - 0.01` return `(False, 0.0)` and log `polymarket.depth.stale_book` — `polymarket.py:386-395` |
| Error never raises | Both adapters return `(False, 0.0)` on exception; logged as warning |
| Test coverage | `test_check_depth_sufficient/insufficient/empty_book/non_200` (Kalshi) + `test_check_depth_stale_book_refuses_when_tick_above_ask/below_bid` (Polymarket) |

### EXEC-04 — Per-platform adapter extraction

**Status:** SATISFIED

| Check | Evidence |
|-------|----------|
| Protocol surface | `PlatformAdapter(Protocol)` with `@runtime_checkable` and 5 methods: `check_depth`, `place_fok`, `cancel_order`, `get_order`, `list_open_orders_by_client_id` |
| Isinstance conformance | `test_kalshi_adapter_satisfies_protocol` + `test_polymarket_adapter_satisfies_protocol` — both pass |
| Engine stripped | `grep '_place_kalshi_order\|_place_polymarket_order\|_cancel_kalshi_order\|_cancel_polymarket_order' arbiter/execution/engine.py` returns 0 |
| Platform-specific types gone | `grep 'OrderArgs\|OrderType\|time_in_force' arbiter/execution/engine.py` returns 0 |
| Dispatch | Engine routes via `self.adapters[platform].place_fok(...)` at engine.py:720,737 and `self.adapters[order.platform].cancel_order(order)` at engine.py:821; no `if platform == "kalshi"` branches |
| SDK retention (D-13) | The only remaining `from py_clob_client.client import ClobClient` is inside `_get_poly_clob_client` (engine.py:907), kept verbatim to share the cached client with the heartbeat task |

### EXEC-05 — Execution timeout + cancel-on-timeout

**Status:** PARTIAL (weakened by CR-01)

| Check | Evidence |
|-------|----------|
| Timeout wrapping | `asyncio.wait_for(adapter.place_fok(...), timeout=self.execution_timeout_s)` at engine.py:736-739 |
| Configurable timeout | `execution_timeout_s: float = 10.0` constructor arg; main.py reads `EXECUTION_TIMEOUT_S` env var |
| Cancel attempted on timeout | engine.py:757 — `await adapter.cancel_order(partial)` inside the `except asyncio.TimeoutError` handler |
| Status mapping | `CANCELLED` on successful cancel; else `FAILED` with error text |
| Test coverage | `test_engine_timeout_triggers_cancel` passes — the hanging adapter's `cancel_order` mock is awaited |
| **Gap (CR-01)** | The `partial` Order passed to `cancel_order` is constructed with `order_id=f"{arb_id}-{side.upper()}-{platform.upper()}"` — a synthetic id that is NOT a Kalshi platform order_id NOR the original client_order_id. `KalshiAdapter.cancel_order` issues `DELETE /portfolio/orders/{synthetic}` which always 404s. An order that actually reached Kalshi is never cancelled. Polymarket's own `_place_fok_reconciling` pre-check saves that side, so the gap is primarily Kalshi-facing. |

### OPS-01 — Structlog JSON logging with contextvars

**Status:** SATISFIED

| Check | Evidence |
|-------|----------|
| JSON output | `setup_logging` configures `ProcessorFormatter(processors=[remove_processors_meta, JSONRenderer()])` (logger.py:74-77) |
| Contextvars propagation | `SHARED_PROCESSORS` starts with `merge_contextvars`; `engine.execute_opportunity` calls `clear_contextvars()` + `bind_contextvars(arb_id=..., canonical_id=..., platform_yes/no=...)` at engine.py:312-318 and clears in `finally` at engine.py:360 |
| `extra={}` flows through | `ExtraAdder()` inserted into `SHARED_PROCESSORS` (discovered during Task 2 testing — SUMMARY Deviation 1) |
| Secret redaction | `_strip_secrets` processor regex `(_KEY\|_SECRET\|_DSN\|^Authorization)$` replaces value with `"***REDACTED***"`; runs after `ExtraAdder` so stdlib-injected secrets are also caught |
| Test coverage | `test_output_is_json_parseable`, `test_contextvars_propagate`, `test_secret_stripping`, `test_existing_call_signature_preserved` — all passing |
| Signature preserved | `setup_logging(level, log_file)` keeps original stdlib signature — all existing `logger.getLogger("arbiter.X").info(...)` calls emit JSON unchanged |

### OPS-02 — Sentry asyncio integration

**Status:** SATISFIED

| Check | Evidence |
|-------|----------|
| Init at main entry | `_init_sentry()` defined at main.py:45; called (per SUMMARY) before `setup_logging` in main's entry-point path |
| Integrations | `AsyncioIntegration()`, `AioHttpIntegration()`, `LoggingIntegration(level=INFO, event_level=ERROR)` |
| PII safety | `send_default_pii=False`, `traces_sample_rate=0.0`, `attach_stacktrace=True` |
| DSN-unset no-op | `dsn=os.getenv("SENTRY_DSN") or None`; sentry-sdk documented to no-op on `dsn=None`; verified by `test_sentry_init_noop_when_dsn_unset` |
| Async exception capture | `test_async_exception_captured` passes using a fake `Transport` subclass (sentry-sdk 2.x API) — RuntimeError("boom") lands in the envelope buffer |
| Env vars documented | `.env.template` lists `SENTRY_DSN`, `ARBITER_ENV`, `ARBITER_RELEASE` |

### OPS-03 — Tenacity transient retry

**Status:** SATISFIED

| Check | Evidence |
|-------|----------|
| Decorator factory | `transient_retry(*, max_attempts=3)` in retry_policy.py:36 |
| Tenacity primitives | `stop_after_attempt(max_attempts)`, `wait_exponential_jitter(initial=0.5, max=10)`, `retry_if_exception_type(TRANSIENT_EXCEPTIONS)`, `reraise=True` |
| Transient tuple | `TRANSIENT_EXCEPTIONS = (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError)` |
| Kalshi usage | `@transient_retry()` applied to `_post_order`, `_delete_order`, `_fetch_depth`, `_fetch_order`, `_list_orders` (5 sites) — safe because `client_order_id` is idempotency key |
| Polymarket non-usage | `grep '@transient_retry' arbiter/execution/adapters/polymarket.py` returns 0 — enforced by `test_polymarket_does_not_decorate_place_fok_with_transient_retry` which inspects the method for tenacity's `.retry`/`.statistics` attributes |
| Test coverage | 7 tests in test_retry_policy.py (retries-then-succeeds, exhaust-and-reraise, permanent-not-retried × 2, asyncio-timeout transient, max=1 no retry, tuple shape) — all passing |

### OPS-04 — Dependency version upgrades

**Status:** SATISFIED

| Check | Evidence |
|-------|----------|
| cryptography | `requirements.txt` line 3: `cryptography>=46.0.0`; installed 46.0.7 |
| py-clob-client | `requirements.txt` line 8: `py-clob-client>=0.34.0` |
| Non-regression | per 02-01-SUMMARY.md: `arbiter/collectors/test_kalshi_collector.py` (2 tests) passes post-upgrade; Kalshi RSA-PSS signing path intact |
| New deps pinned | `structlog>=25.5.0`, `tenacity>=9.1.4`, `sentry-sdk>=2.58.0` |
| Root/arbiter parity | `diff requirements.txt arbiter/requirements.txt` returns empty (files identical) |

---

## Artifact Inventory

| Artifact | Level 1 exists | Level 2 substantive | Level 3 wired | Status |
|----------|---------------|---------------------|---------------|--------|
| `arbiter/utils/logger.py` | ✓ | ✓ (147 lines, JSONRenderer, ExtraAdder, merge_contextvars, _strip_secrets, TradeLogger) | ✓ (called from main.py) | VERIFIED |
| `arbiter/main.py` | ✓ | ✓ (sentry_sdk.init + ExecutionStore + adapter construction + reconcile + heartbeat-unchanged) | ✓ | VERIFIED |
| `arbiter/sql/migrations/001_execution_persistence.sql` | ✓ | ✓ (4 tables + indexes + FKs) | ✓ (applied by migrate.py → init_schema) | VERIFIED |
| `arbiter/sql/migrate.py` | ✓ | ✓ (apply_pending, status, schema_migrations tracking) | ✓ | VERIFIED |
| `arbiter/execution/store.py` | ✓ | ✓ (CRUD, asyncpg pool, _row_to_order, parameterized SQL) | ✓ (engine.py imports + uses) | VERIFIED |
| `arbiter/execution/adapters/__init__.py` | ✓ | ✓ (re-exports 4 symbols) | ✓ | VERIFIED |
| `arbiter/execution/adapters/base.py` | ✓ | ✓ (Protocol, 5 methods, runtime_checkable) | ✓ | VERIFIED |
| `arbiter/execution/adapters/retry_policy.py` | ✓ | ✓ (transient_retry factory, TRANSIENT_EXCEPTIONS) | ✓ (used by kalshi.py) | VERIFIED |
| `arbiter/execution/adapters/kalshi.py` | ✓ | ✓ (5 methods, FOK body, tenacity on HTTP calls, circuit/rate-limiter) | ✓ (constructed in main.py) | VERIFIED |
| `arbiter/execution/adapters/polymarket.py` | ✓ | ✓ (two-phase FOK, reconcile-before-retry, stale-book guard, no transient_retry on POST) | ✓ (constructed in main.py with clob_client_factory) | VERIFIED |
| `arbiter/execution/recovery.py` | ✓ | ✓ (reconcile_non_terminal_orders, per-order get_order, orphaned list) | ✓ (called from main.py:256) | VERIFIED |
| `arbiter/execution/engine.py` | ✓ | ✓ (platform code stripped, adapter dispatch, wait_for, contextvars, store writes) | ✓ | VERIFIED (with CR-01 + CR-02 functional gaps) |

---

## Key Link Verification

| From | To | Via | Status |
|------|-----|-----|--------|
| `main.py` | `ExecutionStore` | `store = ExecutionStore(database_url); await store.connect(); await store.init_schema()` (main.py:131-135) | WIRED |
| `main.py` | `KalshiAdapter` + `PolymarketAdapter` | constructed with circuit+rate-limiter; attached to `engine.adapters` (main.py:173-188) | WIRED |
| `main.py` | `reconcile_non_terminal_orders` | `orphaned = await reconcile_non_terminal_orders(store, adapters)` (main.py:256) | WIRED |
| `main.py` | `engine.polymarket_heartbeat_loop` (D-13) | `asyncio.create_task(engine.polymarket_heartbeat_loop(), name="poly-heartbeat")` at main.py:285 — UNCHANGED | WIRED |
| `engine._place_order_for_leg` | `self.adapters[platform].place_fok` | `asyncio.wait_for(adapter.place_fok(...))` engine.py:736-739 | WIRED |
| `engine._place_order_for_leg` | `self.store.upsert_order` | engine.py:777 | WIRED (but records wrong client_order_id per CR-02) |
| `engine._live_execution` | `self.store.record_arb` | engine.py:700 | WIRED |
| `engine._record_incident` | `self.store.insert_incident` | engine.py:494 | WIRED |
| `engine.execute_opportunity` | `structlog.contextvars.bind_contextvars/clear_contextvars` | engine.py:312-318 + :360 (finally) | WIRED |
| `PolymarketAdapter._get_client` | `engine._get_poly_clob_client` (shared cached ClobClient) | `clob_client_factory=lambda: engine._get_poly_clob_client()` main.py:182 | WIRED |
| `KalshiAdapter._post_order` | Kalshi `/portfolio/orders` | POST w/ body including `"time_in_force": "fill_or_kill"` | WIRED |
| `PolymarketAdapter._place_fok_reconciling` | `client.post_order(signed, OrderType.FOK)` | polymarket.py:149 | WIRED |

---

## Anti-Pattern Scan

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| engine.py | 744-770 | Synthetic order_id fed to adapter.cancel_order | Blocker (CR-01) | Timeout path cannot cancel real Kalshi orders |
| engine.py | 785-792 | `_derive_client_order_id` returns `order.order_id` (already overwritten by platform id) | Blocker (CR-02) | Wrong value persisted; idempotency lookup broken |
| engine.py, polymarket.py, main.py | various | `asyncio.get_event_loop()` on Python 3.12 | Warning (WR-01) | DeprecationWarning; function removal scheduled in future Python |
| kalshi.py | 244, 356-360 | path with `?status=...` query string passed to auth.get_headers | Warning (WR-02) | Potential HMAC signing mismatch; only surfaces against live Kalshi |
| polymarket.py | 204-226 | `_match_existing` concurrency race under same (price, size, side) | Warning (WR-03) | Wrong order could be adopted; current capital makes this rare |
| store.py | 53 | `_pool` class-level default | Warning (WR-04) | Latent footgun for tests with multiple store instances |
| polymarket.py | 186-202 | `_poly_side` silently coerces unknown side to "BUY" | Warning (WR-05) | Latent risk when hedging closes are added |
| engine.py, kalshi.py, polymarket.py, store.py | various | `fill_qty: int` annotation vs `float` reality | Warning (WR-06) | Annotation drift; storage tolerates float via DECIMAL column |
| store.py, recovery.py | 280-287 / 118-129 | Duplicate `_derive_arb_id` helper | Info (IN-01) | Maintainability |
| main.py | 479 | `_init_sentry` ordering comment is misleading | Info (IN-02) | Documentation only |
| engine.py | 901-926 | `_get_poly_clob_client` not re-entry-safe | Info (IN-03) | Wastes an API call on rare race |
| test_store.py | 69-80 | `MockPool.acquire()` vs real pool return-shape drift | Info (IN-04) | Latent test-vs-prod divergence |
| test_sentry_integration.py | 87-92 | "no raise" test could assert client state | Info (IN-05) | Stronger assertion possible |
| engine.py | 199-201 | Hardcoded risk limits | Info (IN-06) | Should flow from config |

---

## Test Posture

- **Unit + mock suite:** 161 passed, 2 skipped (DB integration without `DATABASE_URL`), 1 pre-existing Windows signal-handler failure unrelated to this phase
- **Phase 2 test files:** `test_logger.py`, `test_sentry_integration.py`, `test_store.py` (mock tier), `test_retry_policy.py`, `test_protocol_conformance.py`, `test_kalshi_adapter.py`, `test_polymarket_adapter.py`, `test_recovery.py`, `test_engine.py` extensions — all green
- **Smoke imports:** `python -c "from arbiter.execution.adapters import KalshiAdapter, PolymarketAdapter, PlatformAdapter, transient_retry; from arbiter.execution.engine import ExecutionEngine; from arbiter.execution.recovery import reconcile_non_terminal_orders; from arbiter.execution.store import ExecutionStore; import arbiter.main"` → exits 0
- **Integration gated:** DB round-trip tests skip gracefully without `DATABASE_URL`; operator runs locally with `docker compose up -d postgres` first

---

## Gaps — Recommended Next Steps

### Gap 1 — Fix CR-01 (Kalshi timeout recovery)

**File:** `arbiter/execution/engine.py:744-770` (`_place_order_for_leg` timeout handler)

**Change:** Instead of fabricating a synthetic Order with `order_id=f"{arb_id}-{side.upper()}-{platform.upper()}"` and calling `adapter.cancel_order(synthetic)`, use the existing `list_open_orders_by_client_id` Protocol method to find any real orders placed with the `{arb_id}-{SIDE}-` prefix, then cancel each real one. The Protocol already declares this method; `KalshiAdapter` implements it; `PolymarketAdapter`'s own reconcile-before-retry covers its side.

### Gap 2 — Fix CR-02 (client_order_id storage)

**Preferred (minimal):** Add `external_client_order_id: Optional[str] = None` to `Order` dataclass. Populate it in `KalshiAdapter.place_fok` with the generated `client_order_id`. Change `engine._derive_client_order_id` to return that field. Leave `Order.order_id` semantics unchanged (still holds the platform id for downstream correlation).

**Alternative:** Flip `Order.order_id` semantics for Kalshi so it holds the client_order_id, and add `external_order_id: Optional[str] = None` for the platform id. This has broader blast radius (audit, dashboard, incident records all use `order_id`) and is less advised.

Either fix restores the `list_open_orders_by_client_id` lookup path that startup recovery + Gap 1's timeout-cancel-recovery depend on.

### Warnings to schedule (non-blocking)

- WR-01 — replace `asyncio.get_event_loop()` with `asyncio.get_running_loop()` in `main.py:302` and the Polymarket adapter executor calls
- WR-02 — standardize Kalshi signing to path-only (strip query from signing input); verify in Phase 4 sandbox
- WR-03 — add per-attempt nonce to `_match_existing` key or log warnings on reconcile hits for operator review
- WR-05 — raise `ValueError` on unknown `side` in `_poly_side` before hedging logic ships
- WR-06 — change `Order.fill_qty: int` to `float` in the dataclass to match actual storage

---

## Human Verification Required

See `human_verification:` in frontmatter. Six live-system tests remain unavailable to automated verification: live FOK rejection on both platforms (EXEC-01), Polymarket response-shape validation (EXEC-01 status_map coverage), Polymarket stale-book-guard observation against live Issue #180 behavior (EXEC-03), `kill -9` mid-execution restart recovery (EXEC-02), Sentry live-service `SENTRY_DSN`-unset behavior (OPS-02), and Kalshi HMAC signing under `cryptography 46.x` against a real sandbox (OPS-04 + WR-02). All are scoped to Phase 4 sandbox validation per `02-VALIDATION.md`.

---

## Final Verdict

**PASS WITH REMEDIATION**

Phase 02 ships the structural hardening the roadmap contracted for and all 9 requirements have concrete implementations backed by 161 passing unit tests. However, two REVIEW-identified defects (CR-01, CR-02) materially weaken the live-trading story and must be closed before EXEC-05 and the recovery path are trusted against real Kalshi credentials. Both are localized to `arbiter/execution/engine.py` and can be fixed without touching the adapter contract, the DB schema, or `main.py` wiring. The remaining warnings are schedulable and do not block Phase 02 completion for the purposes of moving to Phase 3 planning — but they should be logged as issues for Phase 3 to pull in or the milestone audit to surface.

**Status to record in STATE.md:** `verified — remediation pending (CR-01, CR-02)`
**Recommended action:** Open `/gsd-plan-phase --gaps` against this VERIFICATION.md to produce a closure plan (`02-07-PLAN.md` or inline into Phase 3 Wave 1) for CR-01 + CR-02.

---

_Verified: 2026-04-16_
_Verifier: Claude (gsd-verifier)_
