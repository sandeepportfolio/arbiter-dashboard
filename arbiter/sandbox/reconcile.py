"""Phase 4 reconciliation tolerance helpers (plus-or-minus $0.01 absolute = hard gate per D-17)."""
from __future__ import annotations

RECONCILE_TOLERANCE_USD = 0.01  # D-17: plus/minus 1 cent absolute


def assert_pnl_within_tolerance(
    platform: str,
    pre_balance: float,
    post_balance: float,
    recorded_pnl: float,
    tolerance: float = RECONCILE_TOLERANCE_USD,
) -> None:
    """Assert actual balance delta matches recorded PnL within tolerance; raise AssertionError otherwise."""
    delta = post_balance - pre_balance
    discrepancy = delta - recorded_pnl
    assert abs(discrepancy) <= tolerance, (
        f"[{platform}] PnL reconciliation FAILED: "
        f"balance_delta={delta:+.4f} recorded_pnl={recorded_pnl:+.4f} "
        f"discrepancy={discrepancy:+.4f} (tolerance +/-{tolerance:.2f}). "
        f"Phase 5 blocked per D-19."
    )


def assert_fee_matches(
    platform: str,
    platform_fee: float,
    computed_fee: float,
    tolerance: float = RECONCILE_TOLERANCE_USD,
) -> None:
    """Assert platform-reported fee matches local fee calculation within tolerance."""
    discrepancy = platform_fee - computed_fee
    assert abs(discrepancy) <= tolerance, (
        f"[{platform}] Fee reconciliation FAILED: "
        f"platform_fee={platform_fee:.4f} computed_fee={computed_fee:.4f} "
        f"discrepancy={discrepancy:+.4f} (tolerance +/-{tolerance:.2f}). "
        f"Phase 5 blocked per D-19."
    )
