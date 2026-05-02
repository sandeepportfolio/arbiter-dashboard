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
