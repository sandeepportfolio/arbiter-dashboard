"""Non-live unit tests for arbiter.live.reconcile.reconcile_post_trade.

These tests stub ``execution`` + adapters with SimpleNamespace + AsyncMock so
no network I/O happens. They verify the four branches of the helper:

* All-FILLED legs + fee_fetcher returning the same number as the fee model
  -> empty discrepancy list.
* One leg's fee_fetcher returns platform_fee that differs by more than
  tolerance -> discrepancy dict returned.
* A FAILED leg is skipped (not FILLED -> no fee to reconcile).
* ``fee_fetcher=None`` -> no discrepancies possible, empty list.

Follows the root-conftest async dispatch style (``async def`` with no marker).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from arbiter.execution.engine import OrderStatus
from arbiter.live.reconcile import (
    RECONCILE_TOLERANCE_USD,
    reconcile_post_trade,
)


def _leg(
    *,
    platform: str,
    status: OrderStatus,
    fill_price: float,
    fill_qty: float,
    order_id: str = "ORD-1",
    canonical_id: str = "CAN",
):
    return SimpleNamespace(
        platform=platform,
        status=status,
        fill_price=fill_price,
        fill_qty=fill_qty,
        order_id=order_id,
        canonical_id=canonical_id,
    )


def _execution(*, leg_yes, leg_no, arb_id: str = "ARB-T"):
    return SimpleNamespace(arb_id=arb_id, leg_yes=leg_yes, leg_no=leg_no)


async def test_reconcile_all_filled_matching_fees_returns_empty():
    """Both legs FILLED, fee_fetcher returns exactly the computed fee -> no drift."""
    # Use prices/quantities that keep fees small; the fetcher returns whatever
    # the fee model returns so drift = 0.
    from arbiter.config.settings import (
        kalshi_order_fee,
        polymarket_order_fee,
    )

    leg_y = _leg(
        platform="kalshi", status=OrderStatus.FILLED,
        fill_price=0.40, fill_qty=10, order_id="K-1", canonical_id="CAN-X",
    )
    leg_n = _leg(
        platform="polymarket", status=OrderStatus.FILLED,
        fill_price=0.60, fill_qty=10, order_id="P-1", canonical_id="CAN-X",
    )

    fetcher_calls: list[tuple[str, str]] = []

    async def fetcher(platform, order_id):
        fetcher_calls.append((platform, order_id))
        if platform == "kalshi":
            return kalshi_order_fee(0.40, 10)
        return polymarket_order_fee(0.60, 10)

    discrepancies = await reconcile_post_trade(
        _execution(leg_yes=leg_y, leg_no=leg_n),
        adapters={},
        fee_fetcher=fetcher,
    )
    assert discrepancies == [], f"expected no discrepancies, got {discrepancies}"
    assert fetcher_calls == [("kalshi", "K-1"), ("polymarket", "P-1")], (
        f"fetcher invocation order wrong: {fetcher_calls}"
    )


async def test_reconcile_fee_drift_beyond_tolerance_returns_discrepancy():
    """A fee off by $0.05 (> $0.01 tolerance) -> discrepancy dict returned."""
    from arbiter.config.settings import kalshi_order_fee

    leg_y = _leg(
        platform="kalshi", status=OrderStatus.FILLED,
        fill_price=0.40, fill_qty=10, order_id="K-DRIFT",
    )
    leg_n = _leg(
        platform="kalshi", status=OrderStatus.FAILED,  # ignored
        fill_price=0.0, fill_qty=0, order_id="K-FAILED",
    )

    computed = kalshi_order_fee(0.40, 10)
    platform_returns = computed + 0.05  # $0.05 drift, well over $0.01 tolerance

    async def fetcher(platform, order_id):
        return platform_returns

    discrepancies = await reconcile_post_trade(
        _execution(leg_yes=leg_y, leg_no=leg_n),
        adapters={},
        fee_fetcher=fetcher,
    )
    assert len(discrepancies) == 1, f"expected 1 discrepancy, got {discrepancies}"
    d = discrepancies[0]
    assert d["leg_order_id"] == "K-DRIFT"
    assert d["platform"] == "kalshi"
    assert d["reason"] == "fee_mismatch"
    assert abs(d["discrepancy"] - 0.05) < 1e-9, f"drift={d['discrepancy']}"
    assert d["tolerance"] == RECONCILE_TOLERANCE_USD


async def test_reconcile_skips_failed_legs():
    """FAILED / non-FILLED legs are skipped without consulting fee_fetcher."""
    leg_y = _leg(
        platform="kalshi", status=OrderStatus.FAILED,
        fill_price=0.0, fill_qty=0, order_id="K-FAIL",
    )
    leg_n = _leg(
        platform="polymarket", status=OrderStatus.CANCELLED,
        fill_price=0.0, fill_qty=0, order_id="P-CAN",
    )

    fetcher_calls: list[tuple[str, str]] = []

    async def fetcher(platform, order_id):
        fetcher_calls.append((platform, order_id))
        return 0.0

    discrepancies = await reconcile_post_trade(
        _execution(leg_yes=leg_y, leg_no=leg_n),
        adapters={},
        fee_fetcher=fetcher,
    )
    assert discrepancies == []
    assert fetcher_calls == [], (
        f"fetcher must not be invoked for non-FILLED legs; got {fetcher_calls}"
    )


async def test_reconcile_returns_empty_when_no_fee_fetcher():
    """``fee_fetcher=None`` -> no way to detect discrepancy, returns []."""
    leg_y = _leg(
        platform="kalshi", status=OrderStatus.FILLED,
        fill_price=0.40, fill_qty=10, order_id="K-1",
    )
    leg_n = _leg(
        platform="polymarket", status=OrderStatus.FILLED,
        fill_price=0.60, fill_qty=10, order_id="P-1",
    )

    discrepancies = await reconcile_post_trade(
        _execution(leg_yes=leg_y, leg_no=leg_n),
        adapters={},
        fee_fetcher=None,
    )
    assert discrepancies == []
