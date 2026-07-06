"""Verify-before-unwind: close the reverse-exposure hole.

When the secondary leg dies AMBIGUOUSLY (local timeout with failed orphan
cancel, unproven empty lookup, or an error saying the order MAY still be
live at the venue), the engine previously unwound the primary immediately.
A late secondary fill after that unwind flips the book into net-short
reverse exposure with no owner.

New contract for ``_recover_one_leg_risk``:
- ambiguous/may-be-live secondary -> re-verify at the venue via
  ``adapter.get_order`` BEFORE selling the primary;
- proven dead -> unwind proceeds as before;
- proven late-filled and balanced -> NO unwind (the arb actually completed);
- still unprovable -> NO unwind; a critical ``unwind_blocked_ambiguous_secondary``
  incident routes it to manual reconciliation and exposure stays booked.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from arbiter.config.settings import ArbiterConfig
from arbiter.execution.engine import ExecutionEngine, Order, OrderStatus
from arbiter.monitor.balance import BalanceMonitor
from arbiter.scanner.arbitrage import ArbitrageOpportunity


def _make_engine() -> ExecutionEngine:
    config = ArbiterConfig()
    config.scanner.dry_run = True
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0
    config.safety.max_platform_exposure_usd = 1_000_000.0
    monitor = BalanceMonitor(
        config.alerts, {"kalshi": object(), "polymarket": object()},
    )
    engine = ExecutionEngine(config, monitor, collectors={})
    engine._smart_unwind_timeout_s = 0.0  # skip the 30s resting phase
    return engine


def _make_opp(canonical: str = "MKT") -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        canonical_id=canonical,
        description="Test market",
        yes_platform="kalshi",
        yes_price=0.45,
        yes_fee_rate=0.0,
        yes_fee=0.0,
        yes_market_id="K-YES",
        no_platform="polymarket",
        no_price=0.50,
        no_fee=0.0,
        no_market_id="P-NO",
        gross_edge=0.05,
        total_fees=0.0,
        net_edge=0.05,
        net_edge_cents=5.0,
        suggested_qty=10,
        timestamp=0.0,
    )


def _filled_yes() -> Order:
    return Order(
        order_id="K-123", platform="kalshi", market_id="K-YES",
        canonical_id="MKT", side="yes", price=0.45, quantity=10,
        status=OrderStatus.FILLED, fill_price=0.45, fill_qty=10,
    )


def _dead_no(*, ambiguous: bool, error: str = "",
             status: OrderStatus = OrderStatus.CANCELLED) -> Order:
    o = Order(
        order_id="P-456", platform="polymarket", market_id="P-NO",
        canonical_id="MKT", side="no", price=0.50, quantity=10,
        status=status, fill_qty=0, error=error,
    )
    o.idempotency_ambiguous = ambiguous
    return o


def _adapters(engine, *, get_order_behavior):
    kalshi = MagicMock()
    kalshi.platform = "kalshi"
    kalshi.cancel_order = AsyncMock(return_value=True)
    unwind_filled = Order(
        order_id="UNW-1", platform="kalshi", market_id="K-YES",
        canonical_id="MKT", side="yes", price=0.01, quantity=10,
        status=OrderStatus.FILLED, fill_price=0.45, fill_qty=10,
    )
    kalshi.place_unwind_sell = AsyncMock(return_value=unwind_filled)
    kalshi.place_resting_sell = AsyncMock(return_value=unwind_filled)

    poly = MagicMock()
    poly.platform = "polymarket"
    poly.cancel_order = AsyncMock(return_value=True)
    poly.place_unwind_sell = AsyncMock()
    poly.get_order = get_order_behavior

    engine.adapters = {"kalshi": kalshi, "polymarket": poly}
    return kalshi, poly


def _drain_incidents(queue) -> list:
    incidents = []
    while not queue.empty():
        incidents.append(queue.get_nowait())
    return incidents



def _unwind_calls(kalshi) -> int:
    return kalshi.place_resting_sell.await_count + kalshi.place_unwind_sell.await_count

# ─── Unverifiable -> blocked ─────────────────────────────────────────────


def test_ambiguous_secondary_blocks_primary_unwind_when_unverifiable():
    async def runner():
        engine = _make_engine()

        async def _still_ambiguous(order):
            order.idempotency_ambiguous = True
            return order

        kalshi, _poly = _adapters(engine, get_order_behavior=_still_ambiguous)
        queue = engine.subscribe_incidents()

        leg_yes = _filled_yes()
        leg_no = _dead_no(ambiguous=True)
        notes, unwind_pnl = await engine._recover_one_leg_risk(
            "ARB-VB1", _make_opp(), leg_yes, leg_no,
        )

        assert _unwind_calls(kalshi) == 0
        assert unwind_pnl == 0.0
        assert any("unwind-blocked" in n for n in notes)
        incidents = _drain_incidents(queue)
        assert any(
            (getattr(i, "metadata", None) or {}).get("event_type")
            == "unwind_blocked_ambiguous_secondary"
            for i in incidents
        )

    asyncio.run(runner())


def test_get_order_exception_blocks_unwind():
    async def runner():
        engine = _make_engine()
        kalshi, _poly = _adapters(
            engine,
            get_order_behavior=AsyncMock(side_effect=RuntimeError("venue 503")),
        )
        leg_yes = _filled_yes()
        leg_no = _dead_no(ambiguous=True)
        notes, unwind_pnl = await engine._recover_one_leg_risk(
            "ARB-VB2", _make_opp(), leg_yes, leg_no,
        )
        assert _unwind_calls(kalshi) == 0
        assert unwind_pnl == 0.0
        assert any("unwind-blocked" in n for n in notes)

    asyncio.run(runner())


def test_cancel_failed_error_string_triggers_verification():
    """Even without the ambiguous flag, an error that says the venue cancel
    failed means the order MAY be live — verification must run and, when
    it cannot prove death, block the unwind."""
    async def runner():
        engine = _make_engine()

        async def _still_unknown(order):
            order.idempotency_ambiguous = True
            return order

        kalshi, poly = _adapters(engine, get_order_behavior=_still_unknown)
        leg_yes = _filled_yes()
        leg_no = _dead_no(
            ambiguous=False,
            status=OrderStatus.FAILED,
            error="timeout; orphan found but cancel failed — order MAY be live",
        )
        notes, _ = await engine._recover_one_leg_risk(
            "ARB-VB3", _make_opp(), leg_yes, leg_no,
        )
        assert _unwind_calls(kalshi) == 0
        assert any("unwind-blocked" in n for n in notes)

    asyncio.run(runner())


# ─── Proven dead -> unwind proceeds ──────────────────────────────────────


def test_ambiguous_secondary_verified_dead_proceeds_to_unwind():
    async def runner():
        engine = _make_engine()

        async def _proven_dead(order):
            order.status = OrderStatus.CANCELLED
            order.fill_qty = 0
            order.idempotency_ambiguous = False
            return order

        kalshi, _poly = _adapters(engine, get_order_behavior=_proven_dead)
        leg_yes = _filled_yes()
        leg_no = _dead_no(ambiguous=True)
        notes, _ = await engine._recover_one_leg_risk(
            "ARB-VB4", _make_opp(), leg_yes, leg_no,
        )
        assert _unwind_calls(kalshi) > 0
        assert any(n.startswith("verify-secondary:dead") for n in notes)

    asyncio.run(runner())


# ─── Late fill -> no unwind (arb actually completed) ─────────────────────


def test_late_secondary_fill_balanced_skips_unwind():
    async def runner():
        engine = _make_engine()

        async def _late_filled(order):
            order.status = OrderStatus.FILLED
            order.fill_qty = 10
            order.fill_price = 0.50
            order.idempotency_ambiguous = False
            return order

        kalshi, _poly = _adapters(engine, get_order_behavior=_late_filled)
        leg_yes = _filled_yes()
        leg_no = _dead_no(ambiguous=True)
        notes, unwind_pnl = await engine._recover_one_leg_risk(
            "ARB-VB5", _make_opp(), leg_yes, leg_no,
        )
        assert _unwind_calls(kalshi) == 0
        assert unwind_pnl == 0.0
        assert any("late-fill" in n for n in notes)

    asyncio.run(runner())


# ─── Non-ambiguous legs keep today's behavior ────────────────────────────


def test_clean_reject_still_unwinds_without_verification():
    """A definitively-dead secondary (no ambiguity markers) must not incur
    a verification round-trip — unwind fires exactly as before."""
    async def runner():
        engine = _make_engine()
        get_order = AsyncMock()
        kalshi, poly = _adapters(engine, get_order_behavior=get_order)
        leg_yes = _filled_yes()
        leg_no = _dead_no(ambiguous=False, status=OrderStatus.FAILED,
                          error="ORDER_STATE_REJECTED")
        notes, _ = await engine._recover_one_leg_risk(
            "ARB-VB6", _make_opp(), leg_yes, leg_no,
        )
        assert _unwind_calls(kalshi) > 0
        get_order.assert_not_awaited()

    asyncio.run(runner())


# ─── Partial secondary: unwind only the unpaired difference ──────────────


def test_partial_secondary_unwinds_only_difference():
    """Primary FILLED 10, secondary PARTIAL 6/10: recovery must cancel the
    partial remainder and unwind exactly 4 on the primary — the paired 6
    stay on. (engine's both-filled qty-mismatch branch; previously had
    zero coverage.)"""
    async def runner():
        engine = _make_engine()

        kalshi = MagicMock()
        kalshi.platform = "kalshi"
        kalshi.cancel_order = AsyncMock(return_value=True)
        unwind_4 = Order(
            order_id="UNW-4", platform="kalshi", market_id="K-YES",
            canonical_id="MKT", side="yes", price=0.01, quantity=4,
            status=OrderStatus.FILLED, fill_price=0.45, fill_qty=4,
        )
        kalshi.place_resting_sell = AsyncMock(return_value=unwind_4)
        kalshi.place_unwind_sell = AsyncMock(return_value=unwind_4)

        poly = MagicMock()
        poly.platform = "polymarket"
        poly.cancel_order = AsyncMock(return_value=True)
        poly.place_unwind_sell = AsyncMock()
        engine.adapters = {"kalshi": kalshi, "polymarket": poly}

        leg_yes = _filled_yes()  # FILLED 10 @ 0.45 on kalshi
        leg_no = Order(
            order_id="P-456", platform="polymarket", market_id="P-NO",
            canonical_id="MKT", side="no", price=0.50, quantity=10,
            status=OrderStatus.PARTIAL, fill_qty=6, fill_price=0.50,
        )

        notes, _ = await engine._recover_one_leg_risk(
            "ARB-PD1", _make_opp(), leg_yes, leg_no,
        )

        # The PARTIAL remainder was cancelled at the venue.
        poly.cancel_order.assert_awaited_once()
        # Unwind fired on the primary for EXACTLY the 4-contract diff.
        assert _unwind_calls(kalshi) > 0
        rest_call = kalshi.place_resting_sell.await_args
        unwind_call = kalshi.place_unwind_sell.await_args
        qty_used = None
        if rest_call is not None:
            qty_used = rest_call.args[5] if len(rest_call.args) > 5 else rest_call.kwargs.get("qty")
        elif unwind_call is not None:
            qty_used = unwind_call.args[4] if len(unwind_call.args) > 4 else unwind_call.kwargs.get("qty")
        assert qty_used == 4, f"expected unwind of the 4-contract diff, got {qty_used}"
        # The polymarket side (paired) is never unwound.
        poly.place_unwind_sell.assert_not_awaited()

    asyncio.run(runner())
