"""Tests for ``arbiter.execution.stuck_trade_recovery``.

The recovery loop reads from Postgres and writes back; for unit-test scope
we mock both the store and the adapters. The integration test against a
real Postgres lives in the sandbox suite.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.engine import Order, OrderStatus
from arbiter.execution.stuck_trade_recovery import (
    StuckTradeRecoveryStats,
    _derive_arb_status_from_legs,
    _persist_outcome,
    list_stuck_arbs,
    recover_stuck_trades,
)


def _leg_row(
    order_id: str = "ARB-1-YES-aa",
    arb_id: str = "ARB-1",
    platform: str = "kalshi",
    side: str = "yes",
    status: str = "submitted",
    price: float = 0.42,
):
    return {
        "order_id": order_id,
        "arb_id": arb_id,
        "platform": platform,
        "market_id": "K-MKT",
        "canonical_id": "MKT_X",
        "side": side,
        "price": price,
        "quantity": 10,
        "status": status,
        "fill_price": 0.0,
        "fill_qty": 0,
        "error": "",
        "submitted_at": datetime.now(timezone.utc) - timedelta(days=5),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=5),
        "terminal_at": None,
    }


def _stuck_arb(
    arb_id: str = "ARB-1",
    status: str = "recovering",
    age_seconds: float = 5 * 86400,
    legs=None,
):
    return {
        "arb_id": arb_id,
        "canonical_id": "MKT_X",
        "status": status,
        "created_at": datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        "updated_at": datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        "last_checked_at": None,
        "recovery_notes": "",
        "age_seconds": age_seconds,
        "legs": legs or [_leg_row()],
    }


def _make_store(stuck_arbs=None, monkeypatch=None):
    """Return a stand-in store whose ``list_stuck_arbs`` returns the given rows.

    We patch the function in the recovery module so we don't have to mock
    the asyncpg pool. ``upsert_order`` and the persist hook are mocked.
    """
    store = MagicMock()
    store._pool = MagicMock()
    store.upsert_order = AsyncMock(return_value=None)
    return store


def _make_adapter(get_order_returns=None, get_order_raises=None):
    adapter = MagicMock()
    if get_order_raises is not None:
        adapter.get_order = AsyncMock(side_effect=get_order_raises)
    else:
        adapter.get_order = AsyncMock(return_value=get_order_returns)
    return adapter


@pytest.fixture(autouse=True)
def _patch_list_and_persist(monkeypatch):
    """Default to empty results + no-op persist; individual tests override."""
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery.list_stuck_arbs",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery._persist_outcome",
        AsyncMock(return_value=None),
    )


def test_derive_status_both_filled():
    assert _derive_arb_status_from_legs(
        [OrderStatus.FILLED, OrderStatus.FILLED]
    ) == "filled"


def test_derive_status_both_failed():
    assert _derive_arb_status_from_legs(
        [OrderStatus.FAILED, OrderStatus.CANCELLED]
    ) == "failed"


def test_derive_status_one_filled_one_aborted_is_recovering():
    # Survivor leg is still naked.
    assert _derive_arb_status_from_legs(
        [OrderStatus.FILLED, OrderStatus.ABORTED]
    ) == "recovering"


def test_derive_status_single_filled_leg_stays_manual():
    # A half-recorded arb with one filled leg is real exposure, not a filled
    # arb. Startup recovery must keep it as a manual blocker.
    assert _derive_arb_status_from_legs([OrderStatus.FILLED]) == ""


def test_derive_status_unchanged_when_leg_nonterminal():
    assert _derive_arb_status_from_legs(
        [OrderStatus.SUBMITTED, OrderStatus.FILLED]
    ) == ""


@pytest.mark.asyncio
async def test_recover_stuck_trades_no_rows(monkeypatch):
    stats = StuckTradeRecoveryStats()
    out = await recover_stuck_trades(_make_store(), {}, stats=stats)
    assert out == []
    assert stats.runs == 1
    assert stats.inspected_last_run == 0


@pytest.mark.asyncio
async def test_recover_stuck_trades_reconciles_to_filled(monkeypatch):
    rows = [
        _stuck_arb(
            arb_id="ARB-1",
            status="recovering",
            legs=[
                _leg_row(order_id="ARB-1-YES-a", side="yes", status="submitted"),
                _leg_row(order_id="ARB-1-NO-b", platform="polymarket", side="no", status="submitted"),
            ],
        ),
    ]
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery.list_stuck_arbs",
        AsyncMock(return_value=rows),
    )

    persist = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery._persist_outcome", persist,
    )

    filled_yes = Order(
        order_id="ARB-1-YES-a", platform="kalshi", market_id="K-MKT",
        canonical_id="MKT_X", side="yes", price=0.42, quantity=10,
        status=OrderStatus.FILLED, fill_price=0.42, fill_qty=10,
    )
    filled_no = Order(
        order_id="ARB-1-NO-b", platform="polymarket", market_id="K-MKT",
        canonical_id="MKT_X", side="no", price=0.55, quantity=10,
        status=OrderStatus.FILLED, fill_price=0.55, fill_qty=10,
    )
    adapters = {
        "kalshi": _make_adapter(get_order_returns=filled_yes),
        "polymarket": _make_adapter(get_order_returns=filled_no),
    }
    store = _make_store()

    stats = StuckTradeRecoveryStats()
    outcomes = await recover_stuck_trades(store, adapters, stats=stats)

    assert len(outcomes) == 1
    assert outcomes[0].arb_id == "ARB-1"
    assert outcomes[0].original_status == "recovering"
    assert outcomes[0].new_status == "filled"
    assert stats.updated_last_run == 1
    # Both legs upserted because their status changed.
    assert store.upsert_order.await_count == 2
    persist.assert_awaited()


@pytest.mark.asyncio
async def test_recover_marks_orphan_when_adapter_raises(monkeypatch):
    rows = [_stuck_arb(arb_id="ARB-2", legs=[_leg_row(order_id="ARB-2-YES-x")])]
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery.list_stuck_arbs",
        AsyncMock(return_value=rows),
    )
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery._persist_outcome",
        AsyncMock(return_value=None),
    )
    adapters = {
        "kalshi": _make_adapter(get_order_raises=RuntimeError("404 not found"))
    }
    store = _make_store()

    outcomes = await recover_stuck_trades(store, adapters)
    assert len(outcomes) == 1
    leg_update = outcomes[0].leg_updates[0]
    assert "raised" in leg_update["note"].lower()
    # The orphaned leg gets persisted as FAILED.
    upserted = store.upsert_order.await_args.args[0]
    assert upserted.status == OrderStatus.FAILED
    assert "orphaned during recovery" in (upserted.error or "")


@pytest.mark.asyncio
async def test_recover_preserves_original_status_in_persist_note(monkeypatch):
    """The original status must end up in the note text so the audit trail
    is preserved even when ``status`` is overwritten."""
    rows = [_stuck_arb(arb_id="ARB-3", status="recovering")]
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery.list_stuck_arbs",
        AsyncMock(return_value=rows),
    )
    persist = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery._persist_outcome", persist,
    )
    # Adapter says FAILED -> arb status derives to "failed".
    failed = Order(
        order_id="ARB-3-YES-aa", platform="kalshi", market_id="K-MKT",
        canonical_id="MKT_X", side="yes", price=0.42, quantity=10,
        status=OrderStatus.FAILED,
    )
    adapters = {"kalshi": _make_adapter(get_order_returns=failed)}

    await recover_stuck_trades(_make_store(), adapters)
    persist.assert_awaited()
    kwargs = persist.await_args.kwargs
    assert kwargs["original_status"] == "recovering"
    assert "original=recovering" in kwargs["note"]


@pytest.mark.asyncio
async def test_recover_continues_on_per_arb_failure(monkeypatch):
    rows = [
        _stuck_arb(arb_id="ARB-A", legs=[_leg_row(order_id="ARB-A-YES")]),
        _stuck_arb(arb_id="ARB-B", legs=[_leg_row(order_id="ARB-B-YES")]),
    ]
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery.list_stuck_arbs",
        AsyncMock(return_value=rows),
    )
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery._persist_outcome",
        AsyncMock(return_value=None),
    )
    filled = Order(
        order_id="ARB-B-YES", platform="kalshi", market_id="K-MKT",
        canonical_id="MKT_X", side="yes", price=0.42, quantity=10,
        status=OrderStatus.FILLED, fill_price=0.42, fill_qty=10,
    )
    # First adapter call raises with a generic error that the per-leg
    # path treats as orphan -> FAILED; second adapter call succeeds.
    # Since the same adapter handles both arbs, raise once then succeed.
    adapter = MagicMock()
    adapter.get_order = AsyncMock(side_effect=[RuntimeError("transient"), filled])
    adapters = {"kalshi": adapter}

    out = await recover_stuck_trades(_make_store(), adapters)
    # Both arbs returned an outcome, neither call aborted the loop.
    assert {o.arb_id for o in out} == {"ARB-A", "ARB-B"}


@pytest.mark.asyncio
async def test_recover_returns_empty_list_when_list_raises(monkeypatch):
    monkeypatch.setattr(
        "arbiter.execution.stuck_trade_recovery.list_stuck_arbs",
        AsyncMock(side_effect=RuntimeError("DB down")),
    )
    stats = StuckTradeRecoveryStats()
    out = await recover_stuck_trades(_make_store(), {}, stats=stats)
    assert out == []
    assert stats.errors_last_run == 1


@pytest.mark.asyncio
async def test_persist_outcome_casts_new_status_for_postgres_type_inference():
    class CaptureConn:
        def __init__(self):
            self.calls = []

        async def execute(self, sql, *params):
            self.calls.append((sql, params))

    class AcquireCtx:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class CapturePool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return AcquireCtx(self.conn)

    conn = CaptureConn()
    store = MagicMock()
    store._pool = CapturePool(conn)

    await _persist_outcome(
        store,
        arb_id="ARB-1",
        new_status="filled",
        original_status="pending",
        note="recovery_check",
        now_ts=123.0,
    )

    sql, params = conn.calls[0]
    assert "$2::text" in sql
    assert params[:2] == ("ARB-1", "filled")


@pytest.mark.asyncio
async def test_list_stuck_arbs_treats_closed_as_terminal():
    class CaptureConn:
        def __init__(self):
            self.calls = []

        async def fetch(self, sql, *params):
            self.calls.append((sql, params))
            return []

    class AcquireCtx:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class CapturePool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return AcquireCtx(self.conn)

    conn = CaptureConn()
    store = MagicMock()
    store._pool = CapturePool(conn)

    rows = await list_stuck_arbs(store, max_age_seconds=86400)

    assert rows == []
    sql, params = conn.calls[0]
    assert "'closed'" in sql
    assert params == (86400.0,)
