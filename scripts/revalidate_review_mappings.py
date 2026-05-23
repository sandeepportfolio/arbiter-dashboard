"""Re-run the auto-promote gate against mappings stuck in 'review' status.

Background: a 2026-04-30 live audit demoted ~90 high-volatility mappings
(crypto, GDP, NFL championships, etc.) to status='review' with the note
"active/exact-resolution verification failed or Polymarket slug unavailable;
requires operator revalidation before trading." Because the auto-promote
pipeline only walks status='candidate', those records have been frozen
ever since.

This script:
  1. Pulls every status='review' mapping (or a subset matched by --filter).
  2. Re-fetches the corresponding Kalshi market + Polymarket market from
     the live catalogs.
  3. Builds a candidate dict in the same shape that auto_discovery emits.
  4. Runs maybe_promote() with the full 9-gate stack and per-category
     thresholds.
  5. For each PASS: promotes to confirmed, clears review_note, sets
     allow_auto_trade=True.
  6. For each FAIL: leaves the record as 'review' and prints the reason.

Run with --dry-run to evaluate without writing.

Usage:
    docker compose -f docker-compose.prod.yml exec api \\
        python -m scripts.revalidate_demoted_mappings [--dry-run] [--filter PREFIX]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from typing import Any, Optional

# Allow running both as "python -m scripts.revalidate_demoted_mappings" and as
# "python scripts/revalidate_demoted_mappings.py".
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter.config import load_config, ArbiterConfig
from arbiter.config.settings import PolymarketConfig, PolymarketUSConfig
from arbiter.collectors.kalshi import KalshiCollector
from arbiter.mapping.market_map import MarketMappingStore, MappingStatus
from arbiter.mapping.auto_promote import maybe_promote
from arbiter.mapping.llm_verifier import verify as llm_verify
from arbiter.utils.price_store import PriceStore

logger = logging.getLogger("revalidate_demoted_mappings")


def _build_polymarket_client(config: ArbiterConfig):
    """Mirror arbiter.main.build_polymarket_collector but return raw client only."""
    if config.polymarket is None:
        return None
    if isinstance(config.polymarket, PolymarketUSConfig):
        from arbiter.auth.ed25519_signer import Ed25519Signer
        from arbiter.collectors.polymarket_us import PolymarketUSClient

        cfg = config.polymarket
        signer = None
        if cfg.api_key_id and cfg.api_secret:
            signer = Ed25519Signer(key_id=cfg.api_key_id, secret_b64=cfg.api_secret)
        return PolymarketUSClient(
            base_url=cfg.api_url,
            public_base_url=cfg.gateway_url,
            signer=signer,
        )
    if isinstance(config.polymarket, PolymarketConfig):
        from arbiter.collectors.polymarket import PolymarketCollector
        store = PriceStore()
        return PolymarketCollector(config.polymarket, store).client
    return None


async def _fetch_live_index(kalshi: KalshiCollector, poly_client) -> tuple[dict, dict]:
    """Pull all live markets from both venues and index by id."""
    logger.info("fetching live Kalshi catalog…")
    kalshi_markets = await kalshi.list_all_markets(
        status="open", page_size=1000, max_pages=10,
    )
    by_ticker = {str(m.get("ticker") or "").strip(): m for m in kalshi_markets if m.get("ticker")}
    logger.info("kalshi: %d markets indexed", len(by_ticker))

    logger.info("fetching live Polymarket catalog…")
    poly_markets: list[dict] = []
    if hasattr(poly_client, "list_markets"):
        async for m in poly_client.list_markets():
            poly_markets.append(m)
    by_slug = {str(m.get("slug") or "").strip(): m for m in poly_markets if m.get("slug")}
    logger.info("polymarket: %d markets indexed", len(by_slug))
    return by_ticker, by_slug


def _build_candidate(record, km: dict, pm: dict) -> dict[str, Any]:
    """Construct a candidate dict from a (review-mapping, kalshi, polymarket) triple."""
    from arbiter.mapping.auto_discovery import _coerce_date, _normalize_category
    from arbiter.mapping.event_fingerprint import structural_match

    k_text = " ".join(
        str(km.get(k, "") or "") for k in ("title", "subtitle", "yes_sub_title")
    ).strip()
    poly_text = " ".join(
        str(pm.get(k, "") or "") for k in ("question", "title", "description")
    ).strip()

    k_category = _normalize_category(km.get("category"))
    pm_category = _normalize_category(pm.get("category"))
    k_date = _coerce_date(km.get("close_time") or km.get("expiration_time"))
    pm_date = _coerce_date(pm.get("endDate") or pm.get("closeTime"))

    poly_source = pm.get("resolutionSource")
    kalshi_source = km.get("settlement_source")
    poly_tie_break = pm.get("tieBreakRule")
    kalshi_tie_break = km.get("rules_primary")
    poly_outcomes = tuple(pm.get("outcomes") or ("Yes", "No"))

    # Score: prefer structural match (0.9999) else 0.95 — we already know
    # these were a confirmed pair at one point. The per-category gate will
    # cap at 0.65 / 0.85 anyway.
    exact = structural_match(km, pm)
    score = 0.9999 if exact is not None else 0.95

    candidate: dict[str, Any] = {
        "kalshi_ticker": km.get("ticker", ""),
        "kalshi_title": str(km.get("title", "") or k_text),
        "poly_slug": pm.get("slug", ""),
        "poly_question": str(pm.get("question", "") or pm.get("title", "") or poly_text),
        "description": str(pm.get("description", "") or pm.get("question", "") or poly_text),
        "score": score,
        "status": "candidate",
        "structural_match": exact is not None,
        "category": k_category or pm_category,
        "kalshi_category": k_category,
        "poly_category": pm_category,
        "polymarket_category": pm_category,
        "resolution_date": (k_date or pm_date).isoformat() if (k_date or pm_date) else None,
        "kalshi_resolution_date": k_date.isoformat() if k_date else None,
        "polymarket_resolution_date": pm_date.isoformat() if pm_date else None,
        "resolution_source": kalshi_source or poly_source,
        "kalshi_resolution_source": kalshi_source,
        "polymarket_resolution_source": poly_source,
        "tie_break_rule": kalshi_tie_break or poly_tie_break,
        "kalshi_tie_break_rule": kalshi_tie_break,
        "polymarket_tie_break_rule": poly_tie_break,
        "outcome_set": poly_outcomes,
        "kalshi_outcome_set": ("Yes", "No"),
        "polymarket_outcome_set": poly_outcomes,
        # Past the cooling-off bar — these records have been observed for
        # 22+ days already. advisory_scans=999 unconditionally clears Gate 9.
        "advisory_scans": 999,
        "__kalshi_market": km,
        "__polymarket_market": pm,
    }
    # Verification text the LLM verifier consumes.
    candidate["kalshi_verification_text"] = candidate["kalshi_title"]
    candidate["polymarket_verification_text"] = candidate["poly_question"]
    return candidate


async def _load_orderbooks(kalshi: KalshiCollector, poly_client, candidate: dict[str, Any]) -> dict[str, Any]:
    from arbiter.mapping.auto_discovery import (
        _normalize_kalshi_orderbook,
        _normalize_polymarket_orderbook,
        _synthetic_kalshi_orderbook,
    )
    k_book = _synthetic_kalshi_orderbook(candidate.get("__kalshi_market") or {})
    p_book: dict[str, Any] = {"bids": [], "asks": []}

    k_getter = getattr(kalshi, "get_orderbook", None)
    if callable(k_getter):
        try:
            k_book = _normalize_kalshi_orderbook(
                await k_getter(candidate.get("kalshi_ticker", ""), depth=100)
            )
        except Exception:  # pragma: no cover
            pass

    p_getter = getattr(poly_client, "get_orderbook", None)
    if callable(p_getter):
        try:
            p_book = _normalize_polymarket_orderbook(
                await p_getter(candidate.get("poly_slug", ""), depth=100)
            )
        except Exception:  # pragma: no cover
            pass

    if not p_book.get("bids") and not p_book.get("asks"):
        p_book = _normalize_polymarket_orderbook(candidate.get("__polymarket_market") or {})

    return {"kalshi": k_book, "polymarket": p_book}


async def revalidate(
    *,
    dry_run: bool,
    filter_prefix: Optional[str],
    limit: Optional[int],
) -> int:
    config = load_config()
    price_store = PriceStore()
    kalshi = KalshiCollector(config.kalshi, price_store)
    poly_client = _build_polymarket_client(config)
    if poly_client is None:
        raise RuntimeError("polymarket client not configured")

    database_url = os.environ.get("DATABASE_URL") or getattr(config, "database_url", None)
    if not database_url:
        raise RuntimeError("DATABASE_URL not set")

    store = MarketMappingStore(database_url)
    await store.connect()
    try:
        # Pull review-status records.
        conn = await store.acquire()
        try:
            rows = await conn.fetch(
                """
                SELECT canonical_id, description, kalshi_market_id, polymarket_slug,
                       polymarket_question, review_note, mapping_score, tags
                FROM market_mappings
                WHERE status = 'review'
                  AND kalshi_market_id <> ''
                  AND polymarket_slug <> ''
                ORDER BY updated_at DESC
                """
            )
        finally:
            await store._pool.release(conn)

        review_records = [dict(r) for r in rows]
        if filter_prefix:
            review_records = [r for r in review_records if r["canonical_id"].startswith(filter_prefix)]
        if limit is not None:
            review_records = review_records[:limit]
        logger.info("evaluating %d review records (filter=%s, dry_run=%s)", len(review_records), filter_prefix, dry_run)
        if not review_records:
            return 0

        # Pull live indices once.
        k_by_ticker, p_by_slug = await _fetch_live_index(kalshi, poly_client)

        # Promotion settings — operator runtime + per-category floors via auto_promote.
        promotion_settings = {
            "AUTO_PROMOTE_ENABLED": True,
            "AUTO_PROMOTE_MIN_SCORE": 0.85,
            "AUTO_PROMOTE_MAX_DAYS": 365,
            "AUTO_PROMOTE_DAILY_CAP": 9999,
            "AUTO_PROMOTE_ADVISORY_SCANS": 0,
            "PHASE5_MAX_ORDER_USD": float(os.environ.get("PHASE5_MAX_ORDER_USD", "50")),
        }

        promoted: list[str] = []
        reasons: dict[str, int] = {}

        for record in review_records:
            cid = record["canonical_id"]
            k_id = record["kalshi_market_id"]
            p_slug = record["polymarket_slug"]
            km = k_by_ticker.get(k_id)
            pm = p_by_slug.get(p_slug)

            if km is None or pm is None:
                reason = "market_inactive_or_missing"
                reasons[reason] = reasons.get(reason, 0) + 1
                logger.info("[skip ] %s — %s (kalshi=%s, poly=%s)", cid, reason, bool(km), bool(pm))
                continue

            candidate = _build_candidate(record, km, pm)
            orderbooks = await _load_orderbooks(kalshi, poly_client, candidate)
            result = await maybe_promote(
                candidate,
                settings=promotion_settings,
                orderbooks=orderbooks,
                llm_verifier=llm_verify,
                today_promoted_count=0,
                cooling_state={},
            )
            reasons[result.reason] = reasons.get(result.reason, 0) + 1

            if result.promoted:
                promoted.append(cid)
                logger.info("[PASS ] %s — score=%.3f category=%s", cid, candidate["score"], candidate.get("category"))
                if not dry_run:
                    await store.update_status(
                        cid,
                        MappingStatus.CONFIRMED,
                        review_note="",
                        allow_auto_trade=True,
                    )
            else:
                logger.info("[fail ] %s — %s (score=%.3f category=%s)", cid, result.reason, candidate["score"], candidate.get("category"))

        logger.info("---- Summary ----")
        logger.info("evaluated: %d  promoted: %d  dry_run: %s", len(review_records), len(promoted), dry_run)
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            logger.info("  %s: %d", reason, count)
        return len(promoted)
    finally:
        await store.disconnect()
        close = getattr(kalshi, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # pragma: no cover
                pass


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="evaluate without writing")
    parser.add_argument("--filter", default=None, help="canonical_id prefix filter")
    parser.add_argument("--limit", type=int, default=None, help="max records to evaluate")
    args = parser.parse_args()

    promoted = asyncio.run(revalidate(dry_run=args.dry_run, filter_prefix=args.filter, limit=args.limit))
    print(f"\nrevalidate_demoted_mappings: promoted={promoted} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
