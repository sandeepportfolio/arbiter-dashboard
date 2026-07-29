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
        self.unavailable_marks: list[tuple[str, bool]] = []

    async def iter_confirmed(self, require_auto_trade: bool = False):
        for cid, m in self._mappings.items():
            if m.status == MappingStatus.CONFIRMED:
                yield cid, m

    async def upsert(self, mapping: MarketMapping) -> MarketMapping:
        self.upserts.append(mapping)
        self._mappings[mapping.canonical_id] = mapping
        return mapping

    async def mark_forecastex_unavailable(
        self, canonical_id: str, *, unavailable: bool = True
    ) -> bool:
        self.unavailable_marks.append((canonical_id, bool(unavailable)))
        if canonical_id in self._mappings:
            self._mappings[canonical_id].forecastex_not_available = bool(unavailable)
            return True
        return False


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


def test_score_event_against_mapping_blocks_sports_vs_weather_false_positive():
    # 2026-05-26 prod trap: NFL 49ers championship mapping bound to
    # "San Francisco Daily Temperature High UHSFO" — same city token,
    # entirely different domain.
    mapping = _make_mapping(
        "CHAMP_KXNFLNFCCHAMP_27_SF",
        description="Will San Francisco 49ers win the 2027 NFL NFC championship?",
        polymarket_question="Will San Francisco 49ers win the 2027 NFL NFC championship?",
    )
    score = _score_event_against_mapping(
        "San Francisco Daily Temperature High UHSFO", mapping,
    )
    assert score == 0.0


def test_score_event_against_mapping_blocks_sports_vs_cpi_false_positive():
    # 2026-05-26 prod trap: MLB Chicago Cubs mapping bound to "Chicago CPI"
    # because both share the "chicago" token.
    mapping = _make_mapping(
        "GAME_MLB_20260527_CHC_b830e715",
        description="Chicago Cubs vs. Pittsburgh Pirates",
        polymarket_question="Chicago Cubs vs. Pittsburgh Pirates",
    )
    score = _score_event_against_mapping(
        "Chicago CPI", mapping,
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


@pytest.mark.asyncio
async def test_discover_negative_caches_unmatched_mappings():
    """When no FORECASTX event scores against a mapping, mark it unavailable
    so future discovery passes skip it instead of re-querying IBKR each cycle.
    """
    # Political events only — sports mapping will score 0 against all of them
    # via the domain-compatibility guard.
    client = _FakeClient(
        {
            "house": [
                {"conid": "111", "companyHeader": "US House Control (FORECASTX)", "symbol": "HCTRL"},
            ],
        }
    )
    sports_mapping = _make_mapping(
        "GAME_MLS_20260516", "Inter Miami vs Atlanta United MLS Game"
    )
    store = _FakeStore([sports_mapping])
    matched = await discover(client, store, keywords=("house",), min_score=0.3)

    assert matched == 0
    assert store.upserts == []
    # Mapping was searched, no event scored, negative-cache set.
    assert store.unavailable_marks == [("GAME_MLS_20260516", True)]
    assert sports_mapping.forecastex_not_available is True


@pytest.mark.asyncio
async def test_discover_skips_negative_cached_mappings():
    """Mappings already flagged as forecastex_not_available must not be
    re-queried — that's the whole point of the negative cache."""
    client = _FakeClient(
        {
            "house": [
                {"conid": "111", "companyHeader": "US House Control (FORECASTX)", "symbol": "HCTRL"},
            ],
        }
    )
    cached = _make_mapping(
        "ALREADY_CACHED", "Senate Race Florida"
    )
    cached.forecastex_not_available = True
    fresh = _make_mapping("FRESH_TARGET", "US House Midterm Winner")
    store = _FakeStore([cached, fresh])

    matched = await discover(client, store, keywords=("house",), min_score=0.3)

    # Only the fresh mapping should be touched.
    assert matched == 1
    assert store.upserts[0].canonical_id == "FRESH_TARGET"
    # The cached mapping must not have been re-marked.
    assert all(cid != "ALREADY_CACHED" for cid, _ in store.unavailable_marks)


@pytest.mark.asyncio
async def test_discover_dry_run_does_not_set_negative_cache():
    """Dry-run is a preview; no writes anywhere, including negative-cache marks."""
    client = _FakeClient(
        {
            "house": [
                {"conid": "111", "companyHeader": "US House Control (FORECASTX)", "symbol": "HCTRL"},
            ],
        }
    )
    sports_mapping = _make_mapping("MLB_GAME", "Yankees vs Red Sox")
    store = _FakeStore([sports_mapping])
    await discover(client, store, keywords=("house",), min_score=0.3, dry_run=True)
    assert store.unavailable_marks == []
    assert sports_mapping.forecastex_not_available is False


def test_default_seed_keywords_are_lowercase_strings():
    assert all(isinstance(k, str) and k == k.lower() for k in DEFAULT_SEED_KEYWORDS)
    # Make sure we cover both election and sports for future inventory.
    assert "election" in DEFAULT_SEED_KEYWORDS
    assert "mlb" in DEFAULT_SEED_KEYWORDS


def test_seed_keywords_cover_known_fx_symbols():
    """2026-05-26 catalog sweep: confirm the seed list contains every
    high-value FORECASTX symbol the live IBKR probe surfaced. These are
    the exact symbols IBKR returns as parent IND conids — if discovery
    doesn't query for them, those parents never make it into the
    candidate set and operators lose visibility into the catalog
    expansion (state Senate / Governor / CPI variants / GDP / crypto).
    """
    # Sample across categories — not exhaustive, but a representative
    # tripwire that catches accidental seed-list truncation.
    must_include = {
        "horc", "senm",              # binary political (resolvable)
        "axxtx", "axxga", "axxnc",   # state Senate general
        "mxxca", "mxxfl", "mxxtx",   # state Governor general
        "gpgar", "gptxr",            # governor primaries
        "cpi", "cpic", "cpiy",       # CPI variants
        "rgdp", "gdp", "gdqtx",      # GDP variants
        "fedrc", "ff", "ffdec",      # Fed rate / decision
        "cbbtc", "cbeth", "yxhbt",   # crypto thresholds
        "uhatl", "uhnyc" if False else "uhlga",  # weather (LGA is NYC code in FX)
    }
    missing = must_include - set(DEFAULT_SEED_KEYWORDS)
    assert not missing, f"seed list missing FX symbols: {sorted(missing)}"


def test_seed_keywords_are_deduplicated():
    """No keyword should appear twice — IBKR's search is idempotent but
    duplicates burn one extra HTTP call per pass for zero new conids."""
    assert len(DEFAULT_SEED_KEYWORDS) == len(set(DEFAULT_SEED_KEYWORDS)), (
        f"duplicate seeds: "
        f"{sorted(k for k in set(DEFAULT_SEED_KEYWORDS) if DEFAULT_SEED_KEYWORDS.count(k) > 1)}"
    )


# ── 2026-06-02 wrong-domain quarantine + NO backfill regression tests ─────


class _ClientWithChildren:
    """FakeClient that also implements resolve_event_children — needed for
    the NO-conid backfill path added 2026-06-02."""

    def __init__(
        self,
        search_results: dict[str, list[dict[str, Any]]],
        children: dict[str, list[dict[str, Any]]],
    ):
        self._search_results = search_results
        self._children = children

    async def _request(self, method, path, *, json_body=None, params=None):
        return {"items": list(self._search_results.get((json_body or {}).get("symbol", ""), []))}

    async def resolve_event_children(self, parent_conid):
        return list(self._children.get(str(parent_conid), []))


def test_blocklist_contains_known_bad_conids():
    from .forecastex_discovery import (
        _BLOCKLISTED_FORECASTEX_CONIDS,
        is_blocklisted_forecastex_conid,
    )
    # Conids observed live 2026-06-02 attached to multiple unrelated sports
    # mappings — keep them on the blocklist as a regression tripwire.
    for bad in ("860934996", "882351367", "831072372"):
        assert bad in _BLOCKLISTED_FORECASTEX_CONIDS
        assert is_blocklisted_forecastex_conid(bad) is True
    # Sanity — random conids must not match.
    assert is_blocklisted_forecastex_conid("000") is False
    assert is_blocklisted_forecastex_conid("") is False


def test_sports_hint_tokens_include_kalshi_channel_slugs():
    """The 2026-06-02 audit found CHAMP_KX*/GAME_MLB_* canonical_ids
    bypassing the domain guard because their tokens (champ/kxncaaf/etc.)
    weren't in the sports set."""
    from .forecastex_discovery import _SPORTS_HINT_TOKENS
    for slug in ("champ", "kxncaaf", "kxnflafcchamp", "kxnflnfcchamp", "kxmlb", "ncaaf"):
        assert slug in _SPORTS_HINT_TOKENS


def test_score_blocks_kxnflnfcchamp_vs_political_event():
    """Regression for the actual prod canonical_ids that mis-bound
    2026-06-02: CHAMP_KXNFLNFCCHAMP_27_SF, GAME_MLB_20260516_TEX_*."""
    sf_mapping = _make_mapping(
        "CHAMP_KXNFLNFCCHAMP_27_SF",
        description="San Francisco 49ers — 2027 NFL NFC Championship",
        polymarket_question="Will the SF 49ers win the 2027 NFC Championship?",
    )
    score = _score_event_against_mapping(
        "San Francisco Daily Temperature High UHSFO", sf_mapping,
    )
    assert score == 0.0, f"weather event must not match NFL mapping; got {score}"

    tex_mapping = _make_mapping(
        "GAME_MLB_20260516_TEX_5b62f9ee",
        description="Texas Rangers vs. Houston Astros",
        polymarket_question="Texas Rangers vs. Houston Astros",
    )
    # Political and CPI events that share Texas/Houston geography must
    # not bind to MLB mappings.
    assert _score_event_against_mapping("Texas Governor Republican Primary", tex_mapping) == 0.0
    assert _score_event_against_mapping("Houston CPI", tex_mapping) == 0.0


@pytest.mark.asyncio
async def test_discover_refuses_blocklisted_conid():
    """If the best-scoring event's conid is on the blocklist, the mapping
    must be marked forecastex_not_available rather than bound."""
    client = _ClientWithChildren(
        search_results={
            "house": [
                # 860934996 is on the blocklist (sports cross-bind)
                {"conid": "860934996", "companyHeader": "US House Control (FORECASTX)", "symbol": "HCTRL"},
            ],
        },
        children={},
    )
    mapping = _make_mapping("GOP_HOUSE_2026", "U.S House of Representatives Midterm Winner")
    store = _FakeStore([mapping])
    matched = await discover(client, store, keywords=("house",), min_score=0.3)
    assert matched == 0
    assert any(canon == "GOP_HOUSE_2026" and flag is True for canon, flag in store.unavailable_marks)
    assert mapping.forecastex_contract_id == ""


@pytest.mark.asyncio
async def test_discover_backfills_no_conid_when_yes_already_bound():
    """When a mapping has YES bound but no NO, discover() must call
    resolve_event_children on the YES conid and persist the NO sibling."""
    # YES=111 → siblings include itself (right=C strike=1) + the NO sibling
    # (right=P strike=1).
    client = _ClientWithChildren(
        search_results={},  # no new events — only backfill path runs
        children={
            "111": [
                {"conid": "111", "right": "C", "strike": 1.0},
                {"conid": "222", "right": "P", "strike": 1.0},
            ],
        },
    )
    mapping = _make_mapping("GOP_HOUSE_2026", "US House Control", forecastex="111")
    store = _FakeStore([mapping])
    matched = await discover(client, store, keywords=("house",), min_score=0.3)
    # matched counts NEW attachments, NO-backfill is a separate counter
    # but does emit an upsert.
    assert any(u.canonical_id == "GOP_HOUSE_2026" and u.forecastex_no_contract_id == "222" for u in store.upserts)


@pytest.mark.asyncio
async def test_discover_backfill_skips_blocklisted_no_sibling():
    """NO sibling that matches a blocklisted conid must be skipped."""
    client = _ClientWithChildren(
        search_results={},
        children={
            "111": [
                {"conid": "111", "right": "C", "strike": 1.0},
                # 860934996 is blocklisted
                {"conid": "860934996", "right": "P", "strike": 1.0},
            ],
        },
    )
    mapping = _make_mapping("GOP_HOUSE_2026", "US House Control", forecastex="111")
    store = _FakeStore([mapping])
    await discover(client, store, keywords=("house",), min_score=0.3)
    assert not any(
        u.canonical_id == "GOP_HOUSE_2026" and u.forecastex_no_contract_id
        for u in store.upserts
    ), "blocklisted NO conid must not be persisted"


@pytest.mark.asyncio
async def test_discover_backfill_dry_run_does_not_upsert():
    client = _ClientWithChildren(
        search_results={},
        children={
            "111": [
                {"conid": "111", "right": "C", "strike": 1.0},
                {"conid": "222", "right": "P", "strike": 1.0},
            ],
        },
    )
    mapping = _make_mapping("GOP_HOUSE_2026", "US House Control", forecastex="111")
    store = _FakeStore([mapping])
    await discover(client, store, keywords=("house",), min_score=0.3, dry_run=True)
    assert store.upserts == []


@pytest.mark.asyncio
async def test_discover_falls_back_to_next_best_when_top_score_is_blocklisted():
    """Adversarial regression: if the highest-scoring candidate is
    blocklisted, the loop must try the next-best instead of skipping the
    whole mapping. Previously the greedy one-shot selection lost the
    legitimate runner-up."""
    client = _ClientWithChildren(
        search_results={
            "house": [
                # Top score (highest token overlap) — blocklisted.
                {"conid": "860934996",
                 "companyHeader": "US House of Representatives Control (FORECASTX)",
                 "symbol": "HCTRL"},
                # Runner-up — clean conid that should be picked instead.
                {"conid": "733131966",
                 "companyHeader": "US House Control (FORECASTX)",
                 "symbol": "HCTRL2"},
            ],
        },
        children={},
    )
    mapping = _make_mapping(
        "GOP_HOUSE_2026", "U.S House of Representatives Midterm Winner",
    )
    store = _FakeStore([mapping])
    matched = await discover(client, store, keywords=("house",), min_score=0.3)
    assert matched == 1
    assert len(store.upserts) == 1
    assert store.upserts[0].forecastex_contract_id == "733131966"


# ── /iserver/secdef/strikes 429-burst regression (2026-07-25 ops audit) ───
#
# Live root cause: discover()'s NO-backfill pass was calling
# resolve_event_children() — which fans out to up to 24 rate-limited
# /iserver/secdef/strikes month probes per conid — for 31 confirmed
# mappings that could never resolve:
#
#   * 30 rows persisted the literal junk conid "0". bool("0") is True, so
#     the old `has_yes = bool(...)` check classified them as "YES already
#     bound" and sent them to the backfill pass.
#   * 1 row (FX_PREMP_202703_3p0, conid 582530257) was durably quarantined
#     via forecastex_not_available, but the quarantine check sat AFTER the
#     `if has_yes: ... continue` branch, making it unreachable for any row
#     that had a YES conid.
#
# 31 mappings x 24 months = ~744 wasted strikes calls per discovery cycle.


class _ProbeRecordingClient(_ClientWithChildren):
    """Records every resolve_event_children() call so a test can assert
    that a mapping was never probed against /iserver/secdef/strikes."""

    def __init__(self, search_results=None, children=None):
        super().__init__(search_results or {}, children or {})
        self.probes: list[str] = []

    async def resolve_event_children(self, parent_conid):
        self.probes.append(str(parent_conid))
        return await super().resolve_event_children(parent_conid)


@pytest.mark.asyncio
async def test_discover_never_probes_strikes_for_quarantined_mapping_with_yes_conid():
    """A forecastex_not_available row that still carries a YES conid must be
    skipped BEFORE the NO-backfill pass.

    Regression: the quarantine check used to sit after `if has_yes: continue`,
    so it was unreachable and the row was probed every cycle. Shape taken
    from live row FX_PREMP_202703_3p0 (YES=582530257, NO empty, quarantined).
    """
    client = _ProbeRecordingClient(
        children={"582530257": [{"conid": "582530257", "right": "C", "strike": 3.0}]},
    )
    quarantined = _make_mapping(
        "FX_PREMP_202703_3p0", "Nonfarm payrolls March 2027 above 3.0",
        forecastex="582530257",
    )
    quarantined.forecastex_not_available = True
    store = _FakeStore([quarantined])

    await discover(client, store, keywords=("payrolls",), min_score=0.3)

    assert client.probes == [], (
        "quarantined mapping must never reach resolve_event_children / "
        f"secdef/strikes; got probes={client.probes}"
    )
    assert store.upserts == []


@pytest.mark.asyncio
async def test_discover_never_probes_strikes_for_junk_zero_conid():
    """forecastex_contract_id == "0" is junk, not a bound YES.

    Regression: `has_yes = bool("0")` is True, which routed 30 live rows
    into the NO-backfill pass and called resolve_event_children("0").
    """
    client = _ProbeRecordingClient(children={})
    junk = _make_mapping("JUNK_ZERO_CONID", "Some Market", forecastex="0")
    store = _FakeStore([junk])

    await discover(client, store, keywords=("house",), min_score=0.3)

    assert "0" not in client.probes, (
        f'resolve_event_children("0") must never be issued; got {client.probes}'
    )
    assert client.probes == []


@pytest.mark.asyncio
async def test_discover_still_backfills_healthy_mapping():
    """Guard against over-correction: a healthy, non-quarantined mapping with
    a real YES conid and no NO must STILL be probed and backfilled."""
    client = _ProbeRecordingClient(
        children={
            "762089343": [
                {"conid": "762089343", "right": "C", "strike": 1.0},
                {"conid": "762089344", "right": "P", "strike": 1.0},
            ],
        },
    )
    healthy = _make_mapping(
        "POL_US_HOUSE_DEM", "US House Control Democratic", forecastex="762089343",
    )
    store = _FakeStore([healthy])

    await discover(client, store, keywords=("house",), min_score=0.3)

    assert client.probes == ["762089343"], (
        f"healthy mapping must still be probed exactly once; got {client.probes}"
    )
    assert any(
        u.canonical_id == "POL_US_HOUSE_DEM"
        and u.forecastex_no_contract_id == "762089344"
        for u in store.upserts
    ), "healthy mapping must still get its NO conid backfilled"
