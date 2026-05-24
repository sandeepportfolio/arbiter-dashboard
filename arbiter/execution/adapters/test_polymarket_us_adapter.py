"""Tests for PolymarketUSAdapter — Task 7 of Polymarket US pivot plan.

TDD: tests written first; red-before-green.

Six required tests (per spec, with C1 fix — call_count == 0 assertions):
1. test_fok_happy_path
2. test_phase4_hard_lock_trips_before_signing
3. test_phase5_hard_lock_trips_before_signing
4. test_supervisor_armed_trips_before_signing
5. test_signing_error_propagates
6. test_order_id_threaded_from_api_response
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbiter.execution.adapters.exceptions import OrderRejected
from arbiter.execution.adapters.polymarket_us import PolymarketUSAdapter
from arbiter.execution.engine import Order, OrderStatus


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_client() -> MagicMock:
    client = MagicMock()
    client.place_order = AsyncMock(
        return_value={"orderId": "ord-default", "status": "FILLED"}
    )
    return client


def _make_supervisor(is_armed: bool = False) -> MagicMock:
    sv = MagicMock()
    sv.is_armed = is_armed
    return sv


def _make_adapter(
    *,
    phase4_max_usd: float | None = None,
    phase5_max_usd: float | None = None,
    supervisor=None,
    client=None,
) -> PolymarketUSAdapter:
    if client is None:
        client = _make_client()
    return PolymarketUSAdapter(
        client=client,
        phase4_max_usd=phase4_max_usd,
        phase5_max_usd=phase5_max_usd,
        supervisor=supervisor,
    )


# ─── Test 1: happy path ───────────────────────────────────────────────────────

async def test_fok_happy_path(monkeypatch):
    """Small order, all gates pass — place_fok returns a filled Order."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.delenv("PHASE5_MAX_ORDER_USD", raising=False)
    client = _make_client()
    client.place_order = AsyncMock(
        return_value={"orderId": "ord-happy", "status": "FILLED"}
    )
    adapter = _make_adapter(client=client)
    # Small notional: 0.5 * 10 = $5
    order = await adapter.place_fok("ARB-1", "mkt-slug", "CAN-1", "yes", 0.50, 10)
    assert order.status == OrderStatus.FILLED
    assert order.order_id == "ord-happy"


# ─── Test 2: PHASE4 hard-lock trips before signing ───────────────────────────

async def test_phase4_hard_lock_trips_before_signing(monkeypatch):
    """PHASE4=$5, notional=$10 — OrderRejected with 'PHASE4' in message.
    _sign_and_send must never be called (C1 fix).
    """
    monkeypatch.delenv("PHASE5_MAX_ORDER_USD", raising=False)
    adapter = _make_adapter(phase4_max_usd=5.0)
    sign_mock = AsyncMock(return_value=MagicMock(status=OrderStatus.FILLED))
    adapter._sign_and_send = sign_mock

    with pytest.raises(OrderRejected) as exc_info:
        # notional = 0.50 * 20 = $10 > $5 PHASE4 cap
        await adapter.place_fok("ARB-2", "mkt-slug", "CAN-2", "yes", 0.50, 20)

    assert "PHASE4" in str(exc_info.value)
    assert sign_mock.call_count == 0, "PHASE4 gate must fire BEFORE _sign_and_send"


# ─── Test 3: PHASE5 hard-lock trips before signing ───────────────────────────

async def test_phase5_hard_lock_trips_before_signing(monkeypatch):
    """PHASE4 unset (None), PHASE5=$10, notional=$11 — OrderRejected with 'PHASE5'.
    _sign_and_send must never be called (C1 fix).
    """
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=10.0)
    sign_mock = AsyncMock(return_value=MagicMock(status=OrderStatus.FILLED))
    adapter._sign_and_send = sign_mock

    with pytest.raises(OrderRejected) as exc_info:
        # notional = 0.55 * 20 = $11 > $10 PHASE5 cap
        await adapter.place_fok("ARB-3", "mkt-slug", "CAN-3", "yes", 0.55, 20)

    assert "PHASE5" in str(exc_info.value)
    assert sign_mock.call_count == 0, "PHASE5 gate must fire BEFORE _sign_and_send"


# ─── Test 4: supervisor armed trips before signing ───────────────────────────

async def test_supervisor_armed_trips_before_signing(monkeypatch):
    """Both caps pass (or unset), supervisor.is_armed=True — OrderRejected.
    _sign_and_send must never be called (C1 fix).
    """
    supervisor = _make_supervisor(is_armed=True)
    # Set caps to $100 so notional ($5) passes both gates
    adapter = _make_adapter(
        phase4_max_usd=100.0,
        phase5_max_usd=100.0,
        supervisor=supervisor,
    )
    sign_mock = AsyncMock(return_value=MagicMock(status=OrderStatus.FILLED))
    adapter._sign_and_send = sign_mock

    with pytest.raises(OrderRejected) as exc_info:
        await adapter.place_fok("ARB-4", "mkt-slug", "CAN-4", "yes", 0.50, 10)

    assert "supervisor" in str(exc_info.value).lower() or "armed" in str(exc_info.value).lower()
    assert sign_mock.call_count == 0, "supervisor gate must fire BEFORE _sign_and_send"


# ─── Test 5: signing error propagates ────────────────────────────────────────

async def test_signing_error_propagates(monkeypatch):
    """All gates pass but _sign_and_send raises — exception bubbles up (no retry, no swallow)."""
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=None, supervisor=None)
    sign_mock = AsyncMock(side_effect=RuntimeError("network error"))
    adapter._sign_and_send = sign_mock

    with pytest.raises(RuntimeError, match="network error"):
        await adapter.place_fok("ARB-5", "mkt-slug", "CAN-5", "yes", 0.50, 10)


# ─── Test 6: order_id threaded from API response ─────────────────────────────

async def test_order_id_threaded_from_api_response(monkeypatch):
    """Happy path response {"orderId":"ord-xyz","status":"FILLED"} -> Order.order_id="ord-xyz"."""
    client = _make_client()
    client.place_order = AsyncMock(
        return_value={"orderId": "ord-xyz", "status": "FILLED"}
    )
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-6", "mkt-slug", "CAN-6", "yes", 0.50, 10)
    assert order.order_id == "ord-xyz"


# ─── place_ioc + tif=IOC + status mapping ────────────────────────────────────
#
# The cross-venue arb pivot replaces FOK with IOC for the SECONDARY leg.
# These tests pin the new behaviour and the status-mapping fixes that
# stop the engine from treating Polymarket KILL responses as live orders.


async def test_place_ioc_routes_immediate_or_cancel_tif(monkeypatch):
    """place_ioc must request TIME_IN_FORCE_IMMEDIATE_OR_CANCEL, not FOK."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.delenv("PHASE5_MAX_ORDER_USD", raising=False)
    client = _make_client()
    client.place_order = AsyncMock(
        return_value={"orderId": "ord-ioc", "status": "FILLED"}
    )
    adapter = _make_adapter(client=client)

    await adapter.place_ioc("ARB-IOC", "mkt-slug", "CAN-1", "yes", 0.50, 10)

    assert client.place_order.await_count == 1
    kwargs = client.place_order.await_args.kwargs
    assert kwargs["tif"] == "IMMEDIATE_OR_CANCEL"
    # Same-shape arguments as place_fok otherwise.
    assert kwargs["slug"] == "mkt-slug"
    assert kwargs["qty"] == 10


async def test_place_ioc_runs_phase_gates_before_signing(monkeypatch):
    """IOC must enforce PHASE4 / PHASE5 / supervisor gates before touching the wire."""
    adapter = _make_adapter(
        phase4_max_usd=4.0, phase5_max_usd=10.0,
        supervisor=_make_supervisor(is_armed=False),
    )
    sign_mock = AsyncMock(return_value=MagicMock(status=OrderStatus.FILLED))
    adapter._sign_and_send = sign_mock

    with pytest.raises(OrderRejected) as exc:
        # 0.50 * 10 = $5 > $4 PHASE4 cap
        await adapter.place_ioc("ARB-IOC2", "mkt", "CAN", "yes", 0.50, 10)
    assert "PHASE4" in str(exc.value)
    assert sign_mock.call_count == 0, "PHASE4 must fire BEFORE _sign_and_send"


async def test_killed_status_maps_to_cancelled(monkeypatch):
    """ORDER_STATE_KILLED must map to CANCELLED so the engine triggers recovery
    instead of treating the order as still-open SUBMITTED.

    This is the regression that produced every soft-naked-leg event observed
    in production: Polymarket replied KILLED, the previous mapping fell through
    to the SUBMITTED default, and the engine waited for a fill that would
    never come while the primary leg sat naked on Kalshi.
    """
    client = _make_client()
    client.place_order = AsyncMock(
        return_value={"orderId": "ord-killed", "state": "ORDER_STATE_KILLED"}
    )
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-K", "mkt", "CAN", "yes", 0.50, 10)
    assert order.status == OrderStatus.CANCELLED


async def test_expired_status_maps_to_cancelled(monkeypatch):
    """ORDER_STATE_EXPIRED (typical IOC reply when nothing matched) → CANCELLED."""
    client = _make_client()
    client.place_order = AsyncMock(
        return_value={"orderId": "ord-exp", "state": "ORDER_STATE_EXPIRED"}
    )
    adapter = _make_adapter(client=client)
    order = await adapter.place_ioc("ARB-E", "mkt", "CAN", "yes", 0.50, 10)
    assert order.status == OrderStatus.CANCELLED


async def test_unknown_status_defaults_to_failed(monkeypatch):
    """Unknown wire status from Polymarket must default to FAILED.

    Previously fell through to SUBMITTED, causing the engine to hold a
    "still-resting" order in memory that the venue had already terminated.
    """
    client = _make_client()
    client.place_order = AsyncMock(
        return_value={"orderId": "ord-unk", "state": "WHATEVER_NEW_STATE"}
    )
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-U", "mkt", "CAN", "yes", 0.50, 10)
    assert order.status == OrderStatus.FAILED


async def test_partially_filled_status_maps_to_partial(monkeypatch):
    """PARTIAL with executions[].order.cumQuantity carries the fill qty
    so the engine can compute the unhedged excess to unwind."""
    client = _make_client()
    client.place_order = AsyncMock(
        return_value={
            "orderId": "ord-part",
            "state": "ORDER_STATE_PARTIALLY_FILLED",
            "executions": [{
                "order": {
                    "cumQuantity": "7",
                    "avgPx": {"value": "0.50", "currency": "USD"},
                }
            }],
        }
    )
    adapter = _make_adapter(client=client)
    order = await adapter.place_ioc("ARB-PF", "mkt", "CAN", "yes", 0.50, 10)
    assert order.status == OrderStatus.PARTIAL
    assert order.fill_qty == 7.0


# ─── Marketability clamp + tick snap (Fix 1 / Fix 2) ─────────────────────────

def _make_client_with_bbo(
    *,
    best_bid: float = 0.0,
    best_ask: float = 0.0,
    tick: float = 0.01,
    place_response: dict | None = None,
) -> MagicMock:
    """Helper: client with realistic ``get_market_by_slug`` + ``get_orderbook``."""
    client = MagicMock()
    client.get_market_by_slug = AsyncMock(
        return_value={"orderPriceMinTickSize": str(tick)}
    )
    bids = [{"px": str(best_bid), "qty": "100"}] if best_bid > 0 else []
    offers = [{"px": str(best_ask), "qty": "100"}] if best_ask > 0 else []
    client.get_orderbook = AsyncMock(
        return_value={"marketData": {"bids": bids, "offers": offers}}
    )
    client.place_order = AsyncMock(
        return_value=place_response or {"id": "ord-clamp", "state": "ORDER_STATE_FILLED"}
    )
    return client


async def test_clamp_sends_engine_price_as_is_for_buy_long():
    """FILL-01: the adapter no longer rewrites the BUY_LONG limit based on
    live BBO. The engine's book_walk + slippage buffer already covers
    adverse movement, and a redundant BBO clamp here was adding 200-600ms
    of wire latency that caused the post-2026-05-14 FOK-kill cascade. We
    must send the engine's price unchanged (subject only to tick-snap).
    """
    client = _make_client_with_bbo(best_bid=0.40, best_ask=0.45, tick=0.01)
    adapter = _make_adapter(client=client)

    await adapter.place_ioc("ARB-NOCLAMP", "slug-1", "CAN-1", "yes", 0.30, 10)

    assert client.place_order.await_count == 1
    sent_price = float(client.place_order.await_args.kwargs["price"])
    assert sent_price == 0.30, f"engine price must be passed through, got {sent_price}"


async def test_clamp_does_not_call_get_orderbook():
    """FILL-01: get_orderbook is the latency bandit and must NOT be hit
    on the order path. Engine handles book-walk upstream; the adapter
    only tick-snaps. A regression that reintroduces the BBO HTTP call
    will silently bring back the 750ms FOK-kill window.
    """
    client = _make_client_with_bbo(best_bid=0.40, best_ask=0.45, tick=0.01)
    adapter = _make_adapter(client=client)

    await adapter.place_ioc("ARB-NOBBO-HTTP", "slug-1", "CAN-1", "yes", 0.50, 10)

    assert client.get_orderbook.await_count == 0, (
        f"adapter must not fetch the book on the order path; "
        f"got {client.get_orderbook.await_count} call(s)"
    )


async def test_clamp_for_buy_short_sends_engine_price_as_is():
    """BUY_SHORT (buy NO) wire flip: no_price=0.30 → wire = 1 - 0.30 = 0.70.
    With BBO clamp removed, the wire price stays at 0.70 even if best_bid
    is below it; the engine is responsible for not sending a non-marketable
    limit.
    """
    client = _make_client_with_bbo(best_bid=0.55, best_ask=0.60, tick=0.01)
    adapter = _make_adapter(client=client)

    await adapter.place_ioc("ARB-NOCLAMP-NO", "slug-2", "CAN-2", "no", 0.30, 10)

    sent_price = float(client.place_order.await_args.kwargs["price"])
    assert sent_price == 0.70, f"wire price = 1 - no_price = 0.70, got {sent_price}"


async def test_tick_snap_rounds_to_market_tick():
    """0.5-cent tick markets: a 0.4533 limit snaps to 0.455, not 0.4533."""
    client = _make_client_with_bbo(best_bid=0.40, best_ask=0.40, tick=0.005)
    adapter = _make_adapter(client=client)

    # 0.4533 snapped to 0.005 grid -> 0.455.
    await adapter.place_ioc("ARB-SNAP", "slug-3", "CAN-3", "yes", 0.4533, 10)

    sent_price = float(client.place_order.await_args.kwargs["price"])
    assert sent_price == 0.455, f"expected 0.455, got {sent_price}"


async def test_max_affordable_aborts_when_snapped_price_too_high():
    """max_affordable must still abort the signing path when tick-snap
    rounds the engine's price above the slippage budget. Engine passes a
    deliberately-strict cap so the adapter is a last-line safety check.
    """
    client = _make_client_with_bbo(best_bid=0.55, best_ask=0.60, tick=0.05)
    adapter = _make_adapter(client=client)

    # 0.48 snapped to 0.05 grid → 0.50. max_affordable=0.49 ⇒ abort.
    with pytest.raises(OrderRejected, match="max_affordable"):
        await adapter.place_ioc(
            "ARB-AFFORD-ABORT", "slug-4", "CAN-4", "yes", 0.48, 10,
            max_affordable=0.49,
        )
    assert client.place_order.await_count == 0, "must not sign after abort"


async def test_clamp_no_bbo_passes_through():
    """Empty book is no longer even queried; engine price must pass through."""
    client = _make_client_with_bbo(best_bid=0.0, best_ask=0.0, tick=0.01)
    adapter = _make_adapter(client=client)

    await adapter.place_ioc("ARB-NOBBO", "slug-5", "CAN-5", "yes", 0.50, 10)

    sent_price = float(client.place_order.await_args.kwargs["price"])
    assert sent_price == 0.50


async def test_market_meta_is_cached_across_orders():
    """FILL-01: tick info is per-market immutable, so cache it across
    orders. A regression that re-fetches meta on every order would
    re-add ~200ms of wire latency per leg.
    """
    client = _make_client_with_bbo(best_bid=0.40, best_ask=0.40, tick=0.01)
    adapter = _make_adapter(client=client)

    await adapter.place_ioc("ARB-META-1", "slug-cache", "CAN-1", "yes", 0.50, 10)
    await adapter.place_ioc("ARB-META-2", "slug-cache", "CAN-1", "yes", 0.51, 10)
    await adapter.place_ioc("ARB-META-3", "slug-cache", "CAN-1", "yes", 0.52, 10)

    assert client.get_market_by_slug.await_count == 1, (
        f"market meta must be fetched once and cached; "
        f"got {client.get_market_by_slug.await_count} call(s)"
    )


async def test_tick_snap_defaults_to_one_cent_when_meta_missing():
    """Adapter defaults to 0.01 tick when the market metadata is unavailable."""
    client = MagicMock()
    client.get_market_by_slug = AsyncMock(return_value={})  # no tick field
    client.get_orderbook = AsyncMock(
        return_value={"marketData": {"bids": [], "offers": []}}
    )
    client.place_order = AsyncMock(
        return_value={"id": "ord-default-tick", "state": "ORDER_STATE_FILLED"}
    )
    adapter = _make_adapter(client=client)

    # 0.5037 → snap to 1¢ grid → 0.50
    await adapter.place_ioc("ARB-TICK-DEFAULT", "slug-6", "CAN-6", "yes", 0.5037, 10)
    sent_price = float(client.place_order.await_args.kwargs["price"])
    assert sent_price == 0.50


async def test_rejected_response_sets_order_error():
    """When the API returns REJECTED, the Order.error field carries the reason
    so retry classification can route it instead of bucketing as 'Platform error'.
    """
    client = MagicMock()
    client.place_order = AsyncMock(
        return_value={
            "id": "ord-rej",
            "state": "ORDER_STATE_REJECTED",
            "rejectionReason": "ORDER_PRICE_OUTSIDE_TICK_GRID",
        }
    )
    adapter = _make_adapter(client=client)

    order = await adapter.place_ioc("ARB-REJ", "slug-r", "CAN-R", "yes", 0.50, 10)

    assert order.status == OrderStatus.FAILED
    assert order.error is not None
    assert "polymarket_us" in order.error
    assert "REJECTED" in order.error.upper()


# ─── Empty-error regression: every non-success terminal must surface a reason ──

async def test_killed_response_sets_order_error():
    """ORDER_STATE_KILLED maps to CANCELLED; before this fix the Order returned
    with error=None and persisted as an empty string in execution_orders.error,
    making post-hoc diagnosis impossible (49 prod failures, 60 prod cancels in
    that exact state).  Now CANCELLED+terminal must always carry a reason.
    """
    client = MagicMock()
    client.place_order = AsyncMock(
        return_value={
            "id": "ord-killed",
            "state": "ORDER_STATE_KILLED",
            "rejectionReason": "NO_FILL_AT_LIMIT",
        }
    )
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-K2", "slug-k", "CAN-K", "yes", 0.50, 10)
    assert order.status == OrderStatus.CANCELLED
    assert order.error, "KILLED must populate order.error (was empty in prod)"
    assert "KILLED" in order.error.upper()


async def test_expired_response_sets_order_error_without_reason():
    """ORDER_STATE_EXPIRED with no rejectionReason field — error must still be
    populated with at least the api_status so the operator can distinguish
    'no liquidity' from 'venue rejected order shape'.
    """
    client = MagicMock()
    client.place_order = AsyncMock(
        return_value={"id": "ord-exp2", "state": "ORDER_STATE_EXPIRED"}
    )
    adapter = _make_adapter(client=client)
    order = await adapter.place_ioc("ARB-E2", "slug-e", "CAN-E", "yes", 0.50, 10)
    assert order.status == OrderStatus.CANCELLED
    assert order.error, "EXPIRED must populate order.error even without reason"
    assert "EXPIRED" in order.error.upper()


async def test_unknown_state_sets_order_error():
    """An unknown wire state defaults to FAILED — must carry the literal
    api_status so the next diagnostic pass sees what new state Polymarket
    started emitting.
    """
    client = MagicMock()
    client.place_order = AsyncMock(
        return_value={"id": "ord-unk2", "state": "WHATEVER_NEW_STATE"}
    )
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-U2", "slug-u", "CAN-U", "yes", 0.50, 10)
    assert order.status == OrderStatus.FAILED
    assert order.error, "Unknown state must populate order.error"
    assert "WHATEVER_NEW_STATE" in order.error


async def test_time_in_force_killed_sets_order_error():
    """TIME_IN_FORCE_KILLED is what Polymarket actually sends back for FOK
    rejections that didn't match — must surface a reason instead of empty error.
    """
    client = MagicMock()
    client.place_order = AsyncMock(
        return_value={"id": "ord-tif", "state": "TIME_IN_FORCE_KILLED"}
    )
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-TIF", "slug-t", "CAN-T", "yes", 0.50, 10)
    # Unknown to _map_status -> FAILED
    assert order.status == OrderStatus.FAILED
    assert order.error, "TIME_IN_FORCE_KILLED must populate order.error"
    assert "TIME_IN_FORCE_KILLED" in order.error


async def test_place_unwind_sell_yes_uses_sell_long_intent():
    """When recovering a naked YES position, the adapter must translate
    side='yes' into the SELL_LONG intent and use IOC TIF. The wire
    payload's intent + tif fields are how the venue distinguishes a
    'sell what I own' from 'short-sell new exposure'."""
    client = MagicMock()
    client.get_market_by_slug = AsyncMock(return_value={"orderPriceMinTickSize": "0.01"})
    client.place_order = AsyncMock(
        return_value={"id": "ord-uw-y", "state": "ORDER_STATE_FILLED"}
    )
    adapter = _make_adapter(client=client)

    await adapter.place_unwind_sell(
        "ARB-UW-Y", "slug-uw", "CAN-UW", "yes", 5, panic_price=0.01,
    )
    assert client.place_order.await_count == 1
    sent = client.place_order.await_args.kwargs
    assert sent["intent"] == "SELL_LONG", f"expected SELL_LONG, got {sent['intent']}"
    assert sent["tif"] == "IMMEDIATE_OR_CANCEL", f"expected IOC, got {sent['tif']}"
    assert float(sent["price"]) == 0.01


async def test_place_unwind_sell_no_uses_sell_short_intent():
    """side='no' → SELL_SHORT with wire price = 1 - panic_price (because
    Polymarket's order book is YES-axis; selling NO = covering a YES
    short, so the wire field flips)."""
    client = MagicMock()
    client.get_market_by_slug = AsyncMock(return_value={"orderPriceMinTickSize": "0.01"})
    client.place_order = AsyncMock(
        return_value={"id": "ord-uw-n", "state": "ORDER_STATE_FILLED"}
    )
    adapter = _make_adapter(client=client)

    await adapter.place_unwind_sell(
        "ARB-UW-N", "slug-uw", "CAN-UW", "no", 5, panic_price=0.01,
    )
    sent = client.place_order.await_args.kwargs
    assert sent["intent"] == "SELL_SHORT"
    assert sent["tif"] == "IMMEDIATE_OR_CANCEL"
    # NO panic 0.01 → wire = 1 - 0.01 = 0.99 (sell our NO at 1c means
    # the YES-axis ask we're posting is 99c).
    assert abs(float(sent["price"]) - 0.99) < 1e-6, (
        f"expected wire 0.99 for SELL_SHORT panic=0.01, got {sent['price']}"
    )


async def test_place_resting_sell_uses_gtc_tif():
    """Resting break-even sell must use GOOD_TILL_CANCEL TIF so the order
    actually rests on the book (IOC would self-cancel immediately).
    Verified against docs.polymarket.us OpenAPI enum 2026-05-24:
    valid GTC value is TIME_IN_FORCE_GOOD_TILL_CANCEL (no "ED").
    """
    client = MagicMock()
    client.get_market_by_slug = AsyncMock(return_value={"orderPriceMinTickSize": "0.01"})
    client.place_order = AsyncMock(
        return_value={"id": "ord-rest", "state": "ORDER_STATE_OPEN"}
    )
    adapter = _make_adapter(client=client)

    await adapter.place_resting_sell(
        "ARB-REST", "slug-r", "CAN-R", "yes", 0.40, 5,
    )
    sent = client.place_order.await_args.kwargs
    assert sent["intent"] == "SELL_LONG"
    assert sent["tif"] == "GOOD_TILL_CANCEL", (
        f"expected GOOD_TILL_CANCEL per the OpenAPI enum, got {sent['tif']}"
    )
    assert float(sent["price"]) == 0.40


async def test_unwind_sell_skips_phase_hardlocks():
    """Naked-leg recovery must NOT be blocked by PHASE4/PHASE5 size caps
    or the supervisor armed gate: those gates exist to stop NEW exposure,
    while unwind closes EXISTING exposure that the supervisor itself
    flagged. Blocking it would lock us into a permanent naked state.
    """
    client = MagicMock()
    client.get_market_by_slug = AsyncMock(return_value={"orderPriceMinTickSize": "0.01"})
    client.place_order = AsyncMock(
        return_value={"id": "ord-no-gate", "state": "ORDER_STATE_FILLED"}
    )
    supervisor = MagicMock()
    supervisor.is_armed = True
    adapter = _make_adapter(
        client=client,
        phase4_max_usd=0.01,   # would block any new order
        phase5_max_usd=0.01,
        supervisor=supervisor,
    )

    # Must NOT raise OrderRejected — recovery path bypasses these gates.
    await adapter.place_unwind_sell("ARB-RECOV", "slug-r", "CAN-R", "yes", 5)
    await adapter.place_resting_sell("ARB-REST", "slug-r", "CAN-R", "yes", 0.40, 5)
    assert client.place_order.await_count == 2
