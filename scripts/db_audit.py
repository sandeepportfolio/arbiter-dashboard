import asyncio, asyncpg, os

async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT status, count(*) as cnt FROM market_mappings GROUP BY status ORDER BY cnt DESC")
        print("=== STATUS COUNTS ===")
        for r in rows:
            print(f"  {r['status']}: {r['cnt']}")

        row = await conn.fetchrow("""
            SELECT 
                count(*) as total,
                count(*) FILTER (WHERE kalshi_market_id <> '') as has_kalshi,
                count(*) FILTER (WHERE polymarket_slug <> '') as has_poly,
                count(*) FILTER (WHERE forecastex_contract_id <> '') as has_fx,
                count(*) FILTER (WHERE kalshi_market_id <> '' AND polymarket_slug <> '') as kp,
                count(*) FILTER (WHERE kalshi_market_id <> '' AND forecastex_contract_id <> '') as kf,
                count(*) FILTER (WHERE polymarket_slug <> '' AND forecastex_contract_id <> '') as pf,
                count(*) FILTER (WHERE kalshi_market_id <> '' AND polymarket_slug <> '' AND forecastex_contract_id <> '') as all3,
                count(*) FILTER (WHERE allow_auto_trade = TRUE) as auto_trade
            FROM market_mappings WHERE status = 'confirmed'
        """)
        print(f"\n=== CONFIRMED PLATFORM COVERAGE ===")
        for k in ['total','has_kalshi','has_poly','has_fx','kp','kf','pf','all3','auto_trade']:
            print(f"  {k}: {row[k]}")

        rows = await conn.fetch("""
            SELECT unnest(tags) as tag, count(*) as cnt 
            FROM market_mappings WHERE status = 'confirmed' 
            GROUP BY tag ORDER BY cnt DESC LIMIT 15
        """)
        print(f"\n=== TAG DISTRIBUTION (confirmed) ===")
        for r in rows:
            print(f"  {r['tag']}: {r['cnt']}")
        
        rows = await conn.fetch("""
            SELECT canonical_id, kalshi_market_id, polymarket_slug, forecastex_contract_id, mapping_score, allow_auto_trade
            FROM market_mappings 
            WHERE status = 'confirmed' AND kalshi_market_id <> '' AND forecastex_contract_id <> ''
            ORDER BY updated_at DESC LIMIT 10
        """)
        print(f"\n=== SAMPLE K+FX CONFIRMED ===")
        for r in rows:
            print(f"  {r['canonical_id'][:55]:55s} K={r['kalshi_market_id'][:25]:25s} FX={r['forecastex_contract_id']:10s} AT={r['allow_auto_trade']} score={r['mapping_score']}")

        rows = await conn.fetch("""
            SELECT canonical_id, kalshi_market_id, polymarket_slug, mapping_score, allow_auto_trade
            FROM market_mappings 
            WHERE status = 'confirmed' AND kalshi_market_id <> '' AND polymarket_slug <> '' AND forecastex_contract_id = ''
            ORDER BY updated_at DESC LIMIT 10
        """)
        print(f"\n=== SAMPLE K+P CONFIRMED (no FX) ===")
        for r in rows:
            print(f"  {r['canonical_id'][:55]:55s} K={r['kalshi_market_id'][:25]:25s} P={r['polymarket_slug'][:25]:25s} AT={r['allow_auto_trade']} score={r['mapping_score']}")

    await pool.close()

asyncio.run(main())
