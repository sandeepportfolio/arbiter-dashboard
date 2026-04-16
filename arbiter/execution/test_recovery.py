"""Tests for ``arbiter.execution.recovery.reconcile_non_terminal_orders``."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.engine import Order, OrderStatus
from arbiter.execution.recovery import _derive_arb_id, reconcile_non_terminal_orders


def _order(
    order_id: str = "ARB-000001-YES-deadbeef",
    platform: str = "kalshi",
    status: OrderStatus = OrderStatus.SUBMITTED,
) -> Order:
    return Order(
        order_id=order_id,
        platform=platform,
        market_id="M",
        canonical_id="C",
        side="yes",
        price=0.5,
        quantity=10,
        status=status,
    )


def _make_store(non_terminal=None):
    store = MagicMock()
    store.list_non_terminal_orders = AsyncMock(return_value=non_terminal or [])
    store.upsert_order = AsyncMock(return_value=None)
    return store


def _make_adapter(get_order_returns=None, get_order_raises=None):
    adapter = MagicMock()
    if get_order_raises is not None:
        adapter.get_order = AsyncMock(side_effect=get_order_raises)
    else:
        adapter.get_order = AsyncMock(return_value=get_order_returns)
    return adapter


def test_derive_arb_id_recognizes_standard():
    assert _derive_arb_id("ARB-000123-YES-abcd1234") == "ARB-000123"
    assert _derive_arb_id("MANUAL-X") == "MANUAL-X"
    assert _derive_arb_id("") == ""


@pytest.mark.asyncio
async def test_reconcile_returns_empty_when_no_non_terminal_orders():
    store = _make_store(non_terminal=[])
    adapters = {"kalshi": _make_adapter()}
    result = await reconcile_non_terminal_orders(store, adapters)
    assert result == []
    assert not adapters["kalshi"].get_order.called


@pytest.mark.asyncio
async def test_reconcile_updates_db_when_status_changed():
    db_order = _order(status=OrderStatus.SUBMITTED)
    fresh_order = _order(status=OrderStatus.FILLED)
    store = _make_store(non_terminal=[db_order])
    adapters = {"kalshi": _make_adapter(get_order_returns=fresh_order)}
    result = await reconcile_non_terminal_orders(store, adapters)
    assert result == []  # not orphaned, just stale
    store.upsert_order.assert_awaited()
    upserted_order = store.upsert_order.await_args.args[0]
    assert upserted_order.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_reconcile_marks_orphaned_when_get_order_raises():
    db_order = _order(status=OrderStatus.SUBMITTED)
    store = _make_store(non_terminal=[db_order])
    adapters = {
        "kalshi": _make_adapter(get_order_raises=RuntimeError("404 not found")),
    }
    result = await reconcile_non_terminal_orders(store, adapters)
    assert len(result) == 1
    assert result[0].order_id == db_order.order_id
    assert result[0].status == OrderStatus.FAILED
    assert "orphaned" in (result[0].error or "").lower()
    store.upsert_order.assert_awaited()


@pytest.mark.asyncio
async def test_reconcile_marks_orphaned_when_adapter_returns_not_found():
    db_order = _order(status=OrderStatus.SUBMITTED)
    not_found = _order(status=OrderStatus.FAILED)
    not_found.error = "not found on platform"
    store = _make_store(non_terminal=[db_order])
    adapters = {"kalshi": _make_adapter(get_order_returns=not_found)}
    result = await reconcile_non_terminal_orders(store, adapters)
    assert len(result) == 1
    assert result[0].error == "not found on platform"


@pytest.mark.asyncio
async def test_reconcile_skips_when_no_adapter_for_platform():
    db_order = _order(platform="unknown_platform")
    store = _make_store(non_terminal=[db_order])
    adapters = {"kalshi": _make_adapter()}  # no "unknown_platform" key
    result = await reconcile_non_terminal_orders(store, adapters)
    assert result == []
    assert not store.upsert_order.called


@pytest.mark.asyncio
async def test_reconcile_no_op_when_status_unchanged():
    db_order = _order(status=OrderStatus.SUBMITTED)
    fresh_order = _order(status=OrderStatus.SUBMITTED)
    store = _make_store(non_terminal=[db_order])
    adapters = {"kalshi": _make_adapter(get_order_returns=fresh_order)}
    result = await reconcile_non_terminal_orders(store, adapters)
    assert result == []
    # No upsert because status unchanged (and not orphaned)
    assert not store.upsert_order.called


@pytest.mark.asyncio
async def test_reconcile_continues_when_list_non_terminal_raises():
    store = MagicMock()
    store.list_non_terminal_orders = AsyncMock(side_effect=RuntimeError("DB down"))
    adapters = {"kalshi": _make_adapter()}
    result = await reconcile_non_terminal_orders(store, adapters)
    assert result == []  # graceful empty return; no exception propagated


@pytest.mark.asyncio
async def test_reconcile_processes_multiple_orders_independently():
    """A failure on one order does not prevent others from being reconciled."""
    o1 = _order(order_id="ARB-000001-YES-aaa", platform="kalshi")
    o2 = _order(order_id="ARB-000002-NO-bbb", platform="polymarket")
    store = _make_store(non_terminal=[o1, o2])

    kalshi = _make_adapter(get_order_raises=RuntimeError("kalshi down"))
    fresh_o2 = _order(
        order_id="ARB-000002-NO-bbb", platform="polymarket",
        status=OrderStatus.FILLED,
    )
    poly = _make_adapter(get_order_returns=fresh_o2)
    adapters = {"kalshi": kalshi, "polymarket": poly}

    result = await reconcile_non_terminal_orders(store, adapters)
    # o1 is orphaned; o2 is reconciled (not orphaned)
    assert len(result) == 1
    assert result[0].order_id == "ARB-000001-YES-aaa"
    # upsert called for both: o1 (orphaned -> FAILED) and o2 (status change)
    assert store.upsert_order.await_count == 2
