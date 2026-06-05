import asyncio
import os
import sys
import asyncpg

sys.path.insert(0, '/app')

DEAD_MAPPINGS = [
    "FX_IJC_202606_195000p0",
    "FX_IJC_202606_200000p0",
    "FX_IJC_202606_205000p0",
    "FX_IJC_202606_210000p0",
    "FX_IJC_202606_215000p0",
    "FX_IJC_202606_220000p0",
    "FX_IJC_202606_225000p0",
    "FX_IJC_202606_230000p0",
    "FX_IJC_202606_235000p0",
    "FX_IJC_202606_240000p0",
    "POL_GOV_2026_PA_DEM",
    "POL_GOV_2026_PA_GOP",
    "POL_SENATE_2026_MA_GOP",
    "POL_SENATE_2026_NC_DEM",
    "POL_SENATE_2026_NC_GOP",
    "POL_SENATE_2026_NH_GOP",
    "POL_SENATE_2026_PA_DEM",
    "POL_SENATE_2026_PA_GOP",
]

async def main():
    pool = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=3)
    async with pool.acquire() as conn:
        expired = 0
        for cid in DEAD_MAPPINGS:
            result = await conn.execute(
                "UPDATE market_mappings SET status = 'expired', "
                "allow_auto_trade = FALSE, "
                "review_note = '[Phase-1 audit 2026-06-05] all platforms dead/settled/not-found', "
                "updated_at = NOW() "
                "WHERE canonical_id = $1 AND status = 'confirmed'",
                cid,
            )
            if 'UPDATE 1' in result:
                expired += 1
                print(f'  EXPIRED: {cid}')
            else:
                print(f'  SKIP (not confirmed): {cid}')
        print(f'\nExpired {expired}/{len(DEAD_MAPPINGS)} mappings')

        # Verify new counts
        rows = await conn.fetch("SELECT status, count(*) as cnt FROM market_mappings GROUP BY status ORDER BY cnt DESC")
        print('\nUpdated status counts:')
        for r in rows:
            print(f'  {r["status"]}: {r["cnt"]}')
    await pool.close()

asyncio.run(main())
