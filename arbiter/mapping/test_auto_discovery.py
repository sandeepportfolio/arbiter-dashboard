"""
Tests for auto_discovery.py — auto-discovery pipeline.

TDD: tests written before implementation.
All tests use mocked clients to avoid network calls.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbiter.mapping.auto_discovery import discover


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_kalshi_client(markets: list[dict]):
    """Mock Kalshi client with list_all_markets() coroutine."""
    client = MagicMock()
    client.list_all_markets = AsyncMock(return_value=markets)
    client.get_orderbook = AsyncMock(return_value={"orderbook": {"yes": [[55, 500]], "no": []}})
    return client


def _make_poly_client(markets: list[dict]):
    """Mock Polymarket US client with list_markets() async generator."""
    async def _gen():
        for m in markets:
            yield m

    client = MagicMock()
    client.list_markets = MagicMock(return_value=_gen())
    client.get_orderbook = AsyncMock(return_value={"bids": [{"px": 0.55, "qty": 500}]})
    return client


def _make_event_capable_kalshi_client(events: list[dict], markets_by_event: dict[str, list[dict]]):
    client = MagicMock()
    client.list_all_events = AsyncMock(return_value=events)
    client.list_markets_for_event = AsyncMock(side_effect=lambda event_ticker, limit=50: markets_by_event.get(event_ticker, []))
    return client


def _make_store():
    """Mock mapping store that records written candidates."""
    store = MagicMock()
    store.written = []

    async def _write(candidates):
        store.written.extend(candidates)
        return len(candidates)

    store.write_candidates = _write
    return store


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pulls_both_platforms():
    """discover() must call both the Kalshi and Polymarket clients."""
    kalshi = _make_kalshi_client([
        {"ticker": "FED-2026-Y", "title": "Fed cuts rates in May 2026", "status": "open"},
    ])
    poly = _make_poly_client([
        {"slug": "fed-rate-cut-may-2026", "question": "Fed rate cut May 2026?"},
    ])
    store = _make_store()

    await discover(kalshi, poly, store, budget_rps=100.0)

    kalshi.list_all_markets.assert_called_once_with()
    # poly.list_markets was called (it returns an async generator)
    poly.list_markets.assert_called()


@pytest.mark.asyncio
async def test_writes_candidates_with_score():
    """discover() must write candidates with status='candidate' and a score."""
    kalshi = _make_kalshi_client([
        {
            "ticker": "FED-MAY26",
            "title": "Will the Federal Reserve cut rates in May 2026?",
            "status": "open",
        },
    ])
    poly = _make_poly_client([
        {
            "slug": "fed-rate-cut-may-2026",
            "question": "Will the Federal Reserve cut rates in May 2026?",
        },
    ])
    store = _make_store()

    count = await discover(kalshi, poly, store, budget_rps=100.0)

    assert count > 0, "Expected at least one candidate to be written"
    assert len(store.written) > 0, "Expected candidates to be written to store"
    for candidate in store.written:
        assert candidate.get("status") == "candidate", (
            f"Candidate status should be 'candidate', got {candidate.get('status')}"
        )
        assert "score" in candidate, "Candidate must have a 'score' field"
        assert isinstance(candidate["score"], float), "Score must be a float"


@pytest.mark.asyncio
async def test_high_score_pairs_surfaced_first():
    """Candidates should be sorted by score descending."""
    # Create markets where one pair has high overlap and one has low overlap
    kalshi = _make_kalshi_client([
        {
            "ticker": "FED-MAY26",
            "title": "Will the Federal Reserve cut rates in May 2026?",
            "status": "open",
        },
        {
            "ticker": "CRYPTO-X",
            "title": "Blockchain digital asset protocol governance",
            "status": "open",
        },
    ])
    poly = _make_poly_client([
        {
            "slug": "fed-rate-cut-may-2026",
            "question": "Will the Federal Reserve cut rates in May 2026?",
        },
        {
            "slug": "some-random-question",
            "question": "Will the baseball team win the championship?",
        },
    ])
    store = _make_store()

    await discover(kalshi, poly, store, budget_rps=100.0)

    if len(store.written) >= 2:
        scores = [c["score"] for c in store.written]
        assert scores == sorted(scores, reverse=True), (
            "Candidates must be sorted by score descending"
        )


@pytest.mark.asyncio
async def test_rate_limit_budget_respected():
    """With budget_rps=2.0, discovery of 10 Kalshi markets should take ≥ expected time."""
    n_kalshi = 10
    n_poly = 1

    kalshi = _make_kalshi_client([
        {"ticker": f"MKT-{i}", "title": f"Market question number {i}", "status": "open"}
        for i in range(n_kalshi)
    ])
    poly = _make_poly_client([
        {"slug": f"poly-market-{i}", "question": f"Poly market question {i}"}
        for i in range(n_poly)
    ])
    store = _make_store()

    # At 2 rps, fetching from 2 platforms takes 2 calls / 2 rps = ≥ 1s
    budget_rps = 2.0
    start = time.monotonic()
    await discover(kalshi, poly, store, budget_rps=budget_rps)
    elapsed = time.monotonic() - start

    # We made 2 API calls (one per platform) at 2 rps → expect ≥ (2/2 - 0.1)s = 0.9s
    expected_min = (2 / budget_rps) - 0.2  # allow 200ms tolerance
    assert elapsed >= expected_min, (
        f"Rate limit not respected: elapsed={elapsed:.3f}s, expected >= {expected_min:.3f}s"
    )


@pytest.mark.asyncio
async def test_returns_zero_when_no_markets():
    """Empty markets on either platform should return 0 candidates."""
    kalshi = _make_kalshi_client([])
    poly = _make_poly_client([])
    store = _make_store()

    count = await discover(kalshi, poly, store, budget_rps=100.0)

    assert count == 0


@pytest.mark.asyncio
async def test_event_discovery_surfaces_long_dated_contracts_not_seen_in_market_paging():
    kalshi = _make_event_capable_kalshi_client(
        [
            {
                "event_ticker": "CONTROLH-2026",
                "title": "Which party will win the U.S. House?",
                "sub_title": "In 2026",
                "category": "Elections",
            },
        ],
        {
            "CONTROLH-2026": [
                {
                    "ticker": "CONTROLH-2026-D",
                    "title": "Will Democrats win the House in 2026?",
                    "close_time": "2027-02-01T15:00:00Z",
                    "status": "active",
                },
                {
                    "ticker": "CONTROLH-2026-R",
                    "title": "Will Republicans win the House in 2026?",
                    "close_time": "2027-02-01T15:00:00Z",
                    "status": "active",
                },
            ]
        },
    )
    poly = _make_poly_client([
        {
            "slug": "paccc-usho-midterms-2026-11-03-dem",
            "question": "Will the Democratic Party win the House in the 2026 Midterms?",
            "description": "Will the Democratic Party win the House in the 2026 Midterms?",
            "category": "politics",
            "endDate": "2027-02-01T23:59:00Z",
            "subject": {"name": "Democratic Party"},
        },
    ])
    store = _make_store()

    count = await discover(kalshi, poly, store, budget_rps=100.0, min_score=0.2)

    assert count == 1
    assert store.written[0]["kalshi_ticker"] == "CONTROLH-2026-D"
    kalshi.list_all_events.assert_called_once()
    kalshi.list_markets_for_event.assert_called_once_with("CONTROLH-2026", limit=50)


@pytest.mark.asyncio
async def test_keeps_only_best_candidate_per_kalshi_market():
    """Discovery should emit the top-scoring Polymarket candidate per Kalshi market.

    This keeps the review queue stable and analyzable instead of flooding it
    with multiple near-duplicate matches for the same executable market.
    """
    kalshi = _make_kalshi_client([
        {
            "ticker": "FED-MAY26",
            "title": "Will the Federal Reserve cut rates in May 2026?",
            "status": "open",
        },
    ])
    poly = _make_poly_client([
        {
            "slug": "fed-rate-cut-may-2026",
            "question": "Will the Federal Reserve cut rates in May 2026?",
        },
        {
            "slug": "fed-policy-may-2026",
            "question": "Will the Fed change policy in May 2026?",
        },
    ])
    store = _make_store()

    count = await discover(kalshi, poly, store, budget_rps=100.0, min_score=0.1)

    assert count == 1
    assert len(store.written) == 1
    assert store.written[0]["poly_slug"] == "fed-rate-cut-may-2026"


@pytest.mark.asyncio
async def test_prefers_date_aligned_unique_pair_for_noisy_sports_candidates():
    """Discovery should prefer the date-aligned sports market and keep the slug unique."""
    kalshi = _make_kalshi_client([
        {
            "ticker": "NFL-GOOD",
            "title": "Los Angeles vs. San Francisco",
            "category": "sports",
            "close_time": "2025-11-09T20:00:00Z",
            "status": "open",
        },
        {
            "ticker": "NFL-BAD",
            "title": "Los Angeles vs. San Francisco",
            "category": "sports",
            "close_time": "2025-12-09T20:00:00Z",
            "status": "open",
        },
    ])
    poly = _make_poly_client([
        {
            "slug": "aec-nfl-lar-sf-2025-11-09",
            "question": "Los Angeles vs. San Francisco",
            "category": "sports",
            "endDate": "2025-11-09",
        },
    ])
    store = _make_store()

    count = await discover(kalshi, poly, store, budget_rps=100.0, min_score=0.2)

    assert count == 1
    assert len(store.written) == 1
    assert store.written[0]["kalshi_ticker"] == "NFL-GOOD"
    assert store.written[0]["poly_slug"] == "aec-nfl-lar-sf-2025-11-09"


@pytest.mark.asyncio
async def test_discover_auto_promotes_when_enabled_and_candidate_passes_gates():
    kalshi = _make_kalshi_client([
        {
            "ticker": "CONTROLH-2026-D",
            "title": "Will Democrats win the House in 2026?",
            "category": "Elections",
            "close_time": "2027-02-01T15:00:00Z",
            "settlement_source": "AP",
            "status": "open",
            "yes_bid": 55,
            "yes_bid_size_fp": 500,
        },
    ])
    poly = _make_poly_client([
        {
            "slug": "paccc-usho-midterms-2026-11-03-dem",
            "question": "Will Democrats win the House in 2026?",
            "description": "Will Democrats win the House in 2026?",
            "category": "politics",
            "endDate": "2027-02-01T23:59:00Z",
            "resolutionSource": "AP",
            "outcomes": ["Yes", "No"],
        },
    ])
    store = _make_store()

    with patch("arbiter.mapping.llm_verifier.verify", new=AsyncMock(return_value="YES")):
        count = await discover(
            kalshi,
            poly,
            store,
            budget_rps=100.0,
            min_score=0.2,
            promotion_settings={
                "auto_promote_enabled": True,
                "auto_promote_min_score": 0.75,
                "auto_promote_daily_cap": 25,
                "auto_promote_advisory_scans": 0,
                "auto_promote_max_days": 400,
                "phase5_max_order_usd": 10,
            },
        )

    assert count == 1
    assert store.written[0]["status"] == "confirmed"
    assert store.written[0]["allow_auto_trade"] is True
    assert store.written[0]["resolution_match_status"] == "identical"
