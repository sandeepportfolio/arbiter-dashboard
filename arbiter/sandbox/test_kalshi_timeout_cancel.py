"""Kalshi demo execution-timeout + cancel (Scenario 5: Phase 2.1 CR-01 live validation).

Live-fires the Phase 2.1 CR-01 (cancel-on-timeout) and CR-02 (client_order_id
persistence) invariants against real Kalshi demo. Phase 2.1 remediated both
issues but only against mocks; this scenario is the last unvalidated safety
invariant on a real exchange.

Flow:
  1. Place a NON-FOK resting limit order on Kalshi demo (aggressive limit so it rests)
  2. Wait briefly (simulates engine asyncio.wait_for timeout)
  3. adapter.list_open_orders_by_client_id(client_order_id) -> returns orphan  (CR-02)
  4. adapter.cancel_order(orphan) -> True                                     (CR-01)
  5. adapter.list_open_orders_by_client_id(client_order_id) -> empty          (verification)
  6. Dump evidence + scenario_manifest.json

Non-FOK placement resolution rule (per plan Task 3 - NON-NEGOTIABLE scope boundary):
  Plan 04-03 is test-only; production adapter is FROZEN.
  Only Plan 04-02 touches production code.

  Resolution order (applied at runtime by _place_resting_limit_via_adapter_or_bypass):
    1) Check adapter for an EXISTING public non-FOK method (place_limit / place_gtc /
       place_resting_limit). If found, use it.
    2) Else: TEST-ONLY bypass via adapter._client.create_order (hypothetical Kalshi
       SDK wrapper). Documented phrase "production adapter is not modified".
    3) Else: TEST-ONLY bypass via adapter.session + adapter.auth (the actual Kalshi
       primitive since KalshiAdapter uses raw aiohttp, not an SDK wrapper). This is
       the real-world fallback for the Kalshi adapter shape observed in Phase 3.
    4) Else: pytest.fail with plan-revision request.

  FORBIDDEN: adding a public non-FOK method (e.g. place_resting_limit) to
  KalshiAdapter inside this plan's commit. Scope creep violation.

Operator pre-flight (Plan 04-03 Task 0):
  - Reuse happy-path ticker (needs orderbook depth so aggressive limit can rest)
  - .env.sandbox sourced; demo account funded; arbiter_sandbox schema applied

Run (operator-gated):
  set -a; source .env.sandbox; set +a
  pytest -m live --live arbiter/sandbox/test_kalshi_timeout_cancel.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
import structlog

from arbiter.execution.engine import OrderStatus
from arbiter.sandbox import evidence

log = structlog.get_logger("arbiter.sandbox.kalshi_timeout")

# -------- OPERATOR-SUPPLIED CONSTANTS (populated from Plan 04-03 Task 0) --------
# Reuse the happy-path ticker (market needs orderbook depth for the resting limit
# order to have somewhere to sit). Aggressive low-side limit so order rests, not fills.
TIMEOUT_MARKET_TICKER = os.getenv(
    "SANDBOX_TIMEOUT_TICKER",
    os.getenv("SANDBOX_HAPPY_TICKER", "REPLACE-WITH-OPERATOR-SUPPLIED-TICKER"),
)
TIMEOUT_AGGRESSIVE_PRICE = float(os.getenv("SANDBOX_TIMEOUT_PRICE", "0.05"))  # below mid
TIMEOUT_QTY = int(os.getenv("SANDBOX_TIMEOUT_QTY", "3"))


async def _place_resting_limit_via_adapter_or_bypass(
    adapter,
    *,
    arb_id: str,
    market_id: str,
    side: str,
    price: float,
    qty: int,
    client_order_id: str,
):
    """Place a non-FOK resting limit order on Kalshi demo.

    Resolution order (per Plan 04-03 Task 3 resolution rule):
      1) Use adapter.place_limit / .place_gtc / .place_resting_limit IF it already
         exists publicly.
      2) Else use adapter._client.create_order(...) TEST-ONLY bypass (Kalshi SDK's
         create-order entry point, if the adapter wraps one).
         Bypasses adapter FOK invariant for test setup only; production adapter
         is not modified. Scope boundary: Plan 04-03 is test-only; production
         code changes are forbidden in this plan.
      3) Else use adapter.session + adapter.auth direct HTTP POST (the actual
         Kalshi primitive - Phase 3 KalshiAdapter uses raw aiohttp, not an SDK
         wrapper). Same TEST-ONLY bypass rule: production adapter is not modified.
      4) Else pytest.fail with a plan-revision request; forbidden to add a public
         method here.
    """
    # Step 1: check for an existing public non-FOK method on the adapter.
    for name in ("place_limit", "place_gtc", "place_resting_limit"):
        fn = getattr(adapter, name, None)
        if callable(fn):
            log.info("scenario.kalshi_timeout.non_fok_strategy", strategy=f"adapter.{name}")
            # G-2 fix (Plan 04-09, 2026-04-20): KalshiAdapter.place_resting_limit
            # does NOT accept a client_order_id kwarg — the adapter generates
            # its own internally and surfaces it as Order.external_client_order_id.
            # The Phase 4.1 adapter signature is frozen; tests must consume the
            # adapter-generated id rather than pass one in. Step 2/Step 3 below
            # are TEST-ONLY raw-HTTP bypass paths that DO use the test-generated
            # client_order_id because they skip the adapter's generator entirely.
            order = await fn(
                arb_id=arb_id,
                market_id=market_id,
                canonical_id=market_id,
                side=side,
                price=price,
                qty=qty,
            )
            return SimpleNamespace(
                order_id=str(order.order_id),
                client_order_id=str(order.external_client_order_id or ""),
                raw=order,
            )

    # Step 2: TEST-ONLY bypass via underlying Kalshi SDK client (adapter._client).
    # If KalshiAdapter ever wraps an SDK (py-kalshi-client or similar), this path
    # is the cheapest bypass that keeps production adapter is not modified.
    client = getattr(adapter, "_client", None)
    if client is not None and hasattr(client, "create_order"):
        log.info(
            "scenario.kalshi_timeout.non_fok_strategy",
            strategy="adapter._client.create_order TEST-ONLY bypass",
        )
        return await _call_kalshi_sdk_create_order(
            client,
            market_id=market_id,
            side=side,
            price=price,
            qty=qty,
            client_order_id=client_order_id,
        )

    # Step 3: TEST-ONLY bypass via adapter.session + adapter.auth direct HTTP POST.
    # Phase 3 KalshiAdapter uses raw aiohttp.ClientSession (no SDK wrapper), so
    # the actual "underlying primitive" is session + auth + base_url. Production
    # adapter is not modified - we only use attributes the adapter already exposes.
    session = getattr(adapter, "session", None)
    auth = getattr(adapter, "auth", None)
    if session is not None and auth is not None and hasattr(auth, "get_headers"):
        log.info(
            "scenario.kalshi_timeout.non_fok_strategy",
            strategy="adapter.session+auth HTTP TEST-ONLY bypass",
        )
        return await _post_kalshi_gtc_via_session(
            adapter,
            market_id=market_id,
            side=side,
            price=price,
            qty=qty,
            client_order_id=client_order_id,
        )

    # Step 4: failure escape hatch - PLAN REVISION REQUIRED.
    pytest.fail(
        "Plan 04-03 Task 3: KalshiAdapter exposes no public non-FOK placement method AND "
        "adapter._client.create_order(...) is not available AND adapter.session+auth is "
        "not usable. Cannot live-fire CR-01 without placing a resting order. Request plan "
        "revision: either Plan 04-02 gains an explicit scope-expansion to add "
        "KalshiAdapter.place_resting_limit (with tests), or this scenario is re-scoped to "
        "drive via ExecutionEngine's timeout branch with a mock that rests (documented "
        "CR-01 live gap). Adding a public method to KalshiAdapter in this plan is FORBIDDEN."
    )


async def _call_kalshi_sdk_create_order(
    client, *, market_id, side, price, qty, client_order_id,
):
    """Invoke a hypothetical Kalshi SDK create_order with GTC (resting) semantics.

    Kept for forward-compatibility: if KalshiAdapter is later refactored to wrap
    a Kalshi SDK as self._client, this helper adapts to it. Phase 3 adapter uses
    raw aiohttp, so Step 3 of the resolution helper is the actually-used branch.
    """
    fn = client.create_order
    kwargs = dict(
        ticker=market_id,
        side=side,
        count=qty,
        price=price,
        type="limit",
        time_in_force="GTC",
        client_order_id=client_order_id,
    )
    if asyncio.iscoroutinefunction(fn):
        raw = await fn(**kwargs)
    else:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, lambda: fn(**kwargs))

    order_id = (
        getattr(raw, "order_id", None)
        or (raw.get("order_id") if isinstance(raw, dict) else None)
        or (raw.get("order", {}).get("order_id") if isinstance(raw, dict) else None)
    )
    if not order_id:
        pytest.fail(
            f"Plan 04-03 Task 3: Kalshi SDK create_order returned no order_id. "
            f"Raw response: {raw!r}. Inspect SDK response shape and adjust wrapping."
        )
    return SimpleNamespace(order_id=order_id, client_order_id=client_order_id, raw=raw)


async def _post_kalshi_gtc_via_session(
    adapter, *, market_id, side, price, qty, client_order_id,
):
    """Direct HTTP POST /portfolio/orders with GTC semantics via adapter.session + auth.

    TEST-ONLY bypass. Uses the exact same primitives KalshiAdapter.place_fok uses
    internally (self.session, self.auth, self.config.kalshi.base_url), but sends
    time_in_force=GTC instead of fill_or_kill so the order rests on the book.
    Production adapter is not modified - we only consume already-exposed attributes.

    Body shape matches KalshiAdapter._post_order body (dollar-string prices, count_fp,
    client_order_id, limit, buy action). GTC is valid on Kalshi /portfolio/orders.
    """
    path = "/trade-api/v2/portfolio/orders"
    url = f"{adapter.config.kalshi.base_url}/portfolio/orders"
    order_body = {
        "ticker": market_id,
        "client_order_id": client_order_id,
        "action": "buy",
        "side": side,
        "type": "limit",
        "count_fp": f"{float(qty):.2f}",
        "time_in_force": "GTC",
    }
    if side == "yes":
        order_body["yes_price_dollars"] = f"{price:.4f}"
    else:
        order_body["no_price_dollars"] = f"{price:.4f}"

    headers = adapter.auth.get_headers("POST", path)
    async with adapter.session.post(url, json=order_body, headers=headers) as response:
        body_text = await response.text()
        status_code = response.status

    if status_code not in (200, 201):
        pytest.fail(
            f"Plan 04-03 Task 3: direct POST /portfolio/orders (GTC bypass) returned "
            f"HTTP {status_code}: {body_text[:300]}. Check Kalshi demo availability, "
            f"auth config, and that 'GTC' time_in_force is accepted on this endpoint."
        )

    try:
        data = json.loads(body_text)
    except Exception as exc:
        pytest.fail(
            f"Plan 04-03 Task 3: direct POST response parse failed: {exc}. "
            f"Body: {body_text[:300]}"
        )

    order_data = data.get("order", data) if isinstance(data, dict) else {}
    order_id = str(order_data.get("order_id", "") or "")
    if not order_id:
        pytest.fail(
            f"Plan 04-03 Task 3: direct POST succeeded but response carried no order_id. "
            f"Body: {body_text[:300]}"
        )
    return SimpleNamespace(
        order_id=order_id,
        client_order_id=client_order_id,
        raw=data,
    )


@pytest.mark.live
async def test_kalshi_timeout_triggers_cancel_via_client_order_id(
    demo_kalshi_adapter, sandbox_db_pool, evidence_dir, balance_snapshot,
):
    """Aggressive-limit resting order; CR-01 branch cancels via client_order_id lookup.

    Scope note: This test places a NON-FOK limit order. Kalshi FOK by definition never
    rests, so we cannot use place_fok for CR-01's timeout-cancel path. Resolution per
    plan Task 3 action:
      - Prefer an existing public non-FOK method on KalshiAdapter.
      - Else use adapter._client.create_order (Kalshi SDK) as a TEST-ONLY bypass. This
        bypasses the adapter FOK invariant for test setup only; production adapter
        is not modified.
      - Else (actual Phase 3 adapter shape) use adapter.session+auth direct HTTP POST
        TEST-ONLY bypass.
      - Adding a public non-FOK method to KalshiAdapter in this plan is FORBIDDEN
        (Plan 04-03 is test-only; only Plan 04-02 touches production code).
    """
    adapter = demo_kalshi_adapter
    assert "arbiter_sandbox" in os.getenv("DATABASE_URL", ""), "wrong DB"

    # Fail-fast if operator forgot to wire the ticker.
    assert TIMEOUT_MARKET_TICKER != "REPLACE-WITH-OPERATOR-SUPPLIED-TICKER", (
        "Plan 04-03 Task 0: SANDBOX_TIMEOUT_TICKER (or SANDBOX_HAPPY_TICKER fallback) "
        "env var not set AND the literal placeholder was not replaced. Operator must "
        "supply a demo market ticker with orderbook depth for the resting limit to sit on."
    )

    arb_id = "ARB-SANDBOX-KALSHI-TIMEOUT"
    # ARB-{n}-{SIDE}-{hex} format per Phase 2.1 convention.
    client_order_id = f"{arb_id}-YES-{os.urandom(4).hex()}"

    # Place a resting limit order via the 3-step resolution helper.
    placed = await _place_resting_limit_via_adapter_or_bypass(
        adapter,
        arb_id=arb_id,
        market_id=TIMEOUT_MARKET_TICKER,
        side="yes",
        price=TIMEOUT_AGGRESSIVE_PRICE,
        qty=TIMEOUT_QTY,
        client_order_id=client_order_id,
    )

    # G-2 fix (Plan 04-09): if Step 1 (adapter.place_resting_limit) was chosen,
    # the adapter generated its own client_order_id — use the effective id
    # returned by the helper. Step 2/Step 3 (TEST-ONLY bypass paths) use the
    # test-generated id as-is (returned unchanged by the helper).
    effective_client_order_id = placed.client_order_id or client_order_id

    log.info(
        "scenario.kalshi_timeout.resting_order_placed",
        order_id=placed.order_id,
        client_order_id=effective_client_order_id,
    )

    # Simulate the engine's timeout path: wait briefly, then invoke CR-01 recovery.
    await asyncio.sleep(1.0)

    # CR-02 invariant: list_open_orders_by_client_id finds the order via
    # engine-chosen ARB-*-SIDE-HEX client_order_id (not the Kalshi server-assigned
    # order_id). This is the Phase 2.1 remediation being live-fired here.
    orphans = await adapter.list_open_orders_by_client_id(effective_client_order_id)
    assert orphans, (
        f"CR-02 INVARIANT VIOLATED: list_open_orders_by_client_id({effective_client_order_id!r}) "
        f"returned empty. Placed order_id was {placed.order_id}. Either the "
        f"client_order_id was not threaded through to Kalshi, the order closed between "
        f"placement and lookup, or the prefix-match in the adapter lost the id."
    )
    orphan = orphans[0]
    log.info(
        "scenario.kalshi_timeout.orphan_found",
        orphan_order_id=orphan.order_id,
        client_order_id=effective_client_order_id,
    )

    # CR-01 invariant: cancel the orphan. KalshiAdapter.cancel_order takes an Order,
    # not an order_id string (per arbiter/execution/adapters/kalshi.py:236).
    cancelled = await adapter.cancel_order(orphan)
    assert cancelled, (
        f"CR-01 INVARIANT VIOLATED: adapter.cancel_order returned falsy for orphan "
        f"{orphan.order_id} (client_order_id={effective_client_order_id})."
    )

    # Verification: order should no longer appear as open after cancel.
    # Kalshi demo is eventually-consistent on the resting-orders list endpoint:
    # a DELETE can return 200 but list_open_orders may still include the order
    # for 1-3 seconds afterward. Poll for up to 5 seconds to tolerate that lag
    # (the GET /portfolio/orders/{id} endpoint reports status="canceled"
    # immediately; it's the list endpoint that lags).
    post_orphans = []
    for _ in range(10):
        post_orphans = await adapter.list_open_orders_by_client_id(effective_client_order_id)
        if not post_orphans:
            break
        await asyncio.sleep(0.5)
    assert not post_orphans, (
        f"Cancel did not stick after 5s: list_open_orders_by_client_id still returns an open "
        f"order. post_orphans: {post_orphans}"
    )

    # Evidence + scenario manifest.
    await evidence.dump_execution_tables(sandbox_db_pool, evidence_dir)
    (evidence_dir / "scenario_manifest.json").write_text(
        json.dumps(
            {
                "scenario": "kalshi_timeout_triggers_cancel_via_client_order_id",
                "requirement_ids": ["TEST-01", "EXEC-05", "EXEC-04"],
                "phase_2_1_refs": ["CR-01", "CR-02"],
                "tag": "real",
                "order_id": placed.order_id,
                "client_order_id": effective_client_order_id,
                "market": TIMEOUT_MARKET_TICKER,
                "price": TIMEOUT_AGGRESSIVE_PRICE,
                "qty": TIMEOUT_QTY,
                "cancel_succeeded": bool(cancelled),
                "cr_02_lookup_succeeded": True,
                "non_fok_placement_strategy": _non_fok_strategy_label(adapter),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _non_fok_strategy_label(adapter) -> str:
    """Describe which non-FOK placement strategy this test run used, for evidence."""
    for name in ("place_limit", "place_gtc", "place_resting_limit"):
        if callable(getattr(adapter, name, None)):
            return f"adapter.{name} (existing public method)"
    client = getattr(adapter, "_client", None)
    if client is not None and hasattr(client, "create_order"):
        return "adapter._client.create_order TEST-ONLY bypass"
    session = getattr(adapter, "session", None)
    auth = getattr(adapter, "auth", None)
    if session is not None and auth is not None:
        return "adapter.session+auth HTTP TEST-ONLY bypass (production adapter is not modified)"
    return "unresolved - pytest.fail escape hatch"
