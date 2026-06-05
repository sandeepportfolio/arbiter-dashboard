#!/usr/bin/env python3
"""Verify the runtime cache has NO conids after DB update."""
import asyncio, os, sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from arbiter.mapping.market_map import MarketMappingStore
from arbiter.config.settings import MARKET_MAP

async def main():
    dsn = os.getenv("DATABASE_URL",
        "postgresql://arbiter:arbiter_secret@arbiter-postgres-prod:5432/arbiter_live")
    ms = MarketMappingStore(dsn)
    await ms.connect()
    await ms.init_schema()
    count = await ms.refresh_runtime_cache()
    print(f"Runtime cache refreshed: {count} mappings")

    no_count = 0
    yes_count = 0
    for canonical_id, m in MARKET_MAP.items():
        yes_conid = m.get("forecastex", "")
        no_conid = m.get("forecastex_no", "")
        if yes_conid:
            yes_count += 1
        if no_conid:
            no_count += 1
            print(f"  {canonical_id:30s}  YES={yes_conid:12s}  NO={no_conid}")

    print(f"\nTotal confirmed in runtime cache: {count}")
    print(f"With forecastex (YES) conid:      {yes_count}")
    print(f"With forecastex_no (NO) conid:    {no_count}")
    await ms.close()

if __name__ == "__main__":
    asyncio.run(main())
