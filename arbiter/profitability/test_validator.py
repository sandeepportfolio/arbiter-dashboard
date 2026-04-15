import time

from arbiter.execution.engine import ArbExecution, ExecutionIncident, Order, OrderStatus
from arbiter.profitability.validator import ProfitabilityConfig, ProfitabilityValidator
from arbiter.scanner.arbitrage import ArbitrageOpportunity


class StubScanner:
    def __init__(self, stats: dict, opportunities: list[ArbitrageOpportunity]):
        self._stats = stats
        self._opportunities = opportunities

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def current_opportunities(self) -> list[ArbitrageOpportunity]:
        return list(self._opportunities)


class StubEngine:
    def __init__(self, stats: dict, executions: list[ArbExecution], incidents: list[ExecutionIncident]):
        self._stats = stats
        self._executions = executions
        self._incidents = incidents

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def execution_history(self) -> list[ArbExecution]:
        return list(self._executions)

    @property
    def incidents(self) -> list[ExecutionIncident]:
        return list(self._incidents)


def make_opportunity(canonical_id: str, edge_cents: float) -> ArbitrageOpportunity:
    now = time.time()
    edge = edge_cents / 100.0
    return ArbitrageOpportunity(
        canonical_id=canonical_id,
        description=f"Opportunity {canonical_id}",
        yes_platform="kalshi",
        yes_price=0.40,
        yes_fee=0.01,
        yes_market_id=f"{canonical_id}-YES",
        no_platform="polymarket",
        no_price=0.45,
        no_fee=0.01,
        no_market_id=f"{canonical_id}-NO",
        gross_edge=0.15,
        total_fees=0.02,
        net_edge=edge,
        net_edge_cents=edge_cents,
        suggested_qty=10,
        max_profit_usd=edge * 10,
        timestamp=now,
        confidence=0.95,
        status="tradable",
        persistence_count=3,
        quote_age_seconds=0.5,
        min_available_liquidity=100.0,
        mapping_status="confirmed",
        mapping_score=0.95,
        requires_manual=False,
        yes_fee_rate=0.07,
        no_fee_rate=0.01,
    )


def make_execution(arb_id: str, pnl: float, edge_cents: float) -> ArbExecution:
    now = time.time()
    opportunity = make_opportunity(arb_id, edge_cents)
    leg_yes = Order(
        order_id=f"{arb_id}-YES",
        platform="kalshi",
        market_id=opportunity.yes_market_id,
        canonical_id=opportunity.canonical_id,
        side="yes",
        price=opportunity.yes_price,
        quantity=opportunity.suggested_qty,
        status=OrderStatus.SIMULATED,
        fill_price=opportunity.yes_price,
        fill_qty=opportunity.suggested_qty,
        timestamp=now,
    )
    leg_no = Order(
        order_id=f"{arb_id}-NO",
        platform="polymarket",
        market_id=opportunity.no_market_id,
        canonical_id=opportunity.canonical_id,
        side="no",
        price=opportunity.no_price,
        quantity=opportunity.suggested_qty,
        status=OrderStatus.SIMULATED,
        fill_price=opportunity.no_price,
        fill_qty=opportunity.suggested_qty,
        timestamp=now,
    )
    return ArbExecution(
        arb_id=arb_id,
        opportunity=opportunity,
        leg_yes=leg_yes,
        leg_no=leg_no,
        status="simulated",
        realized_pnl=pnl,
        timestamp=now,
    )


def make_incident(severity: str) -> ExecutionIncident:
    return ExecutionIncident(
        incident_id=f"INC-{severity}",
        arb_id="ARB-INCIDENT",
        canonical_id="TEST",
        severity=severity,
        message=f"{severity} incident",
        timestamp=time.time(),
    )


def test_validator_marks_profitable_after_strict_thresholds_are_met():
    executions = [
        make_execution("ARB-1", 0.6, 5.0),
        make_execution("ARB-2", 0.7, 5.5),
        make_execution("ARB-3", 0.8, 6.0),
    ]
    scanner = StubScanner(
        stats={
            "scan_count": 20,
            "published": 8,
            "active_opportunities": 2,
            "best_edge_cents": 6.0,
        },
        opportunities=[make_opportunity("LIVE-1", 5.0), make_opportunity("LIVE-2", 6.0)],
    )
    engine = StubEngine(
        stats={
            "total_executions": 3,
            "audit": {"pass_rate": 1.0},
        },
        executions=executions,
        incidents=[],
    )
    validator = ProfitabilityValidator(
        ProfitabilityConfig(
            min_scan_count=10,
            min_published_opportunities=5,
            min_completed_executions=3,
            min_total_realized_pnl=1.5,
            min_average_realized_pnl=0.4,
            min_average_edge_cents=5.0,
            min_profitable_execution_ratio=1.0,
            min_audit_pass_rate=0.99,
            max_incident_rate=0.10,
            max_critical_incidents=0,
        ),
        scanner,
        engine,
    )

    snapshot = validator.get_snapshot()

    assert snapshot.verdict == "validated_profitable"
    assert snapshot.is_profitable is True
    assert snapshot.is_determined is True
    assert snapshot.total_realized_pnl == 2.1


def test_validator_marks_not_profitable_when_sample_is_large_but_pnl_is_weak():
    executions = [
        make_execution("ARB-1", 0.10, 3.0),
        make_execution("ARB-2", -0.05, 2.5),
        make_execution("ARB-3", 0.05, 2.0),
    ]
    scanner = StubScanner(
        stats={
            "scan_count": 30,
            "published": 12,
            "active_opportunities": 1,
            "best_edge_cents": 3.0,
        },
        opportunities=[make_opportunity("LIVE-1", 3.0)],
    )
    engine = StubEngine(
        stats={
            "total_executions": 3,
            "audit": {"pass_rate": 1.0},
        },
        executions=executions,
        incidents=[],
    )
    validator = ProfitabilityValidator(
        ProfitabilityConfig(
            min_scan_count=10,
            min_published_opportunities=5,
            min_completed_executions=3,
            min_total_realized_pnl=1.0,
            min_average_realized_pnl=0.2,
            min_average_edge_cents=2.5,
            min_profitable_execution_ratio=0.8,
            min_audit_pass_rate=0.99,
            max_incident_rate=0.10,
            max_critical_incidents=0,
        ),
        scanner,
        engine,
    )

    snapshot = validator.get_snapshot()

    assert snapshot.verdict == "not_profitable"
    assert snapshot.is_profitable is False
    assert snapshot.is_determined is True
    assert any("Total realized P&L" in reason for reason in snapshot.reasons)


def test_validator_blocks_when_audit_or_critical_incidents_fail():
    executions = [make_execution("ARB-1", 0.8, 6.0)]
    scanner = StubScanner(
        stats={
            "scan_count": 30,
            "published": 12,
            "active_opportunities": 1,
            "best_edge_cents": 6.0,
        },
        opportunities=[make_opportunity("LIVE-1", 6.0)],
    )
    engine = StubEngine(
        stats={
            "total_executions": 1,
            "audit": {"pass_rate": 0.92},
        },
        executions=executions,
        incidents=[make_incident("critical")],
    )
    validator = ProfitabilityValidator(
        ProfitabilityConfig(
            min_scan_count=10,
            min_published_opportunities=5,
            min_completed_executions=1,
            min_total_realized_pnl=0.5,
            min_average_realized_pnl=0.2,
            min_average_edge_cents=5.0,
            min_profitable_execution_ratio=1.0,
            min_audit_pass_rate=0.99,
            max_incident_rate=0.50,
            max_critical_incidents=0,
        ),
        scanner,
        engine,
    )

    snapshot = validator.get_snapshot()

    assert snapshot.verdict == "blocked"
    assert snapshot.is_determined is True
    assert any("Audit pass rate" in reason for reason in snapshot.reasons)
