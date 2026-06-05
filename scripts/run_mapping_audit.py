#!/usr/bin/env python3
"""
Phase 1: Audit all confirmed mappings against live platform APIs.

Runs inside Docker via:
    docker exec arbiter-api-prod python3 /app/scripts/run_mapping_audit.py

Checks every confirmed mapping against Kalshi, Polymarket, and ForecastEx.
Marks dead/expired mappings as 'review'. Prints a full report.
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

# Add the app root so imports work inside Docker
sys.path.insert(0, "/app")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("mapping_audit")


async def check_kalshi_market(session, base_url, auth, rate_limiter, ticker: str) -> dict:
    """Check if a Kalshi market ticker is live."""
    result = {"platform": "kalshi", "ticker": ticker, "exists": False, "is_open": False, "is_expired": False, "has_quotes": False, "error": ""}
    if not ticker:
        return result
    try:
        await rate_limiter.acquire()
        headers = {"Accept": "application/json"}
        if auth.is_authenticated:
            headers.update(auth.get_headers("GET", "/trade-api/v2/markets"))
        async with session.get(
            f"{base_url}/markets",
            params={"tickers": ticker, "limit": "1"},
            headers=headers,
        ) as resp:
            if resp.status == 429:
                retry_after = float(resp.headers.get("Retry-After", "5"))
                await asyncio.sleep(retry_after)
                result["error"] = "rate_limited"
                return result
            if resp.status >= 400:
                result["error"] = f"HTTP {resp.status}"
                return result
            data = await resp.json()
        markets = data.get("markets", [])
        if not markets:
            return result
        m = markets[0]
        result["exists"] = True
        status = str(m.get("status", "")).lower()
        result["is_open"] = status in ("open", "active")
        result["is_expired"] = status in ("closed", "settled", "finalized", "ceased_trading")
        yes_ask = float(m.get("yes_ask", 0) or 0)
        no_ask = float(m.get("no_ask", 0) or 0)
        last_price = float(m.get("last_price", 0) or 0)
        result["has_quotes"] = yes_ask > 0 or no_ask > 0 or last_price > 0
        result["volume"] = float(m.get("volume", 0) or 0)
        result["kalshi_status"] = status
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def check_polymarket(session, slug: str) -> dict:
    """Check if a Polymarket slug is live."""
    result = {"platform": "polymarket", "slug": slug, "exists": False, "is_open": False, "is_expired": False, "has_quotes": False, "error": ""}
    if not slug:
        return result
    try:
        import aiohttp
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug, "limit": "1"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 429:
                result["error"] = "rate_limited"
                return result
            if resp.status >= 400:
                result["error"] = f"HTTP {resp.status}"
                return result
            data = await resp.json()

        if not data:
            return result
        m = data[0] if isinstance(data, list) else data
        result["exists"] = True
        result["is_open"] = bool(m.get("active", False)) and not bool(m.get("closed", True))
        result["is_expired"] = bool(m.get("closed", False)) or bool(m.get("archived", False))
        result["has_quotes"] = bool(m.get("bestAsk") or m.get("bestBid") or m.get("outcomePrices"))
        result["volume"] = float(m.get("volume", 0) or 0)
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def check_forecastex(fx_client, conid: str) -> dict:
    """Check if a ForecastEx conid returns data from IBKR."""
    result = {"platform": "forecastex", "conid": conid, "exists": False, "is_open": False, "has_quotes": False, "error": ""}
    if not conid or not fx_client:
        result["error"] = "no client or empty conid"
        return result
    try:
        if hasattr(fx_client, "live_rate_limiter"):
            await fx_client.live_rate_limiter.acquire()
        snapshot = await fx_client.market_snapshot(conid)
        if snapshot is None:
            return result
        result["exists"] = True
        if isinstance(snapshot, list) and snapshot:
            snap = snapshot[0]
        elif isinstance(snapshot, dict):
            snap = snapshot
        else:
            return result
        bid = float(snap.get("84", 0) or snap.get("bid", 0) or 0)
        ask = float(snap.get("86", 0) or snap.get("ask", 0) or 0)
        last = float(snap.get("31", 0) or snap.get("last", 0) or 0)
        result["has_quotes"] = bid > 0 or ask > 0 or last > 0
        result["is_open"] = result["has_quotes"]
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


async def main():
    import aiohttp
    import asyncpg

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=5)

    # Fetch all confirmed mappings
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT canonical_id, kalshi_market_id, polymarket_slug, "
            "forecastex_contract_id, mapping_score, allow_auto_trade, tags "
            "FROM market_mappings WHERE status = 'confirmed' ORDER BY canonical_id"
        )

    print(f"\n{'='*80}")
    print(f"MAPPING AUDIT: {len(rows)} confirmed mappings")
    print(f"{'='*80}\n")

    # Set up Kalshi auth
    from arbiter.config.settings import KalshiConfig
    from arbiter.collectors.kalshi import KalshiAuth
    from arbiter.utils.retry import RateLimiter

    kalshi_config = KalshiConfig(
        base_url=os.environ.get("KALSHI_API_URL", "https://api.elections.kalshi.com/trade-api/v2"),
        api_key_id=os.environ.get("KALSHI_API_KEY_ID", ""),
        private_key_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", ""),
        poll_interval=10.0,
    )
    auth = KalshiAuth(kalshi_config.api_key_id, kalshi_config.private_key_path)
    rate_limiter = RateLimiter("audit_kalshi", max_requests=6, window_seconds=1.0)

    # Set up ForecastEx client if available
    fx_client = None
    try:
        from arbiter.collectors.forecastex import ForecastExClient
        gateway_url = os.environ.get("IBKR_GATEWAY_URL", "")
        account_id = os.environ.get("IBKR_ACCOUNT_ID", "")
        if gateway_url and account_id:
            fx_client = ForecastExClient(
                gateway_url=gateway_url,
                account_id=account_id,
            )
            logger.info("ForecastEx client initialized: %s", gateway_url)
    except Exception as e:
        logger.warning("ForecastEx client not available: %s", e)

    # Results tracking
    stats = {
        "total": len(rows),
        "kalshi_live": 0, "kalshi_dead": 0, "kalshi_expired": 0, "kalshi_error": 0, "kalshi_missing": 0,
        "poly_live": 0, "poly_dead": 0, "poly_expired": 0, "poly_error": 0, "poly_missing": 0,
        "fx_live": 0, "fx_dead": 0, "fx_error": 0, "fx_missing": 0,
        "fully_live": 0, "partially_live": 0, "fully_dead": 0,
        "to_expire": [],
        "to_review": [],
    }

    async with aiohttp.ClientSession() as session:
        for i, row in enumerate(rows):
            cid = row["canonical_id"]
            k_ticker = row["kalshi_market_id"] or ""
            p_slug = row["polymarket_slug"] or ""
            fx_conid = row["forecastex_contract_id"] or ""

            platforms_live = 0
            platforms_dead = 0
            platforms_checked = 0

            # Check Kalshi
            if k_ticker:
                kr = await check_kalshi_market(session, kalshi_config.base_url, auth, rate_limiter, k_ticker)
                platforms_checked += 1
                if kr["error"]:
                    stats["kalshi_error"] += 1
                elif kr["is_expired"]:
                    stats["kalshi_expired"] += 1
                    platforms_dead += 1
                elif kr["exists"] and kr["is_open"]:
                    stats["kalshi_live"] += 1
                    platforms_live += 1
                else:
                    stats["kalshi_dead"] += 1
                    platforms_dead += 1
            else:
                stats["kalshi_missing"] += 1

            # Check Polymarket (rate-limit 4/sec)
            if p_slug:
                await asyncio.sleep(0.25)
                pr = await check_polymarket(session, p_slug)
                platforms_checked += 1
                if pr["error"]:
                    stats["poly_error"] += 1
                elif pr["is_expired"]:
                    stats["poly_expired"] += 1
                    platforms_dead += 1
                elif pr["exists"] and pr["is_open"]:
                    stats["poly_live"] += 1
                    platforms_live += 1
                else:
                    stats["poly_dead"] += 1
                    platforms_dead += 1
            else:
                stats["poly_missing"] += 1

            # Check ForecastEx (rate-limit 2/sec)
            if fx_conid and fx_client:
                await asyncio.sleep(0.5)
                fxr = await check_forecastex(fx_client, fx_conid)
                platforms_checked += 1
                if fxr["error"]:
                    stats["fx_error"] += 1
                elif fxr["exists"] and fxr["is_open"]:
                    stats["fx_live"] += 1
                    platforms_live += 1
                else:
                    stats["fx_dead"] += 1
                    platforms_dead += 1
            elif fx_conid:
                stats["fx_missing"] += 1

            # Classify
            if platforms_live == platforms_checked and platforms_checked > 0:
                stats["fully_live"] += 1
            elif platforms_live > 0:
                stats["partially_live"] += 1
            elif platforms_checked > 0:
                stats["fully_dead"] += 1
                stats["to_expire"].append(cid)

            # Log progress every 50
            if (i + 1) % 50 == 0:
                logger.info("Progress: %d/%d checked", i + 1, len(rows))

    # Print report
    print(f"\n{'='*80}")
    print("AUDIT RESULTS")
    print(f"{'='*80}")
    print(f"\nTotal confirmed mappings: {stats['total']}")
    print(f"\n--- Kalshi ---")
    print(f"  Live: {stats['kalshi_live']}")
    print(f"  Expired: {stats['kalshi_expired']}")
    print(f"  Dead: {stats['kalshi_dead']}")
    print(f"  Error: {stats['kalshi_error']}")
    print(f"  No ticker: {stats['kalshi_missing']}")
    print(f"\n--- Polymarket ---")
    print(f"  Live: {stats['poly_live']}")
    print(f"  Expired: {stats['poly_expired']}")
    print(f"  Dead: {stats['poly_dead']}")
    print(f"  Error: {stats['poly_error']}")
    print(f"  No slug: {stats['poly_missing']}")
    print(f"\n--- ForecastEx ---")
    print(f"  Live: {stats['fx_live']}")
    print(f"  Dead: {stats['fx_dead']}")
    print(f"  Error: {stats['fx_error']}")
    print(f"  No conid/client: {stats['fx_missing']}")
    print(f"\n--- Overall ---")
    print(f"  Fully live (all platforms): {stats['fully_live']}")
    print(f"  Partially live: {stats['partially_live']}")
    print(f"  Fully dead (should expire): {stats['fully_dead']}")

    if stats["to_expire"]:
        print(f"\n--- SHOULD EXPIRE ({len(stats['to_expire'])}) ---")
        for cid in stats["to_expire"][:30]:
            print(f"  {cid}")
        if len(stats["to_expire"]) > 30:
            print(f"  ... and {len(stats['to_expire']) - 30} more")

    # Optionally auto-expire dead mappings
    if os.environ.get("AUTO_EXPIRE", "").lower() == "true" and stats["to_expire"]:
        print(f"\n--- AUTO-EXPIRING {len(stats['to_expire'])} dead mappings ---")
        async with pool.acquire() as conn:
            for cid in stats["to_expire"]:
                await conn.execute(
                    "UPDATE market_mappings SET status = 'expired', "
                    "allow_auto_trade = FALSE, "
                    "review_note = '[audit-script] expired: all platforms dead/settled', "
                    "updated_at = NOW() "
                    "WHERE canonical_id = $1 AND status = 'confirmed'",
                    cid,
                )
        print(f"  Done. {len(stats['to_expire'])} mappings expired.")

    await pool.close()
    print(f"\nAudit complete at {datetime.now(timezone.utc).isoformat()}")
    return stats


if __name__ == "__main__":
    asyncio.run(main())
