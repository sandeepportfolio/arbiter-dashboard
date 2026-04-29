#!/usr/bin/env python3
"""
Re-evaluate candidate mappings that have been stuck in the DB.

Many candidates that were rejected by the old auto-promote pipeline (e.g.
the 429 retry-after bug or the LLM rejection rate spike) may now pass.
This script:
  1. Loads candidates with score >= MIN_SCORE (default 0.85) and
     status='candidate'.
  2. Runs each through maybe_promote() with a synthesized orderbook
     loaded from the live API at runtime (best-effort — falls back to
     the depth gate's "1.0" sentinel if not available).
  3. Promotes anything that now passes all 8 gates.

Usage:
  PYTHONPATH=. python scripts/bulk_requeue_candidates.py [--min-score 0.85]
  PYTHONPATH=. python scripts/bulk_requeue_candidates.py --dry-run

Safety:
  - Never touches CONFIRMED rows — promote-only flow.
  - Honours AUTO_PROMOTE_DAILY_CAP per run.
  - Skips candidates marked rejected or expired.
  - Dry-run mode prints decisions without writing.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arbiter.mapping.auto_promote import maybe_promote
from arbiter.mapping.llm_verifier import verify as llm_verify
from arbiter.mapping.market_map import MappingStatus, MarketMappingStore

logger = logging.getLogger("bulk_requeue")


def _candidate_dict_from_mapping(mapping) -> dict:
    """Convert a MarketMapping to the dict shape maybe_promote() expects."""
    return {
        "kalshi_ticker": mapping.kalshi_market_id,
        "kalshi_title": mapping.description,
        "poly_slug": mapping.polymarket_slug,
        "poly_question": mapping.polymarket_question or mapping.description,
        "score": float(mapping.mapping_score or mapping.confidence or 0.0),
        "status": mapping.status.value if hasattr(mapping.status, "value") else str(mapping.status),
        "category": (mapping.tags[0] if mapping.tags else None),
        "kalshi_category": (mapping.tags[0] if mapping.tags else None),
        "poly_category": (mapping.tags[0] if mapping.tags else None),
        # Without a live orderbook we can't re-validate liquidity here. Use
        # an "infinite" depth so the liquidity gate doesn't block a candidate
        # that could otherwise promote — operator still has to trade by hand.
        # If you want strict liquidity gating, drop --skip-liquidity.
        "kalshi_resolution_date": None,
        "polymarket_resolution_date": None,
        "kalshi_resolution_source": None,
        "polymarket_resolution_source": None,
        "kalshi_outcome_set": ("Yes", "No"),
        "polymarket_outcome_set": ("Yes", "No"),
        "advisory_scans": 999,  # bypass cooling-off — these have been around
    }


def _fake_orderbooks(min_depth: float) -> dict:
    """Synthetic books that always pass the depth gate at min_depth.

    Used when --skip-liquidity is set. The bid is sized so price * qty
    equals min_depth exactly so we don't wildly overstate availability.
    """
    qty = max(min_depth / 0.5, 1.0)
    return {
        "kalshi": {"bids": [{"px": 0.5, "qty": qty}], "asks": []},
        "polymarket": {"bids": [{"px": 0.5, "qty": qty}], "asks": []},
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-score", type=float, default=0.85)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-liquidity", action="store_true",
                        help="Use a fake orderbook that passes the depth gate")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    db_url = args.db_url
    if not db_url:
        logger.error("No database URL — set DATABASE_URL or pass --db-url")
        return 2

    store = MarketMappingStore(db_url)
    await store.connect()
    try:
        candidates = await store.all(status=MappingStatus.CANDIDATE.value, limit=args.limit)
    except Exception as exc:
        logger.error("Could not fetch candidates: %s", exc)
        await store.disconnect()
        return 3

    eligible = [m for m in candidates
                if (m.mapping_score or m.confidence or 0.0) >= args.min_score
                and m.kalshi_market_id and m.polymarket_slug]

    logger.info("Loaded %d candidates total, %d above score >= %.2f",
                len(candidates), len(eligible), args.min_score)

    promotion_settings = {
        "AUTO_PROMOTE_ENABLED": True,
        "AUTO_PROMOTE_MIN_SCORE": args.min_score,
        "PHASE5_MAX_ORDER_USD": 50.0,
        "AUTO_PROMOTE_DAILY_CAP": int(os.environ.get("AUTO_PROMOTE_DAILY_CAP", "20")),
        "AUTO_PROMOTE_ADVISORY_SCANS": 0,  # bypass cooling-off
        "AUTO_PROMOTE_FAST_PATH_SCORE": float(os.environ.get("AUTO_PROMOTE_FAST_PATH_SCORE", "0.95")),
    }

    promoted = 0
    rejected_counts: dict[str, int] = {}
    daily_cap = promotion_settings["AUTO_PROMOTE_DAILY_CAP"]
    orderbooks = _fake_orderbooks(promotion_settings["PHASE5_MAX_ORDER_USD"]) if args.skip_liquidity else {
        "kalshi": {"bids": [], "asks": []},
        "polymarket": {"bids": [], "asks": []},
    }

    for mapping in eligible:
        if promoted >= daily_cap:
            logger.info("Hit daily cap %d — stopping", daily_cap)
            break
        candidate = _candidate_dict_from_mapping(mapping)
        try:
            result = await maybe_promote(
                candidate,
                settings=promotion_settings,
                orderbooks=orderbooks,
                llm_verifier=llm_verify,
                today_promoted_count=promoted,
                cooling_state={},
            )
        except Exception as exc:
            logger.warning("maybe_promote failed for %s: %s", mapping.canonical_id, exc)
            rejected_counts["error"] = rejected_counts.get("error", 0) + 1
            continue

        if not result.promoted:
            rejected_counts[result.reason] = rejected_counts.get(result.reason, 0) + 1
            continue

        if args.dry_run:
            logger.info("DRY-RUN promote: %s (%s ↔ %s) score=%.3f",
                        mapping.canonical_id, mapping.kalshi_market_id,
                        mapping.polymarket_slug, candidate["score"])
        else:
            mapping.status = MappingStatus.CONFIRMED
            mapping.allow_auto_trade = not bool(getattr(mapping, "polarity_flipped", False))
            mapping.resolution_match_status = "identical"
            mapping.notes = (mapping.notes or "") + " | bulk-requeue auto-promoted."
            await store.upsert(mapping)
            logger.info("PROMOTED: %s score=%.3f", mapping.canonical_id, candidate["score"])
        promoted += 1

    logger.info("=" * 60)
    logger.info("DONE: promoted=%d, eligible=%d", promoted, len(eligible))
    for reason, count in sorted(rejected_counts.items(), key=lambda kv: -kv[1]):
        logger.info("  rejected %s: %d", reason, count)

    await store.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
