"""AutoExecutor + FailureTracker gate."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbiter.execution.auto_executor import AutoExecutor, AutoExecutorConfig
from arbiter.execution.failure_tracker import FailureTracker, FailureTrackerConfig
from arbiter.scanner.arbitrage import ArbitrageOpportunity


@dataclass
class _Mapping:
    canonical_id: str = "TEST-MKT"
    allow_auto_trade: bool = True


class _MappingStore:
    def __init__(self, mapping: _Mapping):
        self._m = mapping

    async def get(self, canonical_id: str):
        return self._m


class _Scanner:
    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()

    def subscribe(self) -> asyncio.Queue:
        return self._q


def _opp(yes_price=0.40, no_price=0.60):
    return ArbitrageOpportunity(
        canonical_id="TEST-MKT",
        description="t",
        yes_platform="kalshi",
        yes_price=yes_price,
        yes_fee=0.01,
        yes_market_id="K",
        no_platform="polymarket",
        no_price=no_price,
        no_fee=0.02,
        no_market_id="P",
        gross_edge=0.05,
        total_fees=0.03,
        net_edge=0.02,
        net_edge_cents=2.0,
        suggested_qty=5,
        max_profit_usd=0.10,
        timestamp=0.0,
        confidence=0.9,
        arb_type="cross_platform",
        status="ready",
        persistence_count=3,
        quote_age_seconds=1.0,
        min_available_liquidity=100.0,
        mapping_status="confirmed",
        mapping_score=0.95,
    )


def _make_ae(tracker):
    engine = SimpleNamespace(
        execute_opportunity=AsyncMock(
            return_value=SimpleNamespace(arb_id="ARB-1", realized_pnl=0.05, status="filled"),
        ),
    )
    cfg = AutoExecutorConfig(
        enabled=True,
        max_position_usd=50.0,
        min_edge_cents_preflight=1.0,
        require_mapping_confirmed=False,
    )
    return AutoExecutor(
        scanner=_Scanner(),
        engine=engine,
        supervisor=SimpleNamespace(is_armed=False, armed_by=None),
        mapping_store=_MappingStore(_Mapping()),
        config=cfg,
        failure_tracker=tracker,
    ), engine


@pytest.mark.asyncio
async def test_failure_tracker_blocks_when_threshold_tripped():
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=3),
    )
    for _ in range(3):
        tracker._record_failure("TEST-MKT", "yes", 0.40)

    ae, engine = _make_ae(tracker)
    await ae._consider_opportunity(_opp(yes_price=0.40))
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_failure_pattern == 1


@pytest.mark.asyncio
async def test_failure_tracker_lets_through_when_under_threshold():
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=3),
    )
    tracker._record_failure("TEST-MKT", "yes", 0.40)  # only 1 < 3
    ae, engine = _make_ae(tracker)
    await ae._consider_opportunity(_opp(yes_price=0.40))
    engine.execute_opportunity.assert_awaited_once()
    assert ae.stats.skipped_failure_pattern == 0


@pytest.mark.asyncio
async def test_failure_tracker_blocks_on_no_leg():
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=2),
    )
    # Trip on the NO side only.
    tracker._record_failure("TEST-MKT", "no", 0.60)
    tracker._record_failure("TEST-MKT", "no", 0.60)
    ae, engine = _make_ae(tracker)
    await ae._consider_opportunity(_opp())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_failure_pattern == 1
