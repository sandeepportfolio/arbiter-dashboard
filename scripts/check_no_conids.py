#!/usr/bin/env python3
"""Quick check of NO conid status in the DB."""
import asyncio, asyncpg, os, sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

async def main():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL",
        "postgresql://arbiter:arbiter_secret@arbiter-postgres-prod:5432/arbiter_live"))

    # Check the 4 known binary political mappings
    print("=== Binary political mappings ===")
    for cid in ("DEM_HOUSE_2026", "GOP_HOUSE_2026", "DEM_SENATE_2026", "GOP_SENATE_2026"):
        r = await conn.fetchrow(
            "SELECT forecastex_contract_id, forecastex_no_contract_id FROM market_mappings WHERE canonical_id = $1", cid)
        if r:
            yes = r[0] or "(empty)"
            no = r[1] or "(empty)"
            print(f"  {cid:20s}  YES={yes:12s}  NO={no}")

    # Overall counts
    print("\n=== Overall counts ===")
    total = await conn.fetchval("SELECT count(*) FROM market_mappings WHERE forecastex_contract_id != '' AND forecastex_contract_id != '0'")
    with_no = await conn.fetchval("SELECT count(*) FROM market_mappings WHERE forecastex_no_contract_id != '' AND forecastex_no_contract_id IS NOT NULL")
    need_no = await conn.fetchval("""
        SELECT count(*) FROM market_mappings
        WHERE forecastex_contract_id != '' AND forecastex_contract_id != '0'
          AND (forecastex_no_contract_id = '' OR forecastex_no_contract_id IS NULL)
          AND forecastex_not_available = FALSE
    """)
    print(f"  Total with YES conid (non-zero): {total}")
    print(f"  Already have NO conid:           {with_no}")
    print(f"  Still need NO conid:             {need_no}")

    # Show the ones with NO conid
    if with_no > 0:
        rows = await conn.fetch("""
            SELECT canonical_id, forecastex_contract_id, forecastex_no_contract_id
            FROM market_mappings
            WHERE forecastex_no_contract_id != '' AND forecastex_no_contract_id IS NOT NULL
            ORDER BY canonical_id
            LIMIT 20
        """)
        print(f"\n=== Mappings WITH NO conid (first 20 of {with_no}) ===")
        for r in rows:
            print(f"  {r[0]:30s}  YES={r[1]:12s}  NO={r[2]}")

    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
