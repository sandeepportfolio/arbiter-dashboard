"""Tests for the rejected-retry cooldown in RetryScheduler.

The cooldown prevents the 19-identical-rejection loops observed against
Polymarket: when a (market, side, price-cent) tuple has been rejected
recently, the scheduler suppresses further retries until the book has
a chance to move.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import pytest

from arbiter.execution.engine import ArbExecution, Order, OrderStatus
from arbiter.execution.retry_scheduler import (
    RetryScheduler,
    RetrySchedulerConfig,
)
from arbiter.scanner.arbitrage import ArbitrageOpportunity


def _make_opp() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        canonical_id="CAN-1",
        description="test market",
        yes_platform="kalshi",
        yes_price=0.45,
        yes_fee=0.005,
        yes_market_id="KX-YES",
        no_platform="polymarket",
        no_price=0.50,
        no_fee=0.005,
        no_market_id="poly-no",
        gross_edge=0.05,
        total_fees=0.01,
        net_edge=0.04,
        net_edge_cents=4.0,
        suggested_qty=10,
        max_profit_usd=0.40,
        quote_age_seconds=0.1,
        timestamp=time.time(),
    )


def _failed_execution(arb_id: str, side: str, market: str, price: float) -> ArbExecution:
    opp = _make_opp()
    yes_leg = Order(
        order_id=f"{arb_id}-YES",
        platform="kalshi",
        market_id="KX-YES" if side == "yes" else market,
        canonical_id="CAN-1",
        side="yes",
        price=opp.yes_price if side != "yes" else price,
        quantity=10,
        status=OrderStatus.FAILED if side == "yes" else OrderStatus.ABORTED,
        timestamp=time.time(),
        error="polymarket_us ORDER_STATE_REJECTED" if side == "yes" else "Second leg skipped",
    )
    no_leg = Order(
        order_id=f"{arb_id}-NO",
        platform="polymarket",
        market_id=market if side == "no" else "poly-no",
        canonical_id="CAN-1",
        side="no",
        price=opp.no_price if side != "no" else price,
        quantity=10,
        status=OrderStatus.FAILED if side == "no" else OrderStatus.ABORTED,
        timestamp=time.time(),
        error="polymarket_us ORDER_STATE_REJECTED" if side == "no" else "",
    )
    return ArbExecution(
        arb_id=arb_id,
        opportunity=opp,
        leg_yes=yes_leg,
        leg_no=no_leg,
        status="failed",
        realized_pnl=0.0,
        timestamp=time.time(),
    )


class _StubEngine:
    """Just enough surface to satisfy ``RetryScheduler.__init__``."""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self.execute_calls: list = []

    def subscribe(self) -> asyncio.Queue:
        return self._queue

    async def execute_opportunity(self, opp):
        self.execute_calls.append(opp)
        return None  # exhaust naturally


class _StubPriceStore:
    async def get(self, *_):
        return None  # forces _requote to bail; not the focus of the cooldown tests


class _StubSupervisor:
    is_armed = False


def _make_scheduler(rejected_cooldown_s: float = 60.0) -> RetryScheduler:
    return RetryScheduler(
        engine=_StubEngine(),
        price_store=_StubPriceStore(),
        supervisor=_StubSupervisor(),
        config=RetrySchedulerConfig(
            max_retries=1,
            retry_delay_s=0.0,
            min_edge_cents_retry=0.0,
            max_quote_age_seconds=30.0,
            rejected_cooldown_s=rejected_cooldown_s,
        ),
    )


async def test_cooldown_suppresses_second_rejection_within_window():
    """Two rejections of the same (market, side, price) tuple inside the
    cooldown window: second one is suppressed and counted in stats."""
    sched = _make_scheduler(rejected_cooldown_s=60.0)

    exec1 = _failed_execution("ARB-1", side="no", market="slug-A", price=0.50)
    exec2 = _failed_execution("ARB-2", side="no", market="slug-A", price=0.50)

    await sched._handle_execution(exec1)
    initial_cooldown = sched.stats.skipped_cooldown
    await sched._handle_execution(exec2)

    assert sched.stats.skipped_cooldown == initial_cooldown + 1
    # The cooldown map carries the rejected tuple.
    assert ("slug-A", "no", 50) in sched._rejected_cooldowns


async def test_cooldown_expires_after_window():
    """Once the cooldown elapses, the next rejection is processed normally."""
    sched = _make_scheduler(rejected_cooldown_s=0.05)

    exec1 = _failed_execution("ARB-1", side="no", market="slug-B", price=0.50)
    exec2 = _failed_execution("ARB-2", side="no", market="slug-B", price=0.50)

    await sched._handle_execution(exec1)
    await asyncio.sleep(0.06)
    pre = sched.stats.skipped_cooldown
    await sched._handle_execution(exec2)

    # Second call after window: not counted as a cooldown skip.
    assert sched.stats.skipped_cooldown == pre


async def test_cooldown_distinguishes_price_buckets():
    """Different price-cent buckets do NOT collide in the cooldown map."""
    sched = _make_scheduler(rejected_cooldown_s=60.0)

    exec1 = _failed_execution("ARB-1", side="no", market="slug-C", price=0.50)
    exec2 = _failed_execution("ARB-2", side="no", market="slug-C", price=0.55)

    pre = sched.stats.skipped_cooldown
    await sched._handle_execution(exec1)
    await sched._handle_execution(exec2)

    # Neither was suppressed — they live in different buckets.
    assert sched.stats.skipped_cooldown == pre
    assert ("slug-C", "no", 50) in sched._rejected_cooldowns
    assert ("slug-C", "no", 55) in sched._rejected_cooldowns


async def test_cooldown_distinguishes_markets():
    """Different markets do NOT collide in the cooldown map."""
    sched = _make_scheduler(rejected_cooldown_s=60.0)

    exec1 = _failed_execution("ARB-1", side="no", market="slug-D1", price=0.50)
    exec2 = _failed_execution("ARB-2", side="no", market="slug-D2", price=0.50)

    pre = sched.stats.skipped_cooldown
    await sched._handle_execution(exec1)
    await sched._handle_execution(exec2)

    assert sched.stats.skipped_cooldown == pre
