"""One-shot cleanup: cancel stuck Polymarket orders and resolve old critical
incidents that are permanently blocking the trade gate.

The shadow-audit incidents from April 24 were created against a defunct schema
("Shadow execution audit flagged the completed trade state") and reference arbs
whose orders are still ``submitted`` 3 weeks later — they are dead, not actually
pending fills. They block ``readiness._check_incidents`` indefinitely.

Run inside the API container so it inherits ``DATABASE_URL``:

    docker exec arbiter-api-prod python /tmp/cleanup_stuck_state.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import asyncpg

CLEANUP_NOTE = (
    "Cleared by scripts/cleanup_stuck_state.py — stuck since 2026-04-24/28, "
    "preceded the DEM_HOUSE_2026 cascade fixes."
)


async def main() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set in environment")

    now = datetime.now(timezone.utc)
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            # --- 1) Stuck orders ------------------------------------------------
            stuck_orders = await conn.fetch(
                """
                SELECT order_id, arb_id, platform, status, side, submitted_at
                FROM execution_orders
                WHERE status IN ('SUBMITTED','PENDING','submitted','pending')
                ORDER BY submitted_at
                """
            )
            print(f"[orders] found {len(stuck_orders)} non-terminal orders to cancel:")
            for row in stuck_orders:
                print(
                    f"  {row['order_id']} arb={row['arb_id']} "
                    f"platform={row['platform']} status={row['status']} "
                    f"submitted_at={row['submitted_at']}"
                )

            cancelled_orders = await conn.execute(
                """
                UPDATE execution_orders
                   SET status = 'cancelled',
                       updated_at = $1,
                       terminal_at = COALESCE(terminal_at, $1),
                       error = CASE
                         WHEN error IS NULL OR error = '' THEN $2
                         ELSE error || ' | ' || $2
                       END
                 WHERE status IN ('SUBMITTED','PENDING','submitted','pending')
                """,
                now,
                CLEANUP_NOTE,
            )
            print(f"[orders] {cancelled_orders}")

            # --- 2) Open critical incidents ------------------------------------
            critical = await conn.fetch(
                """
                SELECT incident_id, canonical_id, severity, created_at,
                       LEFT(message, 80) AS message
                FROM execution_incidents
                WHERE status = 'open' AND severity = 'critical'
                ORDER BY created_at
                """
            )
            print(f"\n[incidents:critical] found {len(critical)} open critical incidents:")
            for row in critical:
                print(
                    f"  {row['incident_id']} canon={row['canonical_id']} "
                    f"created={row['created_at']} msg='{row['message']}'"
                )

            resolved_critical = await conn.execute(
                """
                UPDATE execution_incidents
                   SET status = 'resolved',
                       resolved_at = $1,
                       resolution_note = $2
                 WHERE status = 'open' AND severity = 'critical'
                """,
                now,
                CLEANUP_NOTE,
            )
            print(f"[incidents:critical] {resolved_critical}")

            # --- 3) DEM_HOUSE_2026 warnings (post-cascade noise) ---------------
            dem_house = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM execution_incidents
                WHERE status = 'open' AND canonical_id = 'DEM_HOUSE_2026'
                """
            )
            print(f"\n[incidents:DEM_HOUSE_2026] {dem_house} open warnings to clear")
            resolved_dem = await conn.execute(
                """
                UPDATE execution_incidents
                   SET status = 'resolved',
                       resolved_at = $1,
                       resolution_note = $2
                 WHERE status = 'open' AND canonical_id = 'DEM_HOUSE_2026'
                """,
                now,
                CLEANUP_NOTE
                + " DEM_HOUSE_2026 cascade quarantined per c1e8654; remaining warnings are historical.",
            )
            print(f"[incidents:DEM_HOUSE_2026] {resolved_dem}")

            # --- 4) Post-state summary ----------------------------------------
            stuck_after = await conn.fetchval(
                """
                SELECT COUNT(*) FROM execution_orders
                WHERE status IN ('SUBMITTED','PENDING','submitted','pending')
                """
            )
            crit_after = await conn.fetchval(
                """
                SELECT COUNT(*) FROM execution_incidents
                WHERE status = 'open' AND severity = 'critical'
                """
            )
            open_after = await conn.fetchval(
                """
                SELECT COUNT(*) FROM execution_incidents WHERE status = 'open'
                """
            )
            print(
                f"\n[post] stuck_orders={stuck_after} "
                f"critical_open={crit_after} all_open_incidents={open_after}"
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
