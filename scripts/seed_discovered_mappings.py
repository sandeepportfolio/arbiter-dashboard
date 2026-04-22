"""
Seed loader: reads data/discovered_mappings.json and writes confirmed
(or candidate) mappings into the canonical map store (settings.MARKET_MAP).

Writes to the in-memory MARKET_MAP only (no DB required). Suitable for
bootstrapping the runtime mapping table before the DB is connected.

Usage:
    python scripts/seed_discovered_mappings.py [--input data/discovered_mappings.json] [--min-score 0.80] [--status confirmed]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("arbiter.seed_discovered_mappings")


def load_mappings(input_path: str) -> list[dict]:
    path = Path(input_path)
    if not path.exists():
        logger.error("Input file not found: %s", path)
        sys.exit(1)
    with path.open() as f:
        data = json.load(f)
    logger.info("Loaded %d mapping candidates from %s", len(data), path)
    return data


def seed_to_market_map(
    mappings: list[dict],
    *,
    min_score: float = 0.80,
    status: str = "candidate",
) -> int:
    """Write mappings above min_score into the runtime MARKET_MAP."""
    import sys
    import os

    # Allow running from repo root without installing the package
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from arbiter.config.settings import upsert_runtime_market_mapping  # type: ignore

    written = 0
    skipped = 0
    for m in mappings:
        score = float(m.get("similarity_score") or 0.0)
        if score < min_score:
            skipped += 1
            continue

        kalshi_ticker = str(m.get("kalshi_ticker") or "").strip()
        poly_slug = str(m.get("polymarket_slug") or "").strip()
        if not kalshi_ticker or not poly_slug:
            skipped += 1
            continue

        canonical_id = f"EMB_{kalshi_ticker[:20]}_{poly_slug[:20]}".upper().replace("-", "_")
        payload = {
            "description": str(m.get("polymarket_question") or m.get("kalshi_title") or canonical_id),
            "status": status,
            "allow_auto_trade": False,
            "kalshi": kalshi_ticker,
            "polymarket": poly_slug,
            "polymarket_question": str(m.get("polymarket_question") or ""),
            "mapping_score": score,
            "confidence": score,
            "notes": f"Embedding-discovered (similarity={score:.4f}). Needs operator review before trading.",
            "resolution_match_status": "pending_operator_review",
            "aliases": [
                str(m.get("kalshi_title") or ""),
                str(m.get("polymarket_question") or ""),
            ],
            "tags": [str(m.get("category") or "")],
            "kalshi_expiry": str(m.get("kalshi_expiry") or ""),
            "polymarket_end_date": str(m.get("polymarket_end_date") or ""),
        }

        upsert_runtime_market_mapping(canonical_id, payload)
        written += 1
        logger.debug("Seeded %s <-> %s (%.3f)", kalshi_ticker, poly_slug, score)

    logger.info("Seeded %d mappings (skipped %d below %.2f)", written, skipped, min_score)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed discovered mappings into runtime MARKET_MAP")
    parser.add_argument("--input", default="data/discovered_mappings.json")
    parser.add_argument("--min-score", type=float, default=0.80,
                        help="Only seed mappings above this similarity (default: 0.80)")
    parser.add_argument("--status", default="candidate",
                        choices=["candidate", "review", "confirmed"],
                        help="Status to assign seeded mappings (default: candidate)")
    parser.add_argument("--dump", action="store_true",
                        help="Print seeded mappings as JSON to stdout")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )

    mappings = load_mappings(args.input)
    above = [m for m in mappings if float(m.get("similarity_score") or 0) >= args.min_score]
    logger.info("%d / %d mappings above threshold %.2f", len(above), len(mappings), args.min_score)

    written = seed_to_market_map(mappings, min_score=args.min_score, status=args.status)

    if args.dump:
        print(json.dumps(above[:written], indent=2))

    print(f"\nSeeded {written} mappings with status='{args.status}'")
    print("Run the arbiter API to see them in the dashboard mapping table.")


if __name__ == "__main__":
    main()
