from types import SimpleNamespace

import pytest

from arbiter.config.settings import ScannerConfig
from arbiter.execution.engine import ArbExecution, Order, OrderStatus
from arbiter.portfolio.monitor import PortfolioConfig, PortfolioMonitor
from arbiter.scanner.arbitrage import ArbitrageOpportunity


def make_opportunity(qty=10, yes_price=0.25, no_price=0.5):
    return ArbitrageOpportunity(
        canonical_id="TEST_PORTFOLIO",
        description="Portfolio exposure regression",
        yes_platform="kalshi",
        yes_price=yes_price,
        yes_fee=0.0,
        yes_market_id="K-TEST",
        no_platform="polymarket",
        no_price=no_price,
        no_fee=0.0,
        no_market_id="P-TEST",
        gross_edge=0.0,
        total_fees=0.0,
        net_edge=0.0,
        net_edge_cents=0.0,
        suggested_qty=qty,
        max_profit_usd=0.0,
        timestamp=0.0,
    )


def make_order(side, status=OrderStatus.PENDING, fill_qty=0, fill_price=0.0):
    return Order(
        order_id=f"ARB-PORT-{side}",
        platform="kalshi" if side == "yes" else "polymarket",
        market_id=f"{side.upper()}-TEST",
        canonical_id="TEST_PORTFOLIO",
        side=side,
        price=fill_price or 0.5,
        quantity=10,
        status=status,
        fill_price=fill_price,
        fill_qty=fill_qty,
    )


def make_monitor(executions):
    engine = SimpleNamespace(_executions=executions)
    balances = SimpleNamespace(current_balances={})
    return PortfolioMonitor(
        PortfolioConfig(),
        ScannerConfig(),
        engine,
        balances,
    )


def test_pending_execution_with_zero_filled_legs_is_not_portfolio_exposure():
    execution = ArbExecution(
        arb_id="ARB-ZERO",
        opportunity=make_opportunity(qty=12, yes_price=0.28, no_price=0.5),
        leg_yes=make_order("yes", status=OrderStatus.PENDING, fill_qty=0),
        leg_no=make_order("no", status=OrderStatus.PENDING, fill_qty=0),
        status="pending",
        realized_pnl=0.0,
        timestamp=0.0,
    )

    snapshot = make_monitor([execution]).compute_snapshot(dry_run=False)

    assert snapshot.total_open_positions == 0
    assert snapshot.total_exposure == pytest.approx(0.0)
    assert snapshot.violations == []


def test_one_filled_leg_counts_only_surviving_exposure():
    execution = ArbExecution(
        arb_id="ARB-ONELEG",
        opportunity=make_opportunity(qty=10, yes_price=0.25, no_price=0.5),
        leg_yes=make_order(
            "yes",
            status=OrderStatus.FILLED,
            fill_qty=10,
            fill_price=0.25,
        ),
        leg_no=make_order("no", status=OrderStatus.PENDING, fill_qty=0),
        status="pending",
        realized_pnl=0.0,
        timestamp=0.0,
    )

    snapshot = make_monitor([execution]).compute_snapshot(dry_run=False)

    assert snapshot.total_open_positions == 1
    assert snapshot.total_unhedged == 1
    assert snapshot.total_exposure == pytest.approx(2.5)
    assert snapshot.by_venue["kalshi"].total_exposure == pytest.approx(2.5)
    assert "polymarket" not in snapshot.by_venue


def test_stale_unhedged_violations_are_execution_specific():
    executions = [
        ArbExecution(
            arb_id="ARB-ONELEG-A",
            opportunity=make_opportunity(qty=10, yes_price=0.25, no_price=0.5),
            leg_yes=make_order("yes", status=OrderStatus.FILLED, fill_qty=10, fill_price=0.25),
            leg_no=make_order("no", status=OrderStatus.PENDING, fill_qty=0),
            status="pending",
            realized_pnl=0.0,
            timestamp=0.0,
        ),
        ArbExecution(
            arb_id="ARB-ONELEG-B",
            opportunity=make_opportunity(qty=10, yes_price=0.25, no_price=0.5),
            leg_yes=make_order("yes", status=OrderStatus.FILLED, fill_qty=10, fill_price=0.25),
            leg_no=make_order("no", status=OrderStatus.PENDING, fill_qty=0),
            status="recovering",
            realized_pnl=0.0,
            timestamp=0.0,
        ),
    ]

    snapshot = make_monitor(executions).compute_snapshot(dry_run=False)

    stale_ids = {
        violation.violation_id
        for violation in snapshot.violations
        if violation.category == "stale"
    }
    assert stale_ids == {
        "stale_hedge:ARB-ONELEG-A",
        "stale_hedge:ARB-ONELEG-B",
    }
