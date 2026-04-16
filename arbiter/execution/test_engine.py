import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

from arbiter.config.settings import ArbiterConfig
from arbiter.execution.engine import ExecutionEngine
from arbiter.monitor.balance import BalanceMonitor
from arbiter.scanner.arbitrage import ArbitrageOpportunity
from arbiter.utils.price_store import PricePoint, PriceStore


def make_engine(price_store: PriceStore) -> ExecutionEngine:
    config = ArbiterConfig()
    config.scanner.dry_run = True
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0
    config.scanner.slippage_tolerance = 0.01
    monitor = BalanceMonitor(config.alerts, {"kalshi": object(), "polymarket": object(), "predictit": object()})
    engine = ExecutionEngine(config, monitor, price_store=price_store, collectors={})
    engine.risk._max_daily_trades = 250
    engine.risk._max_total_exposure = 50_000
    return engine


def test_manual_opportunity_creates_manual_position():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        opportunity = ArbitrageOpportunity(
            canonical_id="TEST_MANUAL",
            description="Manual opportunity",
            yes_platform="predictit",
            yes_price=0.34,
            yes_fee=0.116,
            yes_market_id="PI:123",
            no_platform="kalshi",
            no_price=0.50,
            no_fee=0.018,
            no_market_id="K-123",
            gross_edge=0.16,
            total_fees=0.1335630252,
            net_edge=0.0264369748,
            net_edge_cents=2.64369748,
            suggested_qty=119,
            max_profit_usd=3.1459999999999995,
            timestamp=time.time(),
            confidence=0.7,
            status="manual",
            persistence_count=3,
            quote_age_seconds=1.0,
            min_available_liquidity=200.0,
            mapping_status="confirmed",
            mapping_score=0.9,
            requires_manual=True,
            no_fee_rate=0.07,
        )

        execution = await engine.execute_opportunity(opportunity)
        assert execution is not None
        assert execution.status == "manual_pending"
        assert len(engine.manual_positions) == 1

    asyncio.run(runner())


def test_pretrade_requote_aborts_on_slippage():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        now = time.time()

        await store.put(
            PricePoint(
                platform="kalshi",
                canonical_id="TEST_AUTO",
                yes_price=0.55,
                no_price=0.45,
                yes_volume=100,
                no_volume=100,
                timestamp=now,
                raw_market_id="K-TEST",
                yes_market_id="K-TEST",
                no_market_id="K-TEST",
                fee_rate=0.07,
                mapping_status="confirmed",
                mapping_score=0.9,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket",
                canonical_id="TEST_AUTO",
                yes_price=0.48,
                no_price=0.44,
                yes_volume=100,
                no_volume=100,
                timestamp=now,
                raw_market_id="P-YES",
                yes_market_id="P-YES",
                no_market_id="P-NO",
                fee_rate=0.01,
                mapping_status="confirmed",
                mapping_score=0.9,
            )
        )

        opportunity = ArbitrageOpportunity(
            canonical_id="TEST_AUTO",
            description="Auto opportunity",
            yes_platform="kalshi",
            yes_price=0.40,
            yes_fee=0.02,
            yes_market_id="K-TEST",
            no_platform="polymarket",
            no_price=0.44,
            no_fee=0.004,
            no_market_id="P-NO",
            gross_edge=0.16,
            total_fees=0.024,
            net_edge=0.136,
            net_edge_cents=13.6,
            suggested_qty=10,
            max_profit_usd=1.36,
            timestamp=now,
            confidence=0.8,
            status="tradable",
            persistence_count=3,
            quote_age_seconds=1.0,
            min_available_liquidity=100.0,
            mapping_status="confirmed",
            mapping_score=0.9,
            requires_manual=False,
            yes_fee_rate=0.07,
            no_fee_rate=0.01,
        )

        execution = await engine.execute_opportunity(opportunity)
        assert execution is None
        assert len(engine.incidents) == 1
        assert "Slippage exceeded tolerance" in engine.incidents[0].message

    asyncio.run(runner())


def test_bulk_dry_run_executes_120_opportunities():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        now = time.time()

        for index in range(120):
            canonical_id = f"BULK_{index}"
            await store.put(
                PricePoint(
                    platform="kalshi",
                    canonical_id=canonical_id,
                    yes_price=0.09,
                    no_price=0.91,
                    yes_volume=100,
                    no_volume=100,
                    timestamp=now,
                    raw_market_id=f"K-{index}",
                    yes_market_id=f"K-{index}",
                    no_market_id=f"K-{index}",
                    fee_rate=0.07,
                    mapping_status="confirmed",
                    mapping_score=0.9,
                )
            )
            await store.put(
                PricePoint(
                    platform="polymarket",
                    canonical_id=canonical_id,
                    yes_price=0.18,
                    no_price=0.10,
                    yes_volume=100,
                    no_volume=100,
                    timestamp=now,
                    raw_market_id=f"P-YES-{index}",
                    yes_market_id=f"P-YES-{index}",
                    no_market_id=f"P-NO-{index}",
                    fee_rate=0.01,
                    mapping_status="confirmed",
                    mapping_score=0.9,
                )
            )

            opportunity = ArbitrageOpportunity(
                canonical_id=canonical_id,
                description=f"Bulk dry-run {index}",
                yes_platform="kalshi",
                yes_price=0.09,
                yes_fee=0.01,
                yes_market_id=f"K-{index}",
                no_platform="polymarket",
                no_price=0.10,
                no_fee=0.001,
                no_market_id=f"P-NO-{index}",
                gross_edge=0.81,
                total_fees=0.0067,
                net_edge=0.8033,
                net_edge_cents=80.33,
                suggested_qty=100,
                max_profit_usd=80.33,
                timestamp=now,
                confidence=0.95,
                status="tradable",
                persistence_count=3,
                quote_age_seconds=0.5,
                min_available_liquidity=100.0,
                mapping_status="confirmed",
                mapping_score=0.9,
                requires_manual=False,
                yes_fee_rate=0.07,
                no_fee_rate=0.01,
            )
            execution = await engine.execute_opportunity(opportunity)
            assert execution is not None
            assert execution.status == "simulated"

        assert len(engine.execution_history) == 120
        assert engine.stats["simulated"] == 120

    asyncio.run(runner())


def test_manual_position_actions_update_execution_history():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        opportunity = ArbitrageOpportunity(
            canonical_id="TEST_MANUAL_ACTIONS",
            description="Manual lifecycle opportunity",
            yes_platform="predictit",
            yes_price=0.35,
            yes_fee=0.115,
            yes_market_id="PI:456",
            no_platform="kalshi",
            no_price=0.48,
            no_fee=0.0175,
            no_market_id="K-456",
            gross_edge=0.17,
            total_fees=0.1325,
            net_edge=0.0375,
            net_edge_cents=3.75,
            suggested_qty=120,
            max_profit_usd=4.5,
            timestamp=time.time(),
            confidence=0.72,
            status="manual",
            persistence_count=3,
            quote_age_seconds=1.0,
            min_available_liquidity=200.0,
            mapping_status="confirmed",
            mapping_score=0.9,
            requires_manual=True,
            no_fee_rate=0.07,
        )

        execution = await engine.execute_opportunity(opportunity)
        assert execution is not None
        assert engine.execution_history[0].status == "manual_pending"

        position = engine.manual_positions[0]
        entered = await engine.update_manual_position(position.position_id, "mark_entered", note="Entered on PredictIt")
        assert entered is not None
        assert entered.status == "entered"
        assert engine.execution_history[0].status == "manual_entered"

        closed = await engine.update_manual_position(position.position_id, "mark_closed", note="Closed after unwind")
        assert closed is not None
        assert closed.status == "closed"
        assert engine.execution_history[0].status == "manual_closed"
        assert engine.execution_history[0].realized_pnl > 0.0

    asyncio.run(runner())


def test_live_trade_gate_blocks_execution_until_ready():
    async def runner():
        store = PriceStore(ttl=60)
        config = ArbiterConfig()
        config.scanner.dry_run = False
        config.scanner.confidence_threshold = 0.1
        config.scanner.min_edge_cents = 1.0
        monitor = BalanceMonitor(config.alerts, {"kalshi": object(), "polymarket": object()})
        engine = ExecutionEngine(config, monitor, price_store=store, collectors={})
        engine.risk._max_daily_trades = 250
        engine.risk._max_total_exposure = 50_000
        engine.set_trade_gate(lambda opp: (False, "profitability still collecting evidence", {"gate": "readiness"}))

        opportunity = ArbitrageOpportunity(
            canonical_id="TEST_LIVE_GATE",
            description="Live gate block opportunity",
            yes_platform="kalshi",
            yes_price=0.40,
            yes_fee=0.02,
            yes_market_id="K-LIVE",
            no_platform="polymarket",
            no_price=0.45,
            no_fee=0.01,
            no_market_id="P-LIVE",
            gross_edge=0.15,
            total_fees=0.03,
            net_edge=0.12,
            net_edge_cents=12.0,
            suggested_qty=10,
            max_profit_usd=1.2,
            timestamp=time.time(),
            confidence=0.9,
            status="tradable",
            persistence_count=3,
            quote_age_seconds=1.0,
            min_available_liquidity=100.0,
            mapping_status="confirmed",
            mapping_score=0.95,
            requires_manual=False,
            yes_fee_rate=0.07,
            no_fee_rate=0.01,
        )

        execution = await engine.execute_opportunity(opportunity)
        assert execution is None
        assert len(engine.incidents) == 1
        assert "Trade gate blocked execution" in engine.incidents[0].message

    asyncio.run(runner())


def test_manual_position_close_releases_risk_exposure():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        opportunity = ArbitrageOpportunity(
            canonical_id="TEST_MANUAL_RELEASE",
            description="Manual exposure release",
            yes_platform="predictit",
            yes_price=0.35,
            yes_fee=0.115,
            yes_market_id="PI:789",
            no_platform="kalshi",
            no_price=0.48,
            no_fee=0.0175,
            no_market_id="K-789",
            gross_edge=0.17,
            total_fees=0.1325,
            net_edge=0.0375,
            net_edge_cents=3.75,
            suggested_qty=120,
            max_profit_usd=4.5,
            timestamp=time.time(),
            confidence=0.72,
            status="manual",
            persistence_count=3,
            quote_age_seconds=1.0,
            min_available_liquidity=200.0,
            mapping_status="confirmed",
            mapping_score=0.9,
            requires_manual=True,
            no_fee_rate=0.07,
        )

        execution = await engine.execute_opportunity(opportunity)
        assert execution is not None
        position = engine.manual_positions[0]
        exposure = opportunity.suggested_qty * (opportunity.yes_price + opportunity.no_price)

        await engine.update_manual_position(position.position_id, "mark_entered")
        assert engine.risk._open_positions[opportunity.canonical_id] == exposure

        await engine.update_manual_position(position.position_id, "mark_closed")
        assert engine.risk._open_positions.get(opportunity.canonical_id, 0.0) == 0.0

    asyncio.run(runner())


def test_incident_resolution_marks_status():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        incident = await engine.record_incident(
            arb_id="ARB-TEST",
            canonical_id="TEST_INCIDENT",
            severity="warning",
            message="Synthetic incident",
            metadata={"source": "test"},
        )
        resolved = await engine.resolve_incident(incident.incident_id, note="Resolved during test")
        assert resolved is not None
        assert resolved.status == "resolved"
        assert resolved.resolution_note == "Resolved during test"

    asyncio.run(runner())


def test_shadow_audit_blocks_unprofitable_manual_math():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        opportunity = ArbitrageOpportunity(
            canonical_id="TEST_BAD_MATH",
            description="Bad manual math",
            yes_platform="predictit",
            yes_price=0.35,
            yes_fee=0.01,
            yes_market_id="PI:BAD",
            no_platform="kalshi",
            no_price=0.48,
            no_fee=0.01,
            no_market_id="K-BAD",
            gross_edge=0.17,
            total_fees=0.02,
            net_edge=0.15,
            net_edge_cents=15.0,
            suggested_qty=12,
            max_profit_usd=1.8,
            timestamp=time.time(),
            confidence=0.9,
            status="manual",
            persistence_count=3,
            quote_age_seconds=1.0,
            min_available_liquidity=100.0,
            mapping_status="confirmed",
            mapping_score=0.9,
            requires_manual=True,
            no_fee_rate=0.07,
        )

        execution = await engine.execute_opportunity(opportunity)
        assert execution is None
        assert len(engine.manual_positions) == 0
        assert engine.stats["aborted"] == 1
        assert any("Shadow math audit rejected" in incident.message for incident in engine.incidents)

    asyncio.run(runner())


# ─────────────────────────────────────────────────────────────────────────
# Plan 02-06 integration: engine now dispatches leg placement through
# self.adapters[platform], not the deleted _place_kalshi_order /
# _place_polymarket_order helpers. The exhaustive body-shape and
# response-parsing tests that used to live here now live in
# arbiter/execution/adapters/test_kalshi_adapter.py (alongside the
# adapter they actually cover). The tests below prove the adapter
# dispatch contract at the engine level.
# ─────────────────────────────────────────────────────────────────────────


def test_engine_dispatches_to_adapter_for_known_platform():
    """Smoke test: ExecutionEngine constructor accepts adapters dict and
    _place_order_for_leg dispatches through it."""
    from arbiter.execution.engine import OrderStatus
    from arbiter.execution.engine import Order as _Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)

        kalshi_adapter = MagicMock()
        kalshi_adapter.platform = "kalshi"
        kalshi_adapter.place_fok = AsyncMock(
            return_value=_Order(
                order_id="ARB-X-YES-aaa",
                platform="kalshi",
                market_id="M",
                canonical_id="C",
                side="yes",
                price=0.5,
                quantity=1,
                status=OrderStatus.FILLED,
                fill_price=0.5,
                fill_qty=1,
            )
        )
        kalshi_adapter.cancel_order = AsyncMock(return_value=True)
        engine.adapters = {"kalshi": kalshi_adapter}

        order = await engine._place_order_for_leg(
            arb_id="ARB-1", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert order.status == OrderStatus.FILLED
        kalshi_adapter.place_fok.assert_awaited_once()

    asyncio.run(runner())


def test_engine_returns_failed_when_no_adapter_for_platform():
    """No adapter for the requested platform -> Order(status=FAILED)."""
    from arbiter.execution.engine import OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.adapters = {}  # explicitly no adapters wired

        order = await engine._place_order_for_leg(
            arb_id="ARB-1", platform="missing",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert order.status == OrderStatus.FAILED
        assert "No adapter configured" in order.error

    asyncio.run(runner())


def test_engine_timeout_triggers_cancel():
    """asyncio.wait_for fires; cancel_order is called; final status is CANCELLED."""
    from arbiter.execution.engine import OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.1  # short timeout

        slow_adapter = MagicMock()
        slow_adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)  # exceeds the 0.1s timeout

        slow_adapter.place_fok = _hangs
        slow_adapter.cancel_order = AsyncMock(return_value=True)

        engine.adapters = {"kalshi": slow_adapter}

        order = await engine._place_order_for_leg(
            arb_id="ARB-T", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert order.status == OrderStatus.CANCELLED
        slow_adapter.cancel_order.assert_awaited_once()

    asyncio.run(runner())
