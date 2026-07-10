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
from arbiter.mapping.market_map import MappingStatus
from arbiter.scanner.arbitrage import ArbitrageOpportunity


@dataclass
class _FakeMapping:
    canonical_id: str = "TEST-MKT"
    allow_auto_trade: bool = True
    forecastex_not_available: bool = False
    forecastex_contract_id: str = "123456"


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


async def _wait_for(predicate, timeout: float = 1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


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
        # Use the legacy 3¢ preflight threshold here so the clamping /
        # cap / dedup tests stay isolated from the production 7¢ floor.
        # Production wires MIN_EDGE_CENTS=7 via the env, which is covered
        # by integration tests further down.
        min_edge_cents_preflight=3.0,
        # H15 default flipped require_mapping_confirmed to True for prod
        # safety; these gate-isolation tests must opt out so they exercise
        # the gate under test instead of the new confirmation gate.
        require_mapping_confirmed=False,
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
async def test_run_loop_consumes_approved_queue_not_raw_scanner_queue():
    scanner = _FakeScanner()
    approved_queue: asyncio.Queue = asyncio.Queue()
    engine = SimpleNamespace(
        execute_opportunity=AsyncMock(
            return_value=SimpleNamespace(
                arb_id="ARB-1", realized_pnl=0.05, status="filled",
            ),
        ),
    )
    ae = AutoExecutor(
        scanner=scanner,
        engine=engine,
        supervisor=SimpleNamespace(is_armed=False, armed_by=None),
        mapping_store=_FakeMappingStore(_FakeMapping()),
        config=AutoExecutorConfig(
            enabled=True,
            max_position_usd=10.0,
            dedup_window_seconds=5,
            min_edge_cents_preflight=3.0,
            require_mapping_confirmed=False,
        ),
        opportunity_queue=approved_queue,
    )

    await ae.start()
    try:
        await scanner._queue.put(_make_opportunity(canonical_id="RAW-SCANNER"))
        await asyncio.sleep(0.05)
        engine.execute_opportunity.assert_not_awaited()

        await approved_queue.put(_make_opportunity(canonical_id="APPROVED-ALERT"))
        await asyncio.wait_for(_wait_for(lambda: engine.execute_opportunity.await_count == 1), timeout=1.0)
        submitted = engine.execute_opportunity.await_args.args[0]
        assert submitted.canonical_id == "APPROVED-ALERT"
    finally:
        await ae.stop()


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
async def test_forecastex_unavailable_skips_fx_leg_trade():
    # Mapping flagged forecastex_not_available=True must block any opp where
    # a leg trades on ForecastEx — the discovery resolver may have bound the
    # canonical to a wrong-domain parent conid (e.g. NFL Eagles ↔
    # Philadelphia CPI) so we cannot trust prices it emits.
    ae, engine = _make_components(
        mapping=_FakeMapping(
            allow_auto_trade=True,
            forecastex_not_available=True,
            forecastex_contract_id="831072279",
        ),
    )
    opp = _make_opportunity()
    opp.yes_platform = "forecastex"
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_forecastex_unavailable == 1


@pytest.mark.asyncio
async def test_forecastex_empty_conid_skips_fx_leg_trade():
    # Mapping that lost (or never had) a forecastex_contract_id must not
    # try to place an order with a blank/zero contract — the IBKR adapter
    # would either error or, worse, accidentally route to the wrong
    # contract if a default exists.
    ae, engine = _make_components(
        mapping=_FakeMapping(
            allow_auto_trade=True,
            forecastex_contract_id="",
        ),
    )
    opp = _make_opportunity()
    opp.no_platform = "forecastex"
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_forecastex_unavailable == 1


@pytest.mark.asyncio
async def test_forecastex_skips_when_no_side_market_id_empty():
    """ForecastEx NO-side opportunities with empty no_market_id must
    NEVER reach the engine — collector failed NO-conid discovery and
    routing to the YES conid would buy the wrong contract (the exact
    ARB-000695/699 phantom-trade root cause).
    """
    ae, engine = _make_components(
        mapping=_FakeMapping(
            allow_auto_trade=True,
            forecastex_contract_id="111222",
        ),
    )
    opp = _make_opportunity()
    opp.no_platform = "forecastex"
    opp.no_market_id = ""  # collector failed to discover NO conid
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_forecastex_unavailable == 1


@pytest.mark.asyncio
async def test_forecastex_skips_when_yes_and_no_conid_collide():
    """3-way ForecastEx (both legs FX) where YES and NO share the same
    conid is the signature of the phantom-trade bug — the collector
    fell back to the YES conid for the NO market id. Refuse to trade.
    """
    ae, engine = _make_components(
        mapping=_FakeMapping(
            allow_auto_trade=True,
            forecastex_contract_id="111222",
        ),
    )
    opp = _make_opportunity()
    opp.yes_platform = "forecastex"
    opp.no_platform = "forecastex"
    opp.yes_market_id = "111222"
    opp.no_market_id = "111222"  # SAME conid — collision
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_forecastex_unavailable == 1


@pytest.mark.asyncio
async def test_forecastex_allows_distinct_yes_no_conids():
    """3-way FX with distinct YES and NO conids must trade normally."""
    ae, engine = _make_components(
        mapping=_FakeMapping(
            allow_auto_trade=True,
            forecastex_contract_id="111222",
        ),
    )
    opp = _make_opportunity()
    opp.yes_platform = "forecastex"
    opp.no_platform = "forecastex"
    opp.yes_market_id = "111222"
    opp.no_market_id = "111223"  # distinct sibling conid
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_awaited_once()
    assert ae.stats.skipped_forecastex_unavailable == 0


@pytest.mark.asyncio
async def test_forecastex_unavailable_does_not_block_kxp_only():
    # The unavailability gate must only fire when ForecastEx is actually a
    # leg of the opportunity. A K×P opportunity should still execute even
    # when the mapping has forecastex_not_available=True (no FX leg).
    ae, engine = _make_components(
        mapping=_FakeMapping(
            allow_auto_trade=True,
            forecastex_not_available=True,
        ),
    )
    # Default opp is kalshi×polymarket.
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_awaited_once()
    assert ae.stats.skipped_forecastex_unavailable == 0


@pytest.mark.asyncio
async def test_missing_mapping_skips_execute():
    ae, engine = _make_components(mapping=None)
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_not_allowed == 1


@pytest.mark.asyncio
async def test_notional_over_cap_clamps_before_execute():
    # yes_price=0.40, no_price=0.60, qty=50 => pair notional = 50 > 10;
    # AutoExecutor clamps to the per-trade cap, recomputes fees, then executes
    # only if the capped trade still clears the pre-flight edge threshold.
    ae, engine = _make_components(max_position_usd=10.0)
    opp = _make_opportunity(suggested_qty=50)
    opp.gross_edge = 0.06
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_awaited_once()
    executed_opp = engine.execute_opportunity.await_args.args[0]
    assert executed_opp.suggested_qty == 10
    assert executed_opp.suggested_qty * (executed_opp.yes_price + executed_opp.no_price) <= 10.0
    assert executed_opp.net_edge_cents == pytest.approx(3.34)
    assert ae.stats.executed == 1
    assert ae.stats.skipped_over_cap == 0


@pytest.mark.asyncio
async def test_notional_over_cap_skips_when_clamped_edge_is_below_threshold():
    ae, engine = _make_components(max_position_usd=10.0)
    await ae._consider_opportunity(_make_opportunity(suggested_qty=50))
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_over_cap == 1


@pytest.mark.asyncio
async def test_notional_over_cap_skips_when_clamped_edge_is_negative():
    ae, engine = _make_components(max_position_usd=10.0)
    opp = _make_opportunity(suggested_qty=50)
    opp.gross_edge = 0.01
    await ae._consider_opportunity(opp)
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
async def test_gate_structural_deny_skips_execute_with_flat_cooldown():
    """Regression (CV-03, 2026-06-05): when the engine trade-gate denies
    structurally (e.g. ``Platform profitability for forecastex is
    not_profitable``), AutoExecutor must NOT count it as a failure and
    grow the exponential failure cooldown — it's an operator-controlled
    veto, not a flaky execution. The probe runs BEFORE execute_opportunity,
    bumps ``skipped_gate_structural``, and a flat cooldown stops
    subsequent log-spam from the same (canonical_id, reason) for the
    cooldown window.
    """
    ae, engine = _make_components()
    engine.check_trade_gate = AsyncMock(return_value=(
        False, "Platform profitability for forecastex is not_profitable", {},
    ))
    opp = _make_opportunity()

    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_not_awaited()
    engine.check_trade_gate.assert_awaited_once()
    assert ae.stats.skipped_gate_structural == 1
    # NOT counted as a transient failure; exponential backoff must NOT engage.
    assert opp.canonical_id not in ae._failed_count
    assert opp.canonical_id not in ae._failed_cooldown
    # Flat cooldown registered for this (canonical_id, reason) pair.
    assert any(
        key[0] == opp.canonical_id for key in ae._gate_structural_cooldown
    )

    # Second consideration within cooldown: still skipped, NO additional
    # log spam (skipped_gate_structural increments but no new cooldown).
    # Drop the dedup record so the second consideration reaches the gate.
    ae._seen_dedup_keys.clear()
    await ae._consider_opportunity(opp)
    assert ae.stats.skipped_gate_structural == 2
    engine.execute_opportunity.assert_not_awaited()


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
    confirmed_mapping.status = MappingStatus.CONFIRMED  # type: ignore[attr-defined]
    ae, engine = _make_components_with_preflight(
        require_mapping_confirmed=True,
        mapping=confirmed_mapping,
    )
    await ae._consider_opportunity(_make_opportunity())
    engine.execute_opportunity.assert_awaited_once()


# ── Cooldown coverage ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_set_after_recovering_result():
    """Recovering trades (one-leg unwind) must trigger the same cooldown as
    failed trades. Without this, a market whose secondary venue persistently
    rejects orders generates back-to-back naked positions every ~30s — the
    DEM_HOUSE_2026 cascade on 2026-05-08 lost 22 trades / -$105.80 because
    'recovering' fell through both branches of the post-execute switch.
    """
    ae, engine = _make_components()
    engine.execute_opportunity = AsyncMock(
        return_value=SimpleNamespace(
            arb_id="ARB-1", realized_pnl=-4.7, status="recovering"
        )
    )
    await ae._consider_opportunity(_make_opportunity())
    # Cooldown bucket must be populated so the next opportunity skips
    assert "TEST-MKT" in ae._failed_cooldown
    assert ae._failed_count.get("TEST-MKT", 0) == 1


@pytest.mark.asyncio
async def test_cooldown_not_set_after_filled_result():
    ae, engine = _make_components()
    engine.execute_opportunity = AsyncMock(
        return_value=SimpleNamespace(
            arb_id="ARB-1", realized_pnl=0.05, status="filled"
        )
    )
    await ae._consider_opportunity(_make_opportunity())
    assert "TEST-MKT" not in ae._failed_cooldown


@pytest.mark.asyncio
async def test_cooldown_blocks_subsequent_recovering_attempt():
    """After a recovering trade, the next opportunity on the same canonical
    must be skipped until the cooldown expires.
    """
    ae, engine = _make_components()
    engine.execute_opportunity = AsyncMock(
        return_value=SimpleNamespace(
            arb_id="ARB-1", realized_pnl=-4.7, status="recovering"
        )
    )
    await ae._consider_opportunity(_make_opportunity())
    assert engine.execute_opportunity.await_count == 1

    # Second opportunity on same canonical 1s later should be skipped
    await ae._consider_opportunity(_make_opportunity())
    assert engine.execute_opportunity.await_count == 1  # still 1, not 2
    assert ae.stats.skipped_failed_cooldown == 1


# ── Per-canonical loss-streak auto-disable ────────────────────────────────


@pytest.mark.asyncio
async def test_loss_streak_disables_mapping_after_threshold():
    """After N consecutive losing trades on the same canonical, the executor
    must disable auto-trade on that mapping so it can't keep bleeding.
    """

    class _RecordingMappingStore(_FakeMappingStore):
        def __init__(self, mapping):
            super().__init__(mapping)
            self.disabled_canonicals: list[str] = []

        async def disable_auto_trade(self, canonical_id: str, reason: str) -> None:
            self.disabled_canonicals.append(canonical_id)
            if self._mapping is not None:
                self._mapping.allow_auto_trade = False

    mapping = _FakeMapping(allow_auto_trade=True)
    store = _RecordingMappingStore(mapping)
    scanner = _FakeScanner()
    engine = SimpleNamespace(
        execute_opportunity=AsyncMock(
            return_value=SimpleNamespace(
                arb_id="ARB-LOSS", realized_pnl=-4.5, status="recovering"
            )
        )
    )
    supervisor = SimpleNamespace(is_armed=False, armed_by=None)
    cfg = AutoExecutorConfig(
        enabled=True,
        max_position_usd=100.0,
        bootstrap_trades=None,
        dedup_window_seconds=0,  # disable dedup so we can fire repeatedly
        loss_streak_disable_threshold=3,
        require_mapping_confirmed=False,  # H15 default flipped — opt out for gate-isolation
    )
    ae = AutoExecutor(
        scanner=scanner,
        engine=engine,
        supervisor=supervisor,
        mapping_store=store,
        config=cfg,
    )

    # Clear cooldown + dedup between calls so each iteration actually fires
    for i in range(3):
        await ae._consider_opportunity(_make_opportunity(canonical_id="LOSS-MKT"))
        ae._failed_cooldown.clear()
        ae._seen_dedup_keys.clear()

    assert store.disabled_canonicals == ["LOSS-MKT"]
    assert mapping.allow_auto_trade is False


@pytest.mark.asyncio
async def test_loss_streak_resets_on_profitable_trade():
    """A profitable trade resets the loss-streak counter so a single bad day
    doesn't permanently disable a healthy market.
    """
    mapping = _FakeMapping(allow_auto_trade=True)
    store = _FakeMappingStore(mapping)
    scanner = _FakeScanner()

    # Alternate losing and profitable results
    results = [
        SimpleNamespace(arb_id="A1", realized_pnl=-4.5, status="recovering"),
        SimpleNamespace(arb_id="A2", realized_pnl=-4.5, status="recovering"),
        SimpleNamespace(arb_id="A3", realized_pnl=0.30, status="filled"),
        SimpleNamespace(arb_id="A4", realized_pnl=-4.5, status="recovering"),
        SimpleNamespace(arb_id="A5", realized_pnl=-4.5, status="recovering"),
    ]
    engine = SimpleNamespace(execute_opportunity=AsyncMock(side_effect=results))
    supervisor = SimpleNamespace(is_armed=False, armed_by=None)
    cfg = AutoExecutorConfig(
        enabled=True,
        max_position_usd=100.0,
        bootstrap_trades=None,
        dedup_window_seconds=0,
        loss_streak_disable_threshold=3,
        require_mapping_confirmed=False,  # H15 default flipped — opt out for gate-isolation
    )
    ae = AutoExecutor(
        scanner=scanner,
        engine=engine,
        supervisor=supervisor,
        mapping_store=store,
        config=cfg,
    )

    for _ in range(5):
        await ae._consider_opportunity(_make_opportunity(canonical_id="MIX-MKT"))
        ae._failed_cooldown.clear()
        ae._seen_dedup_keys.clear()

    # Streak was 2, then reset by profit, then 2 more losses — never hit 3
    assert mapping.allow_auto_trade is True


# ─────────────────────────────────────────────────────────────────────
# Adaptive-depth (bet sizing audit 2026-05-14): when book depth is below
# the requested qty, halve-and-retry instead of skipping the trade.
# ─────────────────────────────────────────────────────────────────────


class _AdaptiveDepthAdapter:
    """Fake adapter whose orderbook has a configurable max absorbable qty."""

    def __init__(self, *, max_qty: int, best_price: float = 0.50):
        self._max_qty = max_qty
        self._best_price = best_price
        self.calls: list[tuple[str, str, int]] = []

    async def check_depth(self, market_id: str, side: str, qty: int):
        self.calls.append((market_id, side, qty))
        return (qty <= self._max_qty, self._best_price)


def _make_preflight_components(
    *,
    yes_max_qty: int,
    no_max_qty: int,
    suggested_qty: int = 10,
    liquidity_adaptive_sizing: bool = True,
    min_depth_usd: float = 1.0,  # keep depth-USD floor out of the picture
    min_edge_cents_preflight: float = 1.0,
):
    """AutoExecutor wired with adaptive-depth adapters for both legs."""
    pts = {
        ("kalshi", "TEST-MKT"): _FakePricePoint(
            yes_price=0.40, no_price=0.55, age_seconds=2.0,
        ),
        ("polymarket", "TEST-MKT"): _FakePricePoint(
            yes_price=0.40, no_price=0.55, age_seconds=2.0,
        ),
    }
    adapters = {
        "kalshi": _AdaptiveDepthAdapter(max_qty=yes_max_qty, best_price=0.40),
        "polymarket": _AdaptiveDepthAdapter(max_qty=no_max_qty, best_price=0.55),
    }
    scanner = _FakeScanner()
    engine = SimpleNamespace(
        execute_opportunity=AsyncMock(
            return_value=SimpleNamespace(arb_id="ARB-1", realized_pnl=0.05, status="filled"),
        ),
    )
    supervisor = SimpleNamespace(is_armed=False, armed_by=None)
    mapping_store = _FakeMappingStore(_FakeMapping())
    cfg = AutoExecutorConfig(
        enabled=True,
        max_position_usd=100.0,
        dedup_window_seconds=5,
        max_quote_age_s=30.0,
        min_depth_usd=min_depth_usd,
        min_edge_cents_preflight=min_edge_cents_preflight,
        liquidity_adaptive_sizing=liquidity_adaptive_sizing,
        # H15 flipped require_mapping_confirmed to True; these tests
        # exercise the depth gate and must opt out so the mapping gate
        # doesn't filter the test opportunity before depth-gate runs.
        require_mapping_confirmed=False,
    )
    ae = AutoExecutor(
        scanner=scanner, engine=engine, supervisor=supervisor,
        mapping_store=mapping_store, config=cfg,
        price_store=_FakePriceStore(pts),
        adapters_provider=lambda: adapters,
    )
    return ae, engine, adapters


@pytest.mark.asyncio
async def test_preflight_reduces_qty_when_book_only_absorbs_half():
    """Book can absorb 5 contracts; opportunity asks for 10. Adaptive sizing
    halves try_qty until depth is sufficient (5 contracts at the 10/2 step),
    then submits the reduced opportunity to the engine."""
    ae, engine, adapters = _make_preflight_components(
        yes_max_qty=5, no_max_qty=100, suggested_qty=10,
    )
    await ae._consider_opportunity(_make_opportunity(suggested_qty=10))
    engine.execute_opportunity.assert_awaited_once()
    submitted = engine.execute_opportunity.await_args.args[0]
    # 10 -> halved to 5, both legs validated at qty=5
    assert submitted.suggested_qty == 5
    assert ae.stats.executed == 1
    assert ae.stats.skipped_depth_low == 0


@pytest.mark.asyncio
async def test_preflight_uses_smallest_absorbable_qty_across_both_legs():
    """If yes leg can absorb 5 and no leg can absorb 2, final qty must be the
    smaller of the two (capped to a halve-step from the original)."""
    ae, engine, adapters = _make_preflight_components(
        yes_max_qty=5, no_max_qty=2, suggested_qty=10,
    )
    await ae._consider_opportunity(_make_opportunity(suggested_qty=10))
    engine.execute_opportunity.assert_awaited_once()
    submitted = engine.execute_opportunity.await_args.args[0]
    # 10 -> 5 on yes (passes), then 10/2/2=2 step on no (passes at 2)
    # Adaptive takes min across legs; in this implementation it's the
    # smallest absorbable-by-halving qty: 2.
    assert submitted.suggested_qty <= 5
    assert submitted.suggested_qty >= 1


@pytest.mark.asyncio
async def test_preflight_skips_when_adaptive_disabled_legacy_behaviour():
    """liquidity_adaptive_sizing=False preserves legacy skip-on-low-depth."""
    ae, engine, _ = _make_preflight_components(
        yes_max_qty=5, no_max_qty=100, suggested_qty=10,
        liquidity_adaptive_sizing=False,
    )
    await ae._consider_opportunity(_make_opportunity(suggested_qty=10))
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_depth_low == 1


@pytest.mark.asyncio
async def test_preflight_skips_when_book_has_no_depth_even_for_one():
    """A book that can't absorb qty=1 must skip even with adaptive sizing on."""
    ae, engine, _ = _make_preflight_components(
        yes_max_qty=0, no_max_qty=100, suggested_qty=10,
    )
    await ae._consider_opportunity(_make_opportunity(suggested_qty=10))
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_depth_low == 1
    assert ae.stats.executed == 0


# ── Suspicious-edge circuit breaker (2026-07-10 party-swap incident) ───────
#
# A COARSE absurdity backstop, not the primary defense (that is the
# mapping-level coherence sweep). It must never block legitimate arbs — a
# real K×P arb clears 7c net => ~9.5c gross — so the ceiling sits at 15c and
# only stops a catastrophically-wrong mapping's absurd edge from auto-firing.


@pytest.mark.asyncio
async def test_absurd_edge_is_not_auto_executed():
    ae, engine = _make_components()
    opp = _make_opportunity(canonical_id="BROKEN_MAP")
    opp.gross_edge = 0.30          # 30c — a catastrophically wrong mapping
    opp.net_edge = 0.27
    opp.net_edge_cents = 27.0
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_suspicious_edge == 1


@pytest.mark.asyncio
async def test_legitimate_kp_arb_at_preflight_floor_still_executes():
    """A real K×P arb at the 7c-net floor is ~9.5c gross — the breaker must
    NOT block it (that is exactly the volume we want to keep trading)."""
    ae, engine = _make_components()
    opp = _make_opportunity()
    opp.gross_edge = 0.095         # ~9.5c gross = 7c net + ~2.5c fees
    opp.net_edge = 0.07
    opp.net_edge_cents = 7.0
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_awaited_once()
    assert ae.stats.skipped_suspicious_edge == 0


@pytest.mark.asyncio
async def test_normal_few_cent_edge_still_executes():
    ae, engine = _make_components()
    opp = _make_opportunity()      # gross_edge 0.05
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_awaited_once()
    assert ae.stats.skipped_suspicious_edge == 0


@pytest.mark.asyncio
async def test_suspicious_edge_threshold_is_configurable():
    ae, engine = _make_components()
    ae._config.max_auto_gross_edge_cents = 8.0   # operator tightened the band
    opp = _make_opportunity()
    opp.gross_edge = 0.12
    opp.net_edge = 0.10
    opp.net_edge_cents = 10.0
    await ae._consider_opportunity(opp)
    engine.execute_opportunity.assert_not_awaited()
    assert ae.stats.skipped_suspicious_edge == 1


def test_from_env_gross_edge_ceiling_default_matches_config_default():
    """Config-consistency regression: the dataclass default (15c) and the
    make_auto_executor_from_env fallback disagreed (8c), so production —
    which builds from env — over-blocked legitimate sports arbs (live
    2026-07-10 23:38Z: a coherent MLB K×P mapping's 10c edge was blocked
    with ceiling_cents=8.0). The env fallback must match the dataclass."""
    from arbiter.execution.auto_executor import (
        AutoExecutorConfig, make_auto_executor_from_env,
    )
    ae = make_auto_executor_from_env(
        scanner=SimpleNamespace(subscribe=lambda: None),
        engine=SimpleNamespace(),
        supervisor=SimpleNamespace(is_armed=False),
        mapping_store=SimpleNamespace(),
        config_env={},  # nothing set → fallback
    )
    assert ae._config.max_auto_gross_edge_cents == AutoExecutorConfig().max_auto_gross_edge_cents
    assert ae._config.max_auto_gross_edge_cents == 15.0


def test_from_env_gross_edge_ceiling_honors_override():
    from arbiter.execution.auto_executor import make_auto_executor_from_env
    ae = make_auto_executor_from_env(
        scanner=SimpleNamespace(subscribe=lambda: None),
        engine=SimpleNamespace(),
        supervisor=SimpleNamespace(is_armed=False),
        mapping_store=SimpleNamespace(),
        config_env={"MAX_AUTO_GROSS_EDGE_CENTS": "12"},
    )
    assert ae._config.max_auto_gross_edge_cents == 12.0
