"""
One-shot collector verification script.
Tests each platform collector against live API responses.
Safe: read-only calls only, no orders or state changes.

T-01-11: Logs only market IDs and prices, never API keys or auth tokens.
T-01-12: Validates field types (assert price >= 0) before using parsed values.
"""
import asyncio
import json
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger("verify")

from arbiter.config.settings import ArbiterConfig
from arbiter.utils.price_store import PriceStore


async def verify_kalshi(config):
    """Kalshi: requires auth (API key + RSA key)."""
    logger.info("=== Kalshi Collector ===")
    from arbiter.collectors.kalshi import KalshiCollector

    store = PriceStore(ttl=60)
    collector = KalshiCollector(config.kalshi, store)
    if not collector.auth.is_authenticated:
        logger.warning("  SKIP: Kalshi auth not configured (no API key/RSA key)")
        return None
    try:
        # KalshiCollector.fetch_markets() handles both market discovery and price extraction
        prices = await collector.fetch_markets()
        logger.info("Kalshi: %d markets fetched", len(prices))
        if prices:
            sample = prices[0]
            logger.info(
                "  Sample: %s yes=%.4f no=%.4f",
                sample.canonical_id,
                sample.yes_price,
                sample.no_price,
            )
            assert sample.yes_price >= 0, "yes_price must be >= 0"
            assert sample.no_price >= 0, "no_price must be >= 0"
            logger.info("  PASS: Kalshi schema OK")
        else:
            logger.warning("  WARN: No Kalshi markets returned (no events mapped or API issue)")
        return True
    except Exception as exc:
        logger.error("  FAIL: Kalshi: %s", exc)
        return False
    finally:
        await collector.stop()


async def verify_polymarket(config):
    """Polymarket: Gamma API (no auth) for discovery, CLOB (auth) for books."""
    logger.info("=== Polymarket Collector ===")
    from arbiter.collectors.polymarket import PolymarketCollector

    store = PriceStore(ttl=60)
    collector = PolymarketCollector(config.polymarket, store)
    try:
        # Step 1: Discover markets via Gamma API (no auth required)
        discovered = await collector.discover_markets()
        logger.info("Polymarket: discovered %d markets via Gamma API", len(discovered))

        # Step 2: If we have discovered markets, try to fetch prices via Gamma
        # (CLOB book fetches may also work without auth for read-only)
        if discovered:
            prices = await collector.fetch_gamma_prices()
            pm_prices = [p for p in prices if p.platform == "polymarket"]
            logger.info("Polymarket: %d price points fetched", len(pm_prices))
            if pm_prices:
                sample = pm_prices[0]
                logger.info(
                    "  Sample: %s yes=%.4f no=%.4f fee=%.4f",
                    sample.canonical_id,
                    sample.yes_price,
                    sample.no_price,
                    sample.fee_rate,
                )
                assert sample.yes_price >= 0, "yes_price must be >= 0"
                assert sample.no_price >= 0, "no_price must be >= 0"
                assert sample.fee_rate >= 0, "fee_rate must be >= 0"
                logger.info("  PASS: Polymarket schema OK")
            else:
                logger.warning("  WARN: No Polymarket price points returned")
        else:
            logger.warning("  WARN: No Polymarket markets discovered (no slugs mapped or API issue)")
        return True
    except Exception as exc:
        logger.error("  FAIL: Polymarket: %s", exc)
        return False
    finally:
        await collector.stop()


async def main():
    config = ArbiterConfig()
    results = {}

    results["kalshi"] = await verify_kalshi(config)
    results["polymarket"] = await verify_polymarket(config)

    logger.info("")
    logger.info("=== RESULTS ===")
    for platform, result in results.items():
        status = "PASS" if result is True else ("SKIP" if result is None else "FAIL")
        logger.info("  %s: %s", platform, status)

    failures = [k for k, v in results.items() if v is False]
    if failures:
        logger.error("FAILED platforms: %s", ", ".join(failures))
        sys.exit(1)
    else:
        logger.info("All collectors verified (or skipped due to missing auth)")


if __name__ == "__main__":
    asyncio.run(main())
