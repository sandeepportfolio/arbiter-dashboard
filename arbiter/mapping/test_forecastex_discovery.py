"""Tests for the ForecastEx auto-discovery module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from .forecastex_discovery import (
    DEFAULT_SEED_KEYWORDS,
    _score_event_against_mapping,
    _strip_marker,
    discover,
    enumerate_forecastex_events,
)
from .market_map import MarketMapping, MappingStatus


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_mapping(
    canonical_id: str,
    description: str,
    *,
    status: MappingStatus = MappingStatus.CONFIRMED,
    forecastex: str = "",
    polymarket_question: str = "",
    aliases: tuple[str, ...] = (),
) -> MarketMapping:
    return MarketMapping(
        canonical_id=canonical_id,
        description=description,
        status=status,
        kalshi_market_id="KX-FAKE",
        polymarket_slug="poly-fake",
        polymarket_question=polymarket_question,
        forecastex_contract_id=forecastex,
        aliases=aliases,
    )


class _FakeClient:
    """Stand-in for ForecastExClient — only needs ``_request`` for discovery."""

    def __init__(self, search_results: dict[str, list[dict[str, Any]]]):
        self._search_results = search_results

    async def _request(self, method: str, path: str, *, json_body=None, params=None):
        assert method == "POST"
        assert path == "/iserver/secdef/search"
        symbol = (json_body or {}).get("symbol", "")
        # _request wraps top-level lists as {"items": [...]}
        return {"items": list(self._search_results.get(symbol, []))}


class _FakeStore:
    """Stand-in for MarketMappingStore — implements iter_confirmed + upsert."""

    def __init__(self, mappings: list[MarketMapping]):
        self._mappings = {m.canonical_id: m for m in mappings}
        self.upserts: list[MarketMapping] = []

    async def iter_confirmed(self, require_auto_trade: bool = False):
        for cid, m in self._mappings.items():
            if m.status == MappingStatus.CONFIRMED:
                yield cid, m

    async def upsert(self, mapping: MarketMapping) -> MarketMapping:
        self.upserts.append(mapping)
        self._mappings[mapping.canonical_id] = mapping
        return mapping


# ── Pure-function tests ───────────────────────────────────────────────────


def test_strip_marker_removes_forecastx_suffix():
    assert _strip_marker("FL-16 General Election (FORECASTX)") == "FL-16 General Election"
    assert _strip_marker("US House of Representatives Control - FORECASTX") == "US House of Representatives Control"
    # Already clean
    assert _strip_marker("Plain title") == "Plain title"


def test_score_event_against_mapping_uses_best_field():
    mapping = _make_mapping(
        "GOP_HOUSE_2026",
        description="U.S House Midterm Winner",
        polymarket_question="Which party wins the US House in 2026 midterms?",
    )
    # FORECASTX title shares 'us house' tokens with both description and poly_q
    score = _score_event_against_mapping("US House of Representatives Control", mapping)
    assert score > 0.3, f"expected meaningful overlap, got {score}"


def test_score_event_against_mapping_zero_when_unrelated():
    mapping = _make_mapping("MLB_BOS_NYY", description="Boston Red Sox vs New York Yankees")
    score = _score_event_against_mapping("Texas Governor Republican Primary", mapping)
    assert score < 0.2


def test_score_event_against_mapping_blocks_sports_vs_political_false_positive():
    # The trap we found in prod: "New York Yankees" shares geography with
    # "New York Governor". Without the domain guard, overlap coefficient
    # rates this 0.5. With the guard it must return 0.
    mapping = _make_mapping(
        "GAME_MLB_20260517_NYY_fae1a2d9",
        description="New York Yankees vs. New York Mets",
        polymarket_question="New York Yankees vs. New York Mets",
    )
    score = _score_event_against_mapping(
        "New York Governor Republican Primary", mapping,
    )
    assert score == 0.0


def test_score_event_against_mapping_allows_political_to_political():
    # Inverse — make sure the guard doesn't reject the legitimate match
    # between a political control market and the FORECASTX political event.
    mapping = _make_mapping(
        "GOP_HOUSE_2026",
        description="U.S House Midterm Winner",
        polymarket_question="Which party wins the US House in 2026 midterms?",
    )
    score = _score_event_against_mapping(
        "US House of Representatives Control", mapping,
    )
    assert score > 0.3


# ── Async behaviour tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enumerate_dedupes_by_conid():
    # Same event returned for multiple search keywords — must dedupe.
    client = _FakeClient(
        {
            "house": [
                {"conid": "733131966", "companyHeader": "US House of Representatives Control (FORECASTX)", "symbol": "HCTRL"},
            ],
            "control": [
                {"conid": "733131966", "companyHeader": "US House of Representatives Control - FORECASTX", "symbol": "HCTRL"},
                {"conid": "733131974", "companyHeader": "US Senate Control (FORECASTX)", "symbol": "SCTRL"},
            ],
            # Non-FORECASTX rows must be filtered out.
            "senate": [
                {"conid": "999", "companyHeader": "Some Stock (CORPACT)", "symbol": "X"},
                {"conid": "733131974", "companyHeader": "US Senate Control (FORECASTX)", "symbol": "SCTRL"},
            ],
        }
    )
    events = await enumerate_forecastex_events(
        client, keywords=("house", "control", "senate"),
    )
    conids = sorted(e["conid"] for e in events)
    assert conids == ["733131966", "733131974"]
    titles = {e["conid"]: e["title"] for e in events}
    assert titles["733131966"] == "US House of Representatives Control"
    assert titles["733131974"] == "US Senate Control"


@pytest.mark.asyncio
async def test_enumerate_swallows_per_keyword_failures():
    class _FlakyClient:
        async def _request(self, method, path, *, json_body=None, params=None):
            if (json_body or {}).get("symbol") == "bad":
                raise RuntimeError("boom")
            return {"items": [
                {"conid": "1", "companyHeader": "Good (FORECASTX)", "symbol": "G"},
            ]}

    events = await enumerate_forecastex_events(
        _FlakyClient(), keywords=("good", "bad"),
    )
    # bad keyword must not poison the whole pass
    assert len(events) == 1
    assert events[0]["conid"] == "1"


@pytest.mark.asyncio
async def test_discover_attaches_matching_conid():
    client = _FakeClient(
        {
            "house": [
                {"conid": "733131966", "companyHeader": "US House of Representatives Control (FORECASTX)", "symbol": "HCTRL"},
            ],
        }
    )
    store = _FakeStore([
        _make_mapping("GOP_HOUSE_2026", "U.S House of Representatives Midterm Winner"),
        # Already attached — must be skipped.
        _make_mapping("DEM_HOUSE_2026", "U.S House Midterm Winner", forecastex="999"),
        # Not confirmed — must be skipped.
        _make_mapping("EXPIRED", "U.S House Midterm Winner", status=MappingStatus.EXPIRED),
    ])

    matched = await discover(client, store, keywords=("house",), min_score=0.3)
    assert matched == 1
    assert len(store.upserts) == 1
    assert store.upserts[0].canonical_id == "GOP_HOUSE_2026"
    assert store.upserts[0].forecastex_contract_id == "733131966"


@pytest.mark.asyncio
async def test_discover_respects_min_score():
    client = _FakeClient(
        {
            "race": [
                {"conid": "111", "companyHeader": "Texas Governor Republican Primary (FORECASTX)", "symbol": "TXGR"},
            ],
        }
    )
    store = _FakeStore([
        _make_mapping("MLB_BOS_NYY_20260516", "Boston Red Sox vs New York Yankees"),
    ])
    matched = await discover(client, store, keywords=("race",), min_score=0.5)
    assert matched == 0
    assert store.upserts == []


@pytest.mark.asyncio
async def test_discover_dry_run_does_not_write():
    client = _FakeClient(
        {
            "house": [
                {"conid": "733131966", "companyHeader": "US House Control (FORECASTX)", "symbol": "HCTRL"},
            ],
        }
    )
    store = _FakeStore([_make_mapping("GOP_HOUSE_2026", "US House Midterm Winner")])
    matched = await discover(client, store, keywords=("house",), min_score=0.3, dry_run=True)
    assert matched == 1
    assert store.upserts == []  # dry-run: no writes


@pytest.mark.asyncio
async def test_discover_handles_no_client():
    matched = await discover(None, _FakeStore([]))
    assert matched == 0


def test_default_seed_keywords_are_lowercase_strings():
    assert all(isinstance(k, str) and k == k.lower() for k in DEFAULT_SEED_KEYWORDS)
    # Make sure we cover both election and sports for future inventory.
    assert "election" in DEFAULT_SEED_KEYWORDS
    assert "mlb" in DEFAULT_SEED_KEYWORDS
