"""Phase 5 reconciliation helpers — re-exports Phase 4 tolerances + adds post-trade helper.

The ±$0.01 tolerance (D-17) is a single source of truth in
``arbiter/sandbox/reconcile.py``. This module re-exports it unchanged so
Phase 5 inherits the exact same gate, plus adds ``reconcile_post_trade`` —
an async helper that compares platform-reported fills/fees against the
execution record and returns a list of discrepancy dicts.

``reconcile_post_trade`` is INTENTIONALLY pure: it does NOT call
``supervisor.trip_kill`` itself. Plan 05-02 wires an auto-abort hook that
consumes this helper's return value and trips the kill-switch when any
discrepancy exceeds tolerance. Separating the two concerns keeps
reconciliation unit-testable without a SafetySupervisor mock.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional

from arbiter.sandbox.reconcile import (
    RECONCILE_TOLERANCE_USD,
    assert_fee_matches,
    assert_pnl_within_tolerance,
)

__all__ = [
    "RECONCILE_TOLERANCE_USD",
    "assert_fee_matches",
    "assert_pnl_within_tolerance",
    "reconcile_post_trade",
]


async def reconcile_post_trade(
    execution: Any,
    adapters: Dict[str, Any],
    tolerance: float = RECONCILE_TOLERANCE_USD,
    fee_fetcher: Optional[Callable[[str, str], Awaitable[float]]] = None,
) -> List[Dict[str, Any]]:
    """Compare recorded fee vs platform-reported fee for each FILLED leg.

    Args:
        execution: ``ArbExecution`` from engine. Must expose ``leg_yes``,
            ``leg_no``, ``arb_id``. Each leg is an ``Order`` with
            ``platform``, ``order_id``, ``status``, ``fill_price``,
            ``fill_qty``, ``canonical_id``.
        adapters: Mapping ``{"kalshi": KalshiAdapter, "polymarket": PolymarketAdapter}``.
            Currently unused in the pure helper (Plan 05-02 threads an
            adapter-backed ``fee_fetcher``), but accepted for signature parity
            so the live-fire test passes its adapter dict through without
            repackaging.
        tolerance: Absolute USD tolerance for per-leg fee reconciliation.
            Defaults to ``RECONCILE_TOLERANCE_USD`` (±$0.01, D-17).
        fee_fetcher: Optional async callable ``(platform, order_id) -> platform_fee_usd``.
            When ``None``, this helper returns an empty list (no discrepancies
            possible without a ground-truth source). Plan 05-02 injects a real
            adapter-backed implementation here.

    Returns:
        List of discrepancy dicts. Empty list = all FILLED legs reconciled
        within tolerance (or no ``fee_fetcher`` was supplied).

    Contract:
        - Does NOT raise on discrepancy — returns a dict describing the drift.
        - Does NOT trip the kill-switch. Plan 05-02's auto_abort consumes
          this return value and decides whether to arm safety.
        - Legs with ``status != OrderStatus.FILLED`` are skipped silently
          (no fill means no fee to reconcile).
    """
    # Lazy imports keep this module importable without triggering engine/config
    # circular dependencies at package-load time.
    from arbiter.config.settings import kalshi_order_fee, polymarket_order_fee
    from arbiter.execution.engine import OrderStatus

    del adapters  # reserved for Plan 05-02 — documented for signature parity.

    discrepancies: List[Dict[str, Any]] = []

    for leg in (execution.leg_yes, execution.leg_no):
        if leg.status != OrderStatus.FILLED:
            continue
        if fee_fetcher is None:
            # No ground-truth source — cannot detect discrepancies.
            continue

        platform_fee = await fee_fetcher(leg.platform, leg.order_id)

        if leg.platform == "kalshi":
            computed_fee = kalshi_order_fee(
                float(leg.fill_price), float(leg.fill_qty),
            )
        elif leg.platform == "polymarket":
            # Polymarket fee model is market-category-specific; the per-leg
            # ``Order`` dataclass does not carry the category string, so we
            # fall through to the default rate (same behaviour as the Phase 4
            # reconciliation helpers). Plan 05-02 can thread a category-lookup
            # closure through ``fee_fetcher`` if a per-market rate is needed.
            computed_fee = polymarket_order_fee(
                float(leg.fill_price),
                float(leg.fill_qty),
            )
        else:
            # Unknown platform — cannot compute a fee; flag as discrepancy so
            # the operator/auto-abort sees the gap rather than silently skipping.
            discrepancies.append({
                "arb_id": execution.arb_id,
                "leg_order_id": leg.order_id,
                "platform": leg.platform,
                "platform_fee": float(platform_fee),
                "computed_fee": None,
                "discrepancy": None,
                "tolerance": tolerance,
                "reason": "unknown_platform",
            })
            continue

        drift = float(platform_fee) - float(computed_fee)
        if abs(drift) > tolerance:
            discrepancies.append({
                "arb_id": execution.arb_id,
                "leg_order_id": leg.order_id,
                "platform": leg.platform,
                "platform_fee": float(platform_fee),
                "computed_fee": float(computed_fee),
                "discrepancy": drift,
                "tolerance": tolerance,
                "reason": "fee_mismatch",
            })

    return discrepancies
