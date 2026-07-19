"""Tests for the four fixes that unblocked live trading after 0/10 fills:

1. ``best_executable_price`` is used as the FOK limit price (instead of the
   opportunity's top-of-book price), so Kalshi cannot reject with
   ``fill_or_kill_insufficient_resting_volume`` when the visible book is
   fragmented across price levels.
2. ``INTER_LEG_DELAY_MS`` (default 500ms) gives the secondary venue's
   orderbook a chance to refresh between primary fill and secondary place.
3. The soft-naked pattern (primary FILLED + secondary SUBMITTED with
   ``fill_qty=0``) now triggers ``_recover_one_leg_risk`` instead of falling
   through to the silent ``status="submitted"`` path.
4. ``_recover_one_leg_risk`` invokes ``adapter.place_unwind_sell`` to attempt
   a reverse-order unwind on the FILLED leg.
"""
from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.config.settings import ArbiterConfig
from arbiter.execution.engine import (
    ExecutionEngine,
    Order,
    OrderStatus,
)
from arbiter.monitor.balance import BalanceMonitor
from arbiter.scanner.arbitrage import ArbitrageOpportunity


# ─── Fixtures ────────────────────────────────────────────────────────────


def _make_engine(*, inter_leg_delay_ms: str | None = None) -> ExecutionEngine:
    if inter_leg_delay_ms is not None:
        os.environ["INTER_LEG_DELAY_MS"] = inter_leg_delay_ms
    elif "INTER_LEG_DELAY_MS" in os.environ:
        del os.environ["INTER_LEG_DELAY_MS"]

    config = ArbiterConfig()
    config.scanner.dry_run = True
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0
    config.safety.max_platform_exposure_usd = 1_000_000.0
    monitor = BalanceMonitor(
        config.alerts,
        {"kalshi": object(), "polymarket": object()},
    )
    return ExecutionEngine(config, monitor, collectors={})


def _make_opp(canonical: str = "MKT") -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        canonical_id=canonical,
        description="Test market",
        yes_platform="kalshi",
        yes_price=0.55,
        yes_fee=0.005,
        yes_market_id="K-YES",
        no_platform="polymarket",
        no_price=0.40,
        no_fee=0.005,
        no_market_id="P-NO",
        gross_edge=0.05,
        total_fees=0.01,
        net_edge=0.04,
        net_edge_cents=4.0,
        suggested_qty=10,
        confidence=0.9,
        timestamp=time.time(),
        yes_fee_rate=0.01,
        no_fee_rate=0.01,
        mapping_status="confirmed",
    )


def _filled_order(side: str, platform: str, market_id: str, qty: int = 10,
                  price: float = 0.55) -> Order:
    return Order(
        order_id=f"ARB-1-{side.upper()}-FILLED",
        platform=platform,
        market_id=market_id,
        canonical_id="MKT",
        side=side,
        price=price,
        quantity=qty,
        status=OrderStatus.FILLED,
        fill_price=price,
        fill_qty=qty,
    )


def _submitted_unfilled_order(side: str, platform: str, market_id: str,
                              qty: int = 10, price: float = 0.40) -> Order:
    """SUBMITTED with fill_qty=0 — the soft-naked pattern."""
    return Order(
        order_id=f"ARB-1-{side.upper()}-SUBMITTED",
        platform=platform,
        market_id=market_id,
        canonical_id="MKT",
        side=side,
        price=price,
        quantity=qty,
        status=OrderStatus.SUBMITTED,
        fill_price=price,
        fill_qty=0,
    )


# ─── Inter-leg delay ─────────────────────────────────────────────────────


def test_inter_leg_delay_defaults_to_500ms():
    engine = _make_engine()
    assert engine._inter_leg_delay_ms == 500.0


def test_inter_leg_delay_reads_env_override():
    engine = _make_engine(inter_leg_delay_ms="1500")
    assert engine._inter_leg_delay_ms == 1500.0


def test_inter_leg_delay_unparseable_falls_back_to_default():
    engine = _make_engine(inter_leg_delay_ms="not-a-number")
    assert engine._inter_leg_delay_ms == 500.0


def test_inter_leg_delay_negative_clamped_to_zero():
    engine = _make_engine(inter_leg_delay_ms="-200")
    assert engine._inter_leg_delay_ms == 0.0


# ─── Soft-naked recovery ─────────────────────────────────────────────────


def test_recover_one_leg_risk_calls_unwind_on_filled_leg():
    """When YES is FILLED on Kalshi and NO is SUBMITTED-with-fill_qty=0 on
    Polymarket, recovery must:
    1. Cancel the still-resting NO leg, AND
    2. Call place_unwind_sell on the Kalshi adapter for the YES leg.
    """
    async def runner():
        engine = _make_engine()

        kalshi_adapter = MagicMock()
        kalshi_adapter.platform = "kalshi"
        kalshi_adapter.cancel_order = AsyncMock(return_value=True)
        unwind_filled = Order(
            order_id="ARB-1-UNWIND-YES",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="MKT",
            side="yes",
            price=0.01,
            quantity=10,
            status=OrderStatus.FILLED,
            fill_price=0.45,
            fill_qty=10,
        )
        kalshi_adapter.place_unwind_sell = AsyncMock(return_value=unwind_filled)

        poly_adapter = MagicMock()
        poly_adapter.platform = "polymarket"
        poly_adapter.cancel_order = AsyncMock(return_value=True)
        # Polymarket has place_unwind_sell too, but YES leg is on Kalshi
        # so it should NOT be invoked here.
        poly_adapter.place_unwind_sell = AsyncMock()

        engine.adapters = {"kalshi": kalshi_adapter, "polymarket": poly_adapter}

        opp = _make_opp()
        leg_yes = _filled_order("yes", "kalshi", "K-YES")
        leg_no = _submitted_unfilled_order("no", "polymarket", "P-NO")

        notes, _unwind_pnl = await engine._recover_one_leg_risk("ARB-1", opp, leg_yes, leg_no)

        # Hedge cancelled
        poly_adapter.cancel_order.assert_awaited_once()
        # Unwind called on the Kalshi adapter (filled leg)
        kalshi_adapter.place_unwind_sell.assert_awaited_once()
        # Polymarket's unwind NOT called (it had no fill)
        assert not poly_adapter.place_unwind_sell.await_count
        # Note recorded for both cancel + unwind
        cancel_notes = [n for n in notes if n.startswith("cancel-")]
        unwind_notes = [n for n in notes if n.startswith("unwind-")]
        assert len(cancel_notes) == 1
        assert len(unwind_notes) == 1
        assert "filled" in unwind_notes[0]

    asyncio.run(runner())


def test_recover_one_leg_risk_handles_unwind_exception_gracefully():
    """If place_unwind_sell raises, recovery must not propagate — the
    operator-facing incident is the safety net."""
    async def runner():
        engine = _make_engine()

        kalshi_adapter = MagicMock()
        kalshi_adapter.platform = "kalshi"
        kalshi_adapter.cancel_order = AsyncMock(return_value=True)
        kalshi_adapter.place_unwind_sell = AsyncMock(
            side_effect=RuntimeError("kaboom"),
        )

        poly_adapter = MagicMock()
        poly_adapter.platform = "polymarket"
        poly_adapter.cancel_order = AsyncMock(return_value=True)

        engine.adapters = {"kalshi": kalshi_adapter, "polymarket": poly_adapter}

        opp = _make_opp()
        leg_yes = _filled_order("yes", "kalshi", "K-YES")
        leg_no = _submitted_unfilled_order("no", "polymarket", "P-NO")

        notes, _unwind_pnl = await engine._recover_one_leg_risk("ARB-1", opp, leg_yes, leg_no)

        unwind_notes = [n for n in notes if n.startswith("unwind-")]
        assert len(unwind_notes) == 1
        assert "exception" in unwind_notes[0]

    asyncio.run(runner())


def test_recover_one_leg_risk_skips_unwind_when_no_filled_leg():
    """Recovery without a FILLED leg (e.g. both SUBMITTED, both PARTIAL)
    must not call place_unwind_sell — there's nothing to close."""
    async def runner():
        engine = _make_engine()

        kalshi_adapter = MagicMock()
        kalshi_adapter.platform = "kalshi"
        kalshi_adapter.cancel_order = AsyncMock(return_value=True)
        kalshi_adapter.place_unwind_sell = AsyncMock()

        poly_adapter = MagicMock()
        poly_adapter.platform = "polymarket"
        poly_adapter.cancel_order = AsyncMock(return_value=True)
        poly_adapter.place_unwind_sell = AsyncMock()

        engine.adapters = {"kalshi": kalshi_adapter, "polymarket": poly_adapter}

        opp = _make_opp()
        leg_yes = _submitted_unfilled_order("yes", "kalshi", "K-YES")
        leg_no = _submitted_unfilled_order("no", "polymarket", "P-NO")

        await engine._recover_one_leg_risk("ARB-1", opp, leg_yes, leg_no)

        kalshi_adapter.place_unwind_sell.assert_not_awaited()
        poly_adapter.place_unwind_sell.assert_not_awaited()

    asyncio.run(runner())


def test_recover_one_leg_risk_releases_exposure_on_successful_unwind():
    """A successful unwind must release the per-platform reservation so
    the risk manager's exposure tracker stays consistent."""
    async def runner():
        engine = _make_engine()

        kalshi_adapter = MagicMock()
        kalshi_adapter.platform = "kalshi"
        kalshi_adapter.cancel_order = AsyncMock(return_value=True)
        unwind_filled = Order(
            order_id="ARB-1-UNWIND-YES",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="MKT",
            side="yes",
            price=0.01,
            quantity=10,
            status=OrderStatus.FILLED,
            fill_price=0.45,
            fill_qty=10,
        )
        kalshi_adapter.place_unwind_sell = AsyncMock(return_value=unwind_filled)

        poly_adapter = MagicMock()
        poly_adapter.platform = "polymarket"
        poly_adapter.cancel_order = AsyncMock(return_value=True)

        engine.adapters = {"kalshi": kalshi_adapter, "polymarket": poly_adapter}

        # Pre-seed the kalshi exposure to 5.50 (the filled leg's notional).
        engine.risk._platform_exposures["kalshi"] = 5.50

        opp = _make_opp()
        leg_yes = _filled_order("yes", "kalshi", "K-YES", qty=10, price=0.55)
        leg_no = _submitted_unfilled_order("no", "polymarket", "P-NO")

        await engine._recover_one_leg_risk("ARB-1", opp, leg_yes, leg_no)

        # Unwound 10 contracts at original fill_price 0.55 → 5.50 released
        assert engine.risk._platform_exposures.get("kalshi", 0.0) == 0.0

    asyncio.run(runner())


def test_complete_unwind_writes_naked_leg_reconciled_sentinel():
    """A COMPLETE auto-unwind must self-reconcile via
    store.mark_naked_leg_reconciled — store.py's half-recorded mode-2 scan
    keys on the recovery sentinel (not on P&L), so without this every
    restart re-flags the asymmetric-fill arb as a trading-blocking critical
    (live: ARB-001362, 2026-07-18)."""
    async def runner():
        engine = _make_engine()

        kalshi_adapter = MagicMock()
        kalshi_adapter.platform = "kalshi"
        kalshi_adapter.cancel_order = AsyncMock(return_value=True)
        unwind_filled = Order(
            order_id="ARB-1-UNWIND-YES",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="MKT",
            side="yes",
            price=0.01,
            quantity=10,
            status=OrderStatus.FILLED,
            fill_price=0.55,
            fill_qty=10,
        )
        kalshi_adapter.place_unwind_sell = AsyncMock(return_value=unwind_filled)
        poly_adapter = MagicMock()
        poly_adapter.platform = "polymarket"
        poly_adapter.cancel_order = AsyncMock(return_value=True)
        engine.adapters = {"kalshi": kalshi_adapter, "polymarket": poly_adapter}

        store = MagicMock()
        store.mark_naked_leg_reconciled = AsyncMock()
        engine.store = store

        opp = _make_opp()
        leg_yes = _filled_order("yes", "kalshi", "K-YES", qty=10, price=0.55)
        leg_no = _submitted_unfilled_order("no", "polymarket", "P-NO")

        await engine._recover_one_leg_risk("ARB-1", opp, leg_yes, leg_no)

        store.mark_naked_leg_reconciled.assert_awaited_once()
        call = store.mark_naked_leg_reconciled.await_args
        assert call.args[0] == "ARB-1"
        # Break-even unwind (buy 0.55 sell 0.55) → unwind_pnl 0, and the
        # note must describe what the auto-unwind did.
        assert call.kwargs["unwind_pnl"] == 0.0
        assert "auto-unwind closed 10/10" in call.kwargs["note"]

    asyncio.run(runner())


def test_failed_unwind_does_not_write_reconcile_sentinel():
    """When the unwind can NOT close the position (adapter returns no fill),
    the arb must stay unreconciled so the restart scan DOES re-flag it —
    the sentinel is only for verified-flat outcomes."""
    async def runner():
        engine = _make_engine()

        kalshi_adapter = MagicMock()
        kalshi_adapter.platform = "kalshi"
        kalshi_adapter.cancel_order = AsyncMock(return_value=True)
        unwind_unfilled = Order(
            order_id="ARB-1-UNWIND-YES",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="MKT",
            side="yes",
            price=0.01,
            quantity=10,
            status=OrderStatus.FAILED,
            fill_price=0.0,
            fill_qty=0,
        )
        kalshi_adapter.place_unwind_sell = AsyncMock(return_value=unwind_unfilled)
        poly_adapter = MagicMock()
        poly_adapter.platform = "polymarket"
        poly_adapter.cancel_order = AsyncMock(return_value=True)
        engine.adapters = {"kalshi": kalshi_adapter, "polymarket": poly_adapter}

        store = MagicMock()
        store.mark_naked_leg_reconciled = AsyncMock()
        engine.store = store

        opp = _make_opp()
        leg_yes = _filled_order("yes", "kalshi", "K-YES", qty=10, price=0.55)
        leg_no = _submitted_unfilled_order("no", "polymarket", "P-NO")

        await engine._recover_one_leg_risk("ARB-1", opp, leg_yes, leg_no)

        store.mark_naked_leg_reconciled.assert_not_awaited()

    asyncio.run(runner())


# ─── C1: smart-unwind double-sell race ──────────────────────────────────
#
# Bug: after the poll loop times out without seeing a fill, the original
# code unconditionally market-sells the full qty after cancelling the
# resting order.  If the resting order fills in the window between the
# last poll and the cancel, the position closes flat at break-even on
# the resting fill AND ALSO sells at panic price on the market-IOC —
# leaving the engine net SHORT by ``qty`` at a worse-than-break-even
# price.  The fix re-reads the resting order one more time after cancel
# and only market-sells the truly unfilled remainder.


def _make_resting_order(qty: int = 10, price: float = 0.55,
                        status: OrderStatus = OrderStatus.SUBMITTED,
                        fill_qty: int = 0,
                        fill_price: float = 0.0) -> Order:
    return Order(
        order_id="ARB-1-UNWIND-REST",
        platform="kalshi",
        market_id="K-YES",
        canonical_id="MKT",
        side="yes",
        price=price,
        quantity=qty,
        status=status,
        fill_qty=fill_qty,
        fill_price=fill_price,
    )


def _make_filled_leg(qty: int = 10, price: float = 0.55) -> Order:
    return _filled_order("yes", "kalshi", "K-YES", qty=qty, price=price)


def test_smart_unwind_race_resting_fills_before_cancel_skips_market_sell():
    """C1 regression: resting fills in the gap between last poll and
    cancel.  Engine must detect the post-cancel FILLED state via
    ``get_order`` and skip the market-IOC entirely — otherwise it sells
    the position twice (once via the late resting fill, once via the
    fallback IOC) and ends up net SHORT.
    """
    async def runner():
        engine = _make_engine()
        engine._smart_unwind_timeout_s = 0.05  # tiny — force fall-through

        adapter = MagicMock()
        adapter.platform = "kalshi"

        resting_unfilled = _make_resting_order(qty=10, price=0.55)
        adapter.place_resting_sell = AsyncMock(return_value=resting_unfilled)

        # Poll never sees a fill.
        adapter.get_order = AsyncMock(side_effect=[
            _make_resting_order(qty=10, price=0.55,
                                status=OrderStatus.SUBMITTED),
            # POST-CANCEL recheck — resting actually filled in the race window.
            _make_resting_order(qty=10, price=0.55,
                                status=OrderStatus.FILLED,
                                fill_qty=10, fill_price=0.55),
        ])
        adapter.cancel_order = AsyncMock(return_value=True)
        adapter.place_unwind_sell = AsyncMock(
            return_value=_make_resting_order(
                qty=10, price=0.01,
                status=OrderStatus.FILLED,
                fill_qty=10, fill_price=0.30,
            )
        )

        engine.adapters = {"kalshi": adapter}

        filled_leg = _make_filled_leg(qty=10, price=0.55)
        result = await engine._smart_unwind("ARB-1", filled_leg, 10)

        # CRITICAL: the market-IOC must NOT have been called — the
        # resting order already closed the position when cancel raced.
        adapter.place_unwind_sell.assert_not_awaited()
        # The returned order is the (raced) resting fill.
        assert result.status == OrderStatus.FILLED
        assert result.fill_qty == 10
        assert result.fill_price == 0.55

    asyncio.run(runner())


def test_smart_unwind_race_resting_partially_filled_market_sells_only_remainder():
    """C1 regression: resting partially filled before cancel.  The
    market-IOC must close only the unfilled remainder, never the full
    qty.  The returned Order reports the combined fill (partial-rest +
    market) at a quantity-weighted blended price.
    """
    async def runner():
        engine = _make_engine()
        engine._smart_unwind_timeout_s = 0.05

        adapter = MagicMock()
        adapter.platform = "kalshi"

        resting_unfilled = _make_resting_order(qty=10, price=0.55)
        adapter.place_resting_sell = AsyncMock(return_value=resting_unfilled)

        adapter.get_order = AsyncMock(side_effect=[
            _make_resting_order(qty=10, status=OrderStatus.SUBMITTED),
            # POST-CANCEL recheck — venue partially filled (3 of 10)
            # before the cancel landed.
            _make_resting_order(qty=10, status=OrderStatus.CANCELLED,
                                fill_qty=3, fill_price=0.55),
        ])
        adapter.cancel_order = AsyncMock(return_value=True)
        # Market-IOC should be called for ONLY the 7 unfilled contracts.
        adapter.place_unwind_sell = AsyncMock(
            return_value=Order(
                order_id="ARB-1-UNWIND",
                platform="kalshi",
                market_id="K-YES",
                canonical_id="MKT",
                side="yes",
                price=0.01,
                quantity=7,
                status=OrderStatus.FILLED,
                fill_qty=7,
                fill_price=0.30,
            )
        )

        engine.adapters = {"kalshi": adapter}

        filled_leg = _make_filled_leg(qty=10, price=0.55)
        result = await engine._smart_unwind("ARB-1", filled_leg, 10)

        # Market-IOC called once, for qty=7 not 10.
        adapter.place_unwind_sell.assert_awaited_once()
        call_args = adapter.place_unwind_sell.await_args
        # signature: (arb_id, market_id, canonical_id, side, qty)
        assert call_args.args[4] == 7, (
            f"market-IOC must close only remainder qty=7, got {call_args.args[4]}"
        )

        # Returned order must reflect TOTAL close: 3 at $0.55 + 7 at $0.30
        # = 10 contracts at average $0.375.
        assert result.fill_qty == 10
        expected_blended = (3 * 0.55 + 7 * 0.30) / 10
        assert abs(result.fill_price - expected_blended) < 1e-6, (
            f"blended fill_price should be {expected_blended:.4f}, got {result.fill_price:.4f}"
        )

    asyncio.run(runner())


def test_smart_unwind_no_race_market_sells_full_qty_after_cancel():
    """C1 baseline: when the resting order genuinely had no fills before
    cancel (the normal timeout case), the market-IOC closes the full
    quantity — same behavior as before the fix.
    """
    async def runner():
        engine = _make_engine()
        engine._smart_unwind_timeout_s = 0.05

        adapter = MagicMock()
        adapter.platform = "kalshi"

        resting_unfilled = _make_resting_order(qty=10, price=0.55)
        adapter.place_resting_sell = AsyncMock(return_value=resting_unfilled)

        adapter.get_order = AsyncMock(side_effect=[
            _make_resting_order(qty=10, status=OrderStatus.SUBMITTED),
            # POST-CANCEL recheck — cleanly cancelled, no race.
            _make_resting_order(qty=10, status=OrderStatus.CANCELLED,
                                fill_qty=0),
        ])
        adapter.cancel_order = AsyncMock(return_value=True)
        adapter.place_unwind_sell = AsyncMock(
            return_value=Order(
                order_id="ARB-1-UNWIND",
                platform="kalshi",
                market_id="K-YES",
                canonical_id="MKT",
                side="yes",
                price=0.01,
                quantity=10,
                status=OrderStatus.FILLED,
                fill_qty=10,
                fill_price=0.30,
            )
        )

        engine.adapters = {"kalshi": adapter}

        filled_leg = _make_filled_leg(qty=10, price=0.55)
        result = await engine._smart_unwind("ARB-1", filled_leg, 10)

        # Market-IOC closes the full 10 contracts.
        adapter.place_unwind_sell.assert_awaited_once()
        call_args = adapter.place_unwind_sell.await_args
        assert call_args.args[4] == 10

        assert result.fill_qty == 10
        assert result.fill_price == 0.30

    asyncio.run(runner())


def test_smart_unwind_post_cancel_get_order_failure_falls_back_safely():
    """C1 defensive: if ``get_order`` raises after cancel (transient
    venue error), we must NOT panic-sell on stale knowledge of the
    resting state — we use the last-known state from the poll loop.
    If the last poll showed zero fills, the market-IOC closes the full
    qty (safe fallback).
    """
    async def runner():
        engine = _make_engine()
        engine._smart_unwind_timeout_s = 0.05

        adapter = MagicMock()
        adapter.platform = "kalshi"

        resting_unfilled = _make_resting_order(qty=10, price=0.55)
        adapter.place_resting_sell = AsyncMock(return_value=resting_unfilled)

        adapter.get_order = AsyncMock(side_effect=[
            _make_resting_order(qty=10, status=OrderStatus.SUBMITTED),
            RuntimeError("venue 502"),  # post-cancel recheck fails
        ])
        adapter.cancel_order = AsyncMock(return_value=True)
        adapter.place_unwind_sell = AsyncMock(
            return_value=Order(
                order_id="ARB-1-UNWIND",
                platform="kalshi",
                market_id="K-YES",
                canonical_id="MKT",
                side="yes",
                price=0.01,
                quantity=10,
                status=OrderStatus.FILLED,
                fill_qty=10,
                fill_price=0.30,
            )
        )

        engine.adapters = {"kalshi": adapter}

        filled_leg = _make_filled_leg(qty=10, price=0.55)
        result = await engine._smart_unwind("ARB-1", filled_leg, 10)

        # Last-known state from the poll loop was 0 fills, so market
        # sells the full qty.
        adapter.place_unwind_sell.assert_awaited_once()
        call_args = adapter.place_unwind_sell.await_args
        assert call_args.args[4] == 10
        assert result.fill_qty == 10

    asyncio.run(runner())


def test_smart_unwind_cancel_failure_still_rechecks_resting_state():
    """C1 defensive: cancel itself failing is the SCARIEST scenario —
    the resting order is still live on the venue and may fill any
    moment.  We must still call ``get_order`` once to see the latest
    state before deciding to market-sell.  If the live resting order
    has already filled (cancel failed because order was already done),
    skip the market-IOC.
    """
    async def runner():
        engine = _make_engine()
        engine._smart_unwind_timeout_s = 0.05

        adapter = MagicMock()
        adapter.platform = "kalshi"

        resting_unfilled = _make_resting_order(qty=10, price=0.55)
        adapter.place_resting_sell = AsyncMock(return_value=resting_unfilled)

        adapter.get_order = AsyncMock(side_effect=[
            _make_resting_order(qty=10, status=OrderStatus.SUBMITTED),
            # POST-CANCEL recheck (after cancel failed) — order had
            # already filled, that's why cancel reported failure.
            _make_resting_order(qty=10, status=OrderStatus.FILLED,
                                fill_qty=10, fill_price=0.55),
        ])
        adapter.cancel_order = AsyncMock(
            side_effect=RuntimeError("order already in terminal state"),
        )
        adapter.place_unwind_sell = AsyncMock()

        engine.adapters = {"kalshi": adapter}

        filled_leg = _make_filled_leg(qty=10, price=0.55)
        result = await engine._smart_unwind("ARB-1", filled_leg, 10)

        # Critical: even with cancel failing, we re-checked and saw the
        # resting order was already FILLED — no market-IOC sent.
        adapter.place_unwind_sell.assert_not_awaited()
        assert result.status == OrderStatus.FILLED
        assert result.fill_qty == 10
        assert result.fill_price == 0.55

    asyncio.run(runner())
