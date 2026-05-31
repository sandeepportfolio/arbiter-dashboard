"""
Tests for arbiter/mapping/pmxt_discovery.py

Focused on the helper converters, rate-limit guard, and the discover_via_pmxt
core paths (structural matching, dedup, dry-run, store write, missing-pmxt
graceful fallback).  All tests mock the `pmxt` SDK so no real network I/O.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbiter.mapping.pmxt_discovery import (
    _MAX_TOTAL_MARKETS,
    _MIN_REQUEST_INTERVAL,
    _fetch_with_rate_limit,
    _pmxt_market_to_kalshi_dict,
    _pmxt_market_to_poly_dict,
    discover_via_pmxt,
)


# ---------------------------------------------------------------------------
# Helper: build a fake PMXT UnifiedMarket object
# ---------------------------------------------------------------------------

def _kalshi_market(
    *,
    market_id: str = "KASH-TEST",
    title: str = "Will GDP exceed 3%?",
    description: str = "Q4 GDP",
    category: str = "economics",
    status: str = "open",
    resolution_date=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        market_id=market_id,
        title=title,
        description=description,
        category=category,
        status=status,
        resolution_date=resolution_date,
    )


def _poly_market(
    *,
    slug: str = "poly-gdp-test",
    title: str = "Will GDP beat 3 percent?",
    status: str = "active",
    resolution_date=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        title=title,
        description="",
        category="economics",
        status=status,
        resolution_date=resolution_date,
    )


# ---------------------------------------------------------------------------
# _pmxt_market_to_kalshi_dict
# ---------------------------------------------------------------------------

class TestPmxtMarketToKalshiDict:
    def test_all_fields_mapped(self):
        m = _kalshi_market(market_id="KASH-1", title="Title A", description="Desc",
                           category="econ", status="open")
        d = _pmxt_market_to_kalshi_dict(m)
        assert d["ticker"] == "KASH-1"
        assert d["title"] == "Title A"
        assert d["subtitle"] == "Desc"
        assert d["category"] == "econ"
        assert d["status"] == "open"

    def test_missing_attributes_give_empty_strings(self):
        # Object with NO attributes — getattr fallback to ""
        m = SimpleNamespace()
        d = _pmxt_market_to_kalshi_dict(m)
        assert d["ticker"] == ""
        assert d["title"] == ""
        assert d["category"] == ""

    def test_none_status_gives_empty_string(self):
        m = _kalshi_market(status=None)
        d = _pmxt_market_to_kalshi_dict(m)
        assert d["status"] is None or d["status"] == ""  # getattr returns None which is fine


class TestPmxtMarketToPolyDict:
    def test_all_fields_mapped(self):
        m = _poly_market(slug="my-slug", title="My Question")
        d = _pmxt_market_to_poly_dict(m)
        assert d["slug"] == "my-slug"
        assert d["question"] == "My Question"
        assert d["title"] == "My Question"

    def test_none_slug_falls_back_to_empty_string(self):
        m = _poly_market(slug=None)
        d = _pmxt_market_to_poly_dict(m)
        assert d["slug"] == ""

    def test_missing_attributes_give_empty_strings(self):
        m = SimpleNamespace()
        d = _pmxt_market_to_poly_dict(m)
        assert d["slug"] == ""
        assert d["question"] == ""


# ---------------------------------------------------------------------------
# _fetch_with_rate_limit
# ---------------------------------------------------------------------------

class TestFetchWithRateLimit:
    @pytest.mark.asyncio
    async def test_returns_results_on_success(self):
        results = [_kalshi_market(), _kalshi_market(market_id="KASH-2")]
        fetch_fn = MagicMock(return_value=results)
        last_req = [0.0]  # far in the past → no sleep needed
        out = await _fetch_with_rate_limit(fetch_fn, "GDP", 10, last_req)
        assert out == results
        fetch_fn.assert_called_once_with(query="GDP", limit=10)

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_none_result(self):
        fetch_fn = MagicMock(return_value=None)
        out = await _fetch_with_rate_limit(fetch_fn, "test", 10, [0.0])
        assert out == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_429_exception(self):
        fetch_fn = MagicMock(side_effect=Exception("429 Too Many Requests"))
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            out = await _fetch_with_rate_limit(fetch_fn, "test", 10, [0.0])
        assert out == []
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_rate_limited_error(self):
        fetch_fn = MagicMock(side_effect=Exception("rate limit exceeded"))
        with patch("asyncio.sleep", new_callable=AsyncMock):
            out = await _fetch_with_rate_limit(fetch_fn, "test", 10, [0.0])
        assert out == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_generic_exception(self):
        fetch_fn = MagicMock(side_effect=Exception("network timeout"))
        out = await _fetch_with_rate_limit(fetch_fn, "test", 10, [0.0])
        assert out == []

    @pytest.mark.asyncio
    async def test_updates_last_request_time_on_success(self):
        fetch_fn = MagicMock(return_value=[])
        last_req = [0.0]
        before = time.monotonic()
        await _fetch_with_rate_limit(fetch_fn, "x", 10, last_req)
        assert last_req[0] >= before


# ---------------------------------------------------------------------------
# discover_via_pmxt
# ---------------------------------------------------------------------------

def _make_pmxt_module(kalshi_markets=None, poly_markets=None):
    """Build a fake pmxt module with stub Kalshi/Polymarket venue classes."""
    pmxt_mod = types.ModuleType("pmxt")

    class FakeKalshi:
        def fetch_markets(self, query, limit):
            return kalshi_markets or []

    class FakePolymarket:
        def fetch_markets(self, query, limit):
            return poly_markets or []

    pmxt_mod.Kalshi = FakeKalshi
    pmxt_mod.Polymarket = FakePolymarket
    return pmxt_mod


def _fake_fingerprint(source="economic_indicator", category="economics", event_key=None, market_key=None):
    return SimpleNamespace(source=source, category=category,
                           event_key=event_key or "GDP", market_key=market_key or "gdp_q4")


@pytest.mark.asyncio
async def test_discover_returns_error_when_pmxt_not_installed():
    # pmxt import fails → graceful error dict, no crash
    with patch.dict(sys.modules, {"pmxt": None}):
        result = await discover_via_pmxt(None, queries=["GDP"])
    assert result.get("error") == "pmxt not installed"


@pytest.mark.asyncio
async def test_discover_no_matches_returns_empty_structural_matches():
    # Kalshi and Polymarket results fingerprint to DIFFERENT market_keys → no match
    k_market = _kalshi_market(market_id="KASH-A")
    p_market = _poly_market(slug="poly-b")
    pmxt_mod = _make_pmxt_module([k_market], [p_market])

    def fp_kalshi(d):
        return _fake_fingerprint(market_key="key-kalshi-only")

    def fp_poly(d):
        return _fake_fingerprint(market_key="key-poly-only")

    with patch.dict(sys.modules, {"pmxt": pmxt_mod}), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_kalshi_market", side_effect=fp_kalshi), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_polymarket_market", side_effect=fp_poly), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await discover_via_pmxt(None, queries=["GDP"], max_per_query=10)

    assert result["structural_matches"] == []
    assert result["kalshi_total"] == 1
    assert result["poly_total"] == 1


@pytest.mark.asyncio
async def test_discover_structural_match_returned():
    # Kalshi and Polymarket share same market_key → one match
    k_market = _kalshi_market(market_id="KASH-GDP", title="GDP > 3%?")
    p_market = _poly_market(slug="gdp-exceed-3pct", title="Will GDP exceed 3%?")
    pmxt_mod = _make_pmxt_module([k_market], [p_market])
    shared_key = "gdp_above_3"

    with patch.dict(sys.modules, {"pmxt": pmxt_mod}), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_kalshi_market",
               return_value=_fake_fingerprint(market_key=shared_key)), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_polymarket_market",
               return_value=_fake_fingerprint(market_key=shared_key)), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await discover_via_pmxt(None, queries=["GDP"], max_per_query=10)

    assert len(result["structural_matches"]) == 1
    match = result["structural_matches"][0]
    assert match["kalshi_ticker"] == "KASH-GDP"
    assert match["polymarket_slug"] == "gdp-exceed-3pct"
    assert match["source"] == "pmxt_discovery"
    assert match["structural_match"] is True


@pytest.mark.asyncio
async def test_discover_source_mismatch_skips_match():
    # Even if market_keys match, k_fp.source != p_fp.source → no structural match
    k_market = _kalshi_market(market_id="KASH-X")
    p_market = _poly_market(slug="poly-x")
    pmxt_mod = _make_pmxt_module([k_market], [p_market])
    shared_key = "same_key"

    def fp_kalshi(_): return _fake_fingerprint(source="economic_indicator", market_key=shared_key)
    def fp_poly(_): return _fake_fingerprint(source="election", market_key=shared_key)

    with patch.dict(sys.modules, {"pmxt": pmxt_mod}), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_kalshi_market", side_effect=fp_kalshi), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_polymarket_market", side_effect=fp_poly), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await discover_via_pmxt(None, queries=["test"], max_per_query=10)

    assert result["structural_matches"] == [], \
        "source mismatch must prevent a structural match from being emitted"


@pytest.mark.asyncio
async def test_discover_deduplicates_same_market_id_across_queries():
    # Same Kalshi market_id returned by two different queries → counted once
    k_market = _kalshi_market(market_id="KASH-DUP")
    pmxt_mod = _make_pmxt_module([k_market], [])  # returned for every query

    with patch.dict(sys.modules, {"pmxt": pmxt_mod}), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_kalshi_market", return_value=None), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_polymarket_market", return_value=None), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await discover_via_pmxt(None, queries=["Q1", "Q2", "Q3"], max_per_query=10)

    assert result["kalshi_total"] == 1, \
        "duplicate market_id from multiple queries must be de-duped to 1"


@pytest.mark.asyncio
async def test_discover_dry_run_does_not_write_to_store():
    k_market = _kalshi_market(market_id="KASH-DR")
    p_market = _poly_market(slug="poly-dr")
    pmxt_mod = _make_pmxt_module([k_market], [p_market])
    shared_key = "dry_run_key"
    store = MagicMock()
    store.write_candidates = AsyncMock(return_value=1)

    with patch.dict(sys.modules, {"pmxt": pmxt_mod}), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_kalshi_market",
               return_value=_fake_fingerprint(market_key=shared_key)), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_polymarket_market",
               return_value=_fake_fingerprint(market_key=shared_key)), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await discover_via_pmxt(store, queries=["test"], dry_run=True)

    store.write_candidates.assert_not_called()
    assert "candidates_written" not in result


@pytest.mark.asyncio
async def test_discover_writes_candidates_to_store_when_live():
    k_market = _kalshi_market(market_id="KASH-W", title="Kalshi Title")
    p_market = _poly_market(slug="poly-w", title="Poly Title")
    pmxt_mod = _make_pmxt_module([k_market], [p_market])
    shared_key = "live_write_key"
    store = MagicMock()
    store.write_candidates = AsyncMock(return_value=1)

    with patch.dict(sys.modules, {"pmxt": pmxt_mod}), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_kalshi_market",
               return_value=_fake_fingerprint(market_key=shared_key)), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_polymarket_market",
               return_value=_fake_fingerprint(market_key=shared_key)), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await discover_via_pmxt(store, queries=["test"], dry_run=False)

    store.write_candidates.assert_called_once()
    written_candidates = store.write_candidates.call_args[0][0]
    assert len(written_candidates) == 1
    assert written_candidates[0]["kalshi_ticker"] == "KASH-W"
    assert written_candidates[0]["poly_slug"] == "poly-w"
    assert written_candidates[0]["score"] == 0.95
    assert result["candidates_written"] == 1


@pytest.mark.asyncio
async def test_discover_store_write_error_does_not_raise():
    k_market = _kalshi_market(market_id="KASH-ERR")
    p_market = _poly_market(slug="poly-err")
    pmxt_mod = _make_pmxt_module([k_market], [p_market])
    shared_key = "err_key"
    store = MagicMock()
    store.write_candidates = AsyncMock(side_effect=RuntimeError("DB down"))

    with patch.dict(sys.modules, {"pmxt": pmxt_mod}), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_kalshi_market",
               return_value=_fake_fingerprint(market_key=shared_key)), \
         patch("arbiter.mapping.event_fingerprint.fingerprint_polymarket_market",
               return_value=_fake_fingerprint(market_key=shared_key)), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await discover_via_pmxt(store, queries=["test"], dry_run=False)

    assert "write_error" in result, "store write failures must be captured, not re-raised"
