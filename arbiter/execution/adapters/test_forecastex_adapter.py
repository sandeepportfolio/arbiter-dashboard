"""
Tests for ForecastExAdapter — Protocol conformance, BUY-only constraint,
the three hard-lock gates (PHASE4, PHASE5, supervisor), tick snapping, and
order-status mapping.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.adapters import PlatformAdapter
from arbiter.execution.adapters.exceptions import OrderRejected
from arbiter.execution.adapters.forecastex import ForecastExAdapter
from arbiter.execution.engine import Order, OrderStatus


def _mock_client(place_response=None, place_exc=None):
    client = MagicMock()
    if place_exc is not None:
        client.place_order = AsyncMock(side_effect=place_exc)
    else:
        client.place_order = AsyncMock(
            return_value=place_response
            if place_response is not None
            else {"order_id": "abc", "order_status": "Filled", "filled_quantity": 10, "avg_price": 0.55},
        )
    client.get_order = AsyncMock(return_value={})
    client.cancel_order = AsyncMock(return_value={"ok": True})
    client.cancel_all_open_orders = AsyncMock(return_value=["x", "y"])
    client.market_snapshot = AsyncMock(
        return_value={"84": "44", "86": "47", "7295": "5", "7296": "20"},
    )
    client.live_rate_limiter = None
    client.circuit = None
    return client


# ── Protocol conformance ──────────────────────────────────────────────────


def test_adapter_satisfies_protocol():
    adapter = ForecastExAdapter(client=_mock_client())
    assert isinstance(adapter, PlatformAdapter)


# ── BUY-only constraint ───────────────────────────────────────────────────


async def test_adapter_rejects_sell():
    adapter = ForecastExAdapter(client=_mock_client())
    with pytest.raises(OrderRejected, match="only supports BUY"):
        await adapter.place_fok(
            arb_id="arb-1", market_id="111", canonical_id="X",
            side="SELL_YES", price=0.5, qty=1,
        )


async def test_adapter_rejects_plain_sell():
    adapter = ForecastExAdapter(client=_mock_client())
    with pytest.raises(OrderRejected, match="only supports BUY"):
        await adapter.place_ioc(
            arb_id="arb-2", market_id="111", canonical_id="X",
            side="SELL", price=0.5, qty=1,
        )


# ── Hard-lock gates ───────────────────────────────────────────────────────


async def test_phase4_hardlock_blocks_oversized():
    adapter = ForecastExAdapter(client=_mock_client(), phase4_max_usd=10.0)
    with pytest.raises(OrderRejected, match="PHASE4 hard-lock"):
        await adapter.place_fok(
            arb_id="a", market_id="1", canonical_id="X",
            side="BUY", price=0.50, qty=100,  # notional=$50 > $10
        )


async def test_phase4_hardlock_allows_within_cap():
    client = _mock_client()
    adapter = ForecastExAdapter(client=client, phase4_max_usd=100.0)
    order = await adapter.place_fok(
        arb_id="a", market_id="1", canonical_id="X",
        side="BUY", price=0.50, qty=10,  # $5
    )
    assert order.status == OrderStatus.FILLED
    client.place_order.assert_awaited_once()


async def test_phase5_hardlock_blocks_oversized():
    adapter = ForecastExAdapter(
        client=_mock_client(),
        phase4_max_usd=1000.0,
        phase5_max_usd=20.0,
    )
    with pytest.raises(OrderRejected, match="PHASE5 hard-lock"):
        await adapter.place_fok(
            arb_id="a", market_id="1", canonical_id="X",
            side="BUY", price=0.50, qty=100,
        )


async def test_phase4_evaluated_before_phase5():
    adapter = ForecastExAdapter(
        client=_mock_client(),
        phase4_max_usd=10.0,
        phase5_max_usd=20.0,
    )
    # Both would trip; PHASE4 must fire first.
    with pytest.raises(OrderRejected, match="PHASE4 hard-lock"):
        await adapter.place_fok(
            arb_id="a", market_id="1", canonical_id="X",
            side="BUY", price=0.50, qty=100,
        )


async def test_supervisor_armed_blocks():
    supervisor = SimpleNamespace(is_armed=True)
    adapter = ForecastExAdapter(client=_mock_client(), supervisor=supervisor)
    with pytest.raises(OrderRejected, match="supervisor armed"):
        await adapter.place_fok(
            arb_id="a", market_id="1", canonical_id="X",
            side="BUY", price=0.50, qty=1,
        )


async def test_supervisor_disarmed_allows():
    supervisor = SimpleNamespace(is_armed=False)
    client = _mock_client()
    adapter = ForecastExAdapter(client=client, supervisor=supervisor)
    order = await adapter.place_fok(
        arb_id="a", market_id="1", canonical_id="X",
        side="BUY", price=0.50, qty=1,
    )
    assert order.status == OrderStatus.FILLED


# ── Price snapping / clamping ─────────────────────────────────────────────


def test_snap_to_tick_clamps_to_cent_range():
    assert ForecastExAdapter._snap_to_tick(0.00001) == 0.01
    assert ForecastExAdapter._snap_to_tick(0.9999) == 0.99
    assert ForecastExAdapter._snap_to_tick(0.5234) == 0.52
    assert ForecastExAdapter._snap_to_tick(0.5256) == 0.53


async def test_adapter_snaps_price_before_send():
    client = _mock_client()
    adapter = ForecastExAdapter(client=client)
    await adapter.place_fok(
        arb_id="a", market_id="1", canonical_id="X",
        side="BUY", price=0.5733, qty=1,
    )
    kwargs = client.place_order.await_args.kwargs
    assert kwargs["price"] == 0.57
    assert kwargs["tif"] == "IOC"


async def test_adapter_rejects_when_above_max_affordable():
    adapter = ForecastExAdapter(client=_mock_client())
    with pytest.raises(OrderRejected, match="max_affordable"):
        await adapter.place_fok(
            arb_id="a", market_id="1", canonical_id="X",
            side="BUY", price=0.60, qty=1, max_affordable=0.50,
        )


# ── Conid integrity gate (phantom-trade postmortem 2026-05-28) ───────────
# These tests lock in the defensive checks that prevent the ARB-000695/699
# class of bug — where the collector failed NO-conid discovery and the
# scanner/engine still tried to place an order with empty/garbage market_id.


async def test_adapter_rejects_empty_market_id():
    adapter = ForecastExAdapter(client=_mock_client())
    with pytest.raises(OrderRejected, match="empty market_id"):
        await adapter.place_fok(
            arb_id="arb-x", market_id="", canonical_id="HORC",
            side="no", price=0.50, qty=1,
        )


async def test_adapter_rejects_zero_market_id():
    adapter = ForecastExAdapter(client=_mock_client())
    with pytest.raises(OrderRejected, match="empty market_id"):
        await adapter.place_ioc(
            arb_id="arb-x", market_id="0", canonical_id="HORC",
            side="no", price=0.50, qty=1,
        )


async def test_adapter_rejects_none_string_market_id():
    """Defensive: stringified None ('None') must be refused like empty."""
    adapter = ForecastExAdapter(client=_mock_client())
    with pytest.raises(OrderRejected, match="empty market_id"):
        await adapter.place_fok(
            arb_id="arb-x", market_id="None", canonical_id="HORC",
            side="no", price=0.50, qty=1,
        )


async def test_check_depth_rejects_empty_market_id():
    """check_depth must not issue an API call when market_id is empty —
    that's the signal the collector failed NO-conid discovery and the
    engine should treat the leg as unfillable."""
    client = _mock_client()
    adapter = ForecastExAdapter(client=client)
    sufficient, price = await adapter.check_depth(
        market_id="", side="no", required_qty=10,
    )
    assert sufficient is False
    assert price == 0.0
    # Critical: we MUST NOT have called the snapshot API with empty conid.
    client.market_snapshot.assert_not_awaited()


async def test_check_depth_rejects_zero_market_id():
    client = _mock_client()
    adapter = ForecastExAdapter(client=client)
    sufficient, price = await adapter.check_depth(
        market_id="0", side="no", required_qty=10,
    )
    assert sufficient is False
    assert price == 0.0
    client.market_snapshot.assert_not_awaited()


async def test_check_depth_uses_passed_market_id_not_yes_synth():
    """check_depth fetches the snapshot for the conid it was passed —
    no implicit "subtract from 1" synthesis. When the caller asks for the
    NO side with the NO conid, the returned price is the NO conid's own
    ask, not 1 - YES bid.
    """
    from unittest.mock import AsyncMock as _AM
    client = _mock_client()
    # NO snapshot returns ask=0.55. If the adapter mistakenly synthesized
    # from yes_bid, the returned price would NOT match this 0.55.
    client.market_snapshot = _AM(return_value={"84": "0.48", "86": "0.55", "7295": "0", "7296": "0"})
    adapter = ForecastExAdapter(client=client)
    sufficient, price = await adapter.check_depth(
        market_id="no-conid-12345", side="no", required_qty=5,
    )
    # Trusted touch (size=0) returns True with the real ASK.
    assert sufficient is True
    assert abs(price - 0.55) < 1e-9
    client.market_snapshot.assert_awaited_once_with("no-conid-12345")


# ── Status mapping ────────────────────────────────────────────────────────


def test_map_status_filled():
    assert ForecastExAdapter._map_status("Filled", OrderStatus.SUBMITTED) == OrderStatus.FILLED


def test_map_status_partial():
    assert ForecastExAdapter._map_status("PartiallyFilled", OrderStatus.SUBMITTED) == OrderStatus.PARTIAL


def test_map_status_cancelled():
    assert ForecastExAdapter._map_status("Cancelled", OrderStatus.SUBMITTED) == OrderStatus.CANCELLED


def test_map_status_rejected_to_failed():
    assert ForecastExAdapter._map_status("Rejected", OrderStatus.SUBMITTED) == OrderStatus.FAILED


def test_map_status_unknown_fails_when_default_submitted():
    # Unknown wire status must not silently leave order in SUBMITTED state.
    assert ForecastExAdapter._map_status("WEIRD_NEW_STATE", OrderStatus.SUBMITTED) == OrderStatus.FAILED


def test_map_status_unknown_preserves_terminal():
    assert ForecastExAdapter._map_status("WEIRD_NEW_STATE", OrderStatus.FILLED) == OrderStatus.FILLED


# ── Order construction from response ──────────────────────────────────────


async def test_order_from_filled_response():
    client = _mock_client(place_response={
        "order_id": "id-42",
        "order_status": "Filled",
        "filled_quantity": 10,
        "avg_price": 0.55,
    })
    adapter = ForecastExAdapter(client=client)
    order = await adapter.place_fok(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="BUY", price=0.55, qty=10,
    )
    assert order.order_id == "id-42"
    assert order.status == OrderStatus.FILLED
    assert order.fill_qty == 10
    assert abs(order.fill_price - 0.55) < 1e-9


async def test_order_from_filled_response_rescales_avg_price_in_cents():
    client = _mock_client(place_response={
        "order_id": "id",
        "order_status": "Filled",
        "filled_quantity": 5,
        "avg_price": "55.0",  # cents
    })
    adapter = ForecastExAdapter(client=client)
    order = await adapter.place_fok(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="BUY", price=0.55, qty=5,
    )
    assert abs(order.fill_price - 0.55) < 1e-9


async def test_place_order_exception_returns_failed_order():
    client = _mock_client(place_exc=RuntimeError("gateway down"))
    adapter = ForecastExAdapter(client=client)
    order = await adapter.place_fok(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="BUY", price=0.55, qty=1,
    )
    assert order.status == OrderStatus.FAILED
    assert "gateway down" in order.error


async def test_place_order_emits_place_ms_telemetry(capsys):
    """V19: ForecastEx BUY path must emit a ``forecastex.order.placed`` log
    line carrying ``place_ms`` (per-leg wire latency) so operators can
    diagnose venue vs network slowness from the live-fire trace.
    """
    client = _mock_client(place_response={
        "order_id": "id-1",
        "order_status": "Filled",
        "filled_quantity": 1,
        "avg_price": 0.55,
    })
    adapter = ForecastExAdapter(client=client)
    await adapter.place_fok(
        arb_id="arb-LAT", market_id="111", canonical_id="X",
        side="BUY", price=0.55, qty=1,
    )
    out = capsys.readouterr().out
    assert "forecastex.order.placed" in out, (
        f"expected forecastex.order.placed in stdout; got: {out!r}"
    )
    assert "place_ms" in out, (
        f"forecastex.order.placed missing place_ms field; got: {out!r}"
    )


# ── check_depth / best_executable_price ───────────────────────────────────


async def test_check_depth_returns_true_when_size_sufficient():
    client = _mock_client()  # ask=47 (cents), 7296 (ask size) = 20
    adapter = ForecastExAdapter(client=client)
    sufficient, price = await adapter.check_depth("111", "BUY", 10)
    assert sufficient is True
    assert 0.46 < price < 0.48


async def test_check_depth_returns_false_when_size_insufficient():
    client = _mock_client()
    adapter = ForecastExAdapter(client=client)
    sufficient, _ = await adapter.check_depth("111", "BUY", 100)
    assert sufficient is False


async def test_check_depth_returns_zero_when_no_quote():
    client = _mock_client()
    client.market_snapshot = AsyncMock(return_value={})
    adapter = ForecastExAdapter(client=client)
    sufficient, price = await adapter.check_depth("111", "BUY", 1)
    assert sufficient is False
    assert price == 0.0


async def test_check_depth_trusts_touch_when_size_unbroadcast():
    """IBKR Client Portal returns 7296=0 on ForecastEx binary political
    contracts even when the book has real depth. We trust the ask and
    let the IOC primary + partial-fill scaling handle thin-book risk."""
    client = _mock_client()
    client.market_snapshot = AsyncMock(
        return_value={"84": "44", "86": "47", "7295": "0", "7296": "0"}
    )
    adapter = ForecastExAdapter(client=client)
    sufficient, price = await adapter.check_depth("111", "BUY", 160)
    assert sufficient is True
    assert 0.46 < price < 0.48


# ── cancel + list_open_orders pass-through ────────────────────────────────


async def test_cancel_order_delegates():
    client = _mock_client()
    adapter = ForecastExAdapter(client=client)
    order = Order(
        order_id="o-1", platform="forecastex", market_id="m", canonical_id="X",
        side="BUY", price=0.5, quantity=1, status=OrderStatus.SUBMITTED,
    )
    ok = await adapter.cancel_order(order)
    assert ok is True
    client.cancel_order.assert_awaited_with("o-1")


async def test_cancel_all_delegates():
    client = _mock_client()
    adapter = ForecastExAdapter(client=client)
    cancelled = await adapter.cancel_all()
    assert cancelled == ["x", "y"]


async def test_list_open_orders_by_client_id_returns_empty():
    adapter = ForecastExAdapter(client=_mock_client())
    assert await adapter.list_open_orders_by_client_id("ARB-") == []


# ── Sell-side: side-alias acceptance ──────────────────────────────────────


async def test_place_unwind_sell_accepts_sell_yes_side_alias():
    """The engine may pass either 'yes' (original bought side) or 'SELL_YES'
    (post-translation by callers that don't know the venue is BUY-symmetric).
    Both must route to a SELL on the same conid."""
    client = _mock_client()
    adapter = ForecastExAdapter(client=client)
    await adapter.place_unwind_sell(
        arb_id="a", market_id="111", canonical_id="X",
        side="SELL_YES", qty=5,
    )
    kwargs = client.place_order.await_args.kwargs
    assert kwargs["side"] == "SELL"

# ── Sell-side (place_resting_sell / place_unwind_sell) ───────────────────


async def test_place_resting_sell_uses_gtc_and_sells_same_conid():
    """Resting sell should call client.place_order with side=SELL and GTC.

    The same conid we bought is the one we sell — IBKR position accounting
    handles the offset; no opposite-leg routing required.
    """
    client = _mock_client(place_response={
        "order_id": "rest-1", "order_status": "Submitted",
        "filled_quantity": 0, "avg_price": 0.0,
    })
    adapter = ForecastExAdapter(client=client)
    order = await adapter.place_resting_sell(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="yes", price=0.55, qty=10,
    )
    assert order.order_id == "rest-1"
    assert order.status == OrderStatus.SUBMITTED
    kwargs = client.place_order.await_args.kwargs
    assert kwargs["side"] == "SELL"
    assert kwargs["conid"] == "111"
    assert kwargs["tif"] == "GTC"
    assert kwargs["price"] == 0.55
    assert kwargs["quantity"] == 10


async def test_place_unwind_sell_uses_ioc_at_panic_price():
    client = _mock_client(place_response={
        "order_id": "unw-1", "order_status": "Filled",
        "filled_quantity": 10, "avg_price": 0.02,
    })
    adapter = ForecastExAdapter(client=client)
    order = await adapter.place_unwind_sell(
        arb_id="arb-1", market_id="222", canonical_id="X",
        side="no", qty=10, panic_price=0.01,
    )
    assert order.status == OrderStatus.FILLED
    assert order.fill_qty == 10
    kwargs = client.place_order.await_args.kwargs
    assert kwargs["side"] == "SELL"
    assert kwargs["tif"] == "IOC"
    assert kwargs["price"] == 0.01


async def test_place_unwind_sell_default_panic_price_is_one_cent():
    client = _mock_client()
    adapter = ForecastExAdapter(client=client)
    await adapter.place_unwind_sell(
        arb_id="arb-1", market_id="333", canonical_id="X",
        side="yes", qty=5,
    )
    assert client.place_order.await_args.kwargs["price"] == 0.01


async def test_place_resting_sell_snaps_price():
    client = _mock_client()
    adapter = ForecastExAdapter(client=client)
    await adapter.place_resting_sell(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="yes", price=0.5733, qty=1,
    )
    assert client.place_order.await_args.kwargs["price"] == 0.57


async def test_place_resting_sell_skips_phase4_hardlock():
    """Closing exposure must NEVER be blocked by gates that stop opening.

    Phase4 cap of $10 would block a $50 BUY but must not block a sell of
    the same notional — otherwise a naked leg can never be unwound.
    """
    client = _mock_client()
    adapter = ForecastExAdapter(client=client, phase4_max_usd=10.0)
    order = await adapter.place_resting_sell(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="yes", price=0.50, qty=100,  # $50 notional, > $10 cap
    )
    assert order.status == OrderStatus.FILLED  # default mock response
    client.place_order.assert_awaited_once()


async def test_place_unwind_sell_skips_phase5_hardlock():
    client = _mock_client()
    adapter = ForecastExAdapter(
        client=client,
        phase4_max_usd=1000.0,
        phase5_max_usd=5.0,
    )
    order = await adapter.place_unwind_sell(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="yes", qty=100,  # $1 notional at default 0.01 — but check still skipped
    )
    assert order.status == OrderStatus.FILLED
    client.place_order.assert_awaited_once()


async def test_place_unwind_sell_skips_supervisor_armed():
    """Supervisor-armed must not block close-exposure paths."""
    supervisor = SimpleNamespace(is_armed=True)
    client = _mock_client()
    adapter = ForecastExAdapter(client=client, supervisor=supervisor)
    order = await adapter.place_unwind_sell(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="yes", qty=10, panic_price=0.01,
    )
    assert order.status == OrderStatus.FILLED
    client.place_order.assert_awaited_once()


async def test_place_resting_sell_skips_supervisor_armed():
    supervisor = SimpleNamespace(is_armed=True)
    client = _mock_client()
    adapter = ForecastExAdapter(client=client, supervisor=supervisor)
    order = await adapter.place_resting_sell(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="yes", price=0.50, qty=10,
    )
    assert order.status == OrderStatus.FILLED
    client.place_order.assert_awaited_once()


async def test_place_unwind_sell_returns_failed_on_client_exception():
    """Sell path must never propagate exceptions across the boundary —
    the engine relies on a terminal Order to drive recovery."""
    client = _mock_client(place_exc=RuntimeError("gateway 503"))
    adapter = ForecastExAdapter(client=client)
    order = await adapter.place_unwind_sell(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="yes", qty=5, panic_price=0.01,
    )
    assert order.status == OrderStatus.FAILED
    assert "gateway 503" in order.error
    assert "place_unwind_sell" in order.error


async def test_place_resting_sell_returns_failed_on_client_exception():
    client = _mock_client(place_exc=RuntimeError("auth lost"))
    adapter = ForecastExAdapter(client=client)
    order = await adapter.place_resting_sell(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="yes", price=0.50, qty=5,
    )
    assert order.status == OrderStatus.FAILED
    assert "auth lost" in order.error


async def test_place_resting_sell_partial_fill_status():
    """A resting sell may take an instant partial when there's already a
    crossing bid for some of the qty."""
    client = _mock_client(place_response={
        "order_id": "rest-partial",
        "order_status": "PartiallyFilled",
        "filled_quantity": 3,
        "avg_price": 0.55,
    })
    adapter = ForecastExAdapter(client=client)
    order = await adapter.place_resting_sell(
        arb_id="arb-1", market_id="111", canonical_id="X",
        side="yes", price=0.55, qty=10,
    )
    assert order.status == OrderStatus.PARTIAL
    assert order.fill_qty == 3
