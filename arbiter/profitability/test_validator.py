import time

import pytest

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


def make_opportunity(
    canonical_id: str,
    edge_cents: float,
    *,
    yes_platform: str = "kalshi",
    no_platform: str = "polymarket",
) -> ArbitrageOpportunity:
    now = time.time()
    edge = edge_cents / 100.0
    return ArbitrageOpportunity(
        canonical_id=canonical_id,
        description=f"Opportunity {canonical_id}",
        yes_platform=yes_platform,
        yes_price=0.40,
        yes_fee=0.01,
        yes_market_id=f"{canonical_id}-YES",
        no_platform=no_platform,
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


def make_execution(
    arb_id: str,
    pnl: float,
    edge_cents: float,
    *,
    yes_platform: str = "kalshi",
    no_platform: str = "polymarket",
    unwind_pnl: float = 0.0,
) -> ArbExecution:
    now = time.time()
    opportunity = make_opportunity(
        arb_id,
        edge_cents,
        yes_platform=yes_platform,
        no_platform=no_platform,
    )
    leg_yes = Order(
        order_id=f"{arb_id}-YES",
        platform=yes_platform,
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
        platform=no_platform,
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
        unwind_pnl=unwind_pnl,
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


def make_half_recorded_summary(count: int) -> ExecutionIncident:
    return ExecutionIncident(
        incident_id="INC-HALF-RECORDED-SUMMARY",
        arb_id="MULTIPLE",
        canonical_id="HALF_RECORDED_ARBS",
        severity="critical",
        message=f"{count} half-recorded arb(s) remain unresolved.",
        timestamp=time.time(),
        metadata={
            "event_type": "half_recorded_arb_summary",
            "count": count,
        },
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
    assert snapshot.total_realized_pnl == pytest.approx(2.1)


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


def test_validator_ignores_info_severity_incidents_in_incident_rate():
    # 2026-05-27: ``RiskManager.check_trade`` emits info-severity
    # ``Order rejected: Edge too thin`` rows for dashboard visibility on
    # every routine pre-trade filter. Live prod saw 769 such rows push
    # incident_rate to 61.5% against a 15% ceiling, deadlocking the
    # trade gate. Info severity is observational and MUST NOT count.
    executions = [
        make_execution("ARB-1", 0.6, 5.0),
        make_execution("ARB-2", 0.7, 5.5),
        make_execution("ARB-3", 0.8, 6.0),
    ]
    scanner = StubScanner(
        stats={
            "scan_count": 30,
            "published": 12,
            "active_opportunities": 1,
            "best_edge_cents": 6.0,
        },
        opportunities=[make_opportunity("LIVE-1", 6.0)],
    )
    # 200 info incidents would drag incident_rate to 200/3 > max if counted;
    # the validator must drop them and yield validated_profitable.
    engine = StubEngine(
        stats={
            "total_executions": 3,
            "aborted": 0,
            "audit": {"pass_rate": 1.0},
        },
        executions=executions,
        incidents=[make_incident("info") for _ in range(200)],
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

    assert snapshot.verdict == "validated_profitable", snapshot.reasons
    assert snapshot.incident_count == 0
    assert snapshot.incident_rate == 0.0


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


def test_validator_counts_half_recorded_summary_as_underlying_critical_incidents():
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
            "audit": {"pass_rate": 1.0},
        },
        executions=executions,
        incidents=[make_half_recorded_summary(32)],
    )
    validator = ProfitabilityValidator(
        ProfitabilityConfig(
            min_scan_count=10,
            min_published_opportunities=5,
            min_completed_executions=1,
            min_total_realized_pnl=0.1,
            min_average_realized_pnl=0.1,
            min_average_edge_cents=1.0,
            min_profitable_execution_ratio=1.0,
            min_audit_pass_rate=0.99,
            max_incident_rate=1.0,
            max_critical_incidents=0,
        ),
        scanner,
        engine,
    )

    snapshot = validator.get_snapshot()

    assert snapshot.verdict == "blocked"
    assert snapshot.critical_incidents == 32
    assert any("Critical incidents 32 exceed" in reason for reason in snapshot.reasons)


def test_min_profitable_ratio_env_override(monkeypatch):
    monkeypatch.setenv("ARBITER_MIN_PROFITABLE_RATIO", "0.50")
    config = ProfitabilityConfig()
    assert config.min_profitable_execution_ratio == 0.50

    monkeypatch.setenv("ARBITER_MIN_PROFITABLE_RATIO", "1.5")
    assert ProfitabilityConfig().min_profitable_execution_ratio == 0.65

    monkeypatch.setenv("ARBITER_MIN_PROFITABLE_RATIO", "not-a-number")
    assert ProfitabilityConfig().min_profitable_execution_ratio == 0.65

    monkeypatch.delenv("ARBITER_MIN_PROFITABLE_RATIO", raising=False)
    assert ProfitabilityConfig().min_profitable_execution_ratio == 0.65


def test_scope_threshold_overrides_load_from_env(monkeypatch):
    """CV-04: per-scope env vars populate the override dicts in the
    config. Naming convention is the global env name with the lowercase
    scope key appended (platform OR pair)."""
    monkeypatch.setenv("ARBITER_MIN_TOTAL_PNL_FORECASTEX", "0")
    monkeypatch.setenv("ARBITER_MIN_AVG_REALIZED_PNL_FORECASTEX", "0")
    monkeypatch.setenv("ARBITER_MIN_PROFITABLE_RATIO_FORECASTEX", "0")
    monkeypatch.setenv("ARBITER_MIN_TOTAL_PNL_FORECASTEX_KALSHI", "0")
    config = ProfitabilityConfig()
    assert config.min_total_realized_pnl_by_scope == {
        "forecastex": 0.0,
        "forecastex_kalshi": 0.0,
    }
    assert config.min_average_realized_pnl_by_scope == {"forecastex": 0.0}
    assert config.min_profitable_execution_ratio_by_scope == {"forecastex": 0.0}


def test_scope_override_relaxes_floor_below_global_only(monkeypatch):
    """CV-04: relax-only invariant — a per-scope override that's HIGHER
    than the global threshold still resolves to the global. Operators
    can lower a per-scope floor but never raise it above the system-
    wide ceiling, so a typo can't accidentally tighten the gate for
    just one venue."""
    monkeypatch.setenv("ARBITER_MIN_TOTAL_PNL_FORECASTEX", "999")
    config = ProfitabilityConfig(min_total_realized_pnl=5.0)
    assert config.effective_min_total_realized_pnl("forecastex") == 5.0
    assert config.effective_min_total_realized_pnl("kalshi") == 5.0
    assert config.effective_min_total_realized_pnl("polymarket") == 5.0


def test_scope_override_lowers_per_scope_floor(monkeypatch):
    """CV-04: when override < global, the per-scope floor IS the
    override. Other scopes continue using the global. This is the
    operator-controlled unlock for an evidence-thin venue (forecastex
    has zero successful arb fills; this lets the platform-scope verdict
    flip to validated_profitable without weakening kalshi/polymarket)."""
    monkeypatch.setenv("ARBITER_MIN_TOTAL_PNL_FORECASTEX", "0")
    monkeypatch.setenv("ARBITER_MIN_AVG_REALIZED_PNL_FORECASTEX", "0")
    monkeypatch.setenv("ARBITER_MIN_PROFITABLE_RATIO_FORECASTEX", "0")
    config = ProfitabilityConfig(
        min_total_realized_pnl=5.0,
        min_average_realized_pnl=0.25,
        min_profitable_execution_ratio=0.65,
    )
    assert config.effective_min_total_realized_pnl("forecastex") == 0.0
    assert config.effective_min_average_realized_pnl("forecastex") == 0.0
    assert config.effective_min_profitable_execution_ratio("forecastex") == 0.0
    # Other scopes untouched.
    assert config.effective_min_total_realized_pnl("kalshi") == 5.0
    assert config.effective_min_average_realized_pnl("kalshi") == 0.25
    assert config.effective_min_profitable_execution_ratio("kalshi") == 0.65


def test_validator_scope_override_unlocks_forecastex_platform_verdict(monkeypatch):
    """CV-04 integration: a forecastex platform scope with the same
    zero-pnl breakeven evidence base that locked prod's gate flips to
    validated_profitable once the operator sets the three FORECASTEX
    overrides to 0. The pair scope still resolves with the global
    thresholds unless a pair-specific override is also set — covered
    here by also setting the pair override.
    """
    monkeypatch.setenv("ARBITER_MIN_TOTAL_PNL_FORECASTEX", "0")
    monkeypatch.setenv("ARBITER_MIN_AVG_REALIZED_PNL_FORECASTEX", "0")
    monkeypatch.setenv("ARBITER_MIN_PROFITABLE_RATIO_FORECASTEX", "0")
    monkeypatch.setenv("ARBITER_MIN_TOTAL_PNL_FORECASTEX_KALSHI", "0")
    monkeypatch.setenv("ARBITER_MIN_AVG_REALIZED_PNL_FORECASTEX_KALSHI", "0")
    monkeypatch.setenv("ARBITER_MIN_PROFITABLE_RATIO_FORECASTEX_KALSHI", "0")

    # Six FX-pair breakeven executions, like the 16 in prod that all
    # aborted at the secondary-leg adverse-move guard before the
    # 5/27-5/28 FX fixes landed. Plus a kalshi×polymarket sample large
    # enough to keep kalshi validated_profitable independently (mirrors
    # prod, where kalshi has 50+ K×P fills under its belt).
    executions = [
        make_execution(
            f"ARB-FX-{i}", 0.0, 4.5,
            yes_platform="kalshi",
            no_platform="forecastex",
        )
        for i in range(6)
    ] + [
        make_execution(
            f"ARB-KP-{i}", 0.5, 6.0,
            yes_platform="kalshi",
            no_platform="polymarket",
        )
        for i in range(20)
    ]
    scanner = StubScanner(
        stats={"scan_count": 30, "published": 12, "active_opportunities": 1},
        opportunities=[],
    )
    engine = StubEngine(
        stats={"total_executions": len(executions), "audit": {"pass_rate": 1.0}},
        executions=executions,
        incidents=[],
    )
    validator = ProfitabilityValidator(
        ProfitabilityConfig(
            min_scan_count=10,
            min_published_opportunities=5,
            min_completed_executions=1,
            min_total_realized_pnl=2.5,
            min_average_realized_pnl=0.1,
            min_average_edge_cents=1.0,
            min_profitable_execution_ratio=0.65,
            min_audit_pass_rate=0.99,
            max_incident_rate=0.10,
            max_critical_incidents=0,
        ),
        scanner,
        engine,
    )
    snapshot = validator.get_snapshot()

    # Per-scope overrides flip BOTH the forecastex platform and the
    # forecastex_kalshi pair to validated_profitable despite zero
    # arb_pnl across the whole sample.
    assert snapshot.platform_profitability["forecastex"]["verdict"] == "validated_profitable"
    assert snapshot.pair_profitability["forecastex_kalshi"]["verdict"] == "validated_profitable"
    fx_opp = make_opportunity("FX", 5.0, yes_platform="kalshi", no_platform="forecastex")
    allowed, reason, _ = validator.validate_opportunity_scope(fx_opp)
    assert allowed is True, reason


def test_validator_reports_platform_and_pair_profitability():
    executions = [
        make_execution("ARB-1", 0.6, 5.0),
        make_execution("ARB-2", 0.7, 5.5),
        make_execution("ARB-3", 0.8, 6.0),
        make_execution(
            "ARB-FX-LOSS",
            -0.4,
            5.0,
            yes_platform="forecastex",
            no_platform="kalshi",
        ),
    ]
    scanner = StubScanner(
        stats={"scan_count": 30, "published": 12, "active_opportunities": 1},
        opportunities=[make_opportunity("LIVE-1", 6.0)],
    )
    engine = StubEngine(
        stats={"total_executions": 4, "audit": {"pass_rate": 1.0}},
        executions=executions,
        incidents=[],
    )
    validator = ProfitabilityValidator(
        ProfitabilityConfig(
            min_scan_count=10,
            min_published_opportunities=5,
            min_completed_executions=1,
            min_total_realized_pnl=0.1,
            min_average_realized_pnl=0.1,
            min_average_edge_cents=1.0,
            min_profitable_execution_ratio=0.5,
            min_audit_pass_rate=0.99,
            max_incident_rate=0.10,
            max_critical_incidents=0,
        ),
        scanner,
        engine,
    )

    snapshot = validator.get_snapshot()

    assert snapshot.platform_profitability["polymarket"]["verdict"] == "validated_profitable"
    assert snapshot.pair_profitability["kalshi_polymarket"]["verdict"] == "validated_profitable"
    assert snapshot.platform_profitability["forecastex"]["verdict"] == "not_profitable"
    assert snapshot.pair_profitability["forecastex_kalshi"]["verdict"] == "not_profitable"

    allowed, reason, _ = validator.validate_opportunity_scope(make_opportunity("KP", 5.0))
    assert allowed is True
    assert reason == "platform and pair profitability validated"

    fx_opp = make_opportunity("FX", 5.0, yes_platform="forecastex", no_platform="kalshi")
    allowed, reason, _ = validator.validate_opportunity_scope(fx_opp)
    assert allowed is False
    assert "forecastex" in reason


def test_validator_uses_arb_pnl_not_unwind_luck_for_profitability():
    execution = make_execution("ARB-UNWIND", 10.0, 5.0, unwind_pnl=10.0)
    scanner = StubScanner(
        stats={"scan_count": 30, "published": 12, "active_opportunities": 0},
        opportunities=[],
    )
    engine = StubEngine(
        stats={"total_executions": 1, "audit": {"pass_rate": 1.0}},
        executions=[execution],
        incidents=[],
    )
    validator = ProfitabilityValidator(
        ProfitabilityConfig(
            min_scan_count=10,
            min_published_opportunities=5,
            min_completed_executions=1,
            min_total_realized_pnl=0.1,
            min_average_realized_pnl=0.1,
            min_average_edge_cents=1.0,
            min_profitable_execution_ratio=0.5,
            min_audit_pass_rate=0.99,
            max_incident_rate=0.10,
            max_critical_incidents=0,
        ),
        scanner,
        engine,
    )

    snapshot = validator.get_snapshot()

    assert snapshot.total_realized_pnl == 0.0
    assert snapshot.profitable_executions == 0
    assert snapshot.breakeven_executions == 1
    assert snapshot.verdict == "not_profitable"
