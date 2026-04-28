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

from arbiter.mapping.auto_promote import PromotionResult, maybe_promote
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
) -> dict:
    return {
        "AUTO_PROMOTE_ENABLED": auto_promote_enabled,
        "PHASE5_MAX_ORDER_USD": phase5_max_order_usd,
        "AUTO_PROMOTE_DAILY_CAP": daily_cap,
        "AUTO_PROMOTE_ADVISORY_SCANS": advisory_scans,
        "AUTO_PROMOTE_MIN_SCORE": min_score,
        "AUTO_PROMOTE_MAX_DAYS": max_days,
    }


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
async def test_llm_maybe_is_not_yes():
    """MAYBE from LLM is also treated as not-YES → reason='llm_no'."""
    settings = _make_settings()
    candidate = _make_candidate(score=0.90)
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
    assert result.reason == "llm_no"


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
