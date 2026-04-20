"""
Tests for the Trade Execution Math Auditor (shadow calculator).
Verifies that the auditor correctly flags discrepancies and passes clean opps.
"""
import sys
import os
import pytest
from .math_auditor import (
    MathAuditor,
    _kalshi_fee,
    _polymarket_fee,
    DISCREPANCY_THRESHOLD,
)

# Import the primary fee calculator for cross-validation
from arbiter.config.settings import polymarket_order_fee


# ─── Fee Model Tests ──────────────────────────────────────────────────────

class TestFeeModels:
    def test_kalshi_fee_midpoint(self):
        # One-contract order rounds 1.75 cents up to 2 cents.
        assert abs(_kalshi_fee(0.50) - 0.02) < 1e-10

    def test_kalshi_fee_bulk_order(self):
        # 100 contracts at 50 cents -> $1.75 total, or 1.75 cents amortized.
        assert abs(_kalshi_fee(0.50, quantity=100) - 0.0175) < 1e-10

    def test_kalshi_fee_extremes(self):
        # Small single-contract orders round up to a penny.
        assert abs(_kalshi_fee(0.01) - 0.01) < 1e-10
        assert abs(_kalshi_fee(0.99) - 0.01) < 1e-10

    def test_kalshi_fee_zero(self):
        assert _kalshi_fee(0.0) == 0.0

    def test_polymarket_fee_politics(self):
        # 0.60 * 0.40 * 0.04 = 0.0096 (rate: 0.04 per 2026 schedule)
        assert abs(_polymarket_fee(0.60, "politics") - 0.0096) < 1e-10

    def test_polymarket_fee_sports(self):
        # 0.60 * 0.40 * 0.03 = 0.0072 (rate: 0.03 per 2026 schedule)
        assert abs(_polymarket_fee(0.60, "sports") - 0.0072) < 1e-10

    def test_polymarket_fee_explicit_rate(self):
        assert abs(_polymarket_fee(0.60, "politics", fee_rate=0.01) - 0.0024) < 1e-10

    def test_polymarket_fee_unknown_category(self):
        # Unknown categories fall back to default rate 0.05
        # 0.50 * 0.50 * 0.05 = 0.0125
        assert abs(_polymarket_fee(0.50, "unknown") - 0.0125) < 1e-10

    def test_polymarket_fee_crypto(self):
        # 0.60 * 0.40 * 0.072 = 0.01728 (rate: 0.072 per 2026 schedule)
        assert abs(_polymarket_fee(0.60, "crypto") - 0.01728) < 1e-10

    def test_polymarket_fee_geopolitics(self):
        # geopolitics has 0% fee per 2026 schedule
        assert _polymarket_fee(0.60, "geopolitics") == 0.0

    def test_polymarket_fee_finance(self):
        # 0.60 * 0.40 * 0.04 = 0.0096 (rate: 0.04 per 2026 schedule)
        assert abs(_polymarket_fee(0.60, "finance") - 0.0096) < 1e-10

    def test_polymarket_shadow_matches_settings(self):
        # Shadow calculator and primary calculator must produce identical results
        # polymarket_order_fee(price, quantity=1, category=cat) should equal _polymarket_fee(price, cat)
        assert abs(polymarket_order_fee(0.60, category="politics") - _polymarket_fee(0.60, "politics")) < 1e-10

# ─── Auditor Tests: Clean Opportunities ──────────────────────────────────

class TestAuditorCleanOpps:
    def setup_method(self):
        self.auditor = MathAuditor(max_position_usd=100.0)

    def _make_kalshi_poly_opp(self, yes_price=0.42, no_price=0.45):
        """Create a correctly-computed Kalshi↔Polymarket opportunity."""
        cost_per_pair = yes_price + no_price
        qty = max(1, int(100.0 / cost_per_pair))
        fee_a = _kalshi_fee(yes_price, quantity=qty)
        fee_b = _polymarket_fee(no_price, "politics", quantity=qty)
        gross = 1.0 - yes_price - no_price
        total_fees = fee_a + fee_b
        net = gross - total_fees
        return {
            "canonical_id": "TEST_MARKET",
            "yes_platform": "kalshi",
            "no_platform": "polymarket",
            "yes_price": yes_price,
            "no_price": no_price,
            "gross_edge": gross,
            "total_fees": total_fees,
            "net_edge": net,
            "net_edge_cents": net * 100,
            "suggested_qty": qty,
            "max_profit_usd": net * qty,
        }

    def test_clean_kalshi_poly_passes(self):
        opp = self._make_kalshi_poly_opp(0.42, 0.45)
        result = self.auditor.audit_opportunity(opp)
        assert result.passed, f"Should pass clean opp, got flags: {[f.message for f in result.flags]}"

    def test_clean_various_prices(self):
        for yp, np in [(0.30, 0.50), (0.55, 0.35), (0.10, 0.80), (0.70, 0.15)]:
            opp = self._make_kalshi_poly_opp(yp, np)
            result = self.auditor.audit_opportunity(opp)
            assert result.passed, f"Failed for yes={yp}, no={np}: {[f.message for f in result.flags]}"

# ─── Auditor Tests: Discrepancy Detection ────────────────────────────────

class TestAuditorDiscrepancies:
    def setup_method(self):
        self.auditor = MathAuditor(max_position_usd=100.0)

    def test_wrong_gross_edge(self):
        opp = {
            "canonical_id": "TEST", "yes_platform": "kalshi", "no_platform": "polymarket",
            "yes_price": 0.42, "no_price": 0.45,
            "gross_edge": 0.15,  # wrong: should be 0.13
            "total_fees": 0.02, "net_edge": 0.11, "net_edge_cents": 11.0,
            "suggested_qty": 1, "max_profit_usd": 0.11,
        }
        result = self.auditor.audit_opportunity(opp)
        assert not result.passed
        fields = [f.field for f in result.flags]
        assert "gross_edge" in fields

    def test_wrong_fees(self):
        yes_price, no_price = 0.42, 0.45
        gross = 1.0 - yes_price - no_price
        opp = {
            "canonical_id": "TEST", "yes_platform": "kalshi", "no_platform": "polymarket",
            "yes_price": yes_price, "no_price": no_price,
            "gross_edge": gross,
            "total_fees": 0.001,  # way too low
            "net_edge": gross - 0.001, "net_edge_cents": (gross - 0.001) * 100,
            "suggested_qty": 1, "max_profit_usd": gross - 0.001,
        }
        result = self.auditor.audit_opportunity(opp)
        assert not result.passed
        fields = [f.field for f in result.flags]
        assert "total_fees" in fields

    def test_sign_flip_critical(self):
        """Shadow computes negative edge but scanner says positive."""
        opp = {
            "canonical_id": "TEST", "yes_platform": "kalshi", "no_platform": "polymarket",
            "yes_price": 0.52, "no_price": 0.50,  # sum > 1, no edge
            "gross_edge": 0.05, "total_fees": 0.01,
            "net_edge": 0.04, "net_edge_cents": 4.0,
            "suggested_qty": 10, "max_profit_usd": 0.40,
        }
        result = self.auditor.audit_opportunity(opp)
        assert not result.passed
        severities = [f.severity for f in result.flags]
        assert "critical" in severities

    def test_wrong_quantity(self):
        yes_price, no_price = 0.42, 0.45
        correct_qty = max(1, int(100.0 / (yes_price + no_price)))
        fee_a = _kalshi_fee(yes_price, quantity=correct_qty)
        fee_b = _polymarket_fee(no_price, quantity=correct_qty)
        gross = 1.0 - yes_price - no_price
        total_fees = fee_a + fee_b
        net = gross - total_fees
        opp = {
            "canonical_id": "TEST", "yes_platform": "kalshi", "no_platform": "polymarket",
            "yes_price": yes_price, "no_price": no_price,
            "gross_edge": gross, "total_fees": total_fees,
            "net_edge": net, "net_edge_cents": net * 100,
            "suggested_qty": 999,  # wrong
            "max_profit_usd": net * 999,
        }
        result = self.auditor.audit_opportunity(opp)
        assert not result.passed
        fields = [f.field for f in result.flags]
        assert "suggested_qty" in fields

    def test_max_profit_inconsistency(self):
        """max_profit doesn't match net_edge × qty."""
        yes_price, no_price = 0.42, 0.45
        qty = max(1, int(100.0 / (yes_price + no_price)))
        fee_a = _kalshi_fee(yes_price, quantity=qty)
        fee_b = _polymarket_fee(no_price, quantity=qty)
        gross = 1.0 - yes_price - no_price
        total_fees = fee_a + fee_b
        net = gross - total_fees
        opp = {
            "canonical_id": "TEST", "yes_platform": "kalshi", "no_platform": "polymarket",
            "yes_price": yes_price, "no_price": no_price,
            "gross_edge": gross, "total_fees": total_fees,
            "net_edge": net, "net_edge_cents": net * 100,
            "suggested_qty": qty,
            "max_profit_usd": 999.99,  # wrong
        }
        result = self.auditor.audit_opportunity(opp)
        assert not result.passed
        fields = [f.field for f in result.flags]
        assert "max_profit_usd" in fields


# ─── Execution Audit Tests ───────────────────────────────────────────────

class TestExecutionAudit:
    def setup_method(self):
        self.auditor = MathAuditor()

    def test_one_leg_filled_critical(self):
        opp = {
            "canonical_id": "TEST", "yes_platform": "kalshi", "no_platform": "polymarket",
            "yes_price": 0.42, "no_price": 0.45,
            "gross_edge": 0.13, "total_fees": 0.02, "net_edge": 0.11,
            "net_edge_cents": 11.0, "suggested_qty": 1, "max_profit_usd": 0.11,
        }
        exec_dict = {
            "opportunity": opp,
            "leg_yes": {"fill_price": 0.42, "status": "filled"},
            "leg_no": {"fill_price": 0.0, "status": "failed"},
            "realized_pnl": 0.0,
        }
        result = self.auditor.audit_execution(exec_dict)
        assert not result.passed
        assert any(f.field == "leg_mismatch" for f in result.flags)
        assert any(f.severity == "critical" for f in result.flags)

    def test_fill_slippage_flagged(self):
        opp = {
            "canonical_id": "TEST", "yes_platform": "kalshi", "no_platform": "polymarket",
            "yes_price": 0.42, "no_price": 0.45,
            "gross_edge": 0.13, "total_fees": 0.02, "net_edge": 0.11,
            "net_edge_cents": 11.0, "suggested_qty": 1, "max_profit_usd": 0.11,
        }
        exec_dict = {
            "opportunity": opp,
            "leg_yes": {"fill_price": 0.44, "status": "filled"},  # 2¢ slip
            "leg_no": {"fill_price": 0.45, "status": "filled"},
            "realized_pnl": 0.09,
        }
        result = self.auditor.audit_execution(exec_dict)
        assert any(f.field == "yes_fill_slippage" for f in result.flags)


# ─── Stats Tests ──────────────────────────────────────────────────────────

class TestAuditorStats:
    def test_stats_tracking(self):
        auditor = MathAuditor()
        # Run a clean audit
        qty = max(1, int(100.0 / 0.87))
        fee_a = _kalshi_fee(0.42, quantity=qty)
        fee_b = _polymarket_fee(0.45, quantity=qty)
        gross = 1.0 - 0.42 - 0.45
        total_fees = fee_a + fee_b
        net = gross - total_fees
        opp = {
            "canonical_id": "TEST", "yes_platform": "kalshi", "no_platform": "polymarket",
            "yes_price": 0.42, "no_price": 0.45,
            "gross_edge": gross, "total_fees": total_fees,
            "net_edge": net, "net_edge_cents": net * 100,
            "suggested_qty": qty, "max_profit_usd": net * qty,
        }
        auditor.audit_opportunity(opp)
        stats = auditor.stats
        assert stats["audits_run"] == 1
        assert stats["total_flags"] == 0
        assert stats["pass_rate"] == 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
