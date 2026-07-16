"""End-to-end proof: the REAL AutoExecutor drives the REAL ExecutionEngine to
place a (simulated) Kalshi↔Polymarket arbitrage — both legs filled and P&L
booked — with no venue mocks in the execution path.

This ties the two halves of the auto-trading machinery together in one test:
  1. AutoExecutor evaluates its 7 policy gates and fires engine.execute_opportunity.
  2. The engine runs its own gauntlet (mapping-confirmed, resolution-identical,
     requote, shadow audit) and produces an ArbExecution with both legs filled.

It is deliberately a "mock trade" (dry_run → simulated fills, no real orders)
so it is safe to run anywhere, while exercising the real code path a live
Kalshi↔Polymarket capture takes. FX being dark is irrelevant here by design —
this is exactly the pair that stays live-tradable when ForecastEx is down.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from arbiter.config.settings import ArbiterConfig
from arbiter.monitor.balance import BalanceMonitor
from arbiter.utils.price_store import PriceStore
from arbiter.execution.engine import ExecutionEngine
from arbiter.execution.auto_executor import AutoExecutor, AutoExecutorConfig
from arbiter.scanner.arbitrage import ArbitrageOpportunity


@dataclass
class _Mapping:
    canonical_id: str = "KXPRES-DEMO"
    status: str = "confirmed"
    allow_auto_trade: bool = True
    forecastex_not_available: bool = False
    forecastex_contract_id: str = ""


class _MappingStore:
    def __init__(self, mapping):
        self._mapping = mapping

    async def get(self, canonical_id: str):
        return self._mapping


class _Scanner:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()

    def subscribe(self) -> asyncio.Queue:
        return self._queue


# A clean, positive-edge Kalshi(yes)↔Polymarket(no) cross: buy YES @0.40 on
# Kalshi and NO @0.55 on Polymarket costs 0.95 to lock a $1.00 payout → 5¢
# gross, ~2-3¢ net after fees. Fees are computed with the SAME shadow fee
# models the engine's math auditor uses, so the opp is internally consistent
# exactly as a real scanner-built one is (otherwise the auditor rejects it).
_YES_PRICE = 0.40
_NO_PRICE = 0.55
_QTY = 5
_NO_FEE_RATE = 0.02

from arbiter.audit.math_auditor import MathAuditor as _MathAuditor  # noqa: E402

_auditor = _MathAuditor()
_YES_FEE = _auditor._compute_fee("kalshi", _YES_PRICE, "yes", _QTY, fee_rate=None)
_NO_FEE = _auditor._compute_fee("polymarket", _NO_PRICE, "no", _QTY, fee_rate=_NO_FEE_RATE)
_GROSS = 1.0 - _YES_PRICE - _NO_PRICE
_TOTAL_FEES = _YES_FEE + _NO_FEE
_NET_EDGE = _GROSS - _TOTAL_FEES
_EXPECTED_PNL = _NET_EDGE * _QTY


def _demo_opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        canonical_id="KXPRES-DEMO",
        description="Demo cross-platform arb (Kalshi YES vs Polymarket NO)",
        yes_platform="kalshi",
        yes_price=_YES_PRICE,
        yes_fee=_YES_FEE,
        yes_fee_rate=None,
        yes_market_id="KALSHI-DEMO",
        no_platform="polymarket",
        no_price=_NO_PRICE,
        no_fee=_NO_FEE,
        no_fee_rate=_NO_FEE_RATE,
        no_market_id="POLY-DEMO",
        gross_edge=_GROSS,
        total_fees=_TOTAL_FEES,
        net_edge=_NET_EDGE,
        net_edge_cents=_NET_EDGE * 100.0,
        suggested_qty=_QTY,
        max_profit_usd=_EXPECTED_PNL,
        timestamp=1776729648.0,
        confidence=0.9,
        arb_type="cross_platform",
        status="tradable",  # scanner's verdict for an auto-tradable cross
        persistence_count=3,
        quote_age_seconds=1.0,
        min_available_liquidity=100.0,
        mapping_status="confirmed",
        mapping_score=0.95,
        requires_manual=False,
    )


def _build_engine() -> ExecutionEngine:
    config = ArbiterConfig()
    config.scanner.dry_run = True  # simulated fills — no real orders
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0
    config.safety.max_platform_exposure_usd = 1_000_000.0
    monitor = BalanceMonitor(config.alerts, {"kalshi": object(), "polymarket": object()})
    # price_store=None → _pre_trade_requote returns the opp unchanged (no live
    # feed needed for the simulated path); every other engine gate still runs.
    engine = ExecutionEngine(config, monitor, price_store=None, collectors={})
    engine.risk._max_daily_trades = 250
    return engine


@pytest.mark.asyncio
async def test_auto_executor_drives_engine_to_simulated_arb(monkeypatch):
    # The engine's resolution gate reads the durable mapping's
    # resolution_match_status; force "identical" so the demo canonical clears it
    # exactly as a real confirmed mapping would.
    monkeypatch.setattr(
        "arbiter.config.settings.get_market_mapping",
        lambda canonical_id: {"resolution_match_status": "identical"},
    )

    engine = _build_engine()
    ae = AutoExecutor(
        scanner=_Scanner(),
        engine=engine,  # the REAL engine — not a mock
        supervisor=type("S", (), {"is_armed": False, "armed_by": None})(),
        mapping_store=_MappingStore(_Mapping()),
        config=AutoExecutorConfig(
            enabled=True,
            max_position_usd=100.0,
            min_edge_cents_preflight=1.0,
            require_mapping_confirmed=True,
        ),
    )

    # Fire the real auto path: 7 gates → engine.execute_opportunity → simulate.
    await ae._consider_opportunity(_demo_opportunity())

    # The engine must have produced exactly one completed arb, both legs filled.
    assert len(engine._executions) == 1, "AutoExecutor did not drive a trade"
    execution = engine._executions[0]
    assert execution.arb_id.startswith("ARB-")
    assert execution.status == "simulated"

    # Both legs filled at the quoted prices on the correct venues/sides.
    assert execution.leg_yes.platform == "kalshi"
    assert execution.leg_yes.side == "yes"
    assert execution.leg_yes.fill_qty == _QTY
    assert execution.leg_yes.fill_price == _YES_PRICE

    assert execution.leg_no.platform == "polymarket"
    assert execution.leg_no.side == "no"
    assert execution.leg_no.fill_qty == _QTY
    assert execution.leg_no.fill_price == _NO_PRICE

    # Positive P&L booked: net_edge * qty, and it must actually be > 0.
    assert _EXPECTED_PNL > 0
    assert execution.realized_pnl == pytest.approx(_EXPECTED_PNL, abs=1e-9)
