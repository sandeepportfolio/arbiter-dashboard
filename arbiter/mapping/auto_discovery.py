"""
Auto-discovery pipeline — pulls all live markets from both platforms,
scores candidate pairs, and writes them to the mapping store.

Rate-limited to budget_rps (default 2.0 r/s) during discovery via asyncio.sleep.
Returns the count of candidates written to the store.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from arbiter.config.settings import similarity_score

logger = logging.getLogger("arbiter.mapping.auto_discovery")


async def discover(
    kalshi_client,
    polymarket_us_client,
    mapping_store,
    budget_rps: float = 2.0,
) -> int:
    """Pull all live markets from both platforms, score candidate pairs, write candidates.

    Parameters
    ----------
    kalshi_client:
        A Kalshi client with a ``list_all_markets()`` async method that returns
        a list of market dicts (each with 'ticker' and 'title'/'subtitle' keys).
    polymarket_us_client:
        A Polymarket US client with a ``list_markets()`` async generator that
        yields market dicts (each with 'slug' and 'question' keys).
    mapping_store:
        A mapping store with a ``write_candidates(candidates)`` async method.
    budget_rps:
        Discovery rate limit in requests per second. The pipeline makes exactly
        2 API calls (one per platform), sleeping between them to stay within budget.

    Returns
    -------
    int
        Number of candidate pairs written to the mapping store.
    """
    request_interval = 1.0 / budget_rps

    # ── Pull Kalshi markets ────────────────────────────────────────────────────
    # Rate-limit sleep before each platform call so both sleeps count
    await asyncio.sleep(request_interval)
    t0 = time.monotonic()
    logger.info("auto_discovery: fetching Kalshi markets")
    kalshi_markets: list[dict] = await kalshi_client.list_all_markets()
    t1 = time.monotonic()
    logger.info("auto_discovery: got %d Kalshi markets in %.2fs", len(kalshi_markets), t1 - t0)

    # Rate-limit sleep between platform calls
    await asyncio.sleep(request_interval)

    # ── Pull Polymarket US markets ─────────────────────────────────────────────
    logger.info("auto_discovery: fetching Polymarket US markets")
    poly_markets: list[dict] = []
    async for market in polymarket_us_client.list_markets(purpose="discovery"):
        poly_markets.append(market)
    t2 = time.monotonic()
    logger.info("auto_discovery: got %d Polymarket markets in %.2fs", len(poly_markets), t2 - t1)

    if not kalshi_markets or not poly_markets:
        logger.info("auto_discovery: no markets on one or both platforms — no candidates")
        return 0

    # ── Score pairs ────────────────────────────────────────────────────────────
    candidates: list[dict] = []

    for km in kalshi_markets:
        k_text = km.get("title") or km.get("subtitle") or km.get("ticker") or ""
        if not k_text:
            continue

        for pm in poly_markets:
            p_text = pm.get("question") or pm.get("title") or pm.get("slug") or ""
            if not p_text:
                continue

            score = similarity_score(k_text, p_text)
            if score <= 0.0:
                continue

            candidates.append({
                "kalshi_ticker": km.get("ticker", ""),
                "kalshi_title": k_text,
                "poly_slug": pm.get("slug", ""),
                "poly_question": p_text,
                "score": score,
                "status": "candidate",
            })

    # Sort by score descending so highest-quality pairs appear first
    candidates.sort(key=lambda c: c["score"], reverse=True)

    logger.info(
        "auto_discovery: found %d candidate pairs from %d×%d cross-product",
        len(candidates),
        len(kalshi_markets),
        len(poly_markets),
    )

    # ── Write to store ─────────────────────────────────────────────────────────
    if candidates:
        await mapping_store.write_candidates(candidates)

    return len(candidates)
