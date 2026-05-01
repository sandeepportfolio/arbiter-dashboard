# Continuous Market Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a continuous, event-first mapping system that materially increases verified Kalshi/Polymarket US mappings while preserving zero-false-positive trading safety.

**Architecture:** Keep the existing confirmed/auto-trade safety contract intact, then add a shadow-mode event graph that normalizes both venues into canonical events, markets, outcomes, and resolution hashes. Discovery should match event fingerprints first, enumerate outcome-level pairs inside matched events, then promote only pairs with identical resolution criteria and executable orderbook/polarity checks.

**Tech Stack:** Python 3, aiohttp collectors, Postgres/asyncpg mapping store, existing ops API and ops.html console, pytest, Vitest, Docker Compose production deployment with `--no-deps`.

---

## Research And Current Diagnosis

Sources checked:
- Kalshi documents events as real-world occurrences containing one or more markets, and exposes `GET /events` with cursor pagination, status filtering, and optional nested markets: https://docs.kalshi.com/python-sdk/api/EventsApi
- Kalshi documents `GET /markets` with `event_ticker`, `series_ticker`, cursor pagination, and status filtering: https://docs.kalshi.com/python-sdk/api/MarketsApi
- Polymarket US documents a public market-data API for markets, events, series, sports data, and search, plus market WebSockets: https://docs.polymarket.us/api-reference/introduction
- Polymarket US event fields include category, subcategory, active/closed flags, sports game IDs, Sportradar IDs, participants, volume, and liquidity: https://docs.polymarket.us/api-reference/events/overview
- The Odds API, representative of production sports-odds aggregation, organizes by sport/event first, then bookmaker markets and outcomes inside the event: https://the-odds-api.com/liveapi/guides/v4/

Live catalog observations from public APIs on 2026-05-01:
- Kalshi returned at least 30,000 open markets before the audit counter stopped at its safety cap; a longer exact count hit Kalshi 429 before completion.
- Kalshi returned 6,005 open events in the first capped event pass.
- Polymarket US returned 1,277 active, non-closed, non-archived markets and 217 active events.
- Current overlap is mostly sports, climate/weather, macro/economics, and a small politics set. Kalshi also has large categories that Polymarket US currently has little/no active overlap for, including entertainment, companies, science/technology, mentions, financials, commodities, world, health, and transportation.

Root cause:
- `arbiter/mapping/auto_discovery.py` is event-aware, but the main path is still token/fuzzy-first and one-to-one. `_finalize_candidates()` drops any additional candidate using the same Kalshi ticker or Polymarket slug, which blocks event/outcome expansion.
- The Kalshi default discovery cap is `AUTO_DISCOVERY_KALSHI_MARKET_MAX_PAGES=10`, so production sees at most roughly 10,000 Kalshi markets even though the live open catalog is much larger.
- Event discovery caps matched events at 250 and uses only four top Polymarket event matches per Kalshi event, which is too small for sports cards with many game markets.
- Fingerprinting covers only a narrow slice of sports winner, US chamber control, crypto reach, GDP, Fed, CPI, and unemployment formats. Whole active categories are indexed only by fuzzy text or not structurally parsed at all.
- Promotion currently combines three separate concepts: same-market verification, executable liquidity, and auto-trade readiness. Correct but thin markets can fail `liquidity_low` and never become confirmed, making mapping coverage look artificially low.
- LLM verification has batch support for API/HTTP backends, but CLI backend falls back to one Claude process per pair and cache is only in memory.

Safety principle:
- New discovery must never loosen confirmation. Structural candidate volume can go up aggressively, but `status='confirmed'` remains reserved for identical resolution criteria, and `allow_auto_trade=true` remains reserved for same-polarity, active, executable pairs.

## File Map

Create:
- `arbiter/mapping/canonical.py`: immutable canonical event/market/outcome/resolution dataclasses.
- `arbiter/mapping/category_parsers.py`: category-specific parser registry and parse result API.
- `arbiter/mapping/event_graph.py`: builds event-first catalogs from Kalshi events/markets and Polymarket US events/markets.
- `arbiter/mapping/discovery_telemetry.py`: normalized run/funnel/rejection telemetry helpers.
- `arbiter/mapping/test_category_parsers.py`: golden parser tests.
- `arbiter/mapping/test_event_graph.py`: event-first graph and enumeration tests.
- `arbiter/mapping/test_discovery_telemetry.py`: telemetry serialization and reason aggregation tests.

Modify:
- `arbiter/mapping/event_fingerprint.py`: delegate to the new parser registry while keeping existing public functions stable.
- `arbiter/mapping/auto_discovery.py`: add shadow-mode event-first discovery and replace one-to-one finalization with one-to-one-per-outcome.
- `arbiter/mapping/auto_promote.py`: split verified mapping promotion from executable auto-trade gating without changing scanner safety semantics.
- `arbiter/mapping/llm_verifier.py`: add durable cache key shape and true batch path through HTTP/API.
- `arbiter/mapping/market_map.py`: persist parser metadata, resolution hash, validation history, and executable status.
- `arbiter/api.py`: expose mapping run history, per-row validation history, stale unmapped item rerun, and category funnel counts.
- `arbiter/main.py`: record continuous discovery progress, not just API-triggered progress.
- `arbiter/web/ops.html`: show event-level validation steps, stale rerun actions, and category/rejection funnel data on the mappings page.
- `tests/test_safety_guards.py`, `tests/test_mapping_validation.py`, `arbiter/mapping/test_auto_discovery.py`, `arbiter/mapping/test_market_map.py`: lock in no-false-positive behavior.

## Task 1: Add Parser Golden Tests First

**Files:**
- Create: `arbiter/mapping/test_category_parsers.py`
- Modify: none

- [ ] **Step 1: Write failing parser coverage tests**

```python
from arbiter.mapping.category_parsers import parse_kalshi_market, parse_polymarket_market


def test_sports_moneyline_same_event_same_outcome():
    kalshi = parse_kalshi_market({"ticker": "KXMLBGAME-26MAY01HOUBAL-HOU"})
    poly = parse_polymarket_market({"slug": "atc-mlb-hou-bal-2026-05-01-hou"})
    assert kalshi is not None
    assert poly is not None
    assert kalshi.resolution_hash == poly.resolution_hash
    assert kalshi.outcome_key == poly.outcome_key


def test_sports_date_mismatch_never_matches():
    kalshi = parse_kalshi_market({"ticker": "KXMLBGAME-26MAY01HOUBAL-HOU"})
    poly = parse_polymarket_market({"slug": "atc-mlb-hou-bal-2026-05-02-hou"})
    assert kalshi is not None
    assert poly is not None
    assert kalshi.resolution_hash != poly.resolution_hash


def test_sports_total_is_not_moneyline():
    kalshi = parse_kalshi_market({"ticker": "KXMLBGAME-26MAY01HOUBAL-HOU"})
    poly = parse_polymarket_market({"slug": "tsc-mlb-hou-bal-2026-05-01-over-8pt5"})
    assert kalshi is not None
    assert poly is not None
    assert kalshi.market_type != poly.market_type
    assert kalshi.resolution_hash != poly.resolution_hash


def test_politics_popular_vote_is_not_house_control():
    kalshi = parse_kalshi_market({"ticker": "CONTROLH-2026-D"})
    poly = parse_polymarket_market({"slug": "democrats-win-house-popular-vote-2026"})
    assert kalshi is not None
    assert poly is not None
    assert kalshi.resolution_hash != poly.resolution_hash


def test_crypto_threshold_and_date_are_in_hash():
    a = parse_kalshi_market({"ticker": "KXBTC-26DEC31-T100000"})
    b = parse_polymarket_market({"slug": "will-bitcoin-reach-100000-by-december-31-2026"})
    c = parse_polymarket_market({"slug": "will-bitcoin-reach-90000-by-december-31-2026"})
    assert a is not None and b is not None and c is not None
    assert a.resolution_hash == b.resolution_hash
    assert a.resolution_hash != c.resolution_hash
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `python -m pytest arbiter/mapping/test_category_parsers.py -q`

Expected: import failure for `arbiter.mapping.category_parsers`.

## Task 2: Add Canonical Parser Types And Registry

**Files:**
- Create: `arbiter/mapping/canonical.py`
- Create: `arbiter/mapping/category_parsers.py`
- Modify: `arbiter/mapping/event_fingerprint.py`
- Test: `arbiter/mapping/test_category_parsers.py`

- [ ] **Step 1: Create canonical parse types**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CanonicalMarket:
    platform: str
    category: str
    subcategory: str
    event_key: str
    market_type: str
    outcome_key: str
    direction: str
    date: str
    source: str
    threshold: str = ""
    entity: str = ""
    raw_id: str = ""

    @property
    def resolution_hash(self) -> str:
        parts = (
            self.category,
            self.subcategory,
            self.event_key,
            self.market_type,
            self.date,
            self.source,
            self.threshold,
        )
        return "|".join(str(part).strip().lower() for part in parts)

    @property
    def outcome_hash(self) -> str:
        return f"{self.resolution_hash}|{self.direction.lower()}|{self.outcome_key.lower()}"


@dataclass(frozen=True)
class ParseFailure:
    parser: str
    reason: str
    raw_id: str
    raw: dict[str, Any]
```

- [ ] **Step 2: Implement registry functions with existing parser compatibility**

```python
from __future__ import annotations

import re
from typing import Any

from arbiter.mapping.canonical import CanonicalMarket
from arbiter.mapping.sports_safety import parse_kalshi_sports_ticker, parse_polymarket_sports_slug, SUPPORTED_POLY_WINNER_PREFIXES
from arbiter.mapping.team_aliases import canonical_pair, normalize_entity_code, split_compound_code


def parse_kalshi_market(market: dict[str, Any]) -> CanonicalMarket | None:
    return _parse_kalshi_sports_winner(market) or _parse_kalshi_existing_structured(market)


def parse_polymarket_market(market: dict[str, Any]) -> CanonicalMarket | None:
    return _parse_poly_sports_winner(market) or _parse_poly_existing_structured(market)


def _parse_kalshi_sports_winner(market: dict[str, Any]) -> CanonicalMarket | None:
    parsed = parse_kalshi_sports_ticker(str(market.get("ticker") or ""))
    if parsed is None:
        return None
    participants = split_compound_code(parsed.participants_raw)
    if participants is None:
        return None
    return CanonicalMarket(
        platform="kalshi",
        category="sports",
        subcategory=parsed.poly_sport,
        event_key=canonical_pair(*participants),
        market_type="moneyline",
        outcome_key=normalize_entity_code(parsed.side),
        direction="yes",
        date=parsed.date,
        source="official_sports_result",
        threshold="moneyline",
        entity=canonical_pair(*participants),
        raw_id=str(market.get("ticker") or ""),
    )


def _parse_poly_sports_winner(market: dict[str, Any]) -> CanonicalMarket | None:
    parsed = parse_polymarket_sports_slug(str(market.get("slug") or ""))
    if parsed is None or parsed.prefix not in SUPPORTED_POLY_WINNER_PREFIXES:
        return None
    outcome = normalize_entity_code(parsed.side) if parsed.side else normalize_entity_code(parsed.team1)
    return CanonicalMarket(
        platform="polymarket",
        category="sports",
        subcategory=parsed.sport,
        event_key=canonical_pair(parsed.team1, parsed.team2),
        market_type="moneyline",
        outcome_key=outcome,
        direction="yes",
        date=parsed.date,
        source="official_sports_result",
        threshold="moneyline",
        entity=canonical_pair(parsed.team1, parsed.team2),
        raw_id=str(market.get("slug") or ""),
    )
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest arbiter/mapping/test_category_parsers.py -q`

Expected: the sports tests pass; politics/crypto tests may still fail until Task 3 ports existing structured parsers.

## Task 3: Port Existing Fingerprints Into Category Parsers

**Files:**
- Modify: `arbiter/mapping/category_parsers.py`
- Modify: `arbiter/mapping/event_fingerprint.py`
- Test: `arbiter/mapping/test_category_parsers.py`, `arbiter/mapping/test_event_fingerprint.py` if present

- [ ] **Step 1: Move existing regex logic without changing public behavior**

Keep `fingerprint_kalshi_market()`, `fingerprint_polymarket_market()`, and `structural_match()` callable from `event_fingerprint.py`, but let them convert `CanonicalMarket` into the existing `MarketFingerprint`.

```python
def _from_canonical(canonical: CanonicalMarket) -> MarketFingerprint:
    return MarketFingerprint(
        category=canonical.category,
        subcategory=canonical.subcategory,
        entity=canonical.entity or canonical.event_key,
        date=canonical.date,
        metric=canonical.market_type,
        threshold=canonical.threshold,
        outcome=canonical.outcome_key,
        direction=canonical.direction,
        source=canonical.source,
    )
```

- [ ] **Step 2: Preserve current safety tests**

Run: `python -m pytest arbiter/mapping/test_auto_discovery.py arbiter/mapping/test_llm_verifier.py tests/test_safety_guards.py -q`

Expected: existing tests still pass.

## Task 4: Build Event Graph In Shadow Mode

**Files:**
- Create: `arbiter/mapping/event_graph.py`
- Create: `arbiter/mapping/test_event_graph.py`
- Modify: `arbiter/mapping/auto_discovery.py`

- [ ] **Step 1: Write event graph tests**

```python
from arbiter.mapping.event_graph import build_event_graph, enumerate_event_pairs


def test_event_graph_pairs_multiple_outcomes_with_same_event():
    kalshi = [
        {"ticker": "KXMLBGAME-26MAY01HOUBAL-HOU", "event_ticker": "KXMLBGAME-26MAY01HOUBAL"},
        {"ticker": "KXMLBGAME-26MAY01HOUBAL-BAL", "event_ticker": "KXMLBGAME-26MAY01HOUBAL"},
    ]
    poly = [
        {"slug": "atc-mlb-hou-bal-2026-05-01-hou"},
        {"slug": "atc-mlb-hou-bal-2026-05-01-bal"},
    ]
    graph = build_event_graph(kalshi, poly)
    pairs = list(enumerate_event_pairs(graph))
    assert {(p.kalshi_id, p.polymarket_id) for p in pairs} == {
        ("KXMLBGAME-26MAY01HOUBAL-HOU", "atc-mlb-hou-bal-2026-05-01-hou"),
        ("KXMLBGAME-26MAY01HOUBAL-BAL", "atc-mlb-hou-bal-2026-05-01-bal"),
    }
```

- [ ] **Step 2: Implement pure graph builder**

The graph builder must be pure and side-effect free so it can run in shadow mode beside existing discovery.

```python
from dataclasses import dataclass
from collections import defaultdict
from typing import Iterable

from arbiter.mapping.category_parsers import parse_kalshi_market, parse_polymarket_market
from arbiter.mapping.canonical import CanonicalMarket


@dataclass(frozen=True)
class EventPair:
    kalshi_id: str
    polymarket_id: str
    resolution_hash: str
    outcome_hash: str
    category: str


def build_event_graph(kalshi_markets: Iterable[dict], poly_markets: Iterable[dict]) -> dict[str, dict[str, list[CanonicalMarket]]]:
    graph: dict[str, dict[str, list[CanonicalMarket]]] = defaultdict(lambda: {"kalshi": [], "polymarket": []})
    for market in kalshi_markets:
        parsed = parse_kalshi_market(market)
        if parsed:
            graph[parsed.resolution_hash]["kalshi"].append(parsed)
    for market in poly_markets:
        parsed = parse_polymarket_market(market)
        if parsed:
            graph[parsed.resolution_hash]["polymarket"].append(parsed)
    return dict(graph)


def enumerate_event_pairs(graph: dict[str, dict[str, list[CanonicalMarket]]]) -> Iterable[EventPair]:
    for resolution_hash, sides in graph.items():
        by_outcome = {item.outcome_hash: item for item in sides.get("polymarket", [])}
        for kalshi_item in sides.get("kalshi", []):
            poly_item = by_outcome.get(kalshi_item.outcome_hash)
            if not poly_item:
                continue
            yield EventPair(
                kalshi_id=kalshi_item.raw_id,
                polymarket_id=poly_item.raw_id,
                resolution_hash=resolution_hash,
                outcome_hash=kalshi_item.outcome_hash,
                category=kalshi_item.category,
            )
```

- [ ] **Step 3: Add shadow-mode call in discovery**

Add `EVENT_LEVEL_MAPPING_SHADOW=true` runtime/env support. In shadow mode, emit counts only; do not write extra candidates yet.

Run: `python -m pytest arbiter/mapping/test_event_graph.py arbiter/mapping/test_auto_discovery.py -q`

Expected: tests pass and existing discovery output is unchanged when the flag is off.

## Task 5: Replace One-To-One Candidate Finalization With Outcome-Key Finalization

**Files:**
- Modify: `arbiter/mapping/auto_discovery.py`
- Modify: `arbiter/mapping/test_auto_discovery.py`

- [ ] **Step 1: Add regression test for two outcomes in one event**

```python
def test_finalize_candidates_keeps_distinct_outcomes_for_same_event():
    from arbiter.mapping.auto_discovery import _finalize_candidates

    candidates = [
        {"kalshi_ticker": "K1-HOU", "poly_slug": "P-hou", "score": 1, "shared_tokens": ["hou"], "outcome_fingerprint": "game|hou"},
        {"kalshi_ticker": "K1-BAL", "poly_slug": "P-bal", "score": 1, "shared_tokens": ["bal"], "outcome_fingerprint": "game|bal"},
    ]
    assert len(_finalize_candidates(candidates, max_candidates=10)) == 2
```

- [ ] **Step 2: Change uniqueness key**

Use pair identity plus outcome fingerprint. Keep one-to-one protection only when no structural outcome key exists.

```python
outcome_key = str(candidate.get("outcome_fingerprint") or "")
if outcome_key:
    unique_key = (kalshi_ticker, poly_slug, outcome_key)
else:
    unique_key = (kalshi_ticker, poly_slug, "")
```

Run: `python -m pytest arbiter/mapping/test_auto_discovery.py -q`

Expected: new regression passes and existing one-to-one protections for fuzzy pairs still pass.

## Task 6: Separate Verified Mapping From Executable Auto-Trade

**Files:**
- Modify: `arbiter/mapping/auto_promote.py`
- Modify: `arbiter/mapping/auto_discovery.py`
- Modify: `arbiter/mapping/market_map.py`
- Modify: `tests/test_safety_guards.py`

- [ ] **Step 1: Add safety tests**

```python
async def test_structurally_identical_low_liquidity_can_be_confirmed_but_not_auto_trade():
    # Exact shape should match the existing maybe_promote fixture helpers.
    result = await maybe_promote_verified_then_executable(
        candidate=identical_structural_candidate(),
        orderbooks={"kalshi": {"bids": [], "asks": []}, "polymarket": {"bids": [], "asks": []}},
        llm_verifier=lambda *_: "YES",
        settings={"AUTO_PROMOTE_ENABLED": True, "PHASE5_MAX_ORDER_USD": 10},
    )
    assert result.status == "confirmed"
    assert result.allow_auto_trade is False
    assert result.reason == "liquidity_low"
```

- [ ] **Step 2: Implement two-tier result**

Keep `maybe_promote()` stable for callers, but internally return:
- `status='confirmed'` only after structural match, resolution identical, polarity safe, and LLM YES.
- `allow_auto_trade=true` only after confirmed plus active book depth, date window, and scanner polarity support.

Run: `python -m pytest tests/test_safety_guards.py arbiter/mapping/test_market_map.py -q`

Expected: no candidate or divergent mapping can auto-trade; low-liquidity verified mappings can be confirmed with `allow_auto_trade=false`.

## Task 7: Add Durable Validation History And Funnel Telemetry

**Files:**
- Create: `arbiter/mapping/discovery_telemetry.py`
- Modify: `arbiter/mapping/market_map.py`
- Modify: `arbiter/api.py`
- Modify: `arbiter/web/ops.html`
- Test: `arbiter/mapping/test_discovery_telemetry.py`, `arbiter/test_api_integration.py`, `arbiter/web/ops-interactions.test.js`

- [ ] **Step 1: Add idempotent schema columns/tables**

Add to `SQL_INIT`:

```sql
ALTER TABLE market_mappings
    ADD COLUMN IF NOT EXISTS resolution_hash TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS outcome_hash TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS parser_version TEXT DEFAULT '',
    ADD COLUMN IF NOT EXISTS executable_status VARCHAR(40) DEFAULT 'unknown';

CREATE TABLE IF NOT EXISTS mapping_validation_events (
    id BIGSERIAL PRIMARY KEY,
    canonical_id VARCHAR(200) NOT NULL,
    phase VARCHAR(80) NOT NULL,
    status VARCHAR(40) NOT NULL,
    message TEXT DEFAULT '',
    details JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mapping_validation_events_canonical
    ON mapping_validation_events(canonical_id, created_at DESC);
```

- [ ] **Step 2: Show validation history in ops**

When a mapping row is clicked, `/ops` must show:
- fetch status for both platforms
- parser result
- resolution hash comparison
- LLM verdict and cached/non-cached state
- liquidity/executable gate result
- final reason

Run: `npx vitest run arbiter/web/ops-interactions.test.js`

Expected: tests include row-click detail rendering and no blank mobile panel.

## Task 8: Increase Discovery Coverage Without Increasing False Positives

**Files:**
- Modify: `arbiter/mapping/auto_discovery.py`
- Modify: `arbiter/config/settings.py` if runtime defaults live there
- Modify: `.env.production.template` only, not `.env.production`, unless operator explicitly asks

- [ ] **Step 1: Add safe coverage knobs**

Runtime defaults:

```env
EVENT_LEVEL_MAPPING_ENABLED=false
EVENT_LEVEL_MAPPING_SHADOW=true
AUTO_DISCOVERY_KALSHI_MARKET_MAX_PAGES=80
AUTO_DISCOVERY_KALSHI_EVENT_MAX_PAGES=100
AUTO_DISCOVERY_POLYMARKET_MAX_PAGES=80
AUTO_DISCOVERY_EVENT_MATCH_MAX_EVENTS=2000
AUTO_DISCOVERY_EVENT_MATCHES_PER_EVENT=25
```

- [ ] **Step 2: Run shadow discovery first**

Run: `python -m pytest arbiter/mapping/test_auto_discovery.py tests/test_mapping_validation.py -q`

Expected: shadow counts are emitted, but written mapping rows are unchanged when `EVENT_LEVEL_MAPPING_ENABLED=false`.

## Task 9: Durable Batch LLM Cache

**Files:**
- Modify: `arbiter/mapping/llm_verifier.py`
- Modify: `arbiter/mapping/market_map.py`
- Test: `arbiter/mapping/test_llm_verifier.py`

- [ ] **Step 1: Add cache key test**

```python
def test_llm_cache_key_includes_resolution_hash_and_parser_version():
    from arbiter.mapping.llm_verifier import cache_key_for_pair
    a = cache_key_for_pair("K1", "P1", "hash-a", "parser-v1")
    b = cache_key_for_pair("K1", "P1", "hash-b", "parser-v1")
    assert a != b
```

- [ ] **Step 2: Add database-backed cache table**

```sql
CREATE TABLE IF NOT EXISTS mapping_llm_verifications (
    cache_key TEXT PRIMARY KEY,
    kalshi_ticker TEXT NOT NULL,
    polymarket_slug TEXT NOT NULL,
    resolution_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    verdict VARCHAR(12) NOT NULL,
    reason TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Run: `python -m pytest arbiter/mapping/test_llm_verifier.py -q`

Expected: cache tests pass and API/HTTP batch behavior remains fail-closed.

## Task 10: Full Discovery Dry Run, Rebaseline, And Deploy

**Files:**
- No new code unless previous tasks reveal defects.

- [ ] **Step 1: Recover local Docker storage before production checks**

The last deployment attempt found Docker's internal filesystem at 100% and Postgres in recovery. Do not recreate Postgres. After operator confirmation, run only a non-volume image cleanup:

```bash
docker image prune -f
```

- [ ] **Step 2: Run full verification**

```bash
python -m pytest tests/ -x -v
npm test
```

Expected: all tests pass.

- [ ] **Step 3: Run shadow discovery and inspect counts**

```bash
EVENT_LEVEL_MAPPING_SHADOW=true python -m pytest arbiter/mapping/test_auto_discovery.py -q
curl -X POST http://localhost:8080/api/reconciliation/rebaseline
curl http://localhost:8080/api/discovery/status | python3 -m json.tool
```

Expected:
- category counts show Kalshi/Polymarket overlap
- rejection reasons are grouped
- row-click validation history appears in ops
- no new mapping has `allow_auto_trade=true` unless executable checks pass

- [ ] **Step 4: Enable event-level mapping and deploy with no Postgres recreation**

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production build arbiter-api-prod
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --no-deps arbiter-api-prod
curl http://localhost:8080/api/status | python3 -m json.tool
```

Expected:
- `live_trading_ready=true`
- mappings page shows confirmed, pending, rejected, stale, and validation history
- scanner operates only on `status='confirmed'`, `resolution_match_status='identical'`, and `allow_auto_trade=true`

## Self-Review

- No runtime behavior is changed by this plan document.
- The plan preserves Gate 0 for parlays/brackets and keeps fuzzy/LLM-only matches out of auto-trade.
- The plan avoids changing existing scanner semantics until verified mappings and executable mappings are explicitly separated.
- The largest expected mapping increase comes from event-first enumeration, parser coverage, and removing accidental one-to-one candidate collapse, not from lowering safety standards.
