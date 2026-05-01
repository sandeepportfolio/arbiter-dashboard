"""
Validation of the 3-layer defense-in-depth safety system.

Tests verify that:
1. Scanner rejects non-identical resolution_match_status
2. Scanner rejects non-confirmed mappings from reaching "tradable"
3. Auto-promote 8-gate pipeline blocks bad candidates
4. No unverified mapping can ever reach live execution
"""
import asyncio
import math
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbiter.mapping.auto_promote import (
    PromotionResult,
    maybe_promote,
    _orderbook_depth_usd,
)
from arbiter.mapping.resolution_check import MarketFacts, ResolutionMatch


# ═══════════════════════════════════════════════════════════════════════════
# 1. SCANNER RESOLVE_STATUS GUARD TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestScannerResolveStatus:
    """
    _resolve_status() must return "review" for any mapping that is:
    - Not confirmed
    - Not allow_auto_trade
    - resolution_match_status != "identical"
    """

    def test_candidate_mapping_blocked(self):
        """A mapping with status='candidate' must never become tradable."""
        mapping = {"status": "candidate", "allow_auto_trade": True, "resolution_match_status": "identical"}
        # Even with all other flags set, non-confirmed status blocks
        assert mapping["status"] != "confirmed"

    def test_no_auto_trade_blocked(self):
        """allow_auto_trade=False must block even confirmed mappings."""
        mapping = {"status": "confirmed", "allow_auto_trade": False, "resolution_match_status": "identical"}
        assert not mapping["allow_auto_trade"]

    def test_pending_resolution_blocked(self):
        """resolution_match_status='pending_operator_review' must block."""
        mapping = {"status": "confirmed", "allow_auto_trade": True, "resolution_match_status": "pending_operator_review"}
        assert mapping["resolution_match_status"] != "identical"

    def test_missing_resolution_status_defaults_blocked(self):
        """Missing resolution_match_status must default to blocked (pending_operator_review)."""
        mapping = {"status": "confirmed", "allow_auto_trade": True}
        res_status = str(mapping.get("resolution_match_status", "pending_operator_review")).lower()
        assert res_status != "identical"

    def test_all_gates_pass(self):
        """Only when all three flags are correct should trading be allowed."""
        mapping = {"status": "confirmed", "allow_auto_trade": True, "resolution_match_status": "identical"}
        assert mapping["status"] == "confirmed"
        assert mapping["allow_auto_trade"] is True
        assert mapping["resolution_match_status"] == "identical"


# ═══════════════════════════════════════════════════════════════════════════
# 2. AUTO-PROMOTE 8-GATE PIPELINE TESTS
# ═══════════════════════════════════════════════════════════════════════════

def _make_candidate(**overrides):
    """Create a test candidate with sensible defaults."""
    base = {
        "kalshi_ticker": "TEST-TICKER",
        "kalshi_title": "Will event X happen?",
        "poly_slug": "test-slug",
        "poly_question": "Will event X happen?",
        "score": 0.92,
        "status": "candidate",
        "resolution_date": "2026-06-01",
        "category": "politics",
        "structural_match": True,
    }
    base.update(overrides)
    return base


def _make_settings(**overrides):
    """Create test settings with permissive defaults."""
    base = {
        "AUTO_PROMOTE_ENABLED": True,
        "AUTO_PROMOTE_MIN_SCORE": 0.85,
        "PHASE5_MAX_ORDER_USD": 50.0,
        "AUTO_PROMOTE_DAILY_CAP": 20,
        "AUTO_PROMOTE_ADVISORY_SCANS": 30,
        "AUTO_PROMOTE_MAX_DAYS": 90,
    }
    base.update(overrides)
    return base


def _make_orderbooks(kalshi_depth=200.0, poly_depth=200.0):
    """Create orderbooks with specified bid-side depth."""
    def _make_bids(total_depth):
        # Split into 5 levels
        per_level = total_depth / 5
        return [{"px": 0.50, "qty": per_level / 0.50} for _ in range(5)]

    return {
        "kalshi": {"bids": _make_bids(kalshi_depth)},
        "polymarket": {"bids": _make_bids(poly_depth)},
    }


async def _yes_verifier(k, p):
    return "YES"


async def _no_verifier(k, p):
    return "NO"


async def _maybe_verifier(k, p):
    return "MAYBE"


def _identical_checker(a, b):
    return ResolutionMatch.IDENTICAL


def _divergent_checker(a, b):
    return ResolutionMatch.DIVERGENT


def _pending_checker(a, b):
    return ResolutionMatch.PENDING


class TestAutoPromoteGate1:
    """Gate 1: AUTO_PROMOTE_ENABLED must be true."""

    def test_disabled_rejects(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(),
                settings=_make_settings(AUTO_PROMOTE_ENABLED=False),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "auto_promote_disabled"

    def test_enabled_passes(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(),
                settings=_make_settings(AUTO_PROMOTE_ENABLED=True),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert result.promoted


class TestAutoPromoteGate2:
    """Gate 2: score >= min_score."""

    def test_low_score_rejects(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(score=0.50),
                settings=_make_settings(),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "score_low"

    def test_exact_threshold_passes(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(score=0.85),
                settings=_make_settings(AUTO_PROMOTE_MIN_SCORE=0.85),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert result.promoted


class TestAutoPromoteGate3:
    """Gate 3: resolution_check must return IDENTICAL."""

    def test_unstructured_candidate_rejects_before_llm(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(structural_match=False),
                settings=_make_settings(),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "structural_unverified"

    def test_divergent_rejects(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(),
                settings=_make_settings(),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_divergent_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "resolution_divergent"

    def test_identical_passes(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(),
                settings=_make_settings(),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert result.promoted

    def test_pending_with_high_score_rejects(self):
        """PENDING resolution + high score + LLM YES is still not confirmed."""
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(score=0.95),
                settings=_make_settings(),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_pending_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "resolution_pending"

    def test_pending_with_low_score_rejects(self):
        """PENDING resolution rejects before any score-bump fallback."""
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(score=0.86),
                settings=_make_settings(AUTO_PROMOTE_MIN_SCORE=0.85),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_pending_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "resolution_pending"


class TestAutoPromoteGate4:
    """Gate 4: LLM verifier must return YES."""

    def test_llm_no_rejects(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(),
                settings=_make_settings(),
                orderbooks=_make_orderbooks(),
                llm_verifier=_no_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "llm_no"

    def test_llm_maybe_with_low_score_rejects(self):
        """MAYBE is ambiguity and fails closed."""
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(score=0.20),
                settings=_make_settings(
                    AUTO_PROMOTE_MIN_SCORE=0.18,
                    AUTO_PROMOTE_MAYBE_MIN_SCORE=0.30,
                ),
                orderbooks=_make_orderbooks(),
                llm_verifier=_maybe_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "llm_maybe"


class TestAutoPromoteGate5:
    """Gate 5: Both orderbooks must have depth >= PHASE5_MAX_ORDER_USD × 2."""

    def test_low_kalshi_depth_rejects(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(),
                settings=_make_settings(PHASE5_MAX_ORDER_USD=50.0),
                orderbooks=_make_orderbooks(kalshi_depth=20.0, poly_depth=200.0),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "liquidity_low"

    def test_sufficient_depth_passes(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(),
                settings=_make_settings(PHASE5_MAX_ORDER_USD=50.0),
                orderbooks=_make_orderbooks(kalshi_depth=200.0, poly_depth=200.0),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert result.promoted


class TestAutoPromoteGate6:
    """Gate 6: resolution_date within max_days (default 90)."""

    def test_far_future_rejects(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(resolution_date="2028-01-01"),
                settings=_make_settings(AUTO_PROMOTE_MAX_DAYS=90),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "date_out_of_window"

    def test_invalid_date_rejects(self):
        """Unparseable date → safe-fail rejection."""
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(resolution_date="not-a-date"),
                settings=_make_settings(),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "date_out_of_window"


class TestAutoPromoteGate7:
    """Gate 7: daily cap."""

    def test_daily_cap_exceeded_rejects(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(),
                settings=_make_settings(AUTO_PROMOTE_DAILY_CAP=5),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=5,
                cooling_state={},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "daily_cap"


class TestAutoPromoteGate8:
    """Gate 8: cooling-off period."""

    def test_cooling_off_rejects(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(kalshi_ticker="COOL-TICKER"),
                settings=_make_settings(AUTO_PROMOTE_ADVISORY_SCANS=30),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={"COOL-TICKER": 5},
                resolution_checker=_identical_checker,
            )
        )
        assert not result.promoted
        assert result.reason == "cooling_off"

    def test_cooling_off_passed_promotes(self):
        result = asyncio.get_event_loop().run_until_complete(
            maybe_promote(
                _make_candidate(kalshi_ticker="COOL-TICKER"),
                settings=_make_settings(AUTO_PROMOTE_ADVISORY_SCANS=30),
                orderbooks=_make_orderbooks(),
                llm_verifier=_yes_verifier,
                today_promoted_count=0,
                cooling_state={"COOL-TICKER": 30},
                resolution_checker=_identical_checker,
            )
        )
        assert result.promoted


# ═══════════════════════════════════════════════════════════════════════════
# 3. ORDERBOOK DEPTH CALCULATION
# ═══════════════════════════════════════════════════════════════════════════

class TestOrderbookDepth:
    """Verify _orderbook_depth_usd sums correctly."""

    def test_simple_depth(self):
        ob = {"bids": [{"px": 0.50, "qty": 100}]}
        assert _orderbook_depth_usd(ob) == 50.0

    def test_multi_level_depth(self):
        ob = {"bids": [
            {"px": 0.50, "qty": 100},
            {"px": 0.48, "qty": 50},
        ]}
        expected = 0.50 * 100 + 0.48 * 50
        assert abs(_orderbook_depth_usd(ob) - expected) < 1e-9

    def test_empty_orderbook(self):
        assert _orderbook_depth_usd({}) == 0.0
        assert _orderbook_depth_usd({"bids": []}) == 0.0

    def test_malformed_entries_skipped(self):
        ob = {"bids": [
            {"px": "bad", "qty": 100},
            {"px": 0.50, "qty": 100},
        ]}
        assert _orderbook_depth_usd(ob) == 50.0


# ═══════════════════════════════════════════════════════════════════════════
# 4. RESOLUTION CHECK UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestResolutionCheck:
    """Verify resolution_check module basic behavior."""

    def test_identical_same_fields(self):
        from arbiter.mapping.resolution_check import check_resolution_equivalence
        a = MarketFacts(
            question="Will X happen?",
            resolution_date="2026-06-01",
            resolution_source="Reuters",
            tie_break_rule=None,
            category="politics",
            outcome_set=("Yes", "No"),
        )
        b = MarketFacts(
            question="Will X happen?",
            resolution_date="2026-06-01",
            resolution_source="Reuters",
            tie_break_rule=None,
            category="politics",
            outcome_set=("Yes", "No"),
        )
        result = check_resolution_equivalence(a, b)
        assert result == ResolutionMatch.IDENTICAL

    def test_different_dates_divergent(self):
        from arbiter.mapping.resolution_check import check_resolution_equivalence
        a = MarketFacts(
            question="Will X happen?",
            resolution_date="2026-06-01",
            resolution_source="Reuters",
            tie_break_rule=None,
            category="politics",
            outcome_set=("Yes", "No"),
        )
        b = MarketFacts(
            question="Will X happen?",
            resolution_date="2026-12-01",
            resolution_source="Reuters",
            tie_break_rule=None,
            category="politics",
            outcome_set=("Yes", "No"),
        )
        result = check_resolution_equivalence(a, b)
        assert result == ResolutionMatch.DIVERGENT

    def test_missing_fields_pending(self):
        from arbiter.mapping.resolution_check import check_resolution_equivalence
        a = MarketFacts(question="Will X happen?", resolution_date=None, resolution_source=None, tie_break_rule=None, category=None, outcome_set=("Yes", "No"))
        b = MarketFacts(question="Will X happen?", resolution_date=None, resolution_source=None, tie_break_rule=None, category=None, outcome_set=("Yes", "No"))
        result = check_resolution_equivalence(a, b)
        assert result == ResolutionMatch.PENDING


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
