"""Requote-then-retry for the secondary leg (Phase-3 execution hardening).

Live evidence (402 executions, 93 naked arbs): after the primary fills, the
secondary gets exactly one IOC at max-affordable plus a doomed same-price FOK
— the "attempt 2" rung never fires because the live path passes
initial_price == max_affordable_price. One adverse tick at walk time turns
the whole trade into an unwind. These tests pin the new behavior:

- after a CLEAN (non-ambiguous) secondary failure, re-walk the book and
  retry an IOC while it is affordable, bounded by SECONDARY_REQUOTE_MAX_ATTEMPTS;
- the EDGE-LOST pre-submit abort enters the same loop (skip_initial_attempt)
  instead of instantly stranding the primary;
- a break-even completion tier may fire on the FINAL attempt only
  (completing at >= break-even beats a panic unwind);
- idempotency_ambiguous still suppresses every retry, unconditionally.
"""
from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import AsyncMock, MagicMock

from arbiter.config.settings import ArbiterConfig
from arbiter.execution.engine import ExecutionEngine, Order, OrderStatus
from arbiter.monitor.balance import BalanceMonitor
from arbiter.scanner.arbitrage import ArbitrageOpportunity
from arbiter.utils.price_store import PriceStore


def _make_engine() -> ExecutionEngine:
    config = ArbiterConfig()
    config.scanner.dry_run = True
    config.scanner.confidence_threshold = 0.1
    config.scanner.min_edge_cents = 1.0
    config.safety.max_platform_exposure_usd = 1_000_000.0
    monitor = BalanceMonitor(
        config.alerts, {"kalshi": object(), "polymarket": object()},
    )
    engine = ExecutionEngine(
        config, monitor, price_store=PriceStore(ttl=60), collectors={},
    )
    engine._secondary_requote_delay_ms = 0.0  # no sleeps in tests
    return engine


def _make_opp(canonical: str = "MKT") -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        canonical_id=canonical,
        description="Test market",
        yes_platform="kalshi",
        yes_price=0.45,
        yes_fee_rate=0.0,
        yes_fee=0.0,
        yes_market_id="K-YES",
        no_platform="polymarket",
        no_price=0.50,
        no_fee=0.0,
        no_market_id="P-NO",
        gross_edge=0.05,
        total_fees=0.0,
        net_edge=0.05,
        net_edge_cents=5.0,
        suggested_qty=10,
        timestamp=0.0,
    )


def _primary() -> Order:
    return Order(
        order_id="ARB-RQ-YES", platform="kalshi", market_id="K-YES",
        canonical_id="MKT", side="yes", price=0.45, quantity=10,
        status=OrderStatus.FILLED, fill_price=0.45, fill_qty=10,
    )


def _order(status: OrderStatus, *, price: float = 0.50, qty: int = 10,
           fill_qty: int = 0, ambiguous: bool = False,
           order_id: str = "SEC-1") -> Order:
    o = Order(
        order_id=order_id, platform="polymarket", market_id="P-NO",
        canonical_id="MKT", side="no", price=price, quantity=qty,
        status=status, fill_qty=fill_qty,
        fill_price=price if fill_qty else 0.0,
    )
    o.idempotency_ambiguous = ambiguous
    return o


def _adapter(place_results: List[Order], walks: List[tuple],
             fok_result: Order | None = None):
    """Adapter whose place_ioc pops from place_results and whose
    best_executable_price pops from walks (last value repeats)."""
    adapter = MagicMock()
    adapter.platform = "polymarket"
    ioc_calls: List[float] = []
    fok_calls: List[float] = []
    walk_state = {"i": 0}

    async def _ioc(arb_id, market_id, canonical_id, side, price, qty, **kw):
        ioc_calls.append(price)
        return place_results.pop(0) if place_results else _order(OrderStatus.FAILED)

    async def _fok(arb_id, market_id, canonical_id, side, price, qty, **kw):
        fok_calls.append(price)
        return fok_result or _order(OrderStatus.FAILED, order_id="SEC-FOK")

    async def _walk(market_id, side, qty):
        i = min(walk_state["i"], len(walks) - 1)
        walk_state["i"] += 1
        return walks[i]

    adapter.place_ioc = _ioc
    adapter.place_fok = _fok
    adapter.best_executable_price = _walk
    adapter.cancel_order = AsyncMock(return_value=True)
    adapter._ioc_calls = ioc_calls
    adapter._fok_calls = fok_calls
    return adapter


def _run_fallbacks(engine, adapter, **overrides):
    engine.adapters = {"polymarket": adapter, "kalshi": MagicMock()}
    kwargs = dict(
        arb_id="ARB-RQ",
        secondary_platform="polymarket",
        secondary_market="P-NO",
        canonical_id="MKT",
        secondary_side="no",
        initial_price=0.50,
        qty=10,
        max_affordable_price=0.50,
        primary_leg=_primary(),
        primary_platform="kalshi",
        opp=_make_opp(),
    )
    kwargs.update(overrides)
    return engine._execute_with_fallbacks(**kwargs)


# ─── Clean-reject requote ────────────────────────────────────────────────


def test_secondary_requote_retries_after_clean_reject_and_fills():
    """Attempt-1 IOC rejects cleanly; the re-walked book is affordable
    again -> a requote IOC must fire and its fill completes the arb.
    No FOK, no naked leg."""
    async def runner():
        engine = _make_engine()
        adapter = _adapter(
            place_results=[
                _order(OrderStatus.FAILED),                      # attempt 1
                _order(OrderStatus.FILLED, fill_qty=10),         # requote 1
            ],
            walks=[(True, 0.49)],  # affordable on re-walk
        )
        order = await _run_fallbacks(engine, adapter)
        assert order.status == OrderStatus.FILLED
        assert order.fill_qty == 10
        assert len(adapter._ioc_calls) == 2
        assert len(adapter._fok_calls) == 0

    asyncio.run(runner())


def test_secondary_requote_bounded_by_max_attempts():
    """The book never becomes affordable: no requote IOC is ever placed,
    the walk runs at most SECONDARY_REQUOTE_MAX_ATTEMPTS times, and the
    legacy final FOK still fires."""
    async def runner():
        engine = _make_engine()
        engine._secondary_requote_attempts = 2
        adapter = _adapter(
            place_results=[_order(OrderStatus.FAILED)],
            walks=[(True, 0.60)],  # forever above max_affordable + breakeven
        )
        order = await _run_fallbacks(engine, adapter)
        assert order.status == OrderStatus.FAILED
        assert len(adapter._ioc_calls) == 1          # only attempt 1
        assert len(adapter._fok_calls) == 1          # legacy final FOK
        assert "requote" in order.error.lower()

    asyncio.run(runner())


def test_secondary_requote_ambiguous_result_stops_chain():
    """A requote attempt that comes back idempotency_ambiguous must stop
    everything — no further IOC, no FOK."""
    async def runner():
        engine = _make_engine()
        adapter = _adapter(
            place_results=[
                _order(OrderStatus.FAILED),                          # attempt 1
                _order(OrderStatus.CANCELLED, ambiguous=True),       # requote 1
            ],
            walks=[(True, 0.49)],
        )
        order = await _run_fallbacks(engine, adapter)
        assert order.idempotency_ambiguous is True
        assert len(adapter._ioc_calls) == 2
        assert len(adapter._fok_calls) == 0

    asyncio.run(runner())


# ─── EDGE-LOST entry (skip_initial_attempt) ──────────────────────────────


def test_edge_lost_requotes_before_abort():
    """The pre-submit walk was unaffordable (EDGE-LOST path). Entering the
    requote loop finds the book back within max-affordable on the second
    walk -> the secondary is placed and fills. ~35 naked arbs took the
    instant-abort path this replaces."""
    async def runner():
        engine = _make_engine()
        adapter = _adapter(
            place_results=[_order(OrderStatus.FILLED, fill_qty=10)],
            walks=[(True, 0.60), (True, 0.49)],  # unaffordable, then back
        )
        engine._secondary_requote_attempts = 2
        order = await _run_fallbacks(engine, adapter, skip_initial_attempt=True)
        assert order is not None
        assert order.status == OrderStatus.FILLED
        assert len(adapter._ioc_calls) == 1
        assert len(adapter._fok_calls) == 0

    asyncio.run(runner())


def test_edge_lost_exhausted_returns_none_without_orders():
    """skip_initial_attempt with a book that never comes back: no order is
    ever submitted (no doomed FOK either) and the call returns None so the
    call site constructs the ABORTED EDGE-LOST leg."""
    async def runner():
        engine = _make_engine()
        engine._secondary_requote_attempts = 2
        adapter = _adapter(
            place_results=[],
            walks=[(True, 0.60)],
        )
        order = await _run_fallbacks(engine, adapter, skip_initial_attempt=True)
        assert order is None
        assert len(adapter._ioc_calls) == 0
        assert len(adapter._fok_calls) == 0

    asyncio.run(runner())


# ─── Break-even completion tier ──────────────────────────────────────────


def test_breakeven_completion_only_on_final_attempt():
    """Walked price sits between max-affordable (0.50) and break-even
    (0.55): early requote attempts must NOT pay through the edge floor;
    the FINAL attempt may complete at break-even — completing at >= 0¢
    net beats a panic unwind."""
    async def runner():
        engine = _make_engine()
        engine._secondary_requote_attempts = 3
        adapter = _adapter(
            place_results=[
                _order(OrderStatus.FAILED),                      # attempt 1
                _order(OrderStatus.FILLED, fill_qty=10,          # final requote
                       price=0.55),
            ],
            walks=[(True, 0.53)],  # above max_affordable, below break-even
        )
        order = await _run_fallbacks(
            engine, adapter, break_even_price=0.55,
        )
        assert order.status == OrderStatus.FILLED
        # exactly one requote IOC (the final-attempt break-even one)
        assert len(adapter._ioc_calls) == 2
        assert adapter._ioc_calls[-1] <= 0.55 + 1e-9
        assert adapter._ioc_calls[-1] > 0.50

    asyncio.run(runner())


def test_no_breakeven_tier_when_not_provided():
    """Without break_even_price, a walk between max-affordable and
    break-even never produces a requote order."""
    async def runner():
        engine = _make_engine()
        engine._secondary_requote_attempts = 2
        adapter = _adapter(
            place_results=[_order(OrderStatus.FAILED)],
            walks=[(True, 0.53)],
        )
        order = await _run_fallbacks(engine, adapter)
        assert len(adapter._ioc_calls) == 1  # attempt 1 only
        assert order.status == OrderStatus.FAILED

    asyncio.run(runner())
