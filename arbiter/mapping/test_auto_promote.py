"""
Tests for auto_promote.py — 8-condition auto-promote gate.

TDD: tests written before implementation.
One negative-path test per condition + one happy-path test = 9 total.

Condition #5 (liquidity) MUST use arithmetic on a fake orderbook.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbiter.mapping.auto_promote import (
    PromotionResult,
    apply_promotion,
    maybe_promote,
)
from arbiter.mapping.resolution_check import MarketFacts, ResolutionMatch


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _today_iso() -> str:
    return date.today().isoformat()


def _days_from_now(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def _make_candidate(
    score: float = 0.90,
    resolution_date: str | None = None,
) -> dict:
    """Build a minimal candidate dict for testing."""
    if resolution_date is None:
        resolution_date = _days_from_now(30)
    return {
        "kalshi_ticker": "FED-MAY26",
        "kalshi_title": "Will the Federal Reserve cut rates in May 2026?",
        "poly_slug": "fed-rate-cut-may-2026",
        "poly_question": "Will the Federal Reserve cut rates in May 2026?",
        "score": score,
        "status": "candidate",
        "structural_match": True,
        "resolution_date": resolution_date,
        "kalshi_resolution_date": resolution_date,
        "polymarket_resolution_date": resolution_date,
        "kalshi_resolution_source": "Fed",
        "polymarket_resolution_source": "Federal Reserve",
        "kalshi_tie_break_rule": None,
        "polymarket_tie_break_rule": None,
        "kalshi_outcome_set": ("Yes", "No"),
        "polymarket_outcome_set": ("Yes", "No"),
    }


def _make_settings(
    auto_promote_enabled: bool = True,
    phase5_max_order_usd: float = 50.0,
    daily_cap: int = 20,
    advisory_scans: int = 30,
    min_score: float = 0.85,
    max_days: int = 90,
    **overrides,
) -> dict:
    settings = {
        "AUTO_PROMOTE_ENABLED": auto_promote_enabled,
        "PHASE5_MAX_ORDER_USD": phase5_max_order_usd,
        "AUTO_PROMOTE_DAILY_CAP": daily_cap,
        "AUTO_PROMOTE_ADVISORY_SCANS": advisory_scans,
        "AUTO_PROMOTE_MIN_SCORE": min_score,
        "AUTO_PROMOTE_MAX_DAYS": max_days,
    }
    settings.update(overrides)
    return settings


def _make_orderbooks(
    kalshi_depth_usd: float = 200.0,
    poly_depth_usd: float = 200.0,
) -> dict:
    """Construct fake orderbooks with known total depth.

    Each side returns a dict with 'bids' list summing to the given depth in USD.
    depth_usd = sum(price * qty for each bid level).
    We use a single bid at price=0.5 so depth_usd = 0.5 * qty → qty = depth_usd / 0.5.
    """
    def _book(depth_usd: float) -> dict:
        qty = depth_usd / 0.5
        return {"bids": [{"px": 0.5, "qty": qty}], "offers": []}

    return {
        "kalshi": _book(kalshi_depth_usd),
        "polymarket": _book(poly_depth_usd),
    }


def _make_llm_verifier(verdict: str = "YES"):
    async def _verify(kalshi_q, poly_q):
        return verdict
    return _verify


def _resolution_check_identical(a: MarketFacts, b: MarketFacts) -> ResolutionMatch:
    return ResolutionMatch.IDENTICAL


def _resolution_check_divergent(a: MarketFacts, b: MarketFacts) -> ResolutionMatch:
    return ResolutionMatch.DIVERGENT


def _resolution_check_pending(a: MarketFacts, b: MarketFacts) -> ResolutionMatch:
    return ResolutionMatch.PENDING


# ─── Condition 1: AUTO_PROMOTE_ENABLED ────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_promote_disabled():
    """Gate 1: AUTO_PROMOTE_ENABLED=false → reason='auto_promote_disabled'."""
    settings = _make_settings(auto_promote_enabled=False)
    candidate = _make_candidate()
    orderbooks = _make_orderbooks()
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "auto_promote_disabled"


# ─── Condition 2: score >= 0.85 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_low():
    """Gate 2: score < 0.85 → reason='score_low'."""
    settings = _make_settings()
    candidate = _make_candidate(score=0.70)  # below threshold
    orderbooks = _make_orderbooks()
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "score_low"


# ─── Condition 3: resolution_check == IDENTICAL ───────────────────────────────

@pytest.mark.asyncio
async def test_resolution_divergent():
    """Gate 3: resolution_check returns DIVERGENT → reason='resolution_divergent'."""
    settings = _make_settings()
    candidate = _make_candidate(score=0.90)
    orderbooks = _make_orderbooks()
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_divergent,
    )

    assert not result.promoted
    assert result.reason == "resolution_divergent"


@pytest.mark.asyncio
async def test_resolution_pending_cannot_auto_promote_even_with_llm_yes():
    """Gate 3: PENDING structured resolution is insufficient for live auto-trading."""
    settings = _make_settings()
    candidate = _make_candidate(score=0.99)
    orderbooks = _make_orderbooks()
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_pending,
    )

    assert not result.promoted
    assert result.reason == "resolution_pending"


# ─── Condition 4: LLM verifier == YES ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_no():
    """Gate 4: LLM returns NO → reason='llm_no'. MAYBE also counts as not-YES."""
    settings = _make_settings()
    candidate = _make_candidate(score=0.90)
    orderbooks = _make_orderbooks()
    llm = _make_llm_verifier("NO")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "llm_no"


@pytest.mark.asyncio
async def test_llm_maybe_with_low_score_rejected():
    """MAYBE from LLM is ambiguous and must reject regardless of score."""
    settings = _make_settings(min_score=0.18, AUTO_PROMOTE_MAYBE_MIN_SCORE=0.30)
    candidate = _make_candidate(score=0.20)
    orderbooks = _make_orderbooks()
    llm = _make_llm_verifier("MAYBE")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "llm_maybe"


@pytest.mark.asyncio
async def test_llm_maybe_cannot_auto_promote_even_with_high_score():
    """Gate 4: LLM MAYBE is ambiguous and must fail closed for live auto-trading."""
    settings = _make_settings(min_score=0.85, AUTO_PROMOTE_MAYBE_MIN_SCORE=0.30)
    candidate = _make_candidate(score=0.99)
    orderbooks = _make_orderbooks()
    llm = _make_llm_verifier("MAYBE")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "llm_maybe"


# ─── Condition 5: Liquidity depth ≥ PHASE5_MAX_ORDER_USD (ARITHMETIC) ────────

@pytest.mark.asyncio
async def test_liquidity_low_arithmetic():
    """Gate 5: combined bid+ask depth on either venue below PHASE5_MAX_ORDER_USD → fail.

    PHASE5_MAX_ORDER_USD = 50.0 → required depth = 50.0 USD.
    Kalshi orderbook: single bid at price=0.5, qty=40 → depth = 20.0 USD.
    20.0 < 50.0 → FAIL.
    """
    phase5_max = 50.0
    required_depth = phase5_max  # = 50.0 USD (1× now, both sides counted)

    kalshi_depth = 0.5 * 40  # = 20.0 USD
    assert kalshi_depth < required_depth, "test precondition: kalshi depth should be below threshold"

    settings = _make_settings(phase5_max_order_usd=phase5_max)
    candidate = _make_candidate(score=0.90)

    orderbooks = {
        "kalshi": {"bids": [{"px": 0.5, "qty": 40}], "offers": []},        # depth = 20.0
        "polymarket": {"bids": [{"px": 0.5, "qty": 400}], "offers": []},   # depth = 200.0
    }
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "liquidity_low"


@pytest.mark.asyncio
async def test_liquidity_passes_arithmetic():
    """Gate 5 PASS: combined depth ≥ PHASE5_MAX_ORDER_USD on both venues.

    PHASE5_MAX_ORDER_USD = 50.0 → required = 50.0 USD.
    Kalshi: price=0.5, qty=200 → depth = 100.0 USD ≥ 50.0 ✓
    Poly:   price=0.5, qty=200 → depth = 100.0 USD ≥ 50.0 ✓
    """
    phase5_max = 50.0
    required_depth = phase5_max  # = 50.0

    kalshi_depth = 0.5 * 200   # = 100.0 ≥ 50.0 ✓
    poly_depth = 0.5 * 200     # = 100.0 ≥ 50.0 ✓
    assert kalshi_depth >= required_depth
    assert poly_depth >= required_depth

    settings = _make_settings(phase5_max_order_usd=phase5_max)
    candidate = _make_candidate(score=0.90)

    orderbooks = {
        "kalshi": {"bids": [{"px": 0.5, "qty": 200}], "offers": []},
        "polymarket": {"bids": [{"px": 0.5, "qty": 200}], "offers": []},
    }
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    # Should NOT fail on liquidity — may fail on a later gate but not liquidity_low
    assert result.reason != "liquidity_low"


@pytest.mark.asyncio
async def test_liquidity_counts_asks_when_bids_thin():
    """Ask-side depth counts toward Gate 5: a market with thin bids but heavy asks passes."""
    phase5_max = 50.0
    settings = _make_settings(phase5_max_order_usd=phase5_max)
    candidate = _make_candidate(score=0.90)

    # 0 bid depth, 100 USD ask depth → 100 ≥ 50 → passes
    orderbooks = {
        "kalshi": {"bids": [], "asks": [{"px": 0.5, "qty": 200}]},
        "polymarket": {"bids": [], "asks": [{"px": 0.5, "qty": 200}]},
    }
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert result.reason != "liquidity_low"


# ─── Condition 6: resolution_date within 90 days ─────────────────────────────

@pytest.mark.asyncio
async def test_date_out_of_window():
    """Gate 6: resolution_date > 90 days from today → reason='date_out_of_window'."""
    settings = _make_settings()
    candidate = _make_candidate(score=0.90, resolution_date=_days_from_now(120))  # 120 days
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "date_out_of_window"


@pytest.mark.asyncio
async def test_past_resolution_date_rejected_as_out_of_window():
    """Gate 6: past resolution dates are expired and must not auto-promote."""
    settings = _make_settings()
    candidate = _make_candidate(score=0.90, resolution_date=_days_from_now(-1))
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "date_out_of_window"


# ─── Condition 7: daily cap ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_cap():
    """Gate 7: today_promoted_count >= AUTO_PROMOTE_DAILY_CAP → reason='daily_cap'."""
    settings = _make_settings(daily_cap=5)
    candidate = _make_candidate(score=0.90)
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=5,   # == daily_cap → exceeded
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "daily_cap"


# ─── Condition 8: cooling-off ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooling_off():
    """Gate 8: candidate has fewer than advisory_scans since promotion → reason='cooling_off'."""
    settings = _make_settings(advisory_scans=30)
    candidate = _make_candidate(score=0.90)
    candidate["kalshi_ticker"] = "FED-MAY26"
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")

    # Cooling state: 10 scans so far (< 30 advisory scans)
    cooling_state = {"FED-MAY26": 10}

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state=cooling_state,
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "cooling_off"


# ─── Happy path: all 8 gates pass ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_gates_pass_returns_promoted():
    """Happy path: all 8 gates pass → promoted=True, reason='promoted'."""
    settings = _make_settings(
        auto_promote_enabled=True,
        phase5_max_order_usd=50.0,
        daily_cap=20,
        advisory_scans=30,
    )
    candidate = _make_candidate(
        score=0.92,
        resolution_date=_days_from_now(45),  # within 90 days
    )
    candidate["kalshi_ticker"] = "FED-MAY26"

    # Sufficient liquidity: depth = 0.5 × 400 = 200.0 ≥ 50.0 × 2 = 100.0
    orderbooks = {
        "kalshi": {"bids": [{"px": 0.5, "qty": 400}], "offers": []},
        "polymarket": {"bids": [{"px": 0.5, "qty": 400}], "offers": []},
    }
    llm = _make_llm_verifier("YES")

    # No cooling-off for this candidate
    cooling_state = {}

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=5,    # under cap of 20
        cooling_state=cooling_state,
        resolution_checker=_resolution_check_identical,
    )

    assert result.promoted, f"Expected promoted=True, got reason={result.reason}"
    assert result.reason == "promoted"


@pytest.mark.asyncio
async def test_side_specific_resolution_fields_prevent_false_identical_match():
    settings = _make_settings()
    candidate = _make_candidate()
    candidate["kalshi_resolution_date"] = _days_from_now(30)
    candidate["polymarket_resolution_date"] = _days_from_now(120)
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
    )

    assert not result.promoted
    assert result.reason == "resolution_divergent"


@pytest.mark.asyncio
async def test_long_dated_markets_can_pass_with_configured_max_days():
    settings = _make_settings(max_days=400)
    candidate = _make_candidate(resolution_date=_days_from_now(280))
    candidate["kalshi_resolution_date"] = candidate["resolution_date"]
    candidate["polymarket_resolution_date"] = candidate["resolution_date"]
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert result.promoted
    assert result.reason == "promoted"


@pytest.mark.asyncio
async def test_per_category_min_score_relaxes_politics_floor():
    """Per-category floor: politics candidate with score 0.70 passes when
    the global floor is 0.85 because DEFAULT_CATEGORY_MIN_SCORE["politics"]=0.65."""
    settings = _make_settings(min_score=0.85)
    candidate = _make_candidate(score=0.70)
    candidate["category"] = "politics"
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert result.promoted, f"Expected promoted=True, got reason={result.reason}"
    assert result.reason == "promoted"


@pytest.mark.asyncio
async def test_per_category_min_score_keeps_sports_strict():
    """Per-category floor: sports candidate with score 0.80 still fails the
    0.85 sports floor, even though the global floor is 0.50."""
    settings = _make_settings(min_score=0.50)
    candidate = _make_candidate(score=0.80)
    candidate["category"] = "sports"
    candidate["polarity"] = "same"
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "score_low"


@pytest.mark.asyncio
async def test_per_category_settings_override_default_table():
    """Operator can override per-category floor via settings dict."""
    settings = _make_settings(min_score=0.85)
    settings["AUTO_PROMOTE_MIN_SCORE_BY_CATEGORY"] = {"crypto": 0.90}
    candidate = _make_candidate(score=0.70)
    candidate["category"] = "crypto"  # default would be 0.65; override is 0.90
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")

    result = await maybe_promote(
        candidate,
        settings=settings,
        orderbooks=orderbooks,
        llm_verifier=llm,
        today_promoted_count=0,
        cooling_state={},
        resolution_checker=_resolution_check_identical,
    )

    assert not result.promoted
    assert result.reason == "score_low"


# ════════════════════════════════════════════════════════════════════════
# PROMOTION SAFETY CAGE — semantic-only candidates must NEVER reach
# confirmed+allow_auto_trade=True. Every regression fixture below mirrors
# a real-world failure pattern surfaced by past mapping audits.
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_structural_promote_carries_structural_match_type():
    """Sanity: structural path → match_type='structural' so the caller
    can route to confirmed+allow_auto_trade=True."""
    settings = _make_settings()
    candidate = _make_candidate(score=0.92)
    candidate["structural_match"] = True
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")
    result = await maybe_promote(
        candidate, settings=settings, orderbooks=orderbooks,
        llm_verifier=llm, today_promoted_count=0, cooling_state={},
        resolution_checker=_resolution_check_identical,
    )
    assert result.promoted and result.reason == "promoted"
    assert result.match_type == "structural"
    assert result.is_structural and not result.is_semantic


@pytest.mark.asyncio
async def test_semantic_only_promote_carries_semantic_match_type():
    """When structural_match is False and only the score>=0.92 bypass
    fires, the result must announce match_type='semantic' so the caller
    can apply the safety cage."""
    settings = _make_settings()
    candidate = _make_candidate(score=0.93)
    candidate["structural_match"] = False     # NO structural parser
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")
    result = await maybe_promote(
        candidate, settings=settings, orderbooks=orderbooks,
        llm_verifier=llm, today_promoted_count=0, cooling_state={},
        resolution_checker=_resolution_check_identical,
    )
    assert result.promoted
    assert result.match_type == "semantic"
    assert result.is_semantic and not result.is_structural


def test_apply_promotion_cage_structural_path_is_live_tradable():
    candidate = {"kalshi_ticker": "X"}
    apply_promotion(
        candidate,
        PromotionResult(promoted=True, reason="promoted", match_type="structural"),
    )
    assert candidate["status"] == "confirmed"
    assert candidate["allow_auto_trade"] is True
    assert candidate["promotion_match_type"] == "structural"


def test_apply_promotion_cage_semantic_path_routes_to_review_not_live():
    """SAFETY CAGE: semantic-only never becomes live-tradable."""
    candidate = {"kalshi_ticker": "X"}
    apply_promotion(
        candidate,
        PromotionResult(promoted=True, reason="promoted", match_type="semantic"),
    )
    assert candidate["status"] == "review", (
        "Semantic-only promote MUST land at review, not confirmed"
    )
    assert candidate["allow_auto_trade"] is False, (
        "Semantic-only promote MUST NOT set allow_auto_trade=True"
    )
    assert candidate["promotion_match_type"] == "semantic"
    assert "operator" in candidate["review_note"].lower()


def test_apply_promotion_cage_rejection_does_not_flip_status():
    """When the gate rejects (promoted=False), status must not flip."""
    candidate = {"status": "candidate", "kalshi_ticker": "X"}
    apply_promotion(
        candidate, PromotionResult(promoted=False, reason="llm_no"),
    )
    assert candidate["status"] == "candidate"
    assert candidate["allow_auto_trade"] is False
    assert "llm_no" in candidate["review_note"]


# ─── Known-bad regression fixtures ────────────────────────────────────
# Each constructs a candidate that historically slipped past Gate 3
# semantic similarity (>= 0.92) and should now ALWAYS land at review,
# not confirmed+live-tradable.

async def _cage_check(candidate, *, score=0.94):
    """Build a settings/orderbook/llm trio so the ONLY differentiator
    is structural_match=False → semantic-only path. Confirms the cage
    keeps the candidate out of confirmed+live-tradable."""
    settings = _make_settings()
    candidate.setdefault("score", score)
    candidate["structural_match"] = False
    orderbooks = _make_orderbooks(200.0, 200.0)
    llm = _make_llm_verifier("YES")
    result = await maybe_promote(
        candidate, settings=settings, orderbooks=orderbooks,
        llm_verifier=llm, today_promoted_count=0, cooling_state={},
        resolution_checker=_resolution_check_identical,
    )
    apply_promotion(candidate, result)
    return candidate, result


@pytest.mark.asyncio
async def test_known_bad_same_text_different_date_caged():
    """Two markets with identical text — semantic 1.0 — but the
    structural parser would catch a date mismatch. Without parser
    fire → semantic-only → MUST cage."""
    candidate = _make_candidate(score=0.99)
    candidate["kalshi_title"] = "Will BTC reach $100k?"
    candidate["poly_question"] = "Will BTC reach $100k?"
    candidate, _ = await _cage_check(candidate)
    assert candidate["status"] != "confirmed"
    assert candidate["allow_auto_trade"] is False


@pytest.mark.asyncio
async def test_known_bad_winner_vs_spread_caged():
    """Kalshi 'will X win' vs Poly 'will X cover the spread'. Same
    team name dominates tokens; mechanics differ. Without structural
    parser fire → MUST cage."""
    candidate = _make_candidate(score=0.94)
    candidate["kalshi_title"] = "Will Philadelphia Eagles win against Dallas Cowboys?"
    candidate["poly_question"] = "Will Philadelphia Eagles cover the spread against Dallas Cowboys?"
    candidate, _ = await _cage_check(candidate)
    assert candidate["status"] != "confirmed"


@pytest.mark.asyncio
async def test_known_bad_flipped_polarity_caged():
    """Semantic-only sports candidate must STILL cage even if the
    sports-specific polarity gate doesn't fire on a non-sports category."""
    candidate = _make_candidate(score=0.94)
    candidate["category"] = "midterms"     # polarity gate skipped (sports-only)
    candidate["polarity"] = "flipped"
    candidate, _ = await _cage_check(candidate)
    assert candidate["status"] != "confirmed"


@pytest.mark.asyncio
async def test_known_bad_parent_forecastex_conid_caged():
    """A semantic-only candidate pointing at a parent FORECASTX conid
    (no live bid/ask) must NOT become live-tradable."""
    candidate = _make_candidate(score=0.93)
    candidate["forecastex_contract_id"] = "745923952"  # SENM parent
    candidate["forecastex_is_parent"] = True
    candidate, _ = await _cage_check(candidate)
    assert candidate["status"] != "confirmed"


@pytest.mark.asyncio
async def test_known_bad_different_threshold_caged():
    """$100k vs $110k threshold — semantic high but numeric meaning differs."""
    candidate = _make_candidate(score=0.95)
    candidate["kalshi_title"] = "Will Bitcoin reach $100,000 by Dec 31?"
    candidate["poly_question"] = "Will Bitcoin reach $110,000 by Dec 31?"
    candidate, _ = await _cage_check(candidate)
    assert candidate["status"] != "confirmed"


@pytest.mark.asyncio
async def test_known_bad_different_settlement_source_caged():
    """Different oracles (BLS vs Investing.com) — same question text
    can settle to different outcomes hours apart."""
    candidate = _make_candidate(score=0.94)
    candidate["kalshi_resolution_source"] = "BLS"
    candidate["polymarket_resolution_source"] = "Investing.com"
    candidate, _ = await _cage_check(candidate)
    assert candidate["status"] != "confirmed"


@pytest.mark.asyncio
async def test_known_bad_sports_geography_false_positive_caged():
    """'New York Yankees' vs 'New York Governor' shares 'New York' —
    geographic overlap drives semantic score high; must cage."""
    candidate = _make_candidate(score=0.93)
    candidate["kalshi_title"] = "Will the New York Yankees win the World Series?"
    candidate["poly_question"] = "Will the New York Governor be a Democrat in 2027?"
    candidate, _ = await _cage_check(candidate)
    assert candidate["status"] != "confirmed"


@pytest.mark.asyncio
async def test_known_bad_multi_leg_vs_binary_caged():
    """Multi-outcome (5 candidates) vs binary YES/NO. The arb math
    doesn't work for non-binary; must cage."""
    candidate = _make_candidate(score=0.94)
    candidate["kalshi_outcome_set"] = ("Trump", "Vance", "Haley", "DeSantis", "Cheney")
    candidate["polymarket_outcome_set"] = ("Yes", "No")
    candidate, _ = await _cage_check(candidate)
    assert candidate["status"] != "confirmed"


@pytest.mark.asyncio
async def test_known_bad_same_event_different_market_type_caged():
    """Kalshi 'win total' (number) vs Poly 'make playoffs' (binary) —
    same team / season, different mechanics."""
    candidate = _make_candidate(score=0.94)
    candidate["kalshi_title"] = "Will the Dallas Cowboys win 10+ games in 2026?"
    candidate["poly_question"] = "Will the Dallas Cowboys make the 2026 NFL playoffs?"
    candidate, _ = await _cage_check(candidate)
    assert candidate["status"] != "confirmed"


# ─── BUG #4 fast-path tests ─────────────────────────────────────────────────


def _make_fast_mapping(**overrides):
    """Build a MarketMapping for evaluate_structural_promotion tests.

    Defaults satisfy every fast-path criterion so each test only has to
    override the field it wants to flunk.
    """
    from arbiter.mapping.market_map import MappingStatus, MarketMapping

    base = dict(
        canonical_id="FED-MAY26",
        description="Fed May 2026",
        status=MappingStatus.CANDIDATE,
        allow_auto_trade=False,
        kalshi_market_id="KXFEDDECISION-26MAY",
        polymarket_slug="fed-rate-cut-may-2026",
        mapping_score=0.96,
        resolution_match_status="identical",
        tags=(),
    )
    base.update(overrides)
    return MarketMapping(**base)


def test_fast_promote_evaluates_high_score_structural_match():
    from arbiter.mapping.auto_promote import (
        FAST_PROMOTE_PROMOTED, evaluate_structural_promotion,
    )

    m = _make_fast_mapping()
    assert evaluate_structural_promotion(m) == FAST_PROMOTE_PROMOTED


def test_fast_promote_skips_when_score_below_threshold():
    from arbiter.mapping.auto_promote import (
        FAST_PROMOTE_SKIP_SCORE, evaluate_structural_promotion,
    )

    m = _make_fast_mapping(mapping_score=0.90)
    assert evaluate_structural_promotion(m) == FAST_PROMOTE_SKIP_SCORE


def test_fast_promote_skips_when_resolution_not_identical():
    from arbiter.mapping.auto_promote import (
        FAST_PROMOTE_SKIP_RESOLUTION, evaluate_structural_promotion,
    )

    m = _make_fast_mapping(resolution_match_status="pending_operator_review")
    assert evaluate_structural_promotion(m) == FAST_PROMOTE_SKIP_RESOLUTION


def test_fast_promote_skips_when_kalshi_leg_missing():
    from arbiter.mapping.auto_promote import (
        FAST_PROMOTE_SKIP_MISSING_LEG, evaluate_structural_promotion,
    )

    m = _make_fast_mapping(kalshi_market_id="")
    assert evaluate_structural_promotion(m) == FAST_PROMOTE_SKIP_MISSING_LEG


def test_fast_promote_skips_when_polymarket_leg_missing():
    from arbiter.mapping.auto_promote import (
        FAST_PROMOTE_SKIP_MISSING_LEG, evaluate_structural_promotion,
    )

    m = _make_fast_mapping(polymarket_slug="")
    assert evaluate_structural_promotion(m) == FAST_PROMOTE_SKIP_MISSING_LEG


def test_fast_promote_defers_sports_to_slow_path():
    """Sports markets must go through the slow-path safety stack so
    polarity + settlement-date checks fire. The fast-path is too lenient
    for them."""
    from arbiter.mapping.auto_promote import (
        FAST_PROMOTE_SKIP_RESOLUTION, evaluate_structural_promotion,
    )

    m = _make_fast_mapping(tags=("sports",))
    assert evaluate_structural_promotion(m) == FAST_PROMOTE_SKIP_RESOLUTION


@pytest.mark.asyncio
async def test_structural_fast_promote_calls_update_status_for_each_passing_row():
    from arbiter.mapping.auto_promote import structural_fast_promote
    from arbiter.mapping.market_map import MappingStatus

    pass_a = _make_fast_mapping(canonical_id="FED-A", mapping_score=0.97)
    pass_b = _make_fast_mapping(canonical_id="FED-B", mapping_score=0.96)
    fail_score = _make_fast_mapping(canonical_id="FED-C", mapping_score=0.80)
    store = MagicMock()
    # store.all(status="candidate") returns the three rows;
    # store.all(status="review") returns nothing.
    async def _all(*, status, limit):
        if status == "candidate":
            return [pass_a, pass_b, fail_score]
        return []
    store.all = AsyncMock(side_effect=_all)
    store.update_status = AsyncMock(return_value=None)

    n = await structural_fast_promote(store, min_score=0.95)

    assert n == 2
    assert store.update_status.await_count == 2
    promoted_ids = sorted(
        call.kwargs.get("canonical_id") for call in store.update_status.await_args_list
    )
    assert promoted_ids == ["FED-A", "FED-B"]
    for call in store.update_status.await_args_list:
        assert call.kwargs.get("status") == MappingStatus.CONFIRMED
        assert call.kwargs.get("allow_auto_trade") is True


def test_apply_promotion_respects_no_auto_promote_marker():
    """A [no-auto-promote] quarantine (coherence sweep / operator) must be
    terminal for ALL promotion paths. Live 2026-07-10 22:10-22:23Z: the
    coherence sweep demoted GOP_HOUSE_2026 (0.305 cross-venue divergence)
    and THIS pipeline re-confirmed it minutes later, wiping the marker via
    review_note='' on the structural path."""
    candidate = {
        "canonical_id": "GOP_HOUSE_2026",
        "status": "review",
        "allow_auto_trade": False,
        "review_note": "[coherence-quarantine][no-auto-promote] cross-venue yes divergence 0.3050",
    }
    apply_promotion(
        candidate,
        PromotionResult(promoted=True, reason="promoted", match_type="structural"),
    )
    assert candidate["status"] == "review"
    assert candidate["allow_auto_trade"] is False
    assert "[no-auto-promote]" in candidate["review_note"]


@pytest.mark.asyncio
async def test_structural_fast_promote_skips_quarantined_marker():
    """structural_fast_promote is a THIRD promotion path (besides
    apply_promotion and auto_promote_validated). It must also honor the
    [no-auto-promote] quarantine marker. Live 2026-07-10 23:18Z: it
    re-confirmed GOP_HOUSE_2026 (party-swap quarantine) and OVERWROTE the
    marker via review_note, resurrecting auto-trade on a bad mapping."""
    from arbiter.mapping.auto_promote import structural_fast_promote

    from arbiter.mapping.market_map import MappingStatus as _MS
    clean = _make_fast_mapping(canonical_id="FED-CLEAN", mapping_score=0.97,
                               status=_MS.REVIEW)
    quarantined = _make_fast_mapping(
        canonical_id="GOP_HOUSE_2026", mapping_score=1.0, status=_MS.REVIEW,
        review_note="[coherence-quarantine][no-auto-promote] divergence 0.3050",
    )
    store = MagicMock()

    async def _all(*, status, limit):
        if status == "review":
            return [clean, quarantined]
        return []

    store.all = AsyncMock(side_effect=_all)
    store.update_status = AsyncMock(return_value=None)

    n = await structural_fast_promote(store, min_score=0.95)

    promoted_ids = [c.kwargs.get("canonical_id") for c in store.update_status.await_args_list]
    assert "GOP_HOUSE_2026" not in promoted_ids
    assert promoted_ids == ["FED-CLEAN"]
    assert n == 1


@pytest.mark.asyncio
async def test_structural_fast_promote_skips_shared_fx_conid():
    """structural_fast_promote must refuse to auto-trade a mapping whose
    ForecastEx conid is shared by another confirmed mapping (party-swap
    signature). Live 2026-07-11: POL_US_SENATE DEM/REP shared conid
    745923952 and this path re-promoted both to auto-trade 42x/day with no
    coherence gate."""
    from arbiter.mapping.auto_promote import structural_fast_promote

    clean = _make_fast_mapping(canonical_id="FED-CLEAN", mapping_score=0.97,
                               forecastex_contract_id="111")
    from arbiter.mapping.market_map import MappingStatus as _MS
    shared = _make_fast_mapping(canonical_id="POL_US_SENATE_DEM", mapping_score=1.0,
                                status=_MS.REVIEW, forecastex_contract_id="745923952")
    store = MagicMock()

    async def _all(*, status, limit):
        return [clean, shared] if status == "review" else []
    store.all = AsyncMock(side_effect=_all)
    store.update_status = AsyncMock(return_value=None)
    # conid 745923952 owned by 2 confirmed mappings; 111 unique.
    store.fx_conid_owner_counts = AsyncMock(return_value={"745923952": 2, "111": 1})

    promoted = await structural_fast_promote(store, min_score=0.95)

    ids = [c.kwargs.get("canonical_id") for c in store.update_status.await_args_list]
    assert "POL_US_SENATE_DEM" not in ids
    assert ids == ["FED-CLEAN"]
    assert promoted == 1
