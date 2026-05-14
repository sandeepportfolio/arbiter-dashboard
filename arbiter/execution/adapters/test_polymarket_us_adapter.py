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


async def test_marketability_clamp_raises_buy_long_to_ask():
    """BUY_LONG (buy YES) below best_ask gets lifted to best_ask before submit."""
    client = _make_client_with_bbo(best_bid=0.40, best_ask=0.45, tick=0.01)
    adapter = _make_adapter(client=client)

    await adapter.place_ioc("ARB-CLAMP", "slug-1", "CAN-1", "yes", 0.30, 10)

    assert client.place_order.await_count == 1
    sent_price = float(client.place_order.await_args.kwargs["price"])
    assert sent_price == 0.45, f"expected 0.45, got {sent_price}"


async def test_marketability_clamp_lowers_buy_short_to_bid():
    """BUY_SHORT (buy NO) maps to selling YES; wire price must be <= best_bid.

    Engine passes ``no_price``; adapter flips to ``1 - no_price`` for the
    YES-axis wire field, then clamps that DOWN to ``best_bid`` if it sits
    above the bid.
    """
    # no_price=0.30  → wire request_price = 1 - 0.30 = 0.70.  best_bid=0.55 so
    # the clamp pushes the wire price DOWN to 0.55.
    client = _make_client_with_bbo(best_bid=0.55, best_ask=0.60, tick=0.01)
    adapter = _make_adapter(client=client)

    await adapter.place_ioc("ARB-CLAMP-NO", "slug-2", "CAN-2", "no", 0.30, 10)

    sent_price = float(client.place_order.await_args.kwargs["price"])
    assert sent_price == 0.55, f"expected 0.55, got {sent_price}"


async def test_tick_snap_rounds_to_market_tick():
    """0.5-cent tick markets: a 0.4533 limit snaps to 0.455, not 0.4533."""
    client = _make_client_with_bbo(best_bid=0.40, best_ask=0.40, tick=0.005)
    adapter = _make_adapter(client=client)

    # best_ask=0.40 caps the BUY_LONG clamp; nothing to lift.  Pass an above-ask
    # limit so the clamp does NOT activate and we observe pure tick-snap.
    # 0.4533 snapped to 0.005 grid -> 0.455.
    await adapter.place_ioc("ARB-SNAP", "slug-3", "CAN-3", "yes", 0.4533, 10)

    sent_price = float(client.place_order.await_args.kwargs["price"])
    assert sent_price == 0.455, f"expected 0.455, got {sent_price}"


async def test_marketability_clamp_aborts_when_above_max_affordable():
    """Clamp lifting BUY_LONG above ``max_affordable`` must raise OrderRejected.

    Best ask is 0.60 but the engine can only afford 0.50; placing at 0.60
    guarantees a loss, so the adapter aborts before signing.
    """
    client = _make_client_with_bbo(best_bid=0.55, best_ask=0.60, tick=0.01)
    adapter = _make_adapter(client=client)

    with pytest.raises(OrderRejected, match="marketability"):
        await adapter.place_ioc(
            "ARB-CLAMP-ABORT", "slug-4", "CAN-4", "yes", 0.40, 10,
            max_affordable=0.50,
        )
    assert client.place_order.await_count == 0, "must not sign after abort"


async def test_marketability_clamp_no_bbo_falls_through():
    """Missing BBO (empty book) is not a reason to abort — submit as-is."""
    client = _make_client_with_bbo(best_bid=0.0, best_ask=0.0, tick=0.01)
    adapter = _make_adapter(client=client)

    await adapter.place_ioc("ARB-NOBBO", "slug-5", "CAN-5", "yes", 0.50, 10)

    sent_price = float(client.place_order.await_args.kwargs["price"])
    assert sent_price == 0.50


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
