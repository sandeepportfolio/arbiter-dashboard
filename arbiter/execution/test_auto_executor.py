"""Unit tests for AutoExecutor (Phase 6 Plan 06-01).

Policy-gate coverage:
    G1 AUTO_EXECUTE_ENABLED=false        -> skip
    G2 supervisor.is_armed                -> skip
    G3 opportunity.requires_manual        -> skip
    G4 mapping.allow_auto_trade is False  -> skip
    G5 duplicate opportunity              -> skip (second call)
    G6 notional > MAX_POSITION_USD        -> skip
    G7 bootstrap_trades cap reached       -> skip
    H  clean -> engine.execute_opportunity called exactly once
    I  engine.execute_opportunity raises  -> loop survives, failure counted
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbiter.execution.auto_executor import AutoExecutor, AutoExecutorConfig
from arbiter.scanner.arbitrage import ArbitrageOpportunity


@dataclass
class _FakeMapping:
    canonical_id: str = "TEST-MKT"
    allow_auto_trade: bool = True


class _FakeMappingStore:
    def __init__(self, mapping: _FakeMapping | None):
        self._mapping = mapping

    async def get(self, canonical_id: str):
        return self._mapping


class _FakeScanner:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()

    def subscribe(self) -> asyncio.Queue:
        return self._queue


def _make_opportunity(
    *,
    canonical_id: str = "TEST-MKT",
    yes_price: float = 0.40,
    no_price: float = 0.60,
    suggested_qty: int = 5,
    requires_manual: bool = False,
    mapping_status: str = "confirmed",
) -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        canonical_id=canonical_id,
        description="Test market",
        yes_platform="kalshi",
        yes_price=yes_price,
        yes_fee=0.01,
        yes_market_id="KALSHI-TEST",
        no_platform="polymarket",
        no_price=no_price,
        no_fee=0.02,
        no_market_id="POLY-TEST",
        gross_edge=0.05,
        total_fees=0.03,
        net_edge=0.02,
        net_edge_cents=2.0,
        suggested_qty=suggested_qty,
        max_profit_usd=0.10,
        timestamp=1776729648.0,
        confidence=0.9,
        arb_type="cross_platform",
        status="ready",
        persistence_count=3,
        quote_age_seconds=1.0,
        min_available_liquidity=100.0,
        mapping_status=mapping_status,
        mapping_score=0.95,
        requires_manual=requires_manual,
    )


def _make_components(
    *,
    enabled: bool = True,
    is_armed: bool = False,
    mapping: _FakeMapping | None = _FakeMapping(),
    max_position_usd: float = 10.0,
    bootstrap_trades: int | None = None,
):
    scanner = _FakeScanner()
    engine = SimpleNamespace(execute_opportunity=AsyncMock(return_value=SimpleNamespace(arb_id="ARB-1", realized_pnl=0.05)))
    supervisor = SimpleNamespace(is_armed=is_armed, armed_by=None)
    mapping_store = _FakeMappingStore(mapping)
    cfg = AutoExecutorConfig(
        enabled=enabled,
        max_position_usd=max_position_usd,
        bootstrap_trades=bootstrap_trades,
        dedup_window_seconds=5,
    )
    ae = AutoExecutor(
        scanner=scanner,
        engine=engine,
        supervisor=supervisor,
        mapping_store=mapping_store,
        config=cfg,
    )
    return ae, engine


@pytest.mark.asyncio
async def test_disabled_skips_execute():
    ae, engine = _make_components(enabled=False)
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_disabled == 1
    assert ae.stats.executed == 0


@pytest.mark.asyncio
async def test_armed_supervisor_skips_execute():
    ae, engine = _make_components(is_armed=True)
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_armed == 1


@pytest.mark.asyncio
async def test_requires_manual_skips_execute():
    ae, engine = _make_components()
    await ae._consider_opportunity(_make_opportunity(requires_manual=True))
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_requires_manual == 1


@pytest.mark.asyncio
async def test_mapping_disallowed_skips_execute():
    ae, engine = _make_components(mapping=_FakeMapping(allow_auto_trade=False))
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_not_allowed == 1


@pytest.mark.asyncio
async def test_missing_mapping_skips_execute():
    ae, engine = _make_components(mapping=None)
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_not_allowed == 1


@pytest.mark.asyncio
async def test_notional_over_cap_skips_execute():
    # yes_price=0.40, no_price=0.60, qty=50 => max-leg notional = 30 > 10
    ae, engine = _make_components(max_position_usd=10.0)
    await ae._consider_opportunity(_make_opportunity(suggested_qty=50))
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_over_cap == 1


@pytest.mark.asyncio
async def test_bootstrap_cap_limits_executions():
    ae, engine = _make_components(bootstrap_trades=2)
    for i in range(4):
        opp = _make_opportunity(canonical_id=f"TEST-MKT-{i}")
        await ae._consider_opportunity(opp)
    # Only 2 executions allowed before bootstrap-full
    assert engine.execute_opportunity.await_count == 2
    assert ae.stats.executed == 2
    assert ae.stats.skipped_bootstrap_full == 2


@pytest.mark.asyncio
async def test_duplicate_within_dedup_window_skips_second():
    ae, engine = _make_components()
    opp = _make_opportunity()
    await ae._consider_opportunity(opp)
    await ae._consider_opportunity(opp)  # same window -> duplicate
    assert engine.execute_opportunity.await_count == 1
    assert ae.stats.skipped_duplicate == 1


@pytest.mark.asyncio
async def test_clean_opportunity_executes():
    ae, engine = _make_components()
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_awaited_once()
    assert ae.stats.executed == 1
    assert ae.stats.failures == 0


@pytest.mark.asyncio
async def test_engine_exception_is_caught_and_counted():
    ae, engine = _make_components()
    engine.execute_opportunity = AsyncMock(side_effect=RuntimeError("boom"))
    await ae._consider_opportunity(_make_opportunity())
    assert ae.stats.failures == 1
    assert ae.stats.executed == 0


@pytest.mark.asyncio
async def test_start_stop_lifecycle():
    ae, engine = _make_components()
    await ae.start()
    assert ae._running is True
    # Put one opportunity on the scanner queue
    scanner_queue: asyncio.Queue = ae._queue
    await scanner_queue.put(_make_opportunity(canonical_id="LIFECYCLE"))
    # Give loop a tick to consume
    await asyncio.sleep(0.05)
    await ae.stop()
    assert ae._running is False
    # The opportunity should have been considered (possibly executed)
    assert ae.stats.considered >= 1


# ─── Pre-flight check coverage ──────────────────────────────────────────


class _FakePricePoint:
    def __init__(
        self,
        *,
        yes_price: float,
        no_price: float,
        age_seconds: float = 1.0,
        fee_rate: float = 0.0,
        yes_market_id: str = "",
        no_market_id: str = "",
    ):
        self.yes_price = yes_price
        self.no_price = no_price
        self.age_seconds = age_seconds
        self.fee_rate = fee_rate
        self.yes_market_id = yes_market_id
        self.no_market_id = no_market_id


class _FakePriceStore:
    """Returns a configured PricePoint per (platform, canonical_id)."""

    def __init__(self, points: dict[tuple[str, str], _FakePricePoint]):
        self._points = points

    async def get(self, platform: str, canonical_id: str):
        return self._points.get((platform, canonical_id))


class _FakeAdapter:
    def __init__(self, *, sufficient: bool = True, best_price: float = 0.50):
        self._sufficient = sufficient
        self._best_price = best_price
        self.calls: list[tuple[str, str, int]] = []

    async def check_depth(self, market_id: str, side: str, qty: int):
        self.calls.append((market_id, side, qty))
        return (self._sufficient, self._best_price)


def _make_components_with_preflight(
    *,
    price_store=None,
    adapters: dict | None = None,
    max_quote_age_s: float = 30.0,
    min_depth_usd: float = 25.0,
    min_edge_cents_preflight: float = 3.0,
    require_mapping_confirmed: bool = False,
    mapping: _FakeMapping | None = _FakeMapping(),
):
    scanner = _FakeScanner()
    engine = SimpleNamespace(
        execute_opportunity=AsyncMock(
            return_value=SimpleNamespace(arb_id="ARB-1", realized_pnl=0.05, status="filled"),
        ),
    )
    supervisor = SimpleNamespace(is_armed=False, armed_by=None)
    mapping_store = _FakeMappingStore(mapping)
    cfg = AutoExecutorConfig(
        enabled=True,
        max_position_usd=100.0,
        bootstrap_trades=None,
        dedup_window_seconds=5,
        max_quote_age_s=max_quote_age_s,
        min_depth_usd=min_depth_usd,
        min_edge_cents_preflight=min_edge_cents_preflight,
        require_mapping_confirmed=require_mapping_confirmed,
    )
    ae = AutoExecutor(
        scanner=scanner,
        engine=engine,
        supervisor=supervisor,
        mapping_store=mapping_store,
        config=cfg,
        price_store=price_store,
        adapters_provider=(lambda: adapters) if adapters is not None else None,
    )
    return ae, engine


@pytest.mark.asyncio
async def test_preflight_skips_on_missing_quotes():
    price_store = _FakePriceStore({})  # nothing in store
    ae, engine = _make_components_with_preflight(price_store=price_store)
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_stale_quote == 1


@pytest.mark.asyncio
async def test_preflight_skips_on_stale_quote():
    pts = {
        ("kalshi", "TEST-MKT"): _FakePricePoint(yes_price=0.40, no_price=0.55, age_seconds=120.0),
        ("polymarket", "TEST-MKT"): _FakePricePoint(yes_price=0.40, no_price=0.55, age_seconds=2.0),
    }
    ae, engine = _make_components_with_preflight(
        price_store=_FakePriceStore(pts), max_quote_age_s=30.0,
    )
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_stale_quote == 1


@pytest.mark.asyncio
async def test_preflight_skips_when_edge_collapses_at_fresh_prices():
    # Cached edge in opportunity says 5¢ gross; fresh prices yield 0¢ gross.
    pts = {
        ("kalshi", "TEST-MKT"): _FakePricePoint(yes_price=0.50, no_price=0.50, age_seconds=2.0),
        ("polymarket", "TEST-MKT"): _FakePricePoint(yes_price=0.50, no_price=0.50, age_seconds=2.0),
    }
    ae, engine = _make_components_with_preflight(
        price_store=_FakePriceStore(pts), min_edge_cents_preflight=3.0,
    )
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_edge_collapsed == 1


@pytest.mark.asyncio
async def test_preflight_skips_when_orderbook_depth_low():
    pts = {
        ("kalshi", "TEST-MKT"): _FakePricePoint(yes_price=0.40, no_price=0.55, age_seconds=2.0),
        ("polymarket", "TEST-MKT"): _FakePricePoint(yes_price=0.40, no_price=0.55, age_seconds=2.0),
    }
    adapters = {
        "kalshi": _FakeAdapter(sufficient=False, best_price=0.40),
        "polymarket": _FakeAdapter(sufficient=True, best_price=0.55),
    }
    ae, engine = _make_components_with_preflight(
        price_store=_FakePriceStore(pts),
        adapters=adapters,
        min_depth_usd=25.0,
        min_edge_cents_preflight=1.0,  # don't trip the edge gate
    )
    await ae._consider_opportunity(_make_opportunity(suggested_qty=10))
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_depth_low == 1


@pytest.mark.asyncio
async def test_preflight_passes_executes():
    pts = {
        ("kalshi", "TEST-MKT"): _FakePricePoint(yes_price=0.40, no_price=0.55, age_seconds=2.0),
        ("polymarket", "TEST-MKT"): _FakePricePoint(yes_price=0.40, no_price=0.55, age_seconds=2.0),
    }
    adapters = {
        "kalshi": _FakeAdapter(sufficient=True, best_price=0.40),
        "polymarket": _FakeAdapter(sufficient=True, best_price=0.55),
    }
    ae, engine = _make_components_with_preflight(
        price_store=_FakePriceStore(pts), adapters=adapters,
        min_edge_cents_preflight=1.0,
    )
    await ae._consider_opportunity(_make_opportunity(suggested_qty=10))
    engine.execute_opportunity.assert_awaited_once()
    assert ae.stats.executed == 1
    # Both adapters should have been queried for depth.
    assert adapters["kalshi"].calls and adapters["polymarket"].calls


@pytest.mark.asyncio
async def test_require_mapping_confirmed_skips_unconfirmed():
    ae, engine = _make_components_with_preflight(
        require_mapping_confirmed=True,
        mapping=_FakeMapping(allow_auto_trade=True),  # default no .status field
    )
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_mapping_unconfirmed == 1


@pytest.mark.asyncio
async def test_require_mapping_confirmed_passes_confirmed():
    confirmed_mapping = _FakeMapping(allow_auto_trade=True)
    confirmed_mapping.status = "confirmed"  # type: ignore[attr-defined]
    ae, engine = _make_components_with_preflight(
        require_mapping_confirmed=True,
        mapping=confirmed_mapping,
    )
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_awaited_once()
