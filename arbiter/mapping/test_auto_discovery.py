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
    return client


def _make_poly_client(markets: list[dict]):
    """Mock Polymarket US client with list_markets() async generator."""
    async def _gen():
        for m in markets:
            yield m

    client = MagicMock()
    client.list_markets = MagicMock(return_value=_gen())
    return client


def _make_store():
    """Mock mapping store that records written candidates."""
    store = MagicMock()
    store.written = []

    async def _write(candidates):
        store.written.extend(candidates)

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

    kalshi.list_all_markets.assert_called_once()
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
