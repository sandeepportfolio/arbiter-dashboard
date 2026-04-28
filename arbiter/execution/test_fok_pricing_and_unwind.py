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

        notes = await engine._recover_one_leg_risk("ARB-1", opp, leg_yes, leg_no)

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

        notes = await engine._recover_one_leg_risk("ARB-1", opp, leg_yes, leg_no)

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
