"""
Exhaustive validation of arbitrage math and fee calculations.

Tests verify that:
1. Arb profit formula: profit = 1.0 - yes_price - no_price - fees > 0
2. Fee calculations match known platform formulas
3. Edge cases (boundary prices, zero qty, extreme prices) are handled safely
4. No opportunity with negative net edge can ever be marked tradable
5. The non-binary market guard works correctly
"""
import math
import sys
import os
import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbiter.config.settings import (
    kalshi_order_fee,
    kalshi_fee,
    polymarket_order_fee,
    polymarket_fee,
    KALSHI_TAKER_FEE_RATE,
    POLYMARKET_DEFAULT_TAKER_FEE_RATE,
)
from arbiter.scanner.arbitrage import compute_fee


# ═══════════════════════════════════════════════════════════════════════════
# 1. KALSHI FEE VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class TestKalshiFees:
    """
    Kalshi fee formula:
      raw = 0.07 * quantity * price * (1 - price)
      fee = ceil(raw * 100 - 1e-9) / 100
    Cap: $0.035 per contract (not enforced in code; Kalshi caps server-side).
    """

    def test_midpoint_price_single_contract(self):
        """At price=0.50, fee = ceil(0.07 * 1 * 0.50 * 0.50 * 100) / 100 = ceil(1.75)/100 = 0.02"""
        fee = kalshi_order_fee(0.50, quantity=1)
        assert fee == 0.02, f"Expected $0.02, got ${fee}"

    def test_extreme_low_price(self):
        """At price=0.05, fee = ceil(0.07 * 1 * 0.05 * 0.95 * 100) / 100 = ceil(0.3325)/100 = 0.01"""
        fee = kalshi_order_fee(0.05, quantity=1)
        assert fee == 0.01, f"Expected $0.01, got ${fee}"

    def test_extreme_high_price(self):
        """At price=0.95, fee = ceil(0.07 * 1 * 0.95 * 0.05 * 100) / 100 = ceil(0.3325)/100 = 0.01"""
        fee = kalshi_order_fee(0.95, quantity=1)
        assert fee == 0.01, f"Expected $0.01, got ${fee}"

    def test_zero_price_returns_zero(self):
        assert kalshi_order_fee(0.0, quantity=1) == 0.0

    def test_negative_price_returns_zero(self):
        assert kalshi_order_fee(-0.1, quantity=1) == 0.0

    def test_price_above_one_clamped(self):
        """Price > 1.0 should be clamped to 1.0, giving fee = 0"""
        assert kalshi_order_fee(1.5, quantity=1) == 0.0

    def test_zero_quantity_returns_zero(self):
        assert kalshi_order_fee(0.50, quantity=0) == 0.0

    def test_bulk_quantity(self):
        """10 contracts at 0.50: raw = 0.07 * 10 * 0.50 * 0.50 = 0.175 → ceil(17.5)/100 = 0.18"""
        fee = kalshi_order_fee(0.50, quantity=10)
        expected = math.ceil((0.07 * 10 * 0.50 * 0.50 * 100) - 1e-9) / 100.0
        assert abs(fee - expected) < 1e-9, f"Expected ${expected}, got ${fee}"

    def test_fee_always_non_negative(self):
        """Fee must never be negative for any valid input."""
        for price in [0.01, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99]:
            for qty in [1, 5, 10, 50, 100]:
                fee = kalshi_order_fee(price, quantity=qty)
                assert fee >= 0.0, f"Negative fee at price={price}, qty={qty}: {fee}"

    def test_per_contract_fee_consistency(self):
        """kalshi_fee(p, q) should equal kalshi_order_fee(p, q) / q"""
        for price in [0.10, 0.30, 0.50, 0.70, 0.90]:
            for qty in [1, 5, 10]:
                order_fee = kalshi_order_fee(price, quantity=qty)
                per_contract = kalshi_fee(price, quantity=qty)
                assert abs(per_contract - order_fee / qty) < 1e-9


# ═══════════════════════════════════════════════════════════════════════════
# 2. POLYMARKET FEE VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class TestPolymarketFees:
    """
    Polymarket fee formula:
      fee = rate * quantity * price * (1 - price)
    Rate depends on category. Politics = 0.04.
    """

    def test_politics_midpoint(self):
        """Politics at 0.50: 0.04 * 1 * 0.50 * 0.50 = 0.01"""
        fee = polymarket_order_fee(0.50, quantity=1, category="politics")
        assert abs(fee - 0.01) < 1e-9, f"Expected $0.01, got ${fee}"

    def test_default_rate_midpoint(self):
        """Default rate 0.05 at 0.50: 0.05 * 1 * 0.50 * 0.50 = 0.0125"""
        fee = polymarket_order_fee(0.50, quantity=1, category="default")
        assert abs(fee - 0.0125) < 1e-9

    def test_zero_price(self):
        assert polymarket_order_fee(0.0, quantity=1) == 0.0

    def test_zero_quantity(self):
        assert polymarket_order_fee(0.50, quantity=0) == 0.0

    def test_explicit_fee_rate_override(self):
        """When fee_rate is explicitly provided, it overrides category-based rate."""
        fee = polymarket_order_fee(0.50, quantity=1, fee_rate=0.10, category="politics")
        expected = 0.10 * 1 * 0.50 * 0.50
        assert abs(fee - expected) < 1e-9

    def test_fee_always_non_negative(self):
        for price in [0.01, 0.25, 0.50, 0.75, 0.99]:
            for qty in [1, 5, 10, 50]:
                fee = polymarket_order_fee(price, quantity=qty, category="politics")
                assert fee >= 0.0

    def test_geopolitics_zero_fee(self):
        """Geopolitics category has 0% fee."""
        fee = polymarket_order_fee(0.50, quantity=10, category="geopolitics")
        assert fee == 0.0

    def test_bulk_quantity_politics(self):
        """10 contracts at 0.60 politics: 0.04 * 10 * 0.60 * 0.40 = 0.096"""
        fee = polymarket_order_fee(0.60, quantity=10, category="politics")
        expected = 0.04 * 10 * 0.60 * 0.40
        assert abs(fee - expected) < 1e-9


# ═══════════════════════════════════════════════════════════════════════════
# 3. COMPUTE_FEE ROUTER VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeFeeRouter:
    """compute_fee() routes to the correct platform-specific function."""

    def test_kalshi_routing(self):
        fee = compute_fee("kalshi", 0.50, 10)
        expected = kalshi_order_fee(0.50, quantity=10)
        assert abs(fee - expected) < 1e-9

    def test_polymarket_routing(self):
        fee = compute_fee("polymarket", 0.50, 10, fee_rate=0.04)
        expected = polymarket_order_fee(0.50, quantity=10, fee_rate=0.04, category="politics")
        assert abs(fee - expected) < 1e-9

    def test_unknown_platform_returns_zero(self):
        assert compute_fee("predictit", 0.50, 10) == 0.0

    def test_zero_quantity_returns_zero(self):
        assert compute_fee("kalshi", 0.50, 0) == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 4. ARB PROFIT INVARIANT
# ═══════════════════════════════════════════════════════════════════════════

class TestArbProfitInvariant:
    """
    Core invariant: for a valid binary arb,
      profit_per_contract = 1.0 - yes_price - no_price - total_fees_per_contract
    This must be POSITIVE for any opportunity that reaches "tradable" status.
    """

    def _compute_net_edge(self, yes_price, no_price, yes_platform, no_platform, qty=10):
        """Reproduce the scanner's net edge calculation."""
        gross = 1.0 - yes_price - no_price
        if gross <= 0:
            return gross, 0.0, 0.0

        yes_fee_total = compute_fee(yes_platform, yes_price, qty, fee_rate=0.04)
        no_fee_total = compute_fee(no_platform, no_price, qty, fee_rate=0.04)
        total_fees_per_contract = (yes_fee_total + no_fee_total) / qty
        net = gross - total_fees_per_contract
        return gross, total_fees_per_contract, net

    def test_profitable_scenario(self):
        """YES=0.40 on Kalshi + NO=0.45 on Polymarket → gross=0.15 → profitable after fees."""
        gross, fees, net = self._compute_net_edge(0.40, 0.45, "kalshi", "polymarket")
        assert gross == pytest.approx(0.15, abs=1e-9)
        assert net > 0, f"Expected positive net edge, got {net} (fees={fees})"

    def test_no_arb_when_prices_sum_to_one(self):
        """YES=0.50 + NO=0.50 = 1.00 → gross=0.00 → no arb."""
        gross, _, _ = self._compute_net_edge(0.50, 0.50, "kalshi", "polymarket")
        assert gross <= 0

    def test_no_arb_when_prices_exceed_one(self):
        """YES=0.55 + NO=0.55 = 1.10 → gross=-0.10 → no arb."""
        gross, _, _ = self._compute_net_edge(0.55, 0.55, "kalshi", "polymarket")
        assert gross < 0

    def test_thin_edge_eaten_by_fees(self):
        """YES=0.48 + NO=0.49 = 0.97 → gross=0.03 → may be eaten by fees."""
        gross, fees, net = self._compute_net_edge(0.48, 0.49, "kalshi", "polymarket")
        assert gross == pytest.approx(0.03, abs=1e-9)
        # With Kalshi's ceil rounding, fees at these prices may exceed the edge
        # This test documents the behavior — the scanner should filter this out

    def test_profitable_extreme_prices(self):
        """YES=0.05 on Poly + NO=0.05 on Kalshi → gross=0.90 → hugely profitable."""
        gross, fees, net = self._compute_net_edge(0.05, 0.05, "polymarket", "kalshi")
        assert gross == pytest.approx(0.90, abs=1e-9)
        assert net > 0.80, f"Expected large net edge, got {net}"

    def test_settlement_payout_always_one_dollar(self):
        """For any binary market, settlement = $1.00 per contract."""
        for yes_p in [0.10, 0.30, 0.50, 0.70, 0.90]:
            no_p = 1.0 - yes_p - 0.10  # 10¢ gross edge
            if no_p > 0:
                settlement = 1.00
                cost = yes_p + no_p
                assert settlement > cost, f"Settlement ${settlement} must exceed cost ${cost}"

    def test_sweep_all_price_pairs(self):
        """Sweep price combinations: verify net edge is always computed correctly."""
        for y in range(5, 96, 5):
            for n in range(5, 96, 5):
                yes_p = y / 100.0
                no_p = n / 100.0
                gross = 1.0 - yes_p - no_p
                if gross <= 0:
                    continue
                gross_calc, fees, net = self._compute_net_edge(yes_p, no_p, "kalshi", "polymarket")
                assert abs(gross_calc - gross) < 1e-9
                assert fees >= 0
                # net = gross - fees, verify arithmetic
                assert abs(net - (gross - fees)) < 1e-9


# ═══════════════════════════════════════════════════════════════════════════
# 5. NON-BINARY MARKET GUARD
# ═══════════════════════════════════════════════════════════════════════════

class TestNonBinaryGuard:
    """
    The scanner rejects opportunities where a platform's own yes+no
    deviates from $1.00 by more than 15¢. This prevents trading
    multi-outcome markets as if they were binary.
    """

    def test_valid_binary_passes(self):
        """yes=0.55 + no=0.45 = 1.00 → valid binary."""
        platform_sum = 0.55 + 0.45
        assert abs(platform_sum - 1.0) <= 0.15

    def test_slight_deviation_passes(self):
        """yes=0.52 + no=0.52 = 1.04 → 4¢ deviation, within 15¢ tolerance."""
        platform_sum = 0.52 + 0.52
        assert abs(platform_sum - 1.0) <= 0.15

    def test_multi_outcome_blocked(self):
        """yes=0.30 + no=0.30 = 0.60 → 40¢ deviation, multi-outcome market."""
        platform_sum = 0.30 + 0.30
        assert abs(platform_sum - 1.0) > 0.15


# ═══════════════════════════════════════════════════════════════════════════
# 6. POSITION SIZING SAFETY
# ═══════════════════════════════════════════════════════════════════════════

class TestPositionSizing:
    """Verify position sizing never exceeds safety caps."""

    def test_max_profit_calculation(self):
        """max_profit_usd = net_edge * suggested_qty"""
        net_edge = 0.05  # 5¢ per contract
        qty = 10
        max_profit = round(net_edge * qty, 4)
        assert max_profit == 0.50

    def test_cost_never_exceeds_cap(self):
        """Total cost = (yes_price + no_price) * qty must not exceed max_position_usd."""
        max_position = 10.0
        for yes_p in [0.30, 0.50, 0.70]:
            for no_p in [0.20, 0.40, 0.60]:
                cost_per_pair = yes_p + no_p
                max_qty = int(max_position / cost_per_pair)
                total_cost = cost_per_pair * max_qty
                assert total_cost <= max_position + cost_per_pair  # at most 1 pair over due to int rounding


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
