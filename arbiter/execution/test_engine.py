import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.config import settings as settings_module
from arbiter.config.settings import ArbiterConfig
from arbiter.execution.engine import ExecutionEngine
from arbiter.monitor.balance import BalanceMonitor
from arbiter.scanner.arbitrage import ArbitrageOpportunity
from arbiter.utils.price_store import PricePoint, PriceStore


@pytest.fixture(autouse=True)
def confirmed_synthetic_mappings():
    """Register synthetic execution-test markets without touching production seeds."""
    original = dict(settings_module.MARKET_MAP)

    def add_mapping(canonical_id: str) -> None:
        settings_module.MARKET_MAP[canonical_id] = {
            "description": f"Synthetic test mapping for {canonical_id}",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.9,
            "resolution_match_status": "identical",
        }

    for canonical_id in {
        "C",
        "MKT1",
        "MKT_BURST",
        "MKT_BURST_2",
        "MKT_CNC",
        "MKT_DRYRUN_PARITY",
        "MKT_REC",
        "REJ_PER_PLATFORM",
        "REJ_STALE",
        "TEST_AUTO",
        "TEST_BAD_MATH",
        "TEST_INCIDENT",
        "TEST_LIVE_GATE",
        "TEST_MANUAL",
        "TEST_MANUAL_ACTIONS",
        "TEST_MANUAL_RELEASE",
    }:
        add_mapping(canonical_id)
    for index in range(120):
        add_mapping(f"BULK_{index}")

    yield

    settings_module.MARKET_MAP.clear()
    settings_module.MARKET_MAP.update(original)


def make_engine(price_store: PriceStore) -> ExecutionEngine:
    config = ArbiterConfig()
    config.scanner.dry_run = True
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0
    config.scanner.slippage_tolerance = 0.01
    # Plan 03-02: loosen SafetyConfig for bulk/stress tests — the real
    # production default is $300/platform, but the unit tests here run
    # 120 consecutive simulated opportunities against a shared engine.
    config.safety.max_platform_exposure_usd = 1_000_000.0
    monitor = BalanceMonitor(config.alerts, {"kalshi": object(), "polymarket": object()})
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
            yes_platform="polymarket",
            yes_price=0.34,
            yes_fee_rate=0.04,
            yes_fee=0.008976,
            yes_market_id="PM:123",
            no_platform="kalshi",
            no_price=0.50,
            no_fee=0.017563,
            no_market_id="K-123",
            gross_edge=0.16,
            total_fees=0.026539,
            net_edge=0.133461,
            net_edge_cents=13.3461,
            suggested_qty=119,
            max_profit_usd=15.8819,
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
            yes_platform="polymarket",
            yes_price=0.35,
            yes_fee_rate=0.04,
            yes_fee=0.0091,
            yes_market_id="PM:456",
            no_platform="kalshi",
            no_price=0.48,
            no_fee=0.0175,
            no_market_id="K-456",
            gross_edge=0.17,
            total_fees=0.0266,
            net_edge=0.1434,
            net_edge_cents=14.34,
            suggested_qty=120,
            max_profit_usd=17.208,
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
        entered = await engine.update_manual_position(position.position_id, "mark_entered", note="Entered on Polymarket")
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
            yes_platform="polymarket",
            yes_price=0.35,
            yes_fee_rate=0.04,
            yes_fee=0.0091,
            yes_market_id="PM:789",
            no_platform="kalshi",
            no_price=0.48,
            no_fee=0.0175,
            no_market_id="K-789",
            gross_edge=0.17,
            total_fees=0.0266,
            net_edge=0.1434,
            net_edge_cents=14.34,
            suggested_qty=120,
            max_profit_usd=17.208,
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


def test_record_incident_sends_telegram_for_critical():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        sent: list[tuple[str, str]] = []

        async def fake_send(msg, parse_mode="HTML", *, dedup_key=None):
            sent.append((msg, dedup_key or ""))
            return True

        engine.balance_monitor.notifier.send = fake_send  # type: ignore[assignment]
        await engine.record_incident(
            arb_id="ARB-X",
            canonical_id="MKT_X",
            severity="critical",
            message="something exploded",
            metadata={"event_type": "db_write_failure", "op": "insert_execution"},
        )
        assert sent, "Critical incident must trigger Telegram"
        body, dedup = sent[0]
        assert "CRITICAL INCIDENT" in body
        assert "something exploded" in body
        assert dedup.startswith("incident:")

    asyncio.run(runner())


def test_record_incident_sends_telegram_for_stranded_position():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        sent: list[tuple[str, str]] = []

        async def fake_send(msg, parse_mode="HTML", *, dedup_key=None):
            sent.append((msg, dedup_key or ""))
            return True

        engine.balance_monitor.notifier.send = fake_send  # type: ignore[assignment]
        await engine.record_incident(
            arb_id="STRAND-kalshi-deadbeef",
            canonical_id="KX-TEST-MKT",
            severity="warning",
            message="Stranded position detected on kalshi: 7 YES @ avg $0.4200 per contract",
            metadata={
                "event_type": "stranded_position",
                "platform": "kalshi",
                "side": "yes",
                "qty": 7,
                "cost_basis_usd": 2.94,
                "mtm_usd": 3.50,
                "unrealized_usd": 0.56,
                "title": "Test stranded market",
            },
        )
        assert sent, "Stranded position must trigger Telegram even at warning severity"
        body, _ = sent[0]
        assert "STRANDED POSITION" in body
        assert "7" in body and "YES" in body and "KALSHI" in body
        assert "+3.50" in body

    asyncio.run(runner())


def test_record_incident_skips_telegram_for_one_leg_exposure():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        sent: list[str] = []

        async def fake_send(msg, parse_mode="HTML", *, dedup_key=None):
            sent.append(msg)
            return True

        engine.balance_monitor.notifier.send = fake_send  # type: ignore[assignment]
        await engine.record_incident(
            arb_id="ARB-OLE",
            canonical_id="MKT_OLE",
            severity="critical",
            message="One leg exposed",
            metadata={"event_type": "one_leg_exposure"},
        )
        assert sent == [], "one_leg_exposure must be handled by supervisor only"

    asyncio.run(runner())


def test_record_incident_no_telegram_for_routine_warning():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        sent: list[str] = []

        async def fake_send(msg, parse_mode="HTML", *, dedup_key=None):
            sent.append(msg)
            return True

        engine.balance_monitor.notifier.send = fake_send  # type: ignore[assignment]
        await engine.record_incident(
            arb_id="ARB-W",
            canonical_id="MKT_W",
            severity="warning",
            message="Missing fresh quote",
            metadata={"source": "scanner"},
        )
        assert sent == []

    asyncio.run(runner())


def test_shadow_audit_blocks_unprofitable_manual_math():
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        opportunity = ArbitrageOpportunity(
            canonical_id="TEST_BAD_MATH",
            description="Bad manual math",
            yes_platform="polymarket",
            yes_price=0.35,
            yes_fee=0.01,
            yes_market_id="PM:BAD",
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


def test_risk_check_trade_uses_pair_floor_for_fx_kalshi():
    """PROFITABILITY-01: RiskManager.check_trade must accept a K×FX opp whose
    net_edge_cents sits between the pair floor (4.5¢) and the global floor (7.0¢).

    Before commit 4262583 the risk manager read min_edge_cents (7.0¢) directly,
    so a 5.0¢ K×FX opp was rejected with 'Edge too thin'. After the fix it reads
    min_edge_for_pair which returns 4.5¢ for forecastex_kalshi — and the opp passes.
    """
    from arbiter.execution.engine import RiskManager

    config = ArbiterConfig()
    config.scanner.min_edge_cents = 7.0            # global floor — higher than pair
    config.scanner.min_edge_cents_by_pair = {
        "forecastex_kalshi": 4.5,                   # pair-specific relaxation
    }
    config.scanner.max_position_usd = 10_000.0
    config.scanner.confidence_threshold = 0.1
    config.safety.max_platform_exposure_usd = 1_000_000.0

    risk = RiskManager(config.scanner, safety_config=config.safety)
    risk._max_total_exposure = 50_000.0

    # 5.0¢ net edge: above the 4.5¢ pair floor but below the 7.0¢ global floor.
    opp = _make_safety_opp(
        yes_platform="kalshi",
        no_platform="forecastex",
        yes_price=0.50,
        no_price=0.45,
        suggested_qty=10,
    )
    import dataclasses
    # Override reported edge to exactly 5.0¢ so the test is deterministic.
    opp = dataclasses.replace(opp, net_edge_cents=5.0)

    ok, reason = risk.check_trade(opp)
    assert ok is True, (
        f"K×FX opp at 5.0¢ should pass the 4.5¢ pair floor, got: {reason}"
    )


def test_risk_check_trade_global_floor_still_applies_for_kx_poly():
    """PROFITABILITY-01 regression guard: the legacy global floor still
    applies to K×P pairs that have no pair-specific entry."""
    import dataclasses
    from arbiter.execution.engine import RiskManager

    config = ArbiterConfig()
    config.scanner.min_edge_cents = 7.0
    config.scanner.min_edge_cents_by_pair = {
        "forecastex_kalshi": 4.5,   # only FX pairs are relaxed
    }
    config.scanner.max_position_usd = 10_000.0
    config.scanner.confidence_threshold = 0.1
    config.safety.max_platform_exposure_usd = 1_000_000.0

    risk = RiskManager(config.scanner, safety_config=config.safety)
    risk._max_total_exposure = 50_000.0

    # 5.0¢ K×P opp — no pair entry, so global 7.0¢ must apply → reject.
    opp = _make_safety_opp(
        yes_platform="kalshi",
        no_platform="polymarket",
    )
    opp = dataclasses.replace(opp, net_edge_cents=5.0)

    ok, reason = risk.check_trade(opp)
    assert ok is False
    assert "Edge too thin" in reason


def test_risk_check_trade_pair_floor_never_raises_above_global():
    """min_edge_for_pair semantics: if the pair floor is HIGHER than the global,
    the global wins (pair floors only relax, never tighten)."""
    import dataclasses
    from arbiter.execution.engine import RiskManager

    config = ArbiterConfig()
    config.scanner.min_edge_cents = 3.0             # low global
    config.scanner.min_edge_cents_by_pair = {
        "forecastex_kalshi": 10.0,                  # pair entry HIGHER than global
    }
    config.scanner.max_position_usd = 10_000.0
    config.scanner.confidence_threshold = 0.1
    config.safety.max_platform_exposure_usd = 1_000_000.0

    risk = RiskManager(config.scanner, safety_config=config.safety)
    risk._max_total_exposure = 50_000.0

    # 5.0¢ K×FX — above global (3¢) so must pass even though pair entry says 10¢.
    opp = _make_safety_opp(
        yes_platform="kalshi",
        no_platform="forecastex",
    )
    opp = dataclasses.replace(opp, net_edge_cents=5.0)

    ok, reason = risk.check_trade(opp)
    assert ok is True, (
        f"Pair floor must never raise above the global; got rejection: {reason}"
    )


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
            {"kalshi": object(), "polymarket": object()},
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


# ─── Plan 03-03: one-leg exposure surfacing (SAFE-03) ───────────────────────


def test_one_leg_exposure_surfaces_structured_metadata():
    """Plan 03-03: when _recover_one_leg_risk sees one FILLED + one FAILED
    leg, it must emit a single ExecutionIncident whose metadata carries the
    full structured payload that the dashboard and plan 03-07 UI rely on."""
    import pytest
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        queue = engine.subscribe_incidents()

        leg_yes = Order(
            order_id="ARB-1-YES",
            platform="kalshi",
            market_id="K-MKT1",
            canonical_id="MKT1",
            side="yes",
            price=0.56,
            quantity=100,
            status=OrderStatus.FILLED,
            fill_price=0.56,
            fill_qty=100,
            timestamp=time.time(),
        )
        leg_no = Order(
            order_id="ARB-1-NO",
            platform="polymarket",
            market_id="P-MKT1",
            canonical_id="MKT1",
            side="no",
            price=0.40,
            quantity=100,
            status=OrderStatus.FAILED,
            fill_price=0.0,
            fill_qty=0,
            timestamp=time.time(),
            error="rate_limited",
        )

        opp = _make_safety_opp(
            canonical_id="MKT1",
            yes_platform="kalshi",
            no_platform="polymarket",
            yes_price=0.56,
            no_price=0.40,
            suggested_qty=100,
        )

        await engine._recover_one_leg_risk("ARB-1", opp, leg_yes, leg_no)

        # Drain queue — expect exactly one incident.
        incident = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert queue.empty(), "expected exactly one incident, got more"

        meta = incident.metadata
        for key in (
            "event_type",
            "filled_platform",
            "filled_side",
            "filled_qty",
            "filled_price",
            "exposure_usd",
            "failed_platform",
            "failed_reason",
            "recommended_unwind",
        ):
            assert key in meta, f"missing metadata key: {key}"

        assert meta["event_type"] == "one_leg_exposure"
        assert meta["filled_platform"] == "kalshi"
        assert meta["filled_side"] == "yes"
        assert meta["filled_qty"] == 100
        assert meta["filled_price"] == pytest.approx(0.56, rel=1e-3)
        assert meta["exposure_usd"] == pytest.approx(56.0, rel=1e-3)
        assert meta["failed_platform"] == "polymarket"
        assert "rate_limited" in str(meta["failed_reason"])
        assert "Sell 100 YES" in meta["recommended_unwind"]
        assert incident.severity == "critical"

    asyncio.run(runner())


def test_one_leg_exposure_invokes_supervisor_hook():
    """Plan 03-03: _recover_one_leg_risk must also invoke the injected
    SafetySupervisor.handle_one_leg_exposure with (incident, filled_leg,
    failed_leg, opp) exactly once, passing the filled leg (status FILLED)
    as the second positional argument."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._safety = AsyncMock()

        leg_yes = Order(
            order_id="ARB-2-YES",
            platform="kalshi",
            market_id="K-MKT1",
            canonical_id="MKT1",
            side="yes",
            price=0.56,
            quantity=100,
            status=OrderStatus.FILLED,
            fill_price=0.56,
            fill_qty=100,
            timestamp=time.time(),
        )
        leg_no = Order(
            order_id="ARB-2-NO",
            platform="polymarket",
            market_id="P-MKT1",
            canonical_id="MKT1",
            side="no",
            price=0.40,
            quantity=100,
            status=OrderStatus.FAILED,
            fill_price=0.0,
            fill_qty=0,
            timestamp=time.time(),
            error="platform_error",
        )
        opp = _make_safety_opp(
            canonical_id="MKT1",
            yes_platform="kalshi",
            no_platform="polymarket",
            yes_price=0.56,
            no_price=0.40,
            suggested_qty=100,
        )

        await engine._recover_one_leg_risk("ARB-2", opp, leg_yes, leg_no)

        engine._safety.handle_one_leg_exposure.assert_called_once()
        args = engine._safety.handle_one_leg_exposure.call_args.args
        # incident, filled_leg, failed_leg, opp
        assert len(args) == 4
        incident_arg, filled_arg, failed_arg, opp_arg = args
        assert hasattr(incident_arg, "metadata")
        assert incident_arg.metadata.get("event_type") == "one_leg_exposure"
        assert filled_arg.status == OrderStatus.FILLED
        assert filled_arg is leg_yes
        assert failed_arg is leg_no
        assert opp_arg is opp

    asyncio.run(runner())


# ─────────────────────────────────────────────────────────────────────────
# Plan 03-08 (SAFE-02 gap closure): live-mode per-platform exposure
# tracking. Closes the gap from 03-VERIFICATION.md where _live_execution
# only called record_trade on `filled`, leaving `submitted` and
# `recovering` statuses with stale per-platform accounting.
# Pattern: Option A (record on submitted) with explicit symmetry on
# recovery — see 03-08-PLAN.md <objective>.
# ─────────────────────────────────────────────────────────────────────────


def _bypass_math_auditor(engine):
    """Replace the engine's MathAuditor with a stub whose audit_opportunity
    and audit_execution always pass. The SAFE-02 gap-closure tests use
    _make_safety_opp() which hand-sets gross_edge/net_edge fields that
    disagree with the auditor's shadow recomputation (the auditor is
    comprehensive — it catches scanner math drift). We are exercising
    RiskManager per-platform accounting in _live_execution, not auditor
    behaviour — the auditor is asserted elsewhere in this file."""
    from arbiter.audit.math_auditor import AuditResult

    def _always_pass(opp_dict):
        return AuditResult(
            canonical_id=opp_dict.get("canonical_id", ""),
            yes_platform=opp_dict.get("yes_platform", ""),
            no_platform=opp_dict.get("no_platform", ""),
            timestamp=time.time(),
            passed=True,
            flags=[],
        )

    def _always_pass_exec(exec_dict):
        opp = exec_dict.get("opportunity", {})
        return AuditResult(
            canonical_id=opp.get("canonical_id", ""),
            yes_platform=opp.get("yes_platform", ""),
            no_platform=opp.get("no_platform", ""),
            timestamp=time.time(),
            passed=True,
            flags=[],
        )

    engine._auditor.audit_opportunity = _always_pass
    engine._auditor.audit_execution = _always_pass_exec


def _build_live_engine_for_safe02_gap():
    """Construct a live-mode ExecutionEngine configured for the SAFE-02
    gap-closure tests:
      * dry_run=False (takes the _live_execution branch)
      * max_platform_exposure_usd=300.0 (matches VERIFICATION baseline)
      * max_position_usd loosened so per-market does not pre-empt
      * price_store=None so _pre_trade_requote short-circuits (engine.py:618-619)
      * MathAuditor bypassed so _make_safety_opp's static edge fields don't
        collide with shadow-recomputation (auditor is covered elsewhere)
    Adapters remain the caller's responsibility — attach via engine.adapters.
    """
    config = ArbiterConfig()
    config.scanner.dry_run = False
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0
    config.scanner.slippage_tolerance = 0.01
    config.scanner.max_position_usd = 1_000.0
    config.safety.max_platform_exposure_usd = 300.0
    monitor = BalanceMonitor(
        config.alerts,
        {"kalshi": object(), "polymarket": object()},
    )
    engine = ExecutionEngine(config, monitor, price_store=None, collectors={})
    engine.risk._max_daily_trades = 250
    engine.risk._max_total_exposure = 50_000.0
    _bypass_math_auditor(engine)
    return engine


def _wire_live_depth(adapter, price: float) -> None:
    adapter.check_depth = AsyncMock(return_value=(True, price))
    adapter.best_executable_price = AsyncMock(return_value=(True, price))


def _wire_unwind_failed(adapter) -> None:
    """Wire smart-unwind methods to a no-op FAILED Order.

    The SAFE-02 gap tests must reach the post-recovery exposure-tracking
    asserts; without this helper the MagicMock adapter has no awaitable
    ``place_resting_sell`` / ``place_unwind_sell``, smart_unwind raises,
    and the H7 try/finally releases the surviving leg's reservation —
    which is correct in production but not what those tests are
    measuring. Returning a non-raising FAILED order keeps the naked
    exposure tracked exactly as in the SAFE-02 design.
    """
    from arbiter.execution.engine import Order, OrderStatus

    def _failed_order(prefix: str = "UNWIND") -> Order:
        return Order(
            order_id=f"{prefix}-NOOP",
            platform=getattr(adapter, "platform", "test"),
            market_id="UNWIND-MKT",
            canonical_id="UNWIND-CAN",
            side="yes",
            price=0.0,
            quantity=0,
            status=OrderStatus.FAILED,
            fill_price=0.0,
            fill_qty=0,
            timestamp=time.time(),
            error="unwind unavailable in test fixture",
        )

    adapter.place_resting_sell = AsyncMock(return_value=_failed_order("REST"))
    adapter.place_unwind_sell = AsyncMock(return_value=_failed_order("MKT"))
    adapter.get_order = AsyncMock(return_value=_failed_order("GET"))


def test_live_burst_submitted_rejected_at_per_platform_ceiling():
    import os as _os_test
    _old_exec_order = _os_test.environ.get("EXECUTION_ORDER")
    _os_test.environ["EXECUTION_ORDER"] = "kalshi_first"
    """SAFE-02 gap closure: live recovery still books surviving exposure.

    A filled primary plus resting secondary enters recovery, cancels the
    secondary reservation, and leaves the filled leg in per-platform accounting.
    A later opportunity on the same platform is rejected before dispatch when
    that surviving exposure would exceed SafetyConfig.max_platform_exposure_usd.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        engine = _build_live_engine_for_safe02_gap()

        def _mk_adapter(platform: str, price: float, status: OrderStatus, fill_qty: int):
            adapter = MagicMock()
            adapter.platform = platform
            _wire_live_depth(adapter, price)
            adapter.place_fok = AsyncMock(
                return_value=Order(
                    order_id=f"ARB-LIVE-{platform.upper()}",
                    platform=platform,
                    market_id=f"M-{platform}",
                    canonical_id="MKT_BURST",
                    side="yes" if platform == "kalshi" else "no",
                    price=price,
                    quantity=400,
                    status=status,
                    fill_qty=fill_qty,
                    fill_price=price if fill_qty else 0.0,
                    timestamp=time.time(),
                )
            )
            adapter.cancel_order = AsyncMock(return_value=True)
            return adapter

        kalshi = _mk_adapter("kalshi", 0.60, OrderStatus.FILLED, 400)
        poly = _mk_adapter("polymarket", 0.30, OrderStatus.SUBMITTED, 0)
        _wire_unwind_failed(kalshi)
        _wire_unwind_failed(poly)
        engine.adapters = {"kalshi": kalshi, "polymarket": poly}

        incidents_q = engine.subscribe_incidents()

        opp1 = _make_safety_opp(
            canonical_id="MKT_BURST",
            yes_platform="kalshi",
            no_platform="polymarket",
            yes_price=0.60,
            no_price=0.30,
            suggested_qty=400,  # Kalshi leg = 240, Poly leg = 120 — both < 300
        )

        exec1 = await engine.execute_opportunity(opp1)
        assert exec1 is not None
        assert exec1.status == "recovering"

        # Recovery leaves the filled Kalshi leg booked and releases the
        # cancelled resting Polymarket reservation.
        assert engine.risk._platform_exposures.get("kalshi") == 240.0
        assert engine.risk._platform_exposures.get("polymarket", 0.0) == 0.0

        # Second opp on a DIFFERENT canonical_id (per-market would not
        # block), SAME platform pair. Kalshi leg = 100 → aggregate 340 > 300.
        opp2 = _make_safety_opp(
            canonical_id="MKT_BURST_2",
            yes_platform="kalshi",
            no_platform="polymarket",
            yes_price=0.50,
            no_price=0.30,
            suggested_qty=200,
        )

        exec2 = await engine.execute_opportunity(opp2)
        assert exec2 is None

        # Drain incident queue — expect an order_rejected incident with
        # per_platform rejection type on kalshi.
        # First incident may be the incident from exec1's successful flow;
        # we need to scan until we find the rejection.
        rejection_found = False
        while True:
            try:
                incident = await asyncio.wait_for(incidents_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                break
            if incident.metadata.get("event_type") == "order_rejected":
                assert incident.metadata.get("rejection_type") == "per_platform"
                assert incident.metadata.get("platform") == "kalshi"
                rejection_found = True
                break
        assert rejection_found, "expected order_rejected/per_platform incident"

        # Second opp did NOT record.
        assert engine.risk._platform_exposures.get("kalshi") == 240.0
        # place_fok was called exactly once (for opp1 kalshi leg);
        # opp2 was blocked by check_trade BEFORE dispatch.
        assert kalshi.place_fok.await_count == 1

    asyncio.run(runner())
    if _old_exec_order is None:
        _os_test.environ.pop("EXECUTION_ORDER", None)
    else:
        _os_test.environ["EXECUTION_ORDER"] = _old_exec_order


def test_live_recovering_records_only_surviving_leg():
    import os as _os_test
    _old_exec_order = _os_test.environ.get("EXECUTION_ORDER")
    _os_test.environ["EXECUTION_ORDER"] = "kalshi_first"
    """SAFE-02 gap closure: when one leg FILLS and the other venue REJECTS
    (FAILED), _live_execution resolves to status='recovering'. Only the
    surviving (filled) leg's exposure is recorded. The rejected leg is NOT
    recorded — it never had real exposure. Currently (pre-Task 1) this test
    FAILS because _live_execution records nothing for 'recovering'."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        engine = _build_live_engine_for_safe02_gap()

        kalshi = MagicMock()
        kalshi.platform = "kalshi"
        _wire_live_depth(kalshi, 0.60)
        kalshi.place_fok = AsyncMock(
            return_value=Order(
                order_id="ARB-REC-YES",
                platform="kalshi",
                market_id="K-MKT_REC",
                canonical_id="MKT_REC",
                side="yes",
                price=0.60,
                quantity=100,
                status=OrderStatus.FILLED,
                fill_price=0.60,
                fill_qty=100,
                timestamp=time.time(),
            )
        )
        kalshi.cancel_order = AsyncMock(return_value=True)

        poly = MagicMock()
        poly.platform = "polymarket"
        _wire_live_depth(poly, 0.30)
        poly.place_fok = AsyncMock(
            return_value=Order(
                order_id="ARB-REC-NO",
                platform="polymarket",
                market_id="P-MKT_REC",
                canonical_id="MKT_REC",
                side="no",
                price=0.40,
                quantity=100,
                status=OrderStatus.FAILED,
                fill_price=0.0,
                fill_qty=0,
                timestamp=time.time(),
                error="venue rejected",
            )
        )
        poly.cancel_order = AsyncMock(return_value=True)
        _wire_unwind_failed(kalshi)
        _wire_unwind_failed(poly)

        engine.adapters = {"kalshi": kalshi, "polymarket": poly}

        opp = _make_safety_opp(
            canonical_id="MKT_REC",
            yes_platform="kalshi",
            no_platform="polymarket",
            yes_price=0.60,
            no_price=0.30,
            suggested_qty=100,  # Kalshi $60, Polymarket $30
        )

        exec_result = await engine.execute_opportunity(opp)
        assert exec_result is not None
        assert exec_result.status == "recovering"

        # Only the surviving (Kalshi FILLED) leg recorded.
        assert engine.risk._platform_exposures.get("kalshi") == 60.0
        # Polymarket rejected by the venue — no exposure, must not be recorded.
        assert engine.risk._platform_exposures.get("polymarket", 0.0) == 0.0

    asyncio.run(runner())
    if _old_exec_order is None:
        _os_test.environ.pop("EXECUTION_ORDER", None)
    else:
        _os_test.environ["EXECUTION_ORDER"] = _old_exec_order


def test_live_recovery_cancellation_releases_reservation():
    import os as _os_test
    _old_exec_order = _os_test.environ.get("EXECUTION_ORDER")
    _os_test.environ["EXECUTION_ORDER"] = "kalshi_first"
    """SAFE-02 gap closure symmetry: when _recover_one_leg_risk cancels a
    previously-SUBMITTED leg successfully, Task 2's release_trade hook frees
    the per-platform reservation that Task 1 booked. Currently (pre-Task 2)
    this test FAILS because the cancel path does not call release_trade."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        engine = _build_live_engine_for_safe02_gap()

        kalshi = MagicMock()
        kalshi.platform = "kalshi"
        _wire_live_depth(kalshi, 0.60)
        kalshi.place_fok = AsyncMock(
            return_value=Order(
                order_id="ARB-CNC-YES",
                platform="kalshi",
                market_id="K-MKT_CNC",
                canonical_id="MKT_CNC",
                side="yes",
                price=0.60,
                quantity=100,
                status=OrderStatus.SUBMITTED,
                fill_price=0.0,
                fill_qty=0,
                timestamp=time.time(),
            )
        )
        kalshi.cancel_order = AsyncMock(return_value=True)

        poly = MagicMock()
        poly.platform = "polymarket"
        _wire_live_depth(poly, 0.30)
        poly.place_fok = AsyncMock(
            return_value=Order(
                order_id="ARB-CNC-NO",
                platform="polymarket",
                market_id="P-MKT_CNC",
                canonical_id="MKT_CNC",
                side="no",
                price=0.40,
                quantity=100,
                status=OrderStatus.FAILED,
                fill_price=0.0,
                fill_qty=0,
                timestamp=time.time(),
                error="venue rejected",
            )
        )
        poly.cancel_order = AsyncMock(return_value=False)  # nothing to cancel

        engine.adapters = {"kalshi": kalshi, "polymarket": poly}

        # Seed a prior Kalshi exposure ($100 on an unrelated market) so we
        # can prove release_trade only freed the NEW leg's reservation —
        # not the pre-existing one. If release_trade had never fired (or
        # fired with the wrong exposure) the Kalshi aggregate would be
        # wrong afterward.
        engine.risk.record_trade(
            "OTHER_MKT",
            exposure=100.0,
            platform="kalshi",
        )
        assert engine.risk._platform_exposures.get("kalshi") == 100.0

        opp = _make_safety_opp(
            canonical_id="MKT_CNC",
            yes_platform="kalshi",
            no_platform="polymarket",
            yes_price=0.60,
            no_price=0.30,
            suggested_qty=100,  # Kalshi $60, Polymarket $30
        )

        exec_result = await engine.execute_opportunity(opp)
        assert exec_result is not None
        assert exec_result.status == "recovering"

        # _recover_one_leg_risk attempted to cancel Kalshi's SUBMITTED leg
        # and the cancel succeeded.
        assert kalshi.cancel_order.await_count == 1

        # The new release_trade hook (Task 2) fired — the NEW leg's
        # reservation ($60) is freed, leaving the pre-seeded $100 intact.
        # Pre-Task-2: Kalshi would read $160 (recorded $60 + never released).
        # Post-Task-2: Kalshi reads $100 (recorded $60, then released $60,
        # pre-existing $100 untouched).
        assert engine.risk._platform_exposures.get("kalshi") == 100.0
        # Per-market counter on MKT_CNC coherent — release_trade decremented
        # back to zero for the cancelled arb.
        assert engine.risk._open_positions.get(opp.canonical_id, 0.0) == 0.0

    asyncio.run(runner())
    if _old_exec_order is None:
        _os_test.environ.pop("EXECUTION_ORDER", None)
    else:
        _os_test.environ["EXECUTION_ORDER"] = _old_exec_order


def test_dry_run_record_trade_unchanged_after_fix():
    """SAFE-02 parity: dry-run (_simulate_execution) is untouched — the
    existing record_trade call site stays exactly as-is and this test
    passes BOTH before and after Task 1+2. Guards against accidental
    dry-run regression during live-path surgery."""
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        # Bypass requote by disabling price_store lookup.
        engine.price_store = None
        # Bypass MathAuditor: _make_safety_opp's static edge fields disagree
        # with shadow recomputation (the auditor itself is covered elsewhere).
        _bypass_math_auditor(engine)

        opp = _make_safety_opp(
            canonical_id="MKT_DRYRUN_PARITY",
            yes_platform="kalshi",
            no_platform="polymarket",
            yes_price=0.60,
            no_price=0.40,
            suggested_qty=100,
        )

        exec_result = await engine.execute_opportunity(opp)
        assert exec_result is not None
        assert exec_result.status == "simulated"

        # Kalshi yes-leg exposure matches the value dry-run always produced.
        assert engine.risk._platform_exposures.get("kalshi") == 60.0
        assert engine.risk._platform_exposures.get("polymarket") == 40.0

        # Per-market counter: exactly one record_trade fired. Total exposure
        # on this canonical_id equals qty * (yes_price + no_price) = 100.0.
        expected_total = opp.suggested_qty * (opp.yes_price + opp.no_price)
        assert engine.risk._open_positions.get(opp.canonical_id) == expected_total

    asyncio.run(runner())


# ─────────────────────────────────────────────────────────────────────────
# C2: Bounded post-submit poll for SUBMITTED legs
#
# Bug: when ``_place_order_for_leg`` returns ``OrderStatus.SUBMITTED``
# (order accepted onto the venue book but not immediately matched), the
# engine recorded the leg and moved on with no follow-up.  If the venue
# eventually filled or cancelled the order, the engine's DB row and
# RiskManager exposure stayed pegged to SUBMITTED forever — silent
# divergence from venue reality.
#
# Fix: after place returns SUBMITTED, poll ``adapter.get_order`` on a
# bounded loop (default 10s) until the order reaches a terminal status
# or the timeout fires.  Persist the final state via store.upsert_order.
# ─────────────────────────────────────────────────────────────────────────


def test_post_submit_poll_transitions_submitted_to_filled():
    """C2 regression: when place_fok returns SUBMITTED and the venue
    later fills the order, the post-submit poll detects the FILLED state
    and returns it — instead of stopping at SUBMITTED forever.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 0.3
        engine._submit_poll_interval_s = 0.02

        submitted = Order(
            order_id="KALSHI-OPEN-1",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="C",
            side="yes",
            price=0.55,
            quantity=10,
            status=OrderStatus.SUBMITTED,
            external_client_order_id="ARB-1-YES-deadbeef",
        )
        filled = Order(
            order_id="KALSHI-OPEN-1",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="C",
            side="yes",
            price=0.55,
            quantity=10,
            status=OrderStatus.FILLED,
            fill_qty=10,
            fill_price=0.55,
            external_client_order_id="ARB-1-YES-deadbeef",
        )

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock(return_value=submitted)
        # First two polls still SUBMITTED, third poll returns FILLED.
        adapter.get_order = AsyncMock(side_effect=[submitted, submitted, filled])

        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-1", platform="kalshi",
            market_id="K-YES", canonical_id="C", side="yes",
            price=0.55, qty=10,
        )

        # Engine waited for the venue to converge — does NOT return SUBMITTED.
        assert result.status == OrderStatus.FILLED, (
            f"expected FILLED after post-submit poll, got {result.status.value}"
        )
        assert result.fill_qty == 10
        assert result.fill_price == 0.55
        assert adapter.get_order.await_count == 3

    asyncio.run(runner())


def test_post_submit_poll_cancels_order_on_timeout():
    """When the SUBMITTED poll times out, the engine must NOT leave the
    order resting on the venue book with no further action. Without an
    explicit cancel a stuck order can fill later — meanwhile the engine
    moved on, leaving real exposure with no recovery path. The fix:
    fire cancel_order, re-poll to confirm, return the resulting state.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 0.06
        engine._submit_poll_interval_s = 0.02

        submitted = Order(
            order_id="POLY-OPEN-7",
            platform="polymarket",
            market_id="P-NO",
            canonical_id="C",
            side="no",
            price=0.40,
            quantity=10,
            status=OrderStatus.SUBMITTED,
        )
        cancelled = Order(
            order_id="POLY-OPEN-7",
            platform="polymarket",
            market_id="P-NO",
            canonical_id="C",
            side="no",
            price=0.40,
            quantity=10,
            status=OrderStatus.CANCELLED,
            fill_qty=0,
        )

        adapter = MagicMock()
        adapter.platform = "polymarket"
        adapter.place_fok = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(side_effect=[submitted, submitted, submitted, cancelled])
        adapter.cancel_order = AsyncMock(return_value=True)

        engine.adapters = {"polymarket": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-7", platform="polymarket",
            market_id="P-NO", canonical_id="C", side="no",
            price=0.40, qty=10,
        )

        # cancel was attempted exactly once on timeout
        adapter.cancel_order.assert_awaited_once()
        # final status reflects the confirmed cancellation, NOT SUBMITTED
        assert result.status == OrderStatus.CANCELLED
        # not ambiguous because we got positive proof of cancellation
        assert result.idempotency_ambiguous is False

    asyncio.run(runner())


def test_post_submit_poll_marks_ambiguous_when_cancel_fails():
    """If the post-timeout cancel call returns False (venue refused) or
    raises, we have no proof the order is dead — it may still fill. The
    order must be marked idempotency_ambiguous so the fallback chain
    refuses to retry with a new client_order_id and stack a second leg
    on top of a possibly-live one."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 0.06
        engine._submit_poll_interval_s = 0.02

        submitted = Order(
            order_id="KALSHI-OPEN-9",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="C",
            side="yes",
            price=0.55,
            quantity=10,
            status=OrderStatus.SUBMITTED,
        )

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(return_value=submitted)
        adapter.cancel_order = AsyncMock(return_value=False)

        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-9", platform="kalshi",
            market_id="K-YES", canonical_id="C", side="yes",
            price=0.55, qty=10,
        )

        adapter.cancel_order.assert_awaited_once()
        # Cancel failed — order still SUBMITTED on the venue book.
        # Status stays SUBMITTED (we can't claim it's cancelled), but
        # idempotency_ambiguous must be True so retries are blocked.
        assert result.idempotency_ambiguous is True

    asyncio.run(runner())


def test_post_submit_poll_marks_ambiguous_when_cancel_raises():
    """A cancel that raises is just as ambiguous as one that returns
    False — same fail-closed treatment."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 0.06
        engine._submit_poll_interval_s = 0.02

        submitted = Order(
            order_id="KALSHI-OPEN-10",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="C",
            side="yes",
            price=0.55,
            quantity=10,
            status=OrderStatus.SUBMITTED,
        )

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(return_value=submitted)
        adapter.cancel_order = AsyncMock(side_effect=RuntimeError("venue down"))

        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-10", platform="kalshi",
            market_id="K-YES", canonical_id="C", side="yes",
            price=0.55, qty=10,
        )

        adapter.cancel_order.assert_awaited_once()
        assert result.idempotency_ambiguous is True

    asyncio.run(runner())


def test_post_submit_poll_repoll_detects_late_fill_after_cancel():
    """Race: the venue fills our order in the same window we issue cancel.
    The re-poll after cancel must see the FILLED status and return it so
    downstream accounting books the real fill — never a phantom cancel."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 0.06
        engine._submit_poll_interval_s = 0.02

        submitted = Order(
            order_id="POLY-OPEN-11",
            platform="polymarket",
            market_id="P-NO",
            canonical_id="C",
            side="no",
            price=0.40,
            quantity=10,
            status=OrderStatus.SUBMITTED,
        )
        filled = Order(
            order_id="POLY-OPEN-11",
            platform="polymarket",
            market_id="P-NO",
            canonical_id="C",
            side="no",
            price=0.40,
            quantity=10,
            status=OrderStatus.FILLED,
            fill_qty=10,
            fill_price=0.40,
        )

        adapter = MagicMock()
        adapter.platform = "polymarket"
        adapter.place_fok = AsyncMock(return_value=submitted)
        # Poll loop sees submitted; re-poll after cancel sees FILLED.
        adapter.get_order = AsyncMock(side_effect=[submitted, submitted, submitted, filled])
        # Cancel raced with fill — venue says nothing to cancel (returns False).
        adapter.cancel_order = AsyncMock(return_value=False)

        engine.adapters = {"polymarket": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-11", platform="polymarket",
            market_id="P-NO", canonical_id="C", side="no",
            price=0.40, qty=10,
        )

        # Real fill takes precedence over the cancel attempt; no longer ambiguous
        # because we have positive proof from the re-poll.
        assert result.status == OrderStatus.FILLED
        assert result.fill_qty == 10
        assert result.idempotency_ambiguous is False

    asyncio.run(runner())


def test_post_submit_poll_detects_cancellation():
    """C2: if the venue cancels the order (e.g. self-trade prevention,
    user manually killed it), the poll should pick up the CANCELLED
    status and return it instead of staying on SUBMITTED."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 0.3
        engine._submit_poll_interval_s = 0.02

        submitted = Order(
            order_id="KALSHI-OPEN-2",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="C",
            side="yes",
            price=0.55,
            quantity=10,
            status=OrderStatus.SUBMITTED,
        )
        cancelled = Order(
            order_id="KALSHI-OPEN-2",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="C",
            side="yes",
            price=0.55,
            quantity=10,
            status=OrderStatus.CANCELLED,
            fill_qty=0,
        )

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(side_effect=[submitted, cancelled])

        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-2", platform="kalshi",
            market_id="K-YES", canonical_id="C", side="yes",
            price=0.55, qty=10,
        )

        assert result.status == OrderStatus.CANCELLED

    asyncio.run(runner())


def test_post_submit_poll_skipped_when_status_already_terminal():
    """C2: terminal statuses (FILLED, PARTIAL, FAILED, CANCELLED) MUST
    NOT trigger the poll loop — calling get_order on a fresh FOK fill
    would be pure waste and could mis-fire if the adapter doesn't
    support get_order for completed orders.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 1.0
        engine._submit_poll_interval_s = 0.01

        filled = Order(
            order_id="KALSHI-OPEN-3",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="C",
            side="yes",
            price=0.55,
            quantity=10,
            status=OrderStatus.FILLED,
            fill_qty=10,
            fill_price=0.55,
        )

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock(return_value=filled)
        adapter.get_order = AsyncMock()

        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-3", platform="kalshi",
            market_id="K-YES", canonical_id="C", side="yes",
            price=0.55, qty=10,
        )

        assert result.status == OrderStatus.FILLED
        adapter.get_order.assert_not_called()

    asyncio.run(runner())


def test_post_submit_poll_persists_final_status_to_store():
    """C2: the resolved final status must be upserted to ExecutionStore
    — otherwise the DB row stays SUBMITTED forever (the bug).  The
    engine writes the FINAL state, not the place-call intermediate.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 0.3
        engine._submit_poll_interval_s = 0.02

        submitted = Order(
            order_id="POLY-OPEN-9",
            platform="polymarket",
            market_id="P-NO",
            canonical_id="C",
            side="no",
            price=0.40,
            quantity=10,
            status=OrderStatus.SUBMITTED,
        )
        filled = Order(
            order_id="POLY-OPEN-9",
            platform="polymarket",
            market_id="P-NO",
            canonical_id="C",
            side="no",
            price=0.40,
            quantity=10,
            status=OrderStatus.FILLED,
            fill_qty=10,
            fill_price=0.40,
        )

        adapter = MagicMock()
        adapter.platform = "polymarket"
        adapter.place_fok = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(side_effect=[submitted, filled])

        engine.adapters = {"polymarket": adapter}
        engine.store = AsyncMock()

        result = await engine._place_order_for_leg(
            arb_id="ARB-9", platform="polymarket",
            market_id="P-NO", canonical_id="C", side="no",
            price=0.40, qty=10,
        )

        assert result.status == OrderStatus.FILLED

        # The store must see the FILLED state at least once.  Any
        # intermediate writes are fine, but the LAST upsert call must
        # carry the terminal status — otherwise the DB stays SUBMITTED.
        upserts = engine.store.upsert_order.await_args_list
        assert len(upserts) >= 1
        last_order = upserts[-1].args[0]
        assert last_order.status == OrderStatus.FILLED, (
            f"expected last store upsert to carry FILLED, got "
            f"{last_order.status.value}"
        )

    asyncio.run(runner())


def test_post_submit_poll_tolerates_transient_get_order_errors():
    """C2: a transient get_order failure mid-poll must not abort the
    whole poll loop — keep retrying.  If subsequent polls reach a
    terminal status, return that."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 0.4
        engine._submit_poll_interval_s = 0.02

        submitted = Order(
            order_id="KALSHI-OPEN-4",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="C",
            side="yes",
            price=0.55,
            quantity=10,
            status=OrderStatus.SUBMITTED,
        )
        filled = Order(
            order_id="KALSHI-OPEN-4",
            platform="kalshi",
            market_id="K-YES",
            canonical_id="C",
            side="yes",
            price=0.55,
            quantity=10,
            status=OrderStatus.FILLED,
            fill_qty=10,
            fill_price=0.55,
        )

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock(return_value=submitted)
        adapter.get_order = AsyncMock(side_effect=[
            RuntimeError("transient 503"),
            submitted,
            filled,
        ])

        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-4", platform="kalshi",
            market_id="K-YES", canonical_id="C", side="yes",
            price=0.55, qty=10,
        )

        assert result.status == OrderStatus.FILLED
        # Retried after the transient error.
        assert adapter.get_order.await_count == 3

    asyncio.run(runner())


def test_post_submit_poll_skipped_when_adapter_has_no_get_order():
    """C2: adapters that don't expose an async get_order must continue
    to work — the poll silently no-ops and the SUBMITTED order is
    returned as-is (preserving the legacy soft-naked recovery path).
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._submit_poll_timeout_s = 0.5
        engine._submit_poll_interval_s = 0.02

        submitted = Order(
            order_id="POLY-OPEN-5",
            platform="polymarket",
            market_id="P-NO",
            canonical_id="C",
            side="no",
            price=0.40,
            quantity=10,
            status=OrderStatus.SUBMITTED,
        )

        # Plain MagicMock means get_order is auto-created as a non-async
        # MagicMock — must not be awaited.
        adapter = MagicMock()
        adapter.platform = "polymarket"
        adapter.place_fok = AsyncMock(return_value=submitted)
        # NOTE: get_order intentionally NOT set to AsyncMock.

        engine.adapters = {"polymarket": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-5", platform="polymarket",
            market_id="P-NO", canonical_id="C", side="no",
            price=0.40, qty=10,
        )

        # Returns the original SUBMITTED — engine didn't try to poll a
        # non-async get_order (which would crash on await).
        assert result.status == OrderStatus.SUBMITTED

    asyncio.run(runner())


# ─────────────────────────────────────────────────────────────────────────
# C4.2: DB write failures escalate to critical incident + SafetySupervisor
# trip + execution block. Engine MUST NOT continue placing live orders if
# it can't write to the DB — silent persistence loss would leave real
# orders with no audit trail.
# ─────────────────────────────────────────────────────────────────────────


def test_db_write_failure_arms_safety_supervisor():
    """C4.2: When store.upsert_order raises in _place_order_for_leg, the
    engine escalates: critical log, in-memory critical incident, and
    SafetySupervisor.trip_kill is invoked so the kill switch arms and
    subsequent opportunities are blocked."""
    from arbiter.execution.engine import OrderStatus, Order as _Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock(return_value=_Order(
            order_id="KALSHI-SERVER-X",
            platform="kalshi", market_id="M", canonical_id="C", side="yes",
            price=0.5, quantity=1,
            status=OrderStatus.FILLED, fill_price=0.5, fill_qty=1,
            external_client_order_id="ARB-1-YES-deadbeef",
        ))
        engine.adapters = {"kalshi": adapter}

        # Store raises on every upsert — simulates Postgres bounce.
        engine.store = AsyncMock()
        engine.store.upsert_order.side_effect = RuntimeError(
            "postgres connection reset",
        )

        # Wire a SafetySupervisor mock so we can assert trip_kill was called.
        engine._safety = AsyncMock()

        order = await engine._place_order_for_leg(
            arb_id="ARB-1", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )

        # The adapter's order still came back FILLED (the leg DID place at the
        # platform) — but the engine must arm the kill switch so no SECOND
        # leg or SECOND opportunity proceeds without an audit trail.
        engine._safety.trip_kill.assert_awaited_once()
        kwargs = engine._safety.trip_kill.call_args.kwargs
        # trip_kill is invoked by the engine itself (not an operator)
        assert kwargs.get("by", "").startswith("execution_engine")
        # reason must identify the broken write
        assert "upsert_order" in (kwargs.get("reason") or "")
        # Engine surfaces a critical block flag so downstream gates can see it.
        assert engine._db_write_failed is True
        # A critical incident is broadcast on the in-memory deque (the store
        # is broken, so persistence is not attempted).
        critical = [
            i for i in engine._incidents
            if i.severity == "critical"
            and "db" in (i.metadata.get("event_type", "") or "").lower()
        ]
        assert critical, "expected a critical db-failure incident in memory"

    asyncio.run(runner())


def test_db_write_failure_blocks_subsequent_order_placement():
    """C4.2: After a DB write failure has armed the engine, the next
    _place_order_for_leg call MUST NOT reach the adapter — it returns an
    ABORTED order tagged with the db-failure reason. Prevents real money
    from moving while the audit trail is broken."""
    from arbiter.execution.engine import OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._db_write_failed = True  # simulate prior failure

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock()
        engine.adapters = {"kalshi": adapter}

        order = await engine._place_order_for_leg(
            arb_id="ARB-X", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )

        # Adapter MUST NOT be invoked after a DB failure has armed the engine.
        adapter.place_fok.assert_not_awaited()
        assert order.status == OrderStatus.ABORTED
        assert "db write" in order.error.lower()

    asyncio.run(runner())


def test_db_write_failure_is_idempotent():
    """C4.2: Repeated DB failures don't double-arm the kill switch or spam
    the incident deque. The first failure trips and broadcasts; subsequent
    failures just log."""
    from arbiter.execution.engine import OrderStatus, Order as _Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)

        adapter = MagicMock()
        adapter.platform = "kalshi"
        adapter.place_fok = AsyncMock(return_value=_Order(
            order_id="KALSHI-SERVER-Y",
            platform="kalshi", market_id="M", canonical_id="C", side="yes",
            price=0.5, quantity=1, status=OrderStatus.FILLED,
            fill_price=0.5, fill_qty=1,
            external_client_order_id="ARB-2-YES-cafebabe",
        ))
        engine.adapters = {"kalshi": adapter}

        engine.store = AsyncMock()
        engine.store.upsert_order.side_effect = RuntimeError("pg down")
        engine._safety = AsyncMock()

        # First failure -> trip
        await engine._place_order_for_leg(
            arb_id="ARB-2", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        # Second time should be blocked by the gate (no adapter call, no
        # second trip).
        await engine._place_order_for_leg(
            arb_id="ARB-3", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )

        # Only the first call hit the adapter; trip_kill fired once.
        assert adapter.place_fok.await_count == 1
        assert engine._safety.trip_kill.await_count == 1

    asyncio.run(runner())


# ─────────────────────────────────────────────────────────────────────────
# C3: Triple-submit hazard on engine-side timeout.
# When _place_order_for_leg times out, the order's true state on the
# platform is unknown — the lookup may have raced, the cancel may have
# raced. Until we have positive proof that no fill happened, no fallback
# may fire (a retry with a new client_order_id would stack fresh
# exposure on top of any latent fill from attempt 1).
# ─────────────────────────────────────────────────────────────────────────


def test_order_has_idempotency_ambiguous_field_default_false():
    """C3 dataclass smoke: Order carries an ``idempotency_ambiguous`` flag
    so _place_order_for_leg can signal the fallback chain that the leg's
    state is uncertain and retry is unsafe. Defaults to False on normal
    placements; only set True on timeout."""
    from arbiter.execution.engine import Order, OrderStatus
    o = Order(
        order_id="x", platform="kalshi",
        market_id="M", canonical_id="C", side="yes",
        price=0.5, quantity=1, status=OrderStatus.PENDING,
    )
    assert o.idempotency_ambiguous is False


def test_timeout_with_cancelled_orphan_marks_ambiguous():
    """C3: when the timeout-recovery path successfully cancels an orphaned
    order, the leg is still ambiguous — Kalshi's cancel returning success
    does NOT prove zero fill (a partial fill could have raced ahead of the
    cancel). The returned Order MUST carry idempotency_ambiguous=True
    so _execute_with_fallbacks refuses to retry."""
    from arbiter.execution.engine import OrderStatus, Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.05

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        real_order = Order(
            order_id="KALSHI-SERVER-CR3",
            platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, quantity=1,
            status=OrderStatus.SUBMITTED,
            external_client_order_id="ARB-C3-YES-deadbeef",
        )
        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[real_order])
        adapter.cancel_order = AsyncMock(return_value=True)
        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-C3", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert result.status == OrderStatus.CANCELLED
        assert result.idempotency_ambiguous is True, (
            "cancel-after-timeout is ambiguous: partial fill may have raced"
        )

    asyncio.run(runner())


def test_timeout_with_lookup_exception_marks_ambiguous():
    """C3: when the recovery lookup raises, we have no information about
    platform state — must be flagged ambiguous so no retry fires."""
    from arbiter.execution.engine import OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.05

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(
            side_effect=RuntimeError("network blip"),
        )
        adapter.cancel_order = AsyncMock(return_value=False)
        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-LOOKUP-EXC", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert result.status == OrderStatus.FAILED
        assert result.idempotency_ambiguous is True

    asyncio.run(runner())


def test_timeout_with_cancel_failure_marks_ambiguous():
    """C3: lookup finds an open order but cancel fails — the order may
    still be resting or already filled. Definitely ambiguous."""
    from arbiter.execution.engine import OrderStatus, Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.05

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        real_order = Order(
            order_id="KALSHI-SERVER-CF",
            platform="kalshi", market_id="M", canonical_id="C", side="yes",
            price=0.5, quantity=1, status=OrderStatus.SUBMITTED,
        )
        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[real_order])
        adapter.cancel_order = AsyncMock(return_value=False)
        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-CF", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert result.status == OrderStatus.FAILED
        assert result.idempotency_ambiguous is True

    asyncio.run(runner())


def test_timeout_with_empty_lookup_still_ambiguous_by_default():
    """C3 fail-closed: empty lookup is NOT positive proof that the order
    never reached the platform — a filled order would have been removed
    from the open list already. Without a separate filled-order check,
    treat empty as ambiguous so retry is blocked. (When we add a true
    'get_order confirms no fill' check via the adapter, this can flip
    to non-ambiguous; until then, conservative wins.)"""
    from arbiter.execution.engine import OrderStatus

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.05

        adapter = MagicMock()
        adapter.platform = "kalshi"

        async def _hangs(*args, **kwargs):
            await asyncio.sleep(10.0)

        adapter.place_fok = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[])
        adapter.cancel_order = AsyncMock(return_value=False)
        engine.adapters = {"kalshi": adapter}

        result = await engine._place_order_for_leg(
            arb_id="ARB-EMPTY", platform="kalshi",
            market_id="M", canonical_id="C", side="yes",
            price=0.5, qty=1,
        )
        assert result.status == OrderStatus.FAILED
        assert result.idempotency_ambiguous is True

    asyncio.run(runner())


def test_fallback_chain_refuses_retry_when_idempotency_ambiguous():
    """C3 (main): when attempt 1 returns idempotency_ambiguous=True, the
    fallback chain (_execute_with_fallbacks) MUST NOT fire attempt 2 or 3.
    Otherwise a fill that raced the cancel would stack with a fresh
    -F2/-F3 order, producing double exposure.

    Setup: place_fok hangs forever -> timeout -> lookup returns one
    orphan -> cancel succeeds -> attempt 1 returns CANCELLED with
    idempotency_ambiguous=True. _execute_with_fallbacks should return
    immediately without making a second adapter call.
    """
    from arbiter.execution.engine import OrderStatus, Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.execution_timeout_s = 0.05

        adapter = MagicMock()
        adapter.platform = "kalshi"

        # Count how many times place_fok / place_ioc are invoked.
        place_calls: List[str] = []

        async def _hangs(arb_id, *args, **kwargs):
            place_calls.append(arb_id)
            await asyncio.sleep(10.0)

        real_order = Order(
            order_id="KALSHI-SERVER-FB",
            platform="kalshi", market_id="M", canonical_id="C", side="yes",
            price=0.5, quantity=1, status=OrderStatus.SUBMITTED,
            external_client_order_id="ARB-FB-YES-abcd",
        )
        adapter.place_fok = _hangs
        # place_ioc is what _execute_with_fallbacks actually calls first
        adapter.place_ioc = _hangs
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[real_order])
        adapter.cancel_order = AsyncMock(return_value=True)
        engine.adapters = {"kalshi": adapter}

        primary_leg = Order(
            order_id="ARB-FB-NO-primary",
            platform="polymarket",
            market_id="P", canonical_id="C", side="no",
            price=0.5, quantity=1, status=OrderStatus.FILLED,
            fill_price=0.5, fill_qty=1,
        )
        # Build a minimal opp matching the leg
        opp = _make_safety_opp(
            canonical_id="C",
            yes_platform="kalshi", no_platform="polymarket",
            yes_price=0.50, no_price=0.50, suggested_qty=1,
        )

        order = await engine._execute_with_fallbacks(
            arb_id="ARB-FB",
            secondary_platform="kalshi",
            secondary_market="M",
            canonical_id="C",
            secondary_side="yes",
            initial_price=0.5,
            qty=1,
            max_affordable_price=0.55,  # enough room for fallback-2 to be tempting
            primary_leg=primary_leg,
            primary_platform="polymarket",
            opp=opp,
        )

        # Exactly one place attempt — fallback must NOT fire because the
        # leg is idempotency_ambiguous.
        assert len(place_calls) == 1, (
            f"expected 1 placement attempt; got {len(place_calls)}: {place_calls}"
        )
        # The order itself is whatever attempt 1 produced; the test cares
        # that the chain didn't retry.
        assert order.idempotency_ambiguous is True

    asyncio.run(runner())


def test_fallback_chain_still_retries_when_idempotency_proven():
    """C3: when attempt 1 returns FAILED *without* idempotency_ambiguous
    (i.e. a synchronous adapter rejection — order definitively never
    reached the platform), the fallback chain DOES fire attempt 2/3.
    This guards against over-correction: we still want to retry through
    the safer paths after a clean reject."""
    from arbiter.execution.engine import OrderStatus, Order

    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)

        adapter = MagicMock()
        adapter.platform = "kalshi"

        # Track call count; flip to FILLED on attempt 2 so we can confirm
        # the fallback actually fired.
        attempt = {"n": 0}

        async def _place(arb_id, market_id, canonical_id, side, price, qty):
            attempt["n"] += 1
            if attempt["n"] == 1:
                return Order(
                    order_id=f"{arb_id}-{side.upper()}-rejected",
                    platform="kalshi", market_id=market_id,
                    canonical_id=canonical_id, side=side,
                    price=price, quantity=qty,
                    status=OrderStatus.FAILED,
                    fill_price=0.0, fill_qty=0,
                    error="kalshi rejected: market closed (synchronous, idempotency proven)",
                )
            # Attempt 2 succeeds at the wider limit
            return Order(
                order_id=f"{arb_id}-{side.upper()}-filled",
                platform="kalshi", market_id=market_id,
                canonical_id=canonical_id, side=side,
                price=price, quantity=qty,
                status=OrderStatus.FILLED,
                fill_price=price, fill_qty=qty,
            )

        adapter.place_fok = _place
        adapter.place_ioc = _place
        adapter.list_open_orders_by_client_id = AsyncMock(return_value=[])
        adapter.cancel_order = AsyncMock(return_value=False)
        engine.adapters = {"kalshi": adapter}

        primary_leg = Order(
            order_id="ARB-OK-NO-primary",
            platform="polymarket",
            market_id="P", canonical_id="C", side="no",
            price=0.5, quantity=1, status=OrderStatus.FILLED,
            fill_price=0.5, fill_qty=1,
        )
        opp = _make_safety_opp(
            canonical_id="C",
            yes_platform="kalshi", no_platform="polymarket",
            yes_price=0.50, no_price=0.50, suggested_qty=1,
        )

        order = await engine._execute_with_fallbacks(
            arb_id="ARB-OK",
            secondary_platform="kalshi",
            secondary_market="M",
            canonical_id="C",
            secondary_side="yes",
            initial_price=0.50,
            qty=1,
            # Big enough delta so fallback-2 actually fires (>0.005)
            max_affordable_price=0.55,
            primary_leg=primary_leg,
            primary_platform="polymarket",
            opp=opp,
        )
        assert attempt["n"] == 2, (
            "synchronous reject is idempotency-proven — fallback SHOULD fire"
        )
        assert order.status == OrderStatus.FILLED

    asyncio.run(runner())


def test_handle_db_failure_records_critical_incident_and_trips_safety():
    """C4.2: The _handle_db_failure helper is the single entrypoint that
    every `try/except` around a `self.store.*` call funnels into. It must:
      - set ``self._db_write_failed = True`` (block flag)
      - append a critical incident to the in-memory deque with
        ``metadata.event_type == 'db_write_failure'``
      - invoke ``SafetySupervisor.trip_kill`` exactly once

    No DB writes are attempted to record the incident — the store is
    presumed broken, so the in-memory deque is the source of truth.
    """
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine._safety = AsyncMock()

        await engine._handle_db_failure(
            op="record_arb",
            arb_id="ARB-7",
            canonical_id="MKT1",
            exc=RuntimeError("pg down"),
        )

        assert engine._db_write_failed is True
        engine._safety.trip_kill.assert_awaited_once()
        critical = [
            i for i in engine._incidents
            if i.severity == "critical"
            and i.metadata.get("event_type") == "db_write_failure"
        ]
        assert len(critical) == 1, "expected exactly one critical db incident"
        assert critical[0].metadata.get("op") == "record_arb"

    asyncio.run(runner())


def _fake_supervisor():
    """AsyncMock supervisor with realistic is_armed/trip_kill semantics.

    The default ``AsyncMock`` auto-creates a truthy ``is_armed`` attribute
    which makes the engine's "already armed → skip" guard trigger on every
    call. We mimic the real supervisor by flipping ``is_armed`` to True
    only after ``trip_kill`` is awaited.
    """
    safety = AsyncMock()
    safety.is_armed = False

    async def _trip_kill(*args, **kwargs):
        safety.is_armed = True
        return None

    safety.trip_kill = AsyncMock(side_effect=_trip_kill)
    return safety


def test_daily_loss_does_not_trip_below_limit():
    """Sanity guard: when cumulative daily loss is still inside the limit,
    the kill switch must NOT be tripped — that would halt trading on noise."""
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.risk._max_daily_loss = -50.0
        engine._safety = _fake_supervisor()

        # Two trades that don't combine to exceed the limit.
        engine.risk.record_trade("M1", 10.0, -20.0)
        await engine._maybe_halt_on_daily_loss()
        engine._safety.trip_kill.assert_not_awaited()

        engine.risk.record_trade("M2", 10.0, -15.0)
        await engine._maybe_halt_on_daily_loss()
        engine._safety.trip_kill.assert_not_awaited()

    asyncio.run(runner())


def test_daily_loss_trips_kill_switch_when_exceeded():
    """Cumulative daily losses crossing _max_daily_loss must trip the
    kill switch so the system HALTS — not merely reject the next trade.

    Otherwise normal-looking opportunities continue to be picked up
    until the limit is breached on every one, burning capital."""
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.risk._max_daily_loss = -50.0
        engine._safety = _fake_supervisor()

        # Two trades that combine to cross the -$50 threshold.
        engine.risk.record_trade("M1", 10.0, -30.0)
        await engine._maybe_halt_on_daily_loss()
        engine._safety.trip_kill.assert_not_awaited()

        engine.risk.record_trade("M2", 10.0, -25.0)  # cumulative -$55
        await engine._maybe_halt_on_daily_loss()
        engine._safety.trip_kill.assert_awaited_once()
        call_kwargs = engine._safety.trip_kill.await_args.kwargs
        assert call_kwargs.get("by") == "system:daily_loss"
        assert "daily loss" in call_kwargs.get("reason", "").lower()

    asyncio.run(runner())


def test_daily_loss_unwind_pnl_booked_into_risk_manager():
    """Unwind realised loss must roll into RiskManager._daily_pnl so the
    daily-loss check sees the real cumulative loss, not just the entry
    edge. Before this fix, unwind losses landed only on
    execution.realized_pnl and the daily-loss kill switch was blind to them."""
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.risk._max_daily_loss = -50.0
        engine._safety = _fake_supervisor()

        starting_pnl = engine.risk._daily_pnl
        # Book a large unwind loss directly (simulates the path where
        # _recover_one_leg_risk returns a negative unwind_pnl).
        await engine._maybe_halt_on_daily_loss(unwind_pnl=-75.0)
        assert engine.risk._daily_pnl == pytest.approx(starting_pnl - 75.0)
        engine._safety.trip_kill.assert_awaited_once()

    asyncio.run(runner())


def test_daily_loss_trip_kill_is_idempotent():
    """Multiple breaches in a row must not spam trip_kill — once is enough
    (the supervisor itself is idempotent, but the engine should also
    avoid unnecessary calls)."""
    async def runner():
        store = PriceStore(ttl=60)
        engine = make_engine(store)
        engine.risk._max_daily_loss = -50.0
        engine._safety = _fake_supervisor()

        engine.risk.record_trade("M1", 10.0, -80.0)
        await engine._maybe_halt_on_daily_loss()
        await engine._maybe_halt_on_daily_loss()
        # Once is_armed flips True after the first trip_kill, subsequent
        # calls must short-circuit.
        assert engine._safety.trip_kill.await_count == 1
# ─────────────────────────────────────────────────────────────────────────
# Exception-escape gap (silent-naked bug): if an exception raises between
# primary fill and secondary order construction (e.g. best_executable_price
# blows up on a network blip, compute_fee KeyErrors on a new platform),
# the engine MUST still persist an ABORTED-EXCEPTION secondary placeholder,
# write both legs via record_arb, route through one-leg-exposure recovery
# so the supervisor fanout fires, then re-raise. Without this, primary sat
# FILLED in DB with no secondary record and no naked incident was raised.
# ─────────────────────────────────────────────────────────────────────────


def test_secondary_exception_after_primary_fill_persists_naked_and_reraises():
    """Primary FILLs on Kalshi; Polymarket's ``best_executable_price`` raises.
    Without the fix the exception escapes ``_live_execution`` entirely:
    the primary row sits FILLED in DB and no second leg row, naked-incident,
    or supervisor call is recorded. With the fix the engine constructs an
    ABORTED ``-EXCEPTION`` placeholder for the secondary, persists both
    legs through ``record_arb``, fires the one-leg-exposure recovery path
    (which raises the structured incident and calls the supervisor), and
    re-raises the original exception so callers still see the failure."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        import os as _os_test
        _old_exec_order = _os_test.environ.get("EXECUTION_ORDER")
        _os_test.environ["EXECUTION_ORDER"] = "kalshi_first"
        try:
            engine = _build_live_engine_for_safe02_gap()

            kalshi = MagicMock()
            kalshi.platform = "kalshi"
            _wire_live_depth(kalshi, 0.60)
            kalshi.place_fok = AsyncMock(
                return_value=Order(
                    order_id="ARB-000999-YES-KALSHI",
                    platform="kalshi",
                    market_id="K-MKT1",
                    canonical_id="MKT1",
                    side="yes",
                    price=0.60,
                    quantity=100,
                    status=OrderStatus.FILLED,
                    fill_qty=100,
                    fill_price=0.60,
                    timestamp=time.time(),
                )
            )
            kalshi.cancel_order = AsyncMock(return_value=True)
            _wire_unwind_failed(kalshi)

            poly = MagicMock()
            poly.platform = "polymarket"
            poly.check_depth = AsyncMock(return_value=(True, 0.30))
            # The bug surface: secondary path calls
            # secondary_adapter.best_executable_price(...) BEFORE constructing
            # any Order for the secondary leg. A raise here previously
            # escaped _live_execution outright.
            poly.best_executable_price = AsyncMock(
                side_effect=RuntimeError("simulated poly network blip")
            )
            _wire_unwind_failed(poly)

            engine.adapters = {"kalshi": kalshi, "polymarket": poly}
            engine.store = AsyncMock()
            engine._safety = AsyncMock()

            opp = _make_safety_opp(
                canonical_id="MKT1",
                yes_platform="kalshi",
                no_platform="polymarket",
                yes_price=0.60,
                no_price=0.30,
                suggested_qty=100,
            )

            with pytest.raises(RuntimeError, match="simulated poly network blip"):
                await engine._live_execution("ARB-000999", opp)

            # record_arb must have been called with both legs — the primary
            # FILLED on Kalshi and an ABORTED ``-EXCEPTION`` placeholder on
            # Polymarket — so the audit trail reflects the naked state.
            engine.store.record_arb.assert_awaited()
            persisted = engine.store.record_arb.await_args.args[0]
            assert persisted.status == "recovering"
            assert persisted.leg_yes.status == OrderStatus.FILLED
            assert persisted.leg_yes.platform == "kalshi"
            assert persisted.leg_no.status == OrderStatus.ABORTED
            assert persisted.leg_no.platform == "polymarket"
            assert "-EXCEPTION" in persisted.leg_no.order_id
            assert "simulated poly network blip" in (persisted.leg_no.error or "")

            # The supervisor's one-leg-exposure fanout MUST be invoked so
            # Telegram + WS channels page the operator. ``_recover_one_leg_risk``
            # is the only call site for this hook and runs inside the same
            # _live_execution invocation that re-raised.
            engine._safety.handle_one_leg_exposure.assert_called_once()
            args = engine._safety.handle_one_leg_exposure.call_args.args
            incident_arg, filled_arg, failed_arg, _opp_arg = args
            assert filled_arg.status == OrderStatus.FILLED
            assert failed_arg.status == OrderStatus.ABORTED
            assert incident_arg.metadata.get("event_type") == "one_leg_exposure"

            # A dedicated ``secondary_execution_exception`` incident with
            # the exception type captured in metadata must also be emitted
            # so the operator can distinguish exception-escape naked legs
            # from venue-rejection naked legs in the post-mortem.
            exception_incidents = [
                i for i in engine._incidents
                if i.metadata.get("event_type") == "secondary_execution_exception"
            ]
            assert len(exception_incidents) == 1
            assert exception_incidents[0].severity == "critical"
            assert exception_incidents[0].metadata.get("exception_type") == "RuntimeError"
        finally:
            if _old_exec_order is None:
                _os_test.environ.pop("EXECUTION_ORDER", None)
            else:
                _os_test.environ["EXECUTION_ORDER"] = _old_exec_order

    asyncio.run(runner())


# ─────────────────────────────────────────────────────────────────────────
# Audit 2026-05: arb_pnl must be tracked separately from naked-leg unwind
# PnL so the profitability gate cannot be fooled by directional luck on
# unhedged positions ($129.48 of $135.38 reported "profit" was naked-leg
# payouts, not arbitrage edge).  Validate the ArbExecution dataclass split.
# ─────────────────────────────────────────────────────────────────────────


def test_arb_execution_separates_arb_pnl_from_unwind_pnl():
    """``arb_pnl`` is the property that exposes pure arbitrage edge; it
    equals ``realized_pnl - unwind_pnl``.  Pre-migration trades have
    ``unwind_pnl=0`` so ``arb_pnl == realized_pnl`` for them.
    """
    from arbiter.execution.engine import ArbExecution, Order, OrderStatus

    opp = ArbitrageOpportunity(
        canonical_id="C", description="d",
        yes_platform="kalshi", yes_price=0.50, yes_fee=0.0, yes_market_id="K",
        no_platform="polymarket", no_price=0.45, no_fee=0.0, no_market_id="P",
        gross_edge=0.05, total_fees=0.0, net_edge=0.05, net_edge_cents=5.0,
        suggested_qty=10, max_profit_usd=0.50, timestamp=time.time(),
    )
    leg_yes = Order(order_id="A-Y", platform="kalshi", market_id="K", canonical_id="C",
                    side="yes", price=0.50, quantity=10,
                    status=OrderStatus.FILLED, fill_price=0.50, fill_qty=10)
    leg_no = Order(order_id="A-N", platform="polymarket", market_id="P", canonical_id="C",
                   side="no", price=0.45, quantity=10,
                   status=OrderStatus.FILLED, fill_price=0.45, fill_qty=10)

    # Case 1: both legs filled — realized_pnl IS arb_pnl, no unwind component.
    arb = ArbExecution(
        arb_id="ARB-PNL-1", opportunity=opp, leg_yes=leg_yes, leg_no=leg_no,
        status="filled", realized_pnl=0.50, unwind_pnl=0.0,
        timestamp=time.time(),
    )
    assert arb.arb_pnl == pytest.approx(0.50)
    assert arb.to_dict()["arb_pnl"] == pytest.approx(0.50)
    assert arb.to_dict()["unwind_pnl"] == pytest.approx(0.0)

    # Case 2: naked leg recovered via unwind — most of "profit" came from
    # the directional unwind, only a small slice is real arb edge.
    arb_naked = ArbExecution(
        arb_id="ARB-PNL-2", opportunity=opp, leg_yes=leg_yes, leg_no=leg_no,
        status="recovering", realized_pnl=10.0, unwind_pnl=9.5,
        timestamp=time.time(),
    )
    # arb_pnl carries only the small "real" arbitrage slice.
    assert arb_naked.arb_pnl == pytest.approx(0.5)
    assert arb_naked.to_dict()["realized_pnl"] == pytest.approx(10.0)
    assert arb_naked.to_dict()["unwind_pnl"] == pytest.approx(9.5)
    assert arb_naked.to_dict()["arb_pnl"] == pytest.approx(0.5)

    # Case 3: pre-migration rehydrated row (default unwind_pnl=0) — arb_pnl
    # falls back to full realized_pnl for backward-compat.
    arb_legacy = ArbExecution(
        arb_id="ARB-PNL-3", opportunity=opp, leg_yes=leg_yes, leg_no=leg_no,
        status="filled", realized_pnl=2.75,
        timestamp=time.time(),
    )
    assert arb_legacy.unwind_pnl == 0.0
    assert arb_legacy.arb_pnl == pytest.approx(2.75)


def test_fok_slippage_buffer_lifts_primary_price_by_two_ticks_default():
    """FILL-01 + 2026-05-26 zero-fill audit: the engine lifts the FOK
    primary limit by FOK_SLIPPAGE_TICKS ticks above
    ``best_executable_price`` so the order absorbs an adverse book
    move during wire latency. Default was 1 tick — raised to 2 after
    the live ARB-000683 trace showed Polymarket sports books moving
    1+ ticks in the 552ms walk-to-place window even with the original
    buffer.

    Wires both legs to FILLED so the test focuses on what limit the
    primary FOK was placed at, not on naked-leg recovery paths.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        import os as _os_test
        _old_exec = _os_test.environ.get("EXECUTION_ORDER")
        _os_test.environ["EXECUTION_ORDER"] = "poly_first"  # match prod
        _old_buf = _os_test.environ.get("FOK_SLIPPAGE_TICKS")
        _old_pp_buf = _os_test.environ.get("FOK_SLIPPAGE_TICKS_POLYMARKET")
        _os_test.environ.pop("FOK_SLIPPAGE_TICKS", None)            # use default (2)
        _os_test.environ.pop("FOK_SLIPPAGE_TICKS_POLYMARKET", None)
        try:
            engine = _build_live_engine_for_safe02_gap()

            def _mk_filled_adapter(platform: str, side: str, walked_price: float, qty: int):
                adapter = MagicMock()
                adapter.platform = platform
                _wire_live_depth(adapter, walked_price)
                # Echo back the price we were ASKED to send so the test
                # can verify the buffer math without coupling to engine
                # internals.
                async def _place(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                    return Order(
                        order_id=f"ARB-FOK-BUF-{platform.upper()}",
                        platform=platform, market_id=market_id,
                        canonical_id=canonical_id, side=side_,
                        price=price, quantity=qty_,
                        status=OrderStatus.FILLED,
                        fill_qty=qty_, fill_price=price,
                        timestamp=time.time(),
                    )
                adapter.place_fok = AsyncMock(side_effect=_place)
                adapter.place_ioc = AsyncMock(side_effect=_place)
                adapter.get_order = AsyncMock()
                adapter.cancel_order = AsyncMock(return_value=True)
                return adapter

            poly = _mk_filled_adapter("polymarket", "yes", 0.40, 100)
            kalshi = _mk_filled_adapter("kalshi", "no", 0.30, 100)
            engine.adapters = {"polymarket": poly, "kalshi": kalshi}

            opp = _make_safety_opp(
                canonical_id="MKT1",
                yes_platform="polymarket",
                no_platform="kalshi",
                yes_price=0.40,
                no_price=0.30,
                suggested_qty=100,
            )

            result = await engine.execute_opportunity(opp)
            assert result is not None
            assert result.status in {"filled", "recovering"}

            # Primary (polymarket YES) walked at 0.40; with the new
            # 2-tick default the buffered limit is 0.42 (0.40 + 0.02).
            # FILL-02 (2026-05-27): primary now uses IOC by default —
            # the buffer still applies the same way, but the call lands
            # on place_ioc instead of place_fok.
            primary_mock = poly.place_ioc if poly.place_ioc.await_count > 0 else poly.place_fok
            primary_mock.assert_awaited()
            sent_price = float(primary_mock.await_args.kwargs.get(
                "price",
                primary_mock.await_args.args[4] if len(primary_mock.await_args.args) > 4 else 0.0,
            ))
            assert abs(sent_price - 0.42) < 1e-9, (
                f"FOK_SLIPPAGE_TICKS default=2 must lift 0.40 → 0.42; got {sent_price}"
            )
        finally:
            if _old_exec is None:
                _os_test.environ.pop("EXECUTION_ORDER", None)
            else:
                _os_test.environ["EXECUTION_ORDER"] = _old_exec
            if _old_buf is None:
                _os_test.environ.pop("FOK_SLIPPAGE_TICKS", None)
            else:
                _os_test.environ["FOK_SLIPPAGE_TICKS"] = _old_buf
            if _old_pp_buf is None:
                _os_test.environ.pop("FOK_SLIPPAGE_TICKS_POLYMARKET", None)
            else:
                _os_test.environ["FOK_SLIPPAGE_TICKS_POLYMARKET"] = _old_pp_buf

    asyncio.run(runner())


def test_fok_slippage_buffer_per_platform_override():
    """FOK_SLIPPAGE_TICKS_<PLATFORM> takes precedence over the global
    default. Lets the operator dial Polymarket up (fast-moving sports)
    while keeping Kalshi at the cheaper 1-tick setting.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        import os as _os_test
        _old_exec = _os_test.environ.get("EXECUTION_ORDER")
        _old_global = _os_test.environ.get("FOK_SLIPPAGE_TICKS")
        _old_poly = _os_test.environ.get("FOK_SLIPPAGE_TICKS_POLYMARKET")
        _os_test.environ["EXECUTION_ORDER"] = "poly_first"
        _os_test.environ["FOK_SLIPPAGE_TICKS"] = "1"            # global
        _os_test.environ["FOK_SLIPPAGE_TICKS_POLYMARKET"] = "3"  # per-platform
        try:
            engine = _build_live_engine_for_safe02_gap()

            def _mk_filled_adapter(platform: str, side: str, walked_price: float, qty: int):
                adapter = MagicMock()
                adapter.platform = platform
                _wire_live_depth(adapter, walked_price)
                async def _place(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                    return Order(
                        order_id=f"ARB-FOK-PP-{platform.upper()}",
                        platform=platform, market_id=market_id,
                        canonical_id=canonical_id, side=side_,
                        price=price, quantity=qty_,
                        status=OrderStatus.FILLED,
                        fill_qty=qty_, fill_price=price,
                        timestamp=time.time(),
                    )
                adapter.place_fok = AsyncMock(side_effect=_place)
                adapter.place_ioc = AsyncMock(side_effect=_place)
                adapter.get_order = AsyncMock()
                adapter.cancel_order = AsyncMock(return_value=True)
                return adapter

            poly = _mk_filled_adapter("polymarket", "yes", 0.40, 100)
            kalshi = _mk_filled_adapter("kalshi", "no", 0.30, 100)
            engine.adapters = {"polymarket": poly, "kalshi": kalshi}

            opp = _make_safety_opp(
                canonical_id="MKT1",
                yes_platform="polymarket",
                no_platform="kalshi",
                yes_price=0.40, no_price=0.30,
                suggested_qty=100,
            )

            await engine.execute_opportunity(opp)
            primary_mock = poly.place_ioc if poly.place_ioc.await_count > 0 else poly.place_fok
            primary_mock.assert_awaited()
            sent_price = float(primary_mock.await_args.kwargs.get(
                "price",
                primary_mock.await_args.args[4] if len(primary_mock.await_args.args) > 4 else 0.0,
            ))
            # Per-platform override (3 ticks) wins over the global (1)
            assert abs(sent_price - 0.43) < 1e-9, (
                f"PLATFORM override 3 ticks must lift 0.40 → 0.43; got {sent_price}"
            )
        finally:
            for k, v in (
                ("EXECUTION_ORDER", _old_exec),
                ("FOK_SLIPPAGE_TICKS", _old_global),
                ("FOK_SLIPPAGE_TICKS_POLYMARKET", _old_poly),
            ):
                if v is None:
                    _os_test.environ.pop(k, None)
                else:
                    _os_test.environ[k] = v

    asyncio.run(runner())


def test_zero_fill_ioc_does_not_trigger_false_naked_leg_recovery():
    """FILL-02 regression (ARB-000694, 2026-05-27): when the IOC
    primary returns status=FILLED with fill_qty=0 (venue accepted but
    matched zero contracts), the engine must NOT route to naked-leg
    recovery. There is no exposure to unwind. Live deploy 2026-05-27
    flagged this case as a CRITICAL incident with exposure_usd=$0,
    blocking the readiness gate.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        import os as _os_test
        _old_exec = _os_test.environ.get("EXECUTION_ORDER")
        _os_test.environ["EXECUTION_ORDER"] = "poly_first"
        try:
            engine = _build_live_engine_for_safe02_gap()

            async def _poly_zero_fill_ioc(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                # Mimic Polymarket's "IOC accepted, matched zero" path.
                return Order(
                    order_id="POLY-IOC-ZERO", platform="polymarket",
                    market_id=market_id, canonical_id=canonical_id,
                    side=side_, price=price, quantity=qty_,
                    status=OrderStatus.FILLED,
                    fill_qty=0, fill_price=price,
                    timestamp=time.time(),
                )

            poly = MagicMock()
            poly.platform = "polymarket"
            _wire_live_depth(poly, 0.40)
            poly.place_ioc = AsyncMock(side_effect=_poly_zero_fill_ioc)
            poly.place_fok = AsyncMock(side_effect=_poly_zero_fill_ioc)
            poly.get_order = AsyncMock()
            poly.cancel_order = AsyncMock(return_value=True)

            kalshi = MagicMock()
            kalshi.platform = "kalshi"
            _wire_live_depth(kalshi, 0.30)
            # Kalshi MUST NOT be called — primary zero-filled, so the
            # engine should skip the secondary entirely.
            kalshi.place_fok = AsyncMock()
            kalshi.place_ioc = AsyncMock()
            kalshi.get_order = AsyncMock()
            kalshi.cancel_order = AsyncMock(return_value=True)
            engine.adapters = {"polymarket": poly, "kalshi": kalshi}

            opp = _make_safety_opp(
                canonical_id="MKT1",
                yes_platform="polymarket",
                no_platform="kalshi",
                yes_price=0.40, no_price=0.30, suggested_qty=10,
            )
            result = await engine.execute_opportunity(opp)
            assert result is not None
            # Status must be "failed", NOT "recovering". No exposure,
            # nothing to unwind.
            assert result.status == "failed", (
                f"Zero-fill IOC must produce status=failed, not "
                f"recovering. Got: {result.status}"
            )
            # No critical "one_leg_exposure" incident should have fired.
            # (We don't have direct access to engine.incidents here,
            # but the .status==failed gate is what kept it in-spec.)
            # Secondary must NOT have been attempted.
            assert kalshi.place_fok.await_count == 0
            assert kalshi.place_ioc.await_count == 0
        finally:
            if _old_exec is None:
                _os_test.environ.pop("EXECUTION_ORDER", None)
            else:
                _os_test.environ["EXECUTION_ORDER"] = _old_exec

    asyncio.run(runner())


def test_primary_partial_fill_scales_secondary_to_actual_qty():
    """FILL-02 (2026-05-27): when the IOC primary partially fills,
    the engine must size the secondary to the ACTUAL primary fill_qty
    instead of skipping the secondary entirely.

    Reproduces the ARB-000683..690 zero-fill streak where Polymarket
    FOK rejected 100% of orders even though SOME depth existed at
    our limit. With IOC + partial-fill scaling, even a 1-of-12 fill
    gets paired on the secondary so we capture the available arb
    instead of losing 100% of opportunities.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        import os as _os_test
        _old_exec = _os_test.environ.get("EXECUTION_ORDER")
        _old_tif = _os_test.environ.get("EXECUTOR_PRIMARY_TIF")
        _os_test.environ["EXECUTION_ORDER"] = "poly_first"
        _os_test.environ.pop("EXECUTOR_PRIMARY_TIF", None)  # default IOC
        try:
            engine = _build_live_engine_for_safe02_gap()

            # Primary (poly) IOC fills only 4 of 12 contracts.
            captured = {"primary_qty": None, "secondary_qty": None}
            async def _poly_ioc(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                captured["primary_qty"] = qty_
                return Order(
                    order_id="POLY-IOC-PARTIAL", platform="polymarket",
                    market_id=market_id, canonical_id=canonical_id,
                    side=side_, price=price, quantity=qty_,
                    status=OrderStatus.FILLED,   # any-fill status
                    fill_qty=4, fill_price=price,
                    timestamp=time.time(),
                )
            async def _poly_fok(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                # Should NOT be called when IOC is enabled
                return Order(
                    order_id="POLY-FOK-UNEXPECTED", platform="polymarket",
                    market_id=market_id, canonical_id=canonical_id,
                    side=side_, price=price, quantity=qty_,
                    status=OrderStatus.FAILED, timestamp=time.time(),
                )

            poly = MagicMock()
            poly.platform = "polymarket"
            _wire_live_depth(poly, 0.40)
            poly.place_ioc = AsyncMock(side_effect=_poly_ioc)
            poly.place_fok = AsyncMock(side_effect=_poly_fok)
            poly.get_order = AsyncMock()
            poly.cancel_order = AsyncMock(return_value=True)

            async def _kalshi_place(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                captured["secondary_qty"] = qty_
                return Order(
                    order_id="KALSHI-FILLED", platform="kalshi",
                    market_id=market_id, canonical_id=canonical_id,
                    side=side_, price=price, quantity=qty_,
                    status=OrderStatus.FILLED,
                    fill_qty=qty_, fill_price=price,
                    timestamp=time.time(),
                )
            kalshi = MagicMock()
            kalshi.platform = "kalshi"
            _wire_live_depth(kalshi, 0.30)
            kalshi.place_fok = AsyncMock(side_effect=_kalshi_place)
            kalshi.place_ioc = AsyncMock(side_effect=_kalshi_place)
            kalshi.get_order = AsyncMock()
            kalshi.cancel_order = AsyncMock(return_value=True)

            engine.adapters = {"polymarket": poly, "kalshi": kalshi}

            opp = _make_safety_opp(
                canonical_id="MKT1",
                yes_platform="polymarket",
                no_platform="kalshi",
                yes_price=0.40, no_price=0.30,
                suggested_qty=12,
            )

            result = await engine.execute_opportunity(opp)
            assert result is not None

            # Primary asked for 12, returned fill_qty=4.
            assert captured["primary_qty"] == 12
            # Secondary must be scaled to the actual primary fill (4),
            # not the original intended qty (12).
            assert captured["secondary_qty"] == 4, (
                f"Secondary should scale to primary fill_qty=4, got "
                f"{captured['secondary_qty']}"
            )
            # Primary path used IOC, not FOK.
            assert poly.place_ioc.await_count == 1
            assert poly.place_fok.await_count == 0
        finally:
            if _old_exec is None:
                _os_test.environ.pop("EXECUTION_ORDER", None)
            else:
                _os_test.environ["EXECUTION_ORDER"] = _old_exec
            if _old_tif is None:
                _os_test.environ.pop("EXECUTOR_PRIMARY_TIF", None)
            else:
                _os_test.environ["EXECUTOR_PRIMARY_TIF"] = _old_tif

    asyncio.run(runner())


def test_executor_primary_tif_fok_preserves_legacy_atomic_behavior():
    """EXECUTOR_PRIMARY_TIF=FOK opts back into atomic-or-skip primary
    placement. Zero-fill primary → skipped secondary, same as before
    FILL-02. The env var is the kill-switch for the IOC default."""
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        import os as _os_test
        _old_exec = _os_test.environ.get("EXECUTION_ORDER")
        _old_tif = _os_test.environ.get("EXECUTOR_PRIMARY_TIF")
        _os_test.environ["EXECUTION_ORDER"] = "poly_first"
        _os_test.environ["EXECUTOR_PRIMARY_TIF"] = "FOK"
        try:
            engine = _build_live_engine_for_safe02_gap()

            async def _ioc(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                return Order(
                    order_id="UNEXPECTED-IOC", platform="polymarket",
                    market_id=market_id, canonical_id=canonical_id,
                    side=side_, price=price, quantity=qty_,
                    status=OrderStatus.FAILED, timestamp=time.time(),
                )
            async def _fok(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                return Order(
                    order_id="POLY-FOK-FILLED", platform="polymarket",
                    market_id=market_id, canonical_id=canonical_id,
                    side=side_, price=price, quantity=qty_,
                    status=OrderStatus.FILLED,
                    fill_qty=qty_, fill_price=price,
                    timestamp=time.time(),
                )

            poly = MagicMock()
            poly.platform = "polymarket"
            _wire_live_depth(poly, 0.40)
            poly.place_ioc = AsyncMock(side_effect=_ioc)
            poly.place_fok = AsyncMock(side_effect=_fok)
            poly.get_order = AsyncMock()
            poly.cancel_order = AsyncMock(return_value=True)

            kalshi = MagicMock()
            kalshi.platform = "kalshi"
            _wire_live_depth(kalshi, 0.30)
            async def _k(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                return Order(
                    order_id="KALSHI-OK", platform="kalshi",
                    market_id=market_id, canonical_id=canonical_id,
                    side=side_, price=price, quantity=qty_,
                    status=OrderStatus.FILLED, fill_qty=qty_, fill_price=price,
                    timestamp=time.time(),
                )
            kalshi.place_fok = AsyncMock(side_effect=_k)
            kalshi.place_ioc = AsyncMock(side_effect=_k)
            kalshi.get_order = AsyncMock()
            kalshi.cancel_order = AsyncMock(return_value=True)
            engine.adapters = {"polymarket": poly, "kalshi": kalshi}

            opp = _make_safety_opp(
                canonical_id="MKT1",
                yes_platform="polymarket",
                no_platform="kalshi",
                yes_price=0.40, no_price=0.30, suggested_qty=10,
            )
            await engine.execute_opportunity(opp)
            # EXECUTOR_PRIMARY_TIF=FOK routes primary to FOK, NOT IOC.
            assert poly.place_fok.await_count == 1
            assert poly.place_ioc.await_count == 0
        finally:
            if _old_exec is None:
                _os_test.environ.pop("EXECUTION_ORDER", None)
            else:
                _os_test.environ["EXECUTION_ORDER"] = _old_exec
            if _old_tif is None:
                _os_test.environ.pop("EXECUTOR_PRIMARY_TIF", None)
            else:
                _os_test.environ["EXECUTOR_PRIMARY_TIF"] = _old_tif

    asyncio.run(runner())


def test_fok_slippage_buffer_can_be_disabled_via_env():
    """FILL-01 kill-switch: setting FOK_SLIPPAGE_TICKS=0 restores the
    legacy no-buffer behavior. The buffer is the fix for the 2026-05-14
    zero-fill streak but must be controllable in case a future
    regression makes the unbuffered path the safer one.
    """
    from arbiter.execution.engine import Order, OrderStatus

    async def runner():
        import os as _os_test
        _old_exec = _os_test.environ.get("EXECUTION_ORDER")
        _os_test.environ["EXECUTION_ORDER"] = "poly_first"
        _old_buf = _os_test.environ.get("FOK_SLIPPAGE_TICKS")
        _os_test.environ["FOK_SLIPPAGE_TICKS"] = "0"
        try:
            engine = _build_live_engine_for_safe02_gap()

            def _mk_filled_adapter(platform: str, walked_price: float):
                adapter = MagicMock()
                adapter.platform = platform
                _wire_live_depth(adapter, walked_price)
                async def _place(arb_id, market_id, canonical_id, side_, price, qty_, max_affordable=None):
                    return Order(
                        order_id=f"ARB-NOBUF-{platform.upper()}",
                        platform=platform, market_id=market_id,
                        canonical_id=canonical_id, side=side_,
                        price=price, quantity=qty_,
                        status=OrderStatus.FILLED,
                        fill_qty=qty_, fill_price=price,
                        timestamp=time.time(),
                    )
                adapter.place_fok = AsyncMock(side_effect=_place)
                adapter.place_ioc = AsyncMock(side_effect=_place)
                adapter.get_order = AsyncMock()
                adapter.cancel_order = AsyncMock(return_value=True)
                return adapter

            poly = _mk_filled_adapter("polymarket", 0.40)
            kalshi = _mk_filled_adapter("kalshi", 0.30)
            engine.adapters = {"polymarket": poly, "kalshi": kalshi}

            opp = _make_safety_opp(
                canonical_id="MKT1",
                yes_platform="polymarket",
                no_platform="kalshi",
                yes_price=0.40,
                no_price=0.30,
                suggested_qty=100,
            )

            result = await engine.execute_opportunity(opp)
            assert result is not None

            # FILL-02: primary defaults to IOC now; route the assertion
            # to whichever method actually fired.
            primary_mock = poly.place_ioc if poly.place_ioc.await_count > 0 else poly.place_fok
            sent_price = float(primary_mock.await_args.kwargs.get(
                "price",
                primary_mock.await_args.args[4] if len(primary_mock.await_args.args) > 4 else 0.0,
            ))
            assert abs(sent_price - 0.40) < 1e-9, (
                f"FOK_SLIPPAGE_TICKS=0 must send at walked price 0.40; got {sent_price}"
            )
        finally:
            if _old_exec is None:
                _os_test.environ.pop("EXECUTION_ORDER", None)
            else:
                _os_test.environ["EXECUTION_ORDER"] = _old_exec
            if _old_buf is None:
                _os_test.environ.pop("FOK_SLIPPAGE_TICKS", None)
            else:
                _os_test.environ["FOK_SLIPPAGE_TICKS"] = _old_buf

    asyncio.run(runner())


def test_unprofitable_path_does_not_raise_unbound_local():
    """Regression: the unprofitability early-return at engine.py:1213 used
    to fall through to the post-record_arb re-raise tail and hit
    UnboundLocalError on ``_secondary_exception`` (engine.py:2084). The
    variable is now hoisted above the IF/ELSE so every path has a
    definition.

    Triggering the IF branch requires the pre-trade RiskManager.check_
    trade to pass (so we don't get filtered out earlier) and THEN the
    engine's recomputation from prices+fees to come in BELOW min. We
    engineer that by setting yes_price + no_price ≈ 1.0 so gross_edge
    ≈ 0 and net_edge_after_fees ≈ -fees, while leaving the opp's
    reported net_edge_cents at 7 (passes check_trade with min=1).
    """
    from arbiter.execution.engine import OrderStatus

    async def runner():
        engine = _build_live_engine_for_safe02_gap()
        # check_trade min low enough to pass on the opp's reported 7c;
        # recompute floor at 1c so a near-zero recompute trips the IF.
        engine.scanner_config.min_edge_cents = 1.0

        kalshi = MagicMock()
        kalshi.platform = "kalshi"
        _wire_live_depth(kalshi, 0.50)
        poly = MagicMock()
        poly.platform = "polymarket"
        _wire_live_depth(poly, 0.50)
        engine.adapters = {"kalshi": kalshi, "polymarket": poly}

        opp = _make_safety_opp(
            canonical_id="MKT1",
            yes_platform="polymarket",
            no_platform="kalshi",
            yes_price=0.50,
            no_price=0.50,  # gross=0, recompute net < 1c → IF branch
            suggested_qty=10,
        )

        # Must NOT raise UnboundLocalError; must finish with aborted legs.
        result = await engine.execute_opportunity(opp)
        assert result is not None, (
            "execute_opportunity returned None — pre-trade check may "
            "now block the path; broaden the test if so."
        )
        assert result.leg_yes.status == OrderStatus.ABORTED
        assert result.leg_no.status == OrderStatus.ABORTED

    asyncio.run(runner())


def _build_concurrency_adapter(platform: str, price: float, in_flight: dict, hold_s: float):
    """Build a FILLED-returning adapter whose primary FOK takes ``hold_s``
    seconds to complete, while tracking observed concurrency in ``in_flight``.

    in_flight = {"current": 0, "max": 0} — incremented on entry, decremented
    on exit. The MAX value observed is what the per-canonical-lock test
    asserts against.
    """
    from arbiter.execution.engine import Order, OrderStatus

    adapter = MagicMock()
    adapter.platform = platform
    _wire_live_depth(adapter, price)

    async def _slow_place(arb_id, market_id, canonical_id, side, price_, qty, max_affordable=None):
        in_flight["current"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["current"])
        try:
            await asyncio.sleep(hold_s)
            return Order(
                order_id=f"ARB-CONC-{platform.upper()}-{canonical_id}",
                platform=platform, market_id=market_id,
                canonical_id=canonical_id, side=side,
                price=price_, quantity=qty,
                status=OrderStatus.FILLED,
                fill_qty=qty, fill_price=price_,
                timestamp=time.time(),
            )
        finally:
            in_flight["current"] -= 1

    adapter.place_fok = AsyncMock(side_effect=_slow_place)
    adapter.place_ioc = AsyncMock(side_effect=_slow_place)
    adapter.get_order = AsyncMock()
    adapter.cancel_order = AsyncMock(return_value=True)
    return adapter


def test_canonical_lock_serializes_concurrent_same_canonical():
    """Two execute_opportunity calls for the SAME canonical_id must run
    sequentially — the per-canonical asyncio.Lock acquired in
    execute_opportunity (engine.py:606) is what prevents the per-market
    exposure check from passing for both racers at once because
    _open_positions is mutated only after each trade completes.

    Asserts that observed in-flight primary FOK count never exceeds 1.
    """
    async def runner():
        import os as _os_test
        _old_exec = _os_test.environ.get("EXECUTION_ORDER")
        _os_test.environ["EXECUTION_ORDER"] = "poly_first"
        try:
            engine = _build_live_engine_for_safe02_gap()

            in_flight = {"current": 0, "max": 0}
            poly = _build_concurrency_adapter("polymarket", 0.40, in_flight, hold_s=0.05)
            kalshi = _build_concurrency_adapter("kalshi", 0.30, in_flight, hold_s=0.001)
            engine.adapters = {"polymarket": poly, "kalshi": kalshi}

            opp_a = _make_safety_opp(
                canonical_id="MKT_CNC",
                yes_platform="polymarket", no_platform="kalshi",
                yes_price=0.40, no_price=0.30, suggested_qty=10,
            )
            opp_b = _make_safety_opp(
                canonical_id="MKT_CNC",  # SAME canonical — must serialize
                yes_platform="polymarket", no_platform="kalshi",
                yes_price=0.40, no_price=0.30, suggested_qty=10,
            )

            results = await asyncio.gather(
                engine.execute_opportunity(opp_a),
                engine.execute_opportunity(opp_b),
                return_exceptions=True,
            )
            # Both calls completed (no swallowed exception that would skew
            # the concurrency measurement).
            for r in results:
                assert not isinstance(r, BaseException), f"unexpected error: {r!r}"

            assert in_flight["max"] == 1, (
                f"per-canonical lock did not serialize: max in-flight primary "
                f"FOK count = {in_flight['max']} (expected 1)"
            )
        finally:
            if _old_exec is None:
                _os_test.environ.pop("EXECUTION_ORDER", None)
            else:
                _os_test.environ["EXECUTION_ORDER"] = _old_exec

    asyncio.run(runner())


def test_canonical_lock_allows_parallel_distinct_canonicals():
    """The per-canonical lock keys on canonical_id, so two opportunities on
    DIFFERENT canonicals must be allowed to execute concurrently — otherwise
    the engine becomes a global single-threaded bottleneck and throughput
    collapses on multi-market opportunity bursts.
    """
    async def runner():
        import os as _os_test
        _old_exec = _os_test.environ.get("EXECUTION_ORDER")
        _os_test.environ["EXECUTION_ORDER"] = "poly_first"
        try:
            engine = _build_live_engine_for_safe02_gap()

            in_flight = {"current": 0, "max": 0}
            poly = _build_concurrency_adapter("polymarket", 0.40, in_flight, hold_s=0.05)
            kalshi = _build_concurrency_adapter("kalshi", 0.30, in_flight, hold_s=0.001)
            engine.adapters = {"polymarket": poly, "kalshi": kalshi}

            opp_a = _make_safety_opp(
                canonical_id="MKT_CNC",
                yes_platform="polymarket", no_platform="kalshi",
                yes_price=0.40, no_price=0.30, suggested_qty=10,
            )
            opp_b = _make_safety_opp(
                canonical_id="MKT_BURST",  # DIFFERENT canonical — must overlap
                yes_platform="polymarket", no_platform="kalshi",
                yes_price=0.40, no_price=0.30, suggested_qty=10,
            )

            results = await asyncio.gather(
                engine.execute_opportunity(opp_a),
                engine.execute_opportunity(opp_b),
                return_exceptions=True,
            )
            for r in results:
                assert not isinstance(r, BaseException), f"unexpected error: {r!r}"

            assert in_flight["max"] == 2, (
                f"per-canonical lock OVER-serialized distinct canonicals: "
                f"max in-flight = {in_flight['max']} (expected 2)"
            )
        finally:
            if _old_exec is None:
                _os_test.environ.pop("EXECUTION_ORDER", None)
            else:
                _os_test.environ["EXECUTION_ORDER"] = _old_exec

    asyncio.run(runner())
