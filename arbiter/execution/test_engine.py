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
    """CR-01 regression: timeout cancels REAL orders found by client_order_id prefix,
    not the synthetic placeholder Order. Replaces the prior false-green test
    that only asserted .assert_awaited_once() without inspecting call_args.
    """
    from arbiter.execution.engine import OrderStatus, Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.1

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        real_order = Order(
            order_id="KALSHI-SERVER-XYZ-123",
            platform="kalshi",
            market_id="M",
            canonical_id="C",
            side="yes",
            price=0.5,
            quantity=1,
            status=OrderStatus.SUBMITTED,
        )
        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[real_order])
        adapter.cancel_order = AsyncMock(return_value=True)

        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-99", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        adapter.list_open_orders_by_client_id.assert_awaited_once_with("ARB-99-YES-")
        adapter.cancel_order.assert_awaited_once_with(real_order)
        assert result.status == OrderStatus.CANCELLED

    asyncio.run(runner())


# ─────────────────────────────────────────────────────────────────────────
# Phase 02.1 / CR-02 + CR-01: external_client_order_id field, _derive
# helper rewrite, and timeout-recovery via list_open_orders_by_client_id.
# ─────────────────────────────────────────────────────────────────────────


def test_order_external_field_defaults_none():
    """CR-02 dataclass smoke: external_client_order_id defaults to None."""
    from arbiter.execution.engine import Order, OrderStatus
    o = Order(
        order_id="x", platform="kalshi",
        market_id="M", canonical_id="C", side="yes",
        price=0.5, quantity=1, status=OrderStatus.PENDING,
    )
    assert o.external_client_order_id is None

    o2 = Order(
        order_id="x", platform="kalshi",
        market_id="M", canonical_id="C", side="yes",
        price=0.5, quantity=1, status=OrderStatus.PENDING,
        external_client_order_id="ARB-X-YES-deadbeef",
    )
    assert o2.external_client_order_id == "ARB-X-YES-deadbeef"


def test_derive_client_order_id_returns_external_field():
    """CR-02 regression: _derive_client_order_id reads order.external_client_order_id,
    not order.order_id (which after Kalshi success holds the platform-assigned id).
    """
    from arbiter.execution.engine import ExecutionEngine, Order, OrderStatus
    o = Order(
        order_id="KALSHI-SERVER-99",  # platform-assigned id (would mislead old heuristic)
        platform="kalshi",
        market_id="M", canonical_id="C", side="yes",
        price=0.5, quantity=1,
        status=OrderStatus.FILLED,
        external_client_order_id="ARB-000042-YES-deadbeef",
    )
    assert ExecutionEngine._derive_client_order_id(o) == "ARB-000042-YES-deadbeef"

    o2 = Order(
        order_id="KALSHI-SERVER-99", platform="kalshi",
        market_id="M", canonical_id="C", side="yes",
        price=0.5, quantity=1, status=OrderStatus.FILLED,
        # external_client_order_id defaults to None
    )
    assert ExecutionEngine._derive_client_order_id(o2) is None

    o3 = Order(
        order_id="POLY-X", platform="polymarket",
        market_id="M", canonical_id="C", side="yes",
        price=0.5, quantity=1, status=OrderStatus.FILLED,
    )
    assert ExecutionEngine._derive_client_order_id(o3) is None


def test_kalshi_fill_persists_client_order_id_correctly():
    """CR-02 integration: on a successful Kalshi fill, engine.store.upsert_order
    is called with kwarg ``client_order_id`` set to the ARB-prefixed string
    carried by Order.external_client_order_id, NOT the platform order_id.
    """
    from arbiter.execution.engine import OrderStatus, Order as _Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock(return_value=_Order(
            order_id="KALSHI-SERVER-XYZ",          # platform-assigned id
            platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, quantity=1,
            status=OrderStatus.FILLED,
            fill_price=0.5, fill_qty=1,
            external_client_order_id="ARB-000042-YES-abcd1234",
        ))
        engine.adapters = {"kalshi": adapter}
        engine.store = AsyncMock()

        order = await engine._place_order_for_leg(
            arb_id="ARB-000042", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert order.status == OrderStatus.FILLED
        engine.store.upsert_order.assert_awaited_once()
        kwargs = engine.store.upsert_order.call_args.kwargs
        assert kwargs["client_order_id"] == "ARB-000042-YES-abcd1234"
        assert kwargs["client_order_id"] != "KALSHI-SERVER-XYZ"

    asyncio.run(runner())


def test_timeout_looks_up_real_orders_by_client_id():
    """CR-01 regression: on timeout, engine queries adapter.list_open_orders_by_client_id
    with the ARB-prefixed string, not the synthetic placeholder order_id.
    """
    from arbiter.execution.engine import OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.1

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[])
        adapter.cancel_order = AsyncMock(return_value=False)

        engine.adapters = {"kalshi": adapter}
        order = await engine._place_order_for_leg(
            arb_id="ARB-42", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        adapter.list_open_orders_by_client_id.assert_awaited_once_with("ARB-42-YES-")
        assert order.status == OrderStatus.FAILED
        assert "no matching open order found" in order.error
        adapter.cancel_order.assert_not_called()

    asyncio.run(runner())


def test_timeout_uses_list_open_orders_by_client_id_with_correct_prefix():
    """CR-01 prefix-shape: list_open_orders_by_client_id is called with the
    exact prefix ``f"{arb_id}-{SIDE}-"``. Mirrors PATTERNS.md Section 6.
    """
    from arbiter.execution.engine import OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.1

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[])
        adapter.cancel_order = AsyncMock(return_value=False)

        engine.adapters = {"kalshi": adapter}
        order = await engine._place_order_for_leg(
            arb_id="ARB-42", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        adapter.list_open_orders_by_client_id.assert_awaited_once_with("ARB-42-YES-")
        assert order.status == OrderStatus.FAILED
        assert "no matching open order found" in order.error
        adapter.cancel_order.assert_not_called()

    asyncio.run(runner())


def test_timeout_cancel_success_sets_cancelled():
    """CR-01: list_open_orders_by_client_id returns one real order; cancel succeeds;
    final Order.status == CANCELLED.
    """
    from arbiter.execution.engine import OrderStatus, Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.1

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        real_order = Order(
            order_id="KALSHI-SERVER-AAA",
            platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, quantity=1,
            status=OrderStatus.SUBMITTED,
        )
        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[real_order])
        adapter.cancel_order = AsyncMock(return_value=True)

        engine.adapters = {"kalshi": adapter}
        result = await engine._place_order_for_leg(
            arb_id="ARB-77", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        adapter.list_open_orders_by_client_id.assert_awaited_once_with("ARB-77-YES-")
        adapter.cancel_order.assert_awaited_once_with(real_order)
        assert result.status == OrderStatus.CANCELLED

    asyncio.run(runner())


def test_timeout_no_match_sets_failed_with_clear_error():
    """CR-01: lookup returns []; cancel_order is NEVER called; final status FAILED
    with ``"no matching open order found"`` in error.
    """
    from arbiter.execution.engine import OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.1

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[])
        adapter.cancel_order = AsyncMock(return_value=True)

        engine.adapters = {"kalshi": adapter}
        result = await engine._place_order_for_leg(
            arb_id="ARB-NO-MATCH", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        adapter.list_open_orders_by_client_id.assert_awaited_once_with("ARB-NO-MATCH-YES-")
        adapter.cancel_order.assert_not_awaited()
        assert result.status == OrderStatus.FAILED
        assert "no matching open order found" in result.error

    asyncio.run(runner())


def test_timeout_lookup_exception_logs_and_fails_safely():
    """CR-01: list_open_orders_by_client_id raises; engine catches it, logs a
    warning, degrades to FAILED. Engine NEVER re-raises across the
    _place_order_for_leg boundary. cancel_order is NOT called (nothing to
    cancel since the lookup failed).
    """
    from arbiter.execution.engine import OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.1

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(
            side_effect=RuntimeError("network down"),
        )
        adapter.cancel_order = AsyncMock(return_value=True)

        engine.adapters = {"kalshi": adapter}
        # MUST NOT raise — engine boundary invariant
        result = await engine._place_order_for_leg(
            arb_id="ARB-EXC", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert result.status == OrderStatus.FAILED
        adapter.cancel_order.assert_not_awaited()

    asyncio.run(runner())


def test_timeout_recovery_end_to_end():
    """End-to-end: hang on place_fok → lookup returns one real Order with the
    ARB-prefixed external_client_order_id → cancel succeeds → engine.store.upsert_order
    is called with the prefix-derived client_order_id and CANCELLED status.
    """
    from arbiter.execution.engine import OrderStatus, Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.1

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        # The real order returned from the lookup carries the ARB-prefixed id
        # in external_client_order_id — this is what _derive_client_order_id
        # must thread into upsert_order(client_order_id=...).
        real_order = Order(
            order_id="KALSHI-SERVER-LATE",
            platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, quantity=1,
            status=OrderStatus.SUBMITTED,
            external_client_order_id="ARB-42-YES-abcd1234",
        )
        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[real_order])
        adapter.cancel_order = AsyncMock(return_value=True)

        engine.adapters = {"kalshi": adapter}
        engine.store = AsyncMock()

        result = await engine._place_order_for_leg(
            arb_id="ARB-42", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert result.status == OrderStatus.CANCELLED
        engine.store.upsert_order.assert_awaited_once()
        kwargs = engine.store.upsert_order.call_args.kwargs
        # The persisted client_order_id is the prefix-derived ARB string,
        # picked up from the real order returned by the lookup.
        assert kwargs["client_order_id"] == "ARB-42-YES-abcd1234"


# ─────────────────────────────────────────────────────────────────────────
# Plan 03-02 (SAFE-02): Per-platform RiskManager exposure + order_rejected
# incident emission. The RiskManager gains:
#   - optional safety_config kwarg carrying SafetyConfig.max_platform_exposure_usd
#   - _platform_exposures dict tracked alongside _open_positions
#   - check_trade adds a per-platform aggregate check BEFORE total-exposure
#     check but AFTER per-market
#   - record_trade / release_trade accept yes/no platform + exposure kwargs
#     (backward-compatible — legacy callers pass nothing)
# ExecutionEngine emits an ExecutionIncident on every risk-rejection with
# metadata.event_type == "order_rejected" and a structured rejection_type.
# ─────────────────────────────────────────────────────────────────────────


def _make_safety_opp(
    canonical_id: str = "MKT1",
    yes_platform: str = "kalshi",
    no_platform: str = "polymarket",
    yes_price: float = 0.60,
    no_price: float = 0.40,
    suggested_qty: int = 100,
    quote_age_seconds: float = 1.0,
) -> ArbitrageOpportunity:
    """Build an ArbitrageOpportunity suitable for RiskManager.check_trade
    exercises. Edge/confidence/status defaults are 'approvable'."""
    return ArbitrageOpportunity(
        canonical_id=canonical_id,
        description="Safety limit test opp",
        yes_platform=yes_platform,
        yes_price=yes_price,
        yes_fee=0.02,
        yes_market_id=f"{yes_platform[:1].upper()}-{canonical_id}",
        no_platform=no_platform,
        no_price=no_price,
        no_fee=0.01,
        no_market_id=f"{no_platform[:1].upper()}-{canonical_id}",
        gross_edge=0.10,
        total_fees=0.03,
        net_edge=0.07,
        net_edge_cents=7.0,
        suggested_qty=suggested_qty,
        max_profit_usd=7.0,
        timestamp=time.time(),
        confidence=0.9,
        status="tradable",
        persistence_count=3,
        quote_age_seconds=quote_age_seconds,
        min_available_liquidity=500.0,
        mapping_status="confirmed",
        mapping_score=0.95,
        requires_manual=False,
        yes_fee_rate=0.07,
        no_fee_rate=0.01,
    )


def test_risk_per_platform_limit():
    """Per-platform aggregated exposure REJECTS when a new leg would push
    the platform over SafetyConfig.max_platform_exposure_usd."""
    from arbiter.execution.engine import RiskManager

    config = ArbiterConfig()
    config.safety.max_platform_exposure_usd = 300.0
    config.scanner.max_position_usd = 1_000.0  # large so per-market does not fire
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0

    risk = RiskManager(config.scanner, safety_config=config.safety)
    risk._max_total_exposure = 50_000.0

    # Pre-existing $250 Kalshi exposure on some other market.
    risk.record_trade(
        "OTHER_MKT",
        exposure=250.0,
        platform="kalshi",
    )

    # New opp: 100 qty × $0.60 on Kalshi = $60 → Kalshi aggregate 310 > 300 limit.
    opp = _make_safety_opp(
        canonical_id="MKT1",
        yes_platform="kalshi",
        no_platform="polymarket",
        yes_price=0.60,
        no_price=0.40,
        suggested_qty=100,
    )

    ok, reason = risk.check_trade(opp)
    assert ok is False
    assert "Per-platform" in reason
    assert "kalshi" in reason.lower()


def test_risk_per_platform_allows_within_limit():
    """Same shape as per-platform test but prior exposure leaves headroom."""
    from arbiter.execution.engine import RiskManager

    config = ArbiterConfig()
    config.safety.max_platform_exposure_usd = 300.0
    config.scanner.max_position_usd = 1_000.0
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0

    risk = RiskManager(config.scanner, safety_config=config.safety)
    risk._max_total_exposure = 50_000.0

    # Prior Kalshi exposure $100 — new $60 leg keeps Kalshi at $160 < $300.
    risk.record_trade(
        "OTHER_MKT",
        exposure=100.0,
        platform="kalshi",
    )

    opp = _make_safety_opp(
        canonical_id="MKT1",
        yes_platform="kalshi",
        no_platform="polymarket",
        yes_price=0.60,
        no_price=0.40,
        suggested_qty=100,
    )

    ok, reason = risk.check_trade(opp)
    assert ok is True, f"expected approval, got: {reason}"


def test_risk_per_market_limit_still_fires():
    """Regression guard: legacy per-market exposure check still fires when
    breached, even with the new per-platform check layered on top."""
    from arbiter.execution.engine import RiskManager

    config = ArbiterConfig()
    config.scanner.max_position_usd = 500.0
    config.safety.max_platform_exposure_usd = 10_000.0  # disable per-platform
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0

    risk = RiskManager(config.scanner, safety_config=config.safety)
    risk._max_total_exposure = 100_000.0

    # Prior $400 exposure on MKT1.
    risk.record_trade("MKT1", exposure=400.0)

    # New opp on MKT1 adds 100 qty × (0.60 + 0.90) = $150 → total $550 > $500.
    opp = _make_safety_opp(
        canonical_id="MKT1",
        yes_platform="kalshi",
        no_platform="polymarket",
        yes_price=0.60,
        no_price=0.90,
        suggested_qty=100,
    )

    ok, reason = risk.check_trade(opp)
    assert ok is False
    assert "Per-market" in reason


def test_rejected_order_emits_incident():
    """Any RiskManager rejection during execute_opportunity emits a
    structured ExecutionIncident with metadata.event_type='order_rejected'."""
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)

        # Force rejection via stale quote (post-safety wiring this is still
        # a RiskManager rejection path).
        opp = _make_safety_opp(
            canonical_id="REJ_STALE",
            quote_age_seconds=999.0,
        )

        queue = engine.subscribe_incidents()
        execution = await engine.execute_opportunity(opp)
        assert execution is None

        # One incident should have been dispatched to the subscriber queue.
        incident = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert incident.severity == "info"
        assert incident.metadata.get("event_type") == "order_rejected"
        assert "reason" in incident.metadata
        assert incident.metadata["reason"]  # non-empty

    asyncio.run(runner())


def test_rejected_order_incident_per_platform():
    """When the per-platform limit fires, the incident carries
    rejection_type='per_platform' and the offending platform name."""
    async def runner():
        store = PriceStore(ttl=60)
        config = ArbiterConfig()
        config.scanner.dry_run = True
        config.scanner.confidence_threshold = 0.1
        config.scanner.min_edge_cents = 1.0
        config.scanner.max_position_usd = 1_000.0
        config.safety.max_platform_exposure_usd = 300.0
        monitor = BalanceMonitor(
            config.alerts,
            {"kalshi": object(), "polymarket": object(), "predictit": object()},
        )
        engine = ExecutionEngine(config, monitor, price_store=store, collectors={})
        engine.risk._max_daily_trades = 250
        engine.risk._max_total_exposure = 50_000.0

        # Pre-load $290 Kalshi exposure — new $60 leg pushes to $350 > $300.
        engine.risk.record_trade(
            "OTHER_MKT",
            exposure=290.0,
            platform="kalshi",
        )

        opp = _make_safety_opp(
            canonical_id="REJ_PER_PLATFORM",
            yes_platform="kalshi",
            no_platform="polymarket",
            yes_price=0.60,
            no_price=0.40,
            suggested_qty=100,
        )

        queue = engine.subscribe_incidents()
        execution = await engine.execute_opportunity(opp)
        assert execution is None

        incident = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert incident.metadata.get("event_type") == "order_rejected"
        assert incident.metadata.get("rejection_type") == "per_platform"
        assert incident.metadata.get("platform") == "kalshi"

    asyncio.run(runner())

    asyncio.run(runner())
