#!/usr/bin/env python3
"""Check FX mappings in the database."""
import asyncio, asyncpg, os

async def main():
    url = os.getenv("DATABASE_URL", "postgresql://arbiter:arbiter_secret@arbiter-postgres-prod:5432/arbiter_live")
    conn = await asyncpg.connect(url)
    total = await conn.fetchval("SELECT COUNT(*) FROM market_mappings WHERE canonical_id LIKE 'FX_%' AND status = 'confirmed'")
    families = await conn.fetch("SELECT SPLIT_PART(canonical_id, '_', 2) AS fam, COUNT(*) AS cnt FROM market_mappings WHERE canonical_id LIKE 'FX_%' AND status = 'confirmed' GROUP BY 1 ORDER BY 2 DESC")
    print(f"Total FX confirmed mappings: {total}")
    for r in families:
        print(f"  {r['fam']:<8s}: {r['cnt']:3d}")
    samples = await conn.fetch("SELECT canonical_id, kalshi_market_id, forecastex_contract_id, mapping_score FROM market_mappings WHERE canonical_id LIKE 'FX_%' AND status = 'confirmed' ORDER BY canonical_id LIMIT 15")
    print()
    for s in samples:
        print(f"  {s['canonical_id']:<45s} K={s['kalshi_market_id']:<35s} FX={s['forecastex_contract_id']}  score={s['mapping_score']}")
    # Also show total confirmed mappings overall
    all_confirmed = await conn.fetchval("SELECT COUNT(*) FROM market_mappings WHERE status = 'confirmed'")
    print(f"\nTotal confirmed mappings (all): {all_confirmed}")
    await conn.close()

asyncio.run(main())
