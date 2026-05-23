"""ForecastEx auto-discovery — populate ``forecastex_contract_id`` on confirmed
Kalshi/Polymarket mappings by fuzzy-matching IBKR ForecastEx event titles.

Pattern mirrors :mod:`arbiter.mapping.auto_discovery` (the Kalshi↔Polymarket
discoverer) but runs against the IBKR Client Portal Gateway instead. The
gateway exposes ForecastEx event contracts under ``/iserver/secdef/search``
with ``companyHeader`` containing the literal ``FORECASTX`` marker. The
function enumerates those events via a small set of seed keywords (IBKR's
secdef search does not support a "list all by exchange" call from the
Client Portal API), then walks every confirmed mapping that is still
missing a ``forecastex_contract_id`` and attaches the best fuzzy match.

The matching is intentionally conservative — a 3-way arbitrage that uses a
wrong ForecastEx contract bleeds capital silently — so the default
``min_score`` is set high and operators can review every attach via the
mapping log line emitted at INFO level.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional

from ..config.settings import normalize_market_text, similarity_score
from .market_map import MarketMapping, MarketMappingStore

logger = logging.getLogger("arbiter.mapping.forecastex_discovery")

# Tokens that add no semantic signal — filter before similarity scoring so
# titles like "US House of Representatives" and "U.S House" don't lose
# points over connective words.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "for", "from", "in", "of", "on", "or", "s", "the",
    "to", "u", "us", "vs", "with",
})


def _tokenize(text: str) -> set[str]:
    return {
        t for t in normalize_market_text(text).split()
        if t and t not in _STOPWORDS and len(t) > 1
    }


def _overlap_score(a: str, b: str) -> float:
    """Overlap coefficient: |A∩B| / min(|A|,|B|).

    Less punishing than Jaccard when the FORECASTX title is wordier than the
    Kalshi/Polymarket description (which it usually is — "US House of
    Representatives Control" vs "US House Midterm Winner").
    """
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if not inter:
        return 0.0
    return round(len(inter) / min(len(ta), len(tb)), 4)


# Seed keywords for enumerating FORECASTX inventory. IBKR's secdef/search
# is keyword-driven and capped at ~15 results per query, so we issue
# multiple queries and dedupe by conid. Add new keywords as ForecastEx
# expands beyond elections into sports / weather / etc.
DEFAULT_SEED_KEYWORDS: tuple[str, ...] = (
    # Political (current FORECASTX inventory)
    "election", "senate", "house", "governor", "primary", "general",
    "control", "majority", "midterm", "race", "president",
    "republican", "democrat", "congressional",
    # Elections 2026 specifics
    "2026", "midterm 2026", "senate 2026", "house 2026",
    "gubernatorial", "ballot", "referendum", "recall",
    # Sports — covered if/when IBKR adds the inventory
    "mlb", "nfl", "nba", "nhl", "soccer", "tournament", "championship",
    "world cup", "super bowl", "playoff", "world series",
    # Economics / macro
    "cpi", "gdp", "unemployment", "rate", "fed", "fomc", "inflation",
    "recession", "jobs", "payrolls",
    "fed funds", "federal funds", "interest rate", "rate hike", "rate cut",
    "core cpi", "core pce", "pce", "ppi", "retail sales",
    "nonfarm", "jobless", "consumer", "housing starts",
    # Crypto / finance
    "bitcoin", "ethereum", "crypto", "spx", "sp500", "nasdaq",
    # Crypto price levels (BTC/ETH/XRP targets)
    "btc", "eth", "xrp", "solana", "sol", "dogecoin", "doge",
    "bitcoin 100k", "bitcoin 150k", "bitcoin 200k",
    "ethereum 5k", "ethereum 10k", "xrp 5", "xrp 10",
    "btc target", "eth target", "ath", "all-time high",
    # Company earnings
    "earnings", "eps", "revenue", "guidance", "tesla earnings",
    "apple earnings", "nvidia earnings", "microsoft earnings",
    "google earnings", "amazon earnings", "meta earnings",
    "q1 earnings", "q2 earnings", "q3 earnings", "q4 earnings",
    # Tech milestones
    "ai", "gpt", "agi", "openai", "anthropic", "claude", "gemini",
    "tesla", "spacex", "starship", "nvidia", "chip", "semiconductor",
    "iphone", "model release", "product launch",
    # Climate / weather
    "hurricane", "temperature", "climate", "drought", "snowfall",
    # Science / health
    "vaccine", "drug", "approval", "fda", "trial", "study",
    "launch", "rocket", "mission",
    # Entertainment / culture / awards
    "oscar", "emmy", "grammy", "box", "movie", "award",
    "oscars", "academy award", "best picture", "best actor",
    "best actress", "best director", "grammys", "song of the year",
    "album of the year", "emmys", "outstanding drama", "golden globe",
    # Legal / policy / SCOTUS
    "scotus", "court", "ruling", "verdict", "indictment",
    "supreme court", "scotus decision", "scotus ruling",
    "scotus opinion", "appeal", "appellate", "doj", "justice department",
    # Geopolitics
    "russia", "ukraine", "china", "taiwan", "israel", "iran",
    "north korea", "nato", "un", "united nations", "g7", "g20",
    "summit", "ceasefire", "treaty", "sanction", "tariff", "trade war",
    "peace deal", "border", "invasion",
)

# Conservative threshold. Below this, log the near-miss but do not write.
# 0.30 picks up "U.S House Midterm Winner" ↔ "US House Control" (3-of-9
# overlap) while the domain-routing guard below blocks the obvious false
# positives (sports mappings ↔ FORECASTX geographic primaries that share
# only city names).
DEFAULT_MIN_SCORE = 0.30

# Tokens that mark a mapping (or event) as a sports market. When the mapping
# carries any of these and the FORECASTX event title does not, we refuse to
# match — geographic overlap alone ("New York Yankees" vs "New York Governor")
# is a false-positive trap.
_SPORTS_HINT_TOKENS: frozenset[str] = frozenset({
    "mlb", "nfl", "nba", "nhl", "mls", "epl", "uefa", "fifa", "wnba",
    "game", "vs", "yankees", "mets", "dodgers", "celtics", "lakers",
    "sox", "cubs", "padres", "rangers", "phillies", "athletic",
    "fc", "soccer", "football", "basketball", "baseball", "hockey",
    "tournament", "playoff", "championship", "match", "tie",
})

# Tokens that mark a mapping (or event) as political.
_POLITICAL_HINT_TOKENS: frozenset[str] = frozenset({
    "election", "senate", "house", "governor", "primary", "general",
    "control", "majority", "midterm", "president", "presidential",
    "republican", "democrat", "democratic", "congressional", "gop",
    "dem", "party",
})

# Marker IBKR uses in companyHeader to identify ForecastEx event listings.
FORECASTX_MARKER = "FORECASTX"


def _strip_marker(title: str) -> str:
    """Remove the ``(FORECASTX)`` / ``- FORECASTX`` suffix from an event title."""
    cleaned = title.replace("(FORECASTX)", "").replace("- FORECASTX", "")
    return cleaned.strip(" -")


def _domain_compatible(event_title: str, mapping: MarketMapping) -> bool:
    """Block cross-domain matches.

    A FORECASTX event titled "New York Governor Republican Primary" should
    never bind to a Kalshi MLB mapping ("New York Yankees vs Mets") even
    though they share the "new york" tokens. The check fires only when the
    mapping clearly belongs to one domain (sports) and the event clearly
    belongs to another (political), so it stays conservative and won't
    reject mappings whose domain we can't infer.
    """
    mapping_text = " ".join(
        s for s in (
            mapping.canonical_id,
            mapping.description,
            mapping.polymarket_question,
        ) if s
    )
    mapping_tokens = _tokenize(mapping_text)
    event_tokens = _tokenize(event_title)

    mapping_is_sports = bool(mapping_tokens & _SPORTS_HINT_TOKENS)
    event_is_political = bool(event_tokens & _POLITICAL_HINT_TOKENS)
    event_is_sports = bool(event_tokens & _SPORTS_HINT_TOKENS)
    mapping_is_political = bool(mapping_tokens & _POLITICAL_HINT_TOKENS)

    if mapping_is_sports and event_is_political and not event_is_sports:
        return False
    if mapping_is_political and event_is_sports and not event_is_political:
        return False
    return True


def _score_event_against_mapping(event_title: str, mapping: MarketMapping) -> float:
    """Best similarity between the FORECASTX event title and any descriptive
    field on the mapping (description, polymarket question, aliases).

    Combines two signals and keeps the higher one:
      * overlap coefficient — robust when FORECASTX titles are wordier
      * Jaccard (similarity_score) — penalises spurious matches by union size

    Both run with the same stopword-filtered token set. Cross-domain
    pairings (sports mapping ↔ political event, or vice versa) short-circuit
    to 0 so geographic overlap can't carry a match.
    """
    if not _domain_compatible(event_title, mapping):
        return 0.0
    candidates = (
        mapping.description,
        mapping.polymarket_question,
        " ".join(mapping.aliases) if mapping.aliases else "",
    )
    best = 0.0
    for cand in candidates:
        if not cand:
            continue
        s_overlap = _overlap_score(event_title, cand)
        s_jaccard = similarity_score(event_title, cand)
        score = max(s_overlap, s_jaccard)
        if score > best:
            best = score
    return best


async def _search_forecastx_events(
    client, keyword: str,
) -> list[dict[str, Any]]:
    """Issue a single secdef/search call and return only FORECASTX hits."""
    try:
        payload = await client._request(
            "POST",
            "/iserver/secdef/search",
            json_body={"symbol": keyword, "name": True},
        )
    except Exception as exc:  # noqa: BLE001 — broad catch so one bad keyword can't kill the whole pass
        logger.warning(
            "forecastex_discovery: search '%s' failed: %s", keyword, exc,
        )
        return []

    # _request normalises bare lists to ``{"items": [...]}`` so callers can
    # always treat the response as a dict.
    items: Iterable[Any]
    if isinstance(payload, dict):
        items = payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return []

    hits: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        header = str(item.get("companyHeader") or "")
        if FORECASTX_MARKER not in header.upper():
            continue
        conid = str(item.get("conid") or "").strip()
        if not conid:
            continue
        hits.append(
            {
                "conid": conid,
                "title": _strip_marker(header),
                "symbol": str(item.get("symbol") or ""),
                "raw_header": header,
            }
        )
    return hits


async def enumerate_forecastex_events(
    client, *, keywords: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """Walk a seed keyword list and return deduped FORECASTX events."""
    kws = tuple(keywords) if keywords is not None else DEFAULT_SEED_KEYWORDS
    seen: dict[str, dict[str, Any]] = {}
    for kw in kws:
        for hit in await _search_forecastx_events(client, kw):
            seen.setdefault(hit["conid"], hit)
    return list(seen.values())


async def discover(
    forecastex_client,
    mapping_store: MarketMappingStore,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    keywords: Optional[Iterable[str]] = None,
    dry_run: bool = False,
) -> int:
    """Attach ``forecastex_contract_id`` to confirmed mappings missing one.

    Returns the count of mappings that were updated (or would be, in dry-run).
    """
    if forecastex_client is None:
        logger.info("forecastex_discovery: client unavailable — skipping")
        return 0

    events = await enumerate_forecastex_events(
        forecastex_client, keywords=keywords,
    )
    logger.info(
        "forecastex_discovery: enumerated %d unique FORECASTX events", len(events),
    )
    if not events:
        return 0

    # Gather confirmed mappings that haven't been bound to a ForecastEx
    # contract yet. iter_confirmed is an async generator returning
    # (canonical_id, MarketMapping). Skip rows already negative-cached
    # via `forecastex_not_available` — those have been searched in a prior
    # pass and confirmed to have no FORECASTX equivalent, so re-querying
    # IBKR every cycle is wasted rate budget on the long tail of
    # localised sports/cultural markets.
    targets: list[MarketMapping] = []
    skipped_negative_cache = 0
    async for _canonical_id, mapping in mapping_store.iter_confirmed():
        if (mapping.forecastex_contract_id or "").strip():
            continue
        if getattr(mapping, "forecastex_not_available", False):
            skipped_negative_cache += 1
            continue
        targets.append(mapping)

    logger.info(
        "forecastex_discovery: %d confirmed mapping(s) missing forecastex_contract_id "
        "(skipped %d previously-negative-cached)",
        len(targets),
        skipped_negative_cache,
    )
    if not targets:
        return 0

    matched = 0
    unavailable_marked = 0
    for mapping in targets:
        best_score = 0.0
        best_token_count = 10**9  # tiebreaker: prefer fewer tokens = more specific
        best_event: Optional[dict[str, Any]] = None
        any_event_scored = False
        for event in events:
            score = _score_event_against_mapping(event["title"], mapping)
            if score <= 0:
                continue
            any_event_scored = True
            # Tiebreaker — when two FORECASTX events score the same against a
            # mapping (both share only "house" with "US House Midterm Winner",
            # say), prefer the event with fewer tokens overall. The shorter
            # title is almost always the umbrella event ("US House Control")
            # rather than a specific sub-race ("Kentucky 4th District...").
            event_token_count = len(_tokenize(event["title"]))
            if (
                score > best_score
                or (score == best_score and event_token_count < best_token_count)
            ):
                best_score = score
                best_token_count = event_token_count
                best_event = event

        if best_event is None:
            # No FORECASTX event scored above zero against this mapping
            # (typically: localised MLS/NCAA games whose city names share
            # tokens with no political event in the FORECASTX inventory).
            # Negative-cache so we don't keep scanning every 5 min — the
            # FORECASTX inventory is small and turns over slowly, so a
            # missed match here is acceptable; the operator can re-trigger
            # discovery if FORECASTX expands its catalog.
            if not dry_run and not any_event_scored:
                try:
                    await mapping_store.mark_forecastex_unavailable(
                        mapping.canonical_id
                    )
                    unavailable_marked += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "forecastex_discovery: mark_forecastex_unavailable "
                        "failed for %s: %s",
                        mapping.canonical_id, exc,
                    )
            continue

        if best_score < min_score:
            logger.debug(
                "forecastex_discovery: %s — best near-miss '%s' (conid=%s) score=%.2f < %.2f",
                mapping.canonical_id, best_event["title"], best_event["conid"],
                best_score, min_score,
            )
            continue

        # Resolve YES/NO child conids if the attached parent is an event.
        # IBKR's FORECASTX EC endpoints are flaky (503 on weekends, often
        # unsupported) so the resolver returns [] gracefully when it can't
        # enumerate children — we then fall back to the parent conid and
        # let the collector's parent-detection logic skip it gracefully.
        resolved_conid = best_event["conid"]
        try:
            children = await forecastex_client.resolve_event_children(
                best_event["conid"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "forecastex_discovery: resolve_event_children failed for %s: %s",
                best_event["conid"], exc,
            )
            children = []
        if children:
            # Pick the YES side by convention (right in {"Y","C","1"}); if no
            # right info is present, fall back to the first child.
            yes_child = next(
                (c for c in children
                 if str(c.get("right", "")).upper() in ("Y", "C", "1", "YES")),
                children[0],
            )
            resolved_conid = yes_child["conid"]
            logger.info(
                "forecastex_discovery: resolved %s parent %s -> child %s (right=%s, src=%s)",
                mapping.canonical_id, best_event["conid"],
                resolved_conid, yes_child.get("right"), yes_child.get("source"),
            )

        logger.info(
            "forecastex_discovery: MATCH %s ↔ conid=%s '%s' (score=%.2f)%s",
            mapping.canonical_id,
            resolved_conid,
            best_event["title"],
            best_score,
            " [dry-run]" if dry_run else "",
        )

        if dry_run:
            matched += 1
            continue

        mapping.forecastex_contract_id = resolved_conid
        try:
            await mapping_store.upsert(mapping)
            matched += 1
        except Exception as exc:  # noqa: BLE001 — never block discovery on one row
            logger.error(
                "forecastex_discovery: upsert failed for %s: %s",
                mapping.canonical_id, exc,
            )

    logger.info(
        "forecastex_discovery: attached %d/%d mappings "
        "(negative-cached %d with no FORECASTX overlap)",
        matched, len(targets), unavailable_marked,
    )
    return matched


__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULT_SEED_KEYWORDS",
    "discover",
    "enumerate_forecastex_events",
]
