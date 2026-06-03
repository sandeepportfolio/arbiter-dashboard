#!/usr/bin/env python3
"""Run the ForecastEx ↔ Kalshi cross-platform matcher.

Queries IBKR gateway for all ForecastEx contracts, matches them against
Kalshi markets with EXACT threshold/direction/period, and inserts verified
mappings into the arbiter_live database.

Usage::

    # Dry run (no DB writes):
    python scripts/run_fx_cross_platform_matcher.py --dry-run

    # Live run against arbiter_live:
    python scripts/run_fx_cross_platform_matcher.py

    # Skip quote validation (faster, for bulk discovery):
    python scripts/run_fx_cross_platform_matcher.py --skip-validate

    # JSON output for piping:
    python scripts/run_fx_cross_platform_matcher.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

# Ensure project root is on the path
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from arbiter.config.settings import load_config, ForecastExConfig
from arbiter.collectors.forecastex import ForecastExClient
from arbiter.collectors.kalshi import KalshiCollector
from arbiter.mapping.market_map import MarketMappingStore
from arbiter.mapping.fx_cross_platform_matcher import (
    FX_FAMILIES,
    run_cross_platform_matching,
)


async def main():
    parser = argparse.ArgumentParser(
        description="ForecastEx ↔ Kalshi cross-platform matcher",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print matches but do not write to DB",
    )
    parser.add_argument(
        "--skip-validate", action="store_true",
        help="Skip live quote validation (faster)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--families", nargs="*", default=None,
        help="Only match these FX family prefixes (e.g. FF CPIY UNR)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)-40s %(levelname)-8s %(message)s",
    )
    logger = logging.getLogger("fx_matcher.runner")

    # Load config
    cfg = load_config()

    # Determine database URL
    database_url = os.getenv(
        "DATABASE_URL",
        "postgresql://arbiter:arbiter_secret@localhost:5432/arbiter_live",
    )
    logger.info("Connecting to database: %s", database_url.split("@")[-1])

    # Initialize mapping store
    store = MarketMappingStore(database_url)
    await store.connect()
    await store.init_schema()

    # Initialize ForecastEx client
    fx_config = cfg.forecastex or ForecastExConfig()
    if not fx_config.enabled:
        logger.error("ForecastEx is not enabled — set FORECASTEX_ENABLED=true")
        sys.exit(1)

    fx_client = ForecastExClient(
        gateway_url=fx_config.gateway_url,
        account_id=fx_config.account_id,
        verify_ssl=fx_config.verify_ssl,
        paper_trading=fx_config.paper_trading,
    )

    # Initialize Kalshi collector
    kalshi_collector = KalshiCollector(cfg.kalshi)

    # Filter families if specified
    families = FX_FAMILIES
    if args.families:
        requested = set(f.upper() for f in args.families)
        families = tuple(f for f in FX_FAMILIES if f.fx_symbol_prefix in requested)
        missing = requested - {f.fx_symbol_prefix for f in families}
        if missing:
            logger.warning("Unknown family prefixes: %s", missing)

    logger.info(
        "Starting cross-platform matching for %d families: %s",
        len(families),
        [f.fx_symbol_prefix for f in families],
    )

    # Run the matcher
    try:
        summary = await run_cross_platform_matching(
            fx_client,
            kalshi_collector,
            store,
            families=families,
            dry_run=args.dry_run,
            validate_quotes=not args.skip_validate,
        )
    finally:
        await store.disconnect()
        if fx_client.session:
            await fx_client.session.close()

    # Output
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"ForecastEx ↔ Kalshi Cross-Platform Matcher Results")
        print(f"{'='*60}")
        print(f"FX contracts discovered:  {summary['fx_contracts_discovered']}")
        print(f"Kalshi markets found:     {summary['kalshi_markets_found']}")
        print(f"Matches (verified):       {summary['matches_before_validation']}")
        print(f"Mappings inserted:        {summary['mappings_inserted']}")
        print(f"Dry run:                  {summary['dry_run']}")
        print(f"Families searched:        {summary['families_searched']}")
        print()

        if summary["match_details"]:
            print(f"{'Canonical ID':<40} {'FX Symbol':<12} {'Kalshi':<25} {'Threshold':<10} {'Quality'}")
            print("-" * 100)
            for m in summary["match_details"]:
                print(
                    f"{m['canonical_id']:<40} "
                    f"{m['fx_symbol']:<12} "
                    f"{m['kalshi_ticker']:<25} "
                    f"{m['threshold']:<10} "
                    f"{m['quality']}"
                )
        else:
            print("No matches found.")

    return summary


if __name__ == "__main__":
    asyncio.run(main())
