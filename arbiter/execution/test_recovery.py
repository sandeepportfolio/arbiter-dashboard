"""Tests for ``arbiter.execution.recovery`` reconciliation helpers."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.engine import Order, OrderStatus
from arbiter.execution.recovery import (
    RecoveryInitError,
    _derive_arb_id,
    reconcile_half_recorded_arbs,
    reconcile_non_terminal_orders,
)


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
async def test_reconcile_raises_when_list_non_terminal_fails():
    """H21: a failure to enumerate prior-state orders MUST surface to the
    caller. The previous return-[] behaviour let the engine start with no
    idea what was open on the platform — exactly the ghost-position
    scenario this module exists to prevent.
    """
    from arbiter.execution.recovery import RecoveryInitError

    store = MagicMock()
    store.list_non_terminal_orders = AsyncMock(side_effect=RuntimeError("DB down"))
    adapters = {"kalshi": _make_adapter()}
    with pytest.raises(RecoveryInitError):
        await reconcile_non_terminal_orders(store, adapters)


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


# ─── reconcile_half_recorded_arbs ──────────────────────────────────────

def _make_half_recorded_store(orphans=None, raise_exc=None):
    store = MagicMock()
    if raise_exc is not None:
        store.list_half_recorded_arbs = AsyncMock(side_effect=raise_exc)
    else:
        store.list_half_recorded_arbs = AsyncMock(return_value=orphans or [])
    store.insert_incident = AsyncMock(return_value=None)
    return store


@pytest.mark.asyncio
async def test_reconcile_half_recorded_arbs_no_orphans():
    store = _make_half_recorded_store(orphans=[])
    result = await reconcile_half_recorded_arbs(store)
    assert result == []
    assert not store.insert_incident.called


@pytest.mark.asyncio
async def test_reconcile_half_recorded_arbs_emits_critical_per_orphan():
    """For every half-recorded arb the function MUST persist exactly one
    critical ``half_recorded_arb`` ExecutionIncident so the operator is
    paged and can manually reconcile against the venue. Metadata captures
    the leg count, surviving order id(s), and the stuck status so the
    operator can locate the unmatched fill quickly."""
    orphans = [
        {
            "arb_id": "ARB-000491",
            "canonical_id": "MKT_NAKED",
            "status": "pending",
            "created_at": None,
            "leg_count": 1,
            "leg_order_ids": ["ARB-000491-YES-KALSHI"],
        },
        {
            "arb_id": "ARB-000492",
            "canonical_id": "MKT_NAKED_2",
            "status": "pending",
            "created_at": None,
            "leg_count": 0,
            "leg_order_ids": [],
        },
    ]
    store = _make_half_recorded_store(orphans=orphans)
    result = await reconcile_half_recorded_arbs(store)

    assert len(result) == 2
    assert store.insert_incident.await_count == 2

    incidents = [c.args[0] for c in store.insert_incident.await_args_list]
    severities = {i.severity for i in incidents}
    assert severities == {"critical"}

    event_types = {i.metadata.get("event_type") for i in incidents}
    assert event_types == {"half_recorded_arb"}

    by_arb = {i.arb_id: i for i in incidents}
    assert by_arb["ARB-000491"].metadata["leg_count"] == 1
    assert by_arb["ARB-000491"].metadata["leg_order_ids"] == [
        "ARB-000491-YES-KALSHI"
    ]
    assert by_arb["ARB-000491"].canonical_id == "MKT_NAKED"
    assert by_arb["ARB-000491"].metadata["stuck_status"] == "pending"
    assert by_arb["ARB-000492"].metadata["leg_count"] == 0


@pytest.mark.asyncio
async def test_reconcile_half_recorded_arbs_per_row_failure_does_not_abort_loop():
    """If insert_incident raises for one orphan, the loop MUST continue
    so a transient DB blip on row 1 doesn't hide rows 2..N. Otherwise an
    operator might page on one naked position and miss several others."""
    orphans = [
        {
            "arb_id": "ARB-000600",
            "canonical_id": "X",
            "status": "pending",
            "created_at": None,
            "leg_count": 1,
            "leg_order_ids": ["ARB-000600-YES-K"],
        },
        {
            "arb_id": "ARB-000601",
            "canonical_id": "Y",
            "status": "pending",
            "created_at": None,
            "leg_count": 1,
            "leg_order_ids": ["ARB-000601-YES-K"],
        },
    ]
    store = MagicMock()
    store.list_half_recorded_arbs = AsyncMock(return_value=orphans)
    # First insert raises, second must still be attempted.
    store.insert_incident = AsyncMock(
        side_effect=[RuntimeError("blip"), None]
    )

    result = await reconcile_half_recorded_arbs(store)
    assert len(result) == 2
    assert store.insert_incident.await_count == 2


@pytest.mark.asyncio
async def test_reconcile_half_recorded_arbs_raises_on_query_failure():
    """A failure to enumerate half-recorded arbs is a startup-blocking
    error — silently returning [] would hide naked exposures and let the
    engine start trading on top of unknown state."""
    store = _make_half_recorded_store(raise_exc=RuntimeError("DB down"))
    with pytest.raises(RecoveryInitError):
        await reconcile_half_recorded_arbs(store)
