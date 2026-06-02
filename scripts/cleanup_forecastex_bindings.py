#!/usr/bin/env python3
"""One-shot cleanup of wrong-domain ForecastEx bindings.

2026-06-02 live audit found 22 sports mappings (CHAMP_KX*, GAME_MLB_*) with
forecastex_contract_id pointing to FX political/macro events. Several conids
were attached to multiple unrelated sports teams simultaneously — proof of
the fuzzy-match domain leak that ``forecastex_discovery._domain_compatible``
should have rejected but didn't because Kalshi-channel canonical_id tokens
("champ", "kxncaaf", "okla", ...) were missing from ``_SPORTS_HINT_TOKENS``.

This script does TWO things:

1. Clears ``forecastex_contract_id`` and ``forecastex_no_contract_id`` on
   any mapping currently bound to a blocklisted conid (from
   ``forecastex_discovery._BLOCKLISTED_FORECASTEX_CONIDS``).
2. Also clears mappings whose canonical_id pattern marks them as Kalshi
   sports-channel (CHAMP_KX..., GAME_*, KXNCAAF/KXNFL/KXMLB/KXNBA/KXNHL),
   then sets ``forecastex_not_available=true`` so discovery won't retry.
3. Resets ``forecastex_not_available=false`` on confirmed non-sports
   political/economic mappings so discovery can run NO-conid backfill on
   them on the next cycle.

Usage:
    python scripts/cleanup_forecastex_bindings.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from arbiter.mapping.forecastex_discovery import _BLOCKLISTED_FORECASTEX_CONIDS


SPORTS_PREFIX_PATTERNS = (
    "CHAMP_KXNCAAF",
    "CHAMP_KXNFL",
    "CHAMP_KXMLB",
    "CHAMP_KXNBA",
    "CHAMP_KXNHL",
    "GAME_MLB_",
    "GAME_NFL_",
    "GAME_NBA_",
    "GAME_NHL_",
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--dsn",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql://arbiter:arbiter_secret@localhost:5433/arbiter_live",
        ),
    )
    args = parser.parse_args()

    conn = await asyncpg.connect(args.dsn)
    try:
        # Step 1: rows bound to blocklisted conids
        blk_rows = await conn.fetch(
            """
            SELECT canonical_id, forecastex_contract_id
            FROM market_mappings
            WHERE forecastex_contract_id = ANY($1::text[])
            ORDER BY canonical_id
            """,
            list(_BLOCKLISTED_FORECASTEX_CONIDS),
        )
        print(f"\n=== Bound to blocklisted conid: {len(blk_rows)} row(s) ===")
        for r in blk_rows:
            print(f"  {r['canonical_id']:50s} conid={r['forecastex_contract_id']}")

        # Step 2: sports-pattern rows that still carry an FX conid
        sports_pattern_rows = await conn.fetch(
            """
            SELECT canonical_id, forecastex_contract_id, forecastex_no_contract_id,
                   forecastex_not_available
            FROM market_mappings
            WHERE (forecastex_contract_id != '' OR forecastex_no_contract_id != '')
              AND ($1::text[] && string_to_array(canonical_id, ' '))
            """,
            list(SPORTS_PREFIX_PATTERNS),
        )
        # Above $1 array trick rarely works for prefix matching — fall back to
        # client-side filter.
        sports_pattern_rows = [
            r for r in await conn.fetch(
                """
                SELECT canonical_id, forecastex_contract_id, forecastex_no_contract_id
                FROM market_mappings
                WHERE forecastex_contract_id != ''
                """,
            )
            if any(r["canonical_id"].startswith(p) for p in SPORTS_PREFIX_PATTERNS)
        ]
        print(f"\n=== Sports-pattern bound to ANY FX conid: {len(sports_pattern_rows)} row(s) ===")
        for r in sports_pattern_rows:
            print(f"  {r['canonical_id']:50s} conid={r['forecastex_contract_id']}")

        to_clear: set[str] = set()
        to_clear.update(r["canonical_id"] for r in blk_rows)
        to_clear.update(r["canonical_id"] for r in sports_pattern_rows)
        if not to_clear:
            print("\nNothing to clear.")
        else:
            print(f"\nWould clear {len(to_clear)} mapping(s).")

        # Step 3: confirmed non-sports, non-blocklisted negative-cached rows
        # that the user wants to retry discovery for.
        retry_rows = await conn.fetch(
            """
            SELECT canonical_id
            FROM market_mappings
            WHERE forecastex_not_available = TRUE
              AND status = 'confirmed'
              AND allow_auto_trade = TRUE
            """,
        )
        non_sports_retry: list[str] = []
        for r in retry_rows:
            cid = r["canonical_id"]
            if any(cid.startswith(p) for p in SPORTS_PREFIX_PATTERNS):
                continue
            non_sports_retry.append(cid)
        print(
            f"\n=== Non-sports negative-cached confirmed+auto_trade: "
            f"{len(non_sports_retry)} row(s) — would reset for retry ===",
        )
        for cid in non_sports_retry[:30]:
            print(f"  {cid}")
        if len(non_sports_retry) > 30:
            print(f"  ...and {len(non_sports_retry)-30} more")

        if args.dry_run:
            print("\n[dry-run] No changes made.")
            return 0

        # Wrap both UPDATEs in a single transaction so a concurrent
        # forecastex_discovery cycle can't race between them.
        async with conn.transaction():
            if to_clear:
                res1 = await conn.execute(
                    """
                    UPDATE market_mappings
                    SET forecastex_contract_id = '',
                        forecastex_no_contract_id = '',
                        forecastex_not_available = TRUE,
                        updated_at = now()
                    WHERE canonical_id = ANY($1::text[])
                    """,
                    list(to_clear),
                )
                print(f"\nCleared bad bindings: {res1}")

            if non_sports_retry:
                res2 = await conn.execute(
                    """
                    UPDATE market_mappings
                    SET forecastex_not_available = FALSE,
                        updated_at = now()
                    WHERE canonical_id = ANY($1::text[])
                    """,
                    non_sports_retry,
                )
                print(f"Reset negative-cache on non-sports: {res2}")

    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
