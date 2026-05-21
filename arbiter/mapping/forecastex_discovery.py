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
    # Future-proofing for sports / events when ForecastEx expands
    "mlb", "nfl", "nba", "nhl", "soccer", "tournament", "championship",
    "world cup", "super bowl", "playoff", "world series",
)

# Conservative threshold. Below this, log the near-miss but do not write.
DEFAULT_MIN_SCORE = 0.55

# Marker IBKR uses in companyHeader to identify ForecastEx event listings.
FORECASTX_MARKER = "FORECASTX"


def _strip_marker(title: str) -> str:
    """Remove the ``(FORECASTX)`` / ``- FORECASTX`` suffix from an event title."""
    cleaned = title.replace("(FORECASTX)", "").replace("- FORECASTX", "")
    return cleaned.strip(" -")


def _score_event_against_mapping(event_title: str, mapping: MarketMapping) -> float:
    """Best similarity between the FORECASTX event title and any descriptive
    field on the mapping (description, polymarket question, aliases).

    Combines two signals and keeps the higher one:
      * overlap coefficient — robust when FORECASTX titles are wordier
      * Jaccard (similarity_score) — penalises spurious matches by union size

    Both run with the same stopword-filtered token set.
    """
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
    # (canonical_id, MarketMapping).
    targets: list[MarketMapping] = []
    async for _canonical_id, mapping in mapping_store.iter_confirmed():
        if (mapping.forecastex_contract_id or "").strip():
            continue
        targets.append(mapping)

    logger.info(
        "forecastex_discovery: %d confirmed mapping(s) missing forecastex_contract_id",
        len(targets),
    )
    if not targets:
        return 0

    matched = 0
    for mapping in targets:
        best_score = 0.0
        best_event: Optional[dict[str, Any]] = None
        for event in events:
            score = _score_event_against_mapping(event["title"], mapping)
            if score > best_score:
                best_score = score
                best_event = event

        if best_event is None:
            continue

        if best_score < min_score:
            logger.debug(
                "forecastex_discovery: %s — best near-miss '%s' (conid=%s) score=%.2f < %.2f",
                mapping.canonical_id, best_event["title"], best_event["conid"],
                best_score, min_score,
            )
            continue

        logger.info(
            "forecastex_discovery: MATCH %s ↔ conid=%s '%s' (score=%.2f)%s",
            mapping.canonical_id,
            best_event["conid"],
            best_event["title"],
            best_score,
            " [dry-run]" if dry_run else "",
        )

        if dry_run:
            matched += 1
            continue

        mapping.forecastex_contract_id = best_event["conid"]
        try:
            await mapping_store.upsert(mapping)
            matched += 1
        except Exception as exc:  # noqa: BLE001 — never block discovery on one row
            logger.error(
                "forecastex_discovery: upsert failed for %s: %s",
                mapping.canonical_id, exc,
            )

    logger.info(
        "forecastex_discovery: attached %d/%d mappings", matched, len(targets),
    )
    return matched


__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULT_SEED_KEYWORDS",
    "discover",
    "enumerate_forecastex_events",
]
