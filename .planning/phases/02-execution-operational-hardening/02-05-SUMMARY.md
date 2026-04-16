---
phase: 02-execution-operational-hardening
plan: 05
subsystem: execution/adapters
tags: [polymarket, exec-01, exec-03, exec-04, ops-03, fok, idempotency, stale-book]
requires:
  - "arbiter/execution/adapters/base.PlatformAdapter (Wave 1, Plan 03)"
  - "py_clob_client.client.ClobClient (installed via Plan 01 deps)"
  - "py_clob_client.clob_types.{OrderArgs, OrderType} (OrderType.FOK verified available)"
  - "arbiter/utils/retry.{CircuitBreaker, RateLimiter}"
  - "arbiter/execution/engine.{Order, OrderStatus} (kept in engine.py for now, adapters import from ..engine)"
provides:
  - "arbiter/execution/adapters/polymarket.PolymarketAdapter"
  - "arbiter/execution/adapters.__init__.PolymarketAdapter re-export"
affects:
  - "arbiter/execution/engine.py (UNCHANGED in this plan — Plan 06 will strip the extracted code and inject adapters)"
tech-stack:
  added:
    - "PolymarketAdapter class (PlatformAdapter Protocol implementation)"
  patterns:
    - "Two-phase FOK submission: create_order(args) then post_order(signed, OrderType.FOK)"
    - "Reconcile-before-retry loop: pre-check get_orders(market=...) before each submission to defeat non-idempotent POST on TimeoutError (Pitfall 2)"
    - "Stale-book guard: cross-check get_order_book vs get_price; refuse trade when tick is >1¢ outside cached spread (Pitfall 1)"
    - "Side normalization helper (_poly_side): engine's 'yes'/'no' → Polymarket CLOB 'BUY'; 'BUY'/'SELL' pass-through"
    - "run_in_executor wrapping for synchronous py-clob-client SDK calls"
    - "Factory-injected ClobClient: adapter receives `clob_client_factory` callable (D-13) so engine's cached client is shared with the heartbeat task"
key-files:
  created:
    - "arbiter/execution/adapters/polymarket.py (PolymarketAdapter, ~430 lines)"
    - "arbiter/execution/adapters/test_polymarket_adapter.py (20 unit tests, ~340 lines)"
  modified:
    - "arbiter/execution/adapters/__init__.py (re-exports PolymarketAdapter)"
decisions:
  - "Adapter `place_fok` is NEVER decorated with @transient_retry; reconcile-before-retry is implemented inline in `_place_fok_reconciling`. An invariant test (`test_polymarket_does_not_decorate_place_fok_with_transient_retry`) pins this permanently."
  - "Response status 'matched' / 'filled' / 'executed' → OrderStatus.FILLED for FOK responses. Ambiguity in py-clob-client's exact post_order response shape; flagged for Phase 4 sandbox validation."
  - "For parallel-worktree hygiene, __init__.py only imports PolymarketAdapter — Plan 04's parallel worktree will add `from .kalshi import KalshiAdapter`. Both worktrees must converge on the final re-export block during integration (mechanical text-merge of two additive imports)."
  - "Side mapping: 'yes'/'no' → Polymarket CLOB 'BUY'. Extracted code hardcoded `side=\"BUY\"` at engine.py:1007; this adapter preserves that semantics explicitly (the token_id determines which token is bought)."
metrics:
  duration_min: 9
  tasks_completed: 2
  files_created: 2
  files_modified: 1
  commits: 2
  tests_added: 20
  tests_passing: 31   # 20 new + 11 pre-existing Wave-1 adapter tests
  completed: "2026-04-16T21:00:11Z"
---

# Phase 02 Plan 05: Polymarket Adapter Extraction Summary

Extract Polymarket order placement + cancellation from `engine.py` into a
`PolymarketAdapter` satisfying `PlatformAdapter`, and apply three critical
Polymarket-specific functional changes: two-phase FOK, reconcile-before-retry,
and stale-book guard.

## What was built

**`arbiter/execution/adapters/polymarket.py`** — `PolymarketAdapter(config, clob_client_factory, rate_limiter, circuit)`:

- `platform = "polymarket"` class attribute.
- `place_fok(arb_id, market_id, canonical_id, side, price, qty) -> Order`
  — Two-phase submission via `client.create_order(OrderArgs)` then
  `client.post_order(signed, OrderType.FOK)` inside a reconcile-before-retry
  loop with `max_attempts=3`. Short-circuits when wallet is unconfigured,
  circuit is open, or the factory returns None. All error paths return
  `Order(status=FAILED)`; nothing raises across the engine boundary.
- `_place_fok_reconciling(...)` — on each attempt, first queries
  `client.get_orders(market=market_id)` and returns any matching open order
  instead of re-submitting (Pitfall 2 mitigation). `TimeoutError` /
  `asyncio.TimeoutError` trigger a backoff-and-retry; any other exception
  bails immediately.
- `check_depth(market_id, side, required_qty) -> (sufficient, best_ask)`
  — Cross-checks `get_order_book` against `get_price`. Refuses (False, 0.0)
  when the tick falls outside the cached `[best_bid-0.01, best_ask+0.01]`
  window (Pitfall 1 stale-book guard).
- `cancel_order(order) -> bool` — Tries `client.cancel(order_id)` then
  `client.cancel_order(order_id)` (mirrors the fallback from
  `engine.py:732-745`).
- `get_order(order) -> Order` — Refreshes an order from the platform.
  Maps `matched`/`filled`/`executed` → FILLED, `canceled`/`cancelled` →
  CANCELLED, `live`/`open` → SUBMITTED.
- `list_open_orders_by_client_id(prefix) -> []` — Polymarket has no
  client_order_id concept; returns `[]` and logs a one-shot warning so
  operators know recovery relies on DB-side `get_order(...)` matching.
- `_poly_side(side)` helper — maps engine's `"yes"`/`"no"` leg labels to
  the CLOB's `"BUY"` and passes through explicit `"BUY"`/`"SELL"`.
- `_extract_levels(book, key)` helper — normalizes book levels returned
  as dict (`{asks,bids}`), object with attributes, or `[price, size]`
  lists into `(price, size)` tuples.

D-13 invariant is preserved: the adapter never constructs its own
`ClobClient` and never makes heartbeat calls. The engine will retain
ownership of the cached client and the heartbeat task when Plan 06 wires
the adapter with `clob_client_factory=lambda: engine._poly_clob_client`.

**`arbiter/execution/adapters/test_polymarket_adapter.py`** — 20 passing
pytest cases, all using mocked `ClobClient` (MagicMock) with synchronous
methods:

| Area | Tests |
|---|---|
| Protocol conformance | `test_polymarket_adapter_satisfies_protocol` |
| Two-phase FOK | `test_place_fok_uses_two_phase_create_then_post`, `test_place_fok_post_order_called_with_fok_order_type`, `test_place_fok_create_and_post_NOT_used` |
| Refusal paths | `test_place_fok_returns_failed_when_no_wallet`, `test_place_fok_returns_failed_when_factory_returns_none`, `test_place_fok_circuit_open_short_circuits` |
| Reconcile-before-retry (Pitfall 2) | `test_place_fok_reconcile_finds_existing_order_skips_resubmit`, `test_place_fok_timeout_then_reconcile_finds_order`, `test_place_fok_max_attempts_exhausted`, `test_place_fok_non_timeout_exception_bails_immediately` |
| Stale-book guard (Pitfall 1) | `test_check_depth_sufficient`, `test_check_depth_stale_book_refuses_when_tick_above_ask`, `test_check_depth_stale_book_refuses_when_tick_below_bid`, `test_check_depth_empty_book_refuses`, `test_check_depth_exception_returns_false` |
| cancel + list_open_orders | `test_cancel_returns_true_on_first_method`, `test_cancel_returns_false_on_no_client`, `test_list_open_orders_returns_empty_with_warning` |
| Tenacity safety invariant | `test_polymarket_does_not_decorate_place_fok_with_transient_retry` |

**`arbiter/execution/adapters/__init__.py`** updated to re-export
`PolymarketAdapter`. Note: the file does NOT yet import `KalshiAdapter`
because Plan 04 runs in a parallel worktree. Integration must reconcile
both additions (mechanical text merge — both are additive to the same
import block and `__all__` list).

## Threat model mitigations (as proven in tests)

| Threat ID | Mitigation | Proven by |
|---|---|---|
| T-02-15 (duplicate order via blind retry) | Reconcile-before-retry in `_place_fok_reconciling`; no `@transient_retry` on place_fok | `test_place_fok_timeout_then_reconcile_finds_order` (finds existing order on retry, zero `post_order` calls), `test_polymarket_does_not_decorate_place_fok_with_transient_retry` |
| T-02-16 (trade at inflated price via stale book) | `check_depth` cross-checks `get_order_book` vs `get_price`; refuses on >1¢ divergence | `test_check_depth_stale_book_refuses_when_tick_above_ask`, `test_check_depth_stale_book_refuses_when_tick_below_bid` |
| T-02-17 (heartbeat interference) | Factory-injected client; adapter never calls `post_heartbeat`, never constructs `ClobClient` | Grep-verified in commit (post_heartbeat: 0, create_and_post_order: 0); heartbeat invariant documented in module docstring |
| T-02-18 (partial fill / EXEC-01 violation) | Explicit `OrderType.FOK` on every `post_order`; legacy combined call is never used | `test_place_fok_post_order_called_with_fok_order_type`, `test_place_fok_create_and_post_NOT_used` |
| T-02-19 (secret disclosure) | No private key in logs; structlog events contain only `arb_id`, `market_id`, `attempt`, `err=str(exc)` | Code inspection; Sentry PII suppression is Plan 01 concern |

## Deviations from plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Side mapping: 'yes'/'no' → Polymarket CLOB 'BUY'**
- **Found during:** Task 2 (first run of `test_place_fok_reconcile_finds_existing_order_skips_resubmit`)
- **Issue:** The plan's sketch used `side.upper()` when building
  `OrderArgs` and when matching existing orders. Engine input labels are
  `"yes"`/`"no"` (arbitrage leg direction), not CLOB-native sides. This
  would send `"YES"`/`"NO"` to the Polymarket SDK, which expects
  `"BUY"`/`"SELL"`. The pre-extraction code at `engine.py:1007`
  hardcoded `side="BUY"` — that semantics had to be preserved.
- **Fix:** Added `_poly_side(side)` static helper that maps `"yes"`/`"no"`
  → `"BUY"` (the token_id determines which token is bought) and passes
  through explicit `"BUY"`/`"SELL"`. Applied in `_match_existing` and in
  the `OrderArgs.side` field of the submission path.
- **Files modified:** `arbiter/execution/adapters/polymarket.py`
- **Commit:** `a7b36a5` (committed alongside Task 2's tests)

### Documentation edits (not plan deviations)

- The adapter's module docstring explained the two-phase approach by
  referencing the legacy string `create_and_post_order` and the
  forbidden method `post_heartbeat`. Plan acceptance criteria required
  `grep -c` for those strings to return 0 (enforcing "never calls"),
  so the docstring was reworded to describe the same constraints
  without using those literals.

## Key decisions / notes for Plan 06

1. **Constructor signature:** Plan 06 must wire
   `PolymarketAdapter(config, clob_client_factory=lambda: engine._poly_clob_client, rate_limiter=engine._poly_rate_limiter, circuit=engine._poly_circuit)`. The factory pattern is mandatory — direct client injection would break D-13's shared-instance invariant.
2. **Heartbeat:** `engine.polymarket_heartbeat_loop` MUST continue to run
   unchanged after adapter injection. Adapter and heartbeat share the
   cached client via the factory.
3. **`matched` → FILLED mapping is a best-guess for FOK responses.**
   Phase 4 sandbox validation should log the raw `post_order` response
   shape for a real FOK fill and confirm the `status` field value. If
   it differs, extend `status_map` in `_order_from_response`.
4. **Reconcile pre-check fires even on attempt 0** (before any POST).
   This is intentional — it also covers the case where a process crash
   between POST and response-receipt leaves an order on the platform.
   Startup recovery (engine.recovery in a later plan) is the primary
   mechanism for that case, but the attempt-0 pre-check adds a second
   layer of defense.

## Deferred items / unknowns

- **`client.get_order(order_id)` is assumed to exist** but was not
  verified against the installed py-clob-client. The adapter guards
  this with `hasattr(client, "get_order")` and treats missing as
  "not found on platform", which is conservative.
- **Response parsing for partial-fill edge cases** — if FOK ever
  returns with `size_matched < size` (shouldn't, by FOK semantics),
  the current adapter still maps it as FILLED with `fill_qty=size_matched`.
  That matches the dataclass contract but an explicit PARTIAL status
  could be warranted. Revisit in Phase 4.

## Self-Check: PASSED

Verification commands:

```
$ python -c "from arbiter.execution.adapters import PolymarketAdapter, PlatformAdapter; assert PolymarketAdapter.platform == 'polymarket'; print('ok')"
ok

$ python -m pytest arbiter/execution/adapters/test_polymarket_adapter.py -x -v
20 passed

$ python -m pytest arbiter/execution/adapters/ -x -v
31 passed  (20 new + 11 Wave-1)

$ python -m pytest arbiter/execution/test_engine.py -x
11 passed  (engine.py untouched, pre-existing tests stable)
```

**Files verified present:**
- `arbiter/execution/adapters/polymarket.py` — FOUND
- `arbiter/execution/adapters/test_polymarket_adapter.py` — FOUND
- `arbiter/execution/adapters/__init__.py` (modified) — FOUND

**Commits verified on branch:**
- `a87976a` feat(02-05): add PolymarketAdapter with two-phase FOK + reconcile + stale-book guard — FOUND
- `a7b36a5` test(02-05): cover PolymarketAdapter (FOK, reconcile, stale-book, conformance) — FOUND
