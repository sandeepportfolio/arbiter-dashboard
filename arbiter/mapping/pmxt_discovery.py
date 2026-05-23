"""
PMXT supplemental discovery — fetch markets from Kalshi & Polymarket
via the PMXT unified SDK and feed them through structural matching.

This module provides an alternative discovery path using the open-source
PMXT library (pip install pmxt) which can:
  - Fetch active markets from both platforms with a unified API
  - No API keys needed in local mode (talks directly to venues)
  - Provides consistent market schema across platforms

Usage:
    from arbiter.mapping.pmxt_discovery import discover_via_pmxt
    new_pairs = await discover_via_pmxt(mapping_store)

The module is rate-limit aware and respects platform constraints.
Discovered pairs are fed into the existing validation pipeline
(event_fingerprint.py structural matching + auto_promote.py gates).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("arbiter.mapping.pmxt_discovery")

# Categories and queries to search across platforms. Keep these grouped by
# category so coverage gaps are easy to spot — discovery casts a wide net
# and the structural matcher / LLM verifier prune false positives downstream.
_DISCOVERY_QUERIES = [
    # Economics & finance
    "GDP", "federal funds rate", "unemployment rate", "CPI inflation",
    "interest rate", "FOMC", "recession", "jobs report", "retail sales",
    "S&P 500", "Nasdaq", "Dow Jones",
    # Crypto
    "bitcoin price", "ethereum price", "XRP price", "solana price",
    "dogecoin price", "cardano price", "crypto",
    # Politics
    "senate election", "house election", "president",
    "governor election", "democratic", "republican",
    "midterm", "primary", "approval rating",
    # Geopolitics
    "russia ukraine", "israel", "china taiwan", "nato",
    "ceasefire", "sanctions",
    # Sports
    "NBA", "NFL", "MLB", "NHL", "MLS", "soccer",
    "Premier League", "Champions League", "Serie A", "La Liga",
    "World Cup", "Super Bowl", "World Series", "Stanley Cup",
    "tennis", "golf", "boxing", "UFC",
    # Weather / climate
    "hurricane", "temperature record", "snowfall", "climate",
    "wildfire", "earthquake",
    # Entertainment / culture
    "Oscars", "Grammys", "Emmys", "box office", "movie",
    "album", "Spotify", "Netflix", "TIME person of the year",
    # Science / tech
    "SpaceX", "NASA", "rocket launch", "AI", "OpenAI",
    "GPT", "self-driving", "Apple", "Tesla earnings",
]

# Rate limiting
_MIN_REQUEST_INTERVAL = 1.0  # seconds between requests (conservative)
_MAX_MARKETS_PER_QUERY = 100
_MAX_TOTAL_MARKETS = 5000


def _pmxt_market_to_kalshi_dict(m) -> dict:
    """Convert a PMXT UnifiedMarket from Kalshi to our fingerprint dict format."""
    return {
        "ticker": getattr(m, "market_id", ""),
        "title": getattr(m, "title", ""),
        "subtitle": getattr(m, "description", ""),
        "category": getattr(m, "category", ""),
        "status": getattr(m, "status", ""),
        "resolution_date": getattr(m, "resolution_date", None),
    }


def _pmxt_market_to_poly_dict(m) -> dict:
    """Convert a PMXT UnifiedMarket from Polymarket to our fingerprint dict format."""
    return {
        "slug": getattr(m, "slug", "") or "",
        "question": getattr(m, "title", ""),
        "title": getattr(m, "title", ""),
        "status": getattr(m, "status", ""),
        "resolution_date": getattr(m, "resolution_date", None),
    }


async def _fetch_with_rate_limit(
    fetch_fn: Callable,
    query: str,
    limit: int,
    last_request_time: list[float],
) -> list:
    """Call a PMXT fetch_markets with rate limiting."""
    elapsed = time.monotonic() - last_request_time[0]
    if elapsed < _MIN_REQUEST_INTERVAL:
        await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)

    try:
        # PMXT venue methods are synchronous
        result = fetch_fn(query=query, limit=limit)
        last_request_time[0] = time.monotonic()
        return result or []
    except Exception as e:
        if "429" in str(e) or "rate" in str(e).lower():
            logger.warning("pmxt_discovery: rate limited on query=%s, backing off", query)
            await asyncio.sleep(5.0)
            last_request_time[0] = time.monotonic()
            return []
        logger.error("pmxt_discovery: error fetching query=%s: %s", query, e)
        return []


async def discover_via_pmxt(
    mapping_store=None,
    *,
    queries: list[str] | None = None,
    max_per_query: int = _MAX_MARKETS_PER_QUERY,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Discover cross-platform market pairs using PMXT.

    Parameters
    ----------
    mapping_store:
        Optional mapping store with write_candidates() method.
        If None or dry_run=True, just returns discovered pairs.
    queries:
        List of search queries. Defaults to _DISCOVERY_QUERIES.
    max_per_query:
        Max markets to fetch per query per platform.
    dry_run:
        If True, don't write to store — just return results.

    Returns
    -------
    dict with keys:
        kalshi_total: int — total Kalshi markets fetched
        poly_total: int — total Polymarket markets fetched
        kalshi_fingerprinted: int — Kalshi markets that matched a parser
        poly_fingerprinted: int — Polymarket markets that matched a parser
        structural_matches: list[dict] — cross-platform matched pairs
        unmatched_kalshi: list[dict] — high-potential Kalshi markets without matches
        unmatched_poly: list[dict] — high-potential Poly markets without matches
    """
    try:
        import pmxt
    except ImportError:
        logger.error("pmxt not installed. Run: pip install pmxt")
        return {"error": "pmxt not installed"}

    from arbiter.mapping.event_fingerprint import (
        fingerprint_kalshi_market,
        fingerprint_polymarket_market,
    )

    queries = queries or _DISCOVERY_QUERIES
    last_req = [0.0]

    # ── Fetch from both platforms ────────────────────────────────────────────
    kalshi = pmxt.Kalshi()
    poly = pmxt.Polymarket()

    kalshi_markets: dict[str, Any] = {}  # market_id → (pmxt_market, our_dict)
    poly_markets: dict[str, Any] = {}  # slug → (pmxt_market, our_dict)

    for query in queries:
        if len(kalshi_markets) + len(poly_markets) >= _MAX_TOTAL_MARKETS:
            break

        # Fetch Kalshi
        k_results = await _fetch_with_rate_limit(
            kalshi.fetch_markets, query, max_per_query, last_req
        )
        for m in k_results:
            mid = getattr(m, "market_id", None)
            if mid and mid not in kalshi_markets:
                kalshi_markets[mid] = (m, _pmxt_market_to_kalshi_dict(m))

        # Fetch Polymarket
        p_results = await _fetch_with_rate_limit(
            poly.fetch_markets, query, max_per_query, last_req
        )
        for m in p_results:
            slug = getattr(m, "slug", None)
            if slug and slug not in poly_markets:
                poly_markets[slug] = (m, _pmxt_market_to_poly_dict(m))

        logger.debug(
            "pmxt_discovery: query=%s kalshi=%d poly=%d",
            query, len(kalshi_markets), len(poly_markets),
        )

    logger.info(
        "pmxt_discovery: fetched %d Kalshi, %d Polymarket markets",
        len(kalshi_markets), len(poly_markets),
    )

    # ── Fingerprint all markets ──────────────────────────────────────────────
    kalshi_fps: dict[str, list] = {}  # market_key → [(market_id, fingerprint, dict)]
    poly_fps: dict[str, list] = {}  # market_key → [(slug, fingerprint, dict)]

    k_fp_count = 0
    for mid, (pmxt_m, our_dict) in kalshi_markets.items():
        fp = fingerprint_kalshi_market(our_dict)
        if fp:
            k_fp_count += 1
            kalshi_fps.setdefault(fp.market_key, []).append((mid, fp, our_dict))

    p_fp_count = 0
    for slug, (pmxt_m, our_dict) in poly_markets.items():
        fp = fingerprint_polymarket_market(our_dict)
        if fp:
            p_fp_count += 1
            poly_fps.setdefault(fp.market_key, []).append((slug, fp, our_dict))

    logger.info(
        "pmxt_discovery: fingerprinted %d/%d Kalshi, %d/%d Polymarket",
        k_fp_count, len(kalshi_markets),
        p_fp_count, len(poly_markets),
    )

    # ── Find cross-platform structural matches ───────────────────────────────
    matched_keys = set(kalshi_fps.keys()) & set(poly_fps.keys())
    structural_matches = []

    for key in matched_keys:
        for k_mid, k_fp, k_dict in kalshi_fps[key]:
            for p_slug, p_fp, p_dict in poly_fps[key]:
                # Verify source matches (safety check)
                if k_fp.source != p_fp.source:
                    continue
                structural_matches.append({
                    "kalshi_ticker": k_mid,
                    "kalshi_title": k_dict.get("title", ""),
                    "polymarket_slug": p_slug,
                    "polymarket_question": p_dict.get("question", ""),
                    "category": k_fp.category,
                    "event_key": k_fp.event_key,
                    "market_key": key,
                    "source": "pmxt_discovery",
                    "structural_match": True,
                })

    logger.info("pmxt_discovery: found %d structural matches", len(structural_matches))

    # ── Identify unmatched high-potential markets ────────────────────────────
    unmatched_kalshi = [
        {"market_id": mid, "title": d.get("title", "")}
        for mid, (_, d) in kalshi_markets.items()
        if not fingerprint_kalshi_market(d)
        and any(kw in (d.get("title", "") or "").lower()
                for kw in ["price", "rate", "gdp", "election", "winner"])
    ]

    unmatched_poly = [
        {"slug": slug, "title": d.get("title", "")}
        for slug, (_, d) in poly_markets.items()
        if not fingerprint_polymarket_market(d)
        and any(kw in (d.get("title", "") or "").lower()
                for kw in ["price", "rate", "gdp", "election", "winner"])
    ]

    result = {
        "kalshi_total": len(kalshi_markets),
        "poly_total": len(poly_markets),
        "kalshi_fingerprinted": k_fp_count,
        "poly_fingerprinted": p_fp_count,
        "structural_matches": structural_matches,
        "unmatched_kalshi_high_potential": unmatched_kalshi[:50],
        "unmatched_poly_high_potential": unmatched_poly[:50],
    }

    # ── Write candidates to store if available ───────────────────────────────
    if mapping_store and structural_matches and not dry_run:
        try:
            candidates = []
            for match in structural_matches:
                candidates.append({
                    "kalshi_ticker": match["kalshi_ticker"],
                    "kalshi_title": match["kalshi_title"],
                    "poly_slug": match["polymarket_slug"],
                    "poly_question": match["polymarket_question"],
                    "score": 0.95,  # High base score for structural matches
                    "category": match["category"],
                    "source": "pmxt_discovery",
                    "structural_match": True,
                })
            written = await mapping_store.write_candidates(candidates)
            result["candidates_written"] = written
            logger.info("pmxt_discovery: wrote %d candidates to store", written)
        except Exception as e:
            logger.error("pmxt_discovery: failed to write candidates: %s", e)
            result["write_error"] = str(e)

    return result
