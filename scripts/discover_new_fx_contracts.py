#!/usr/bin/env python3
"""Discover new ForecastEx parent events not yet in the mapping DB.

Queries /iserver/secdef/search for a comprehensive set of keywords, dedupes
by conid, and reports any FORECASTX events not already tracked.

Specifically checks:
  - RSM (Retail Sales Monthly) — new months
  - NHS (New Home Sales) — new expirations
  - GT (Global Temperature) — new thresholds
  - ND (National Debt) — threshold expansion
  - Any brand-new product families since last check

Usage:
    python3 /app/scripts/discover_new_fx_contracts.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

import asyncpg

from arbiter.collectors.forecastex import ForecastExClient
from arbiter.config.settings import ForecastExConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("discover_new_fx")

# Keywords to search — covers the full FORECASTX inventory plus speculative
# new product families. IBKR secdef/search returns max ~15 results per query.
SEARCH_KEYWORDS = [
    # Political
    "senate", "house", "governor", "president", "election", "primary",
    "midterm", "control", "majority", "ballot", "referendum",
    "republican", "democrat", "congressional", "gubernatorial",
    # Economics
    "cpi", "gdp", "unemployment", "fed", "inflation", "jobs", "payroll",
    "rate", "fomc", "recession", "retail sales", "housing",
    "jobless", "permits", "debt", "trade", "deficit",
    # Specific symbols to check for new months/thresholds
    "RSM", "NHS", "GT", "ND", "IJC", "CPIY", "FF",
    "retail sales monthly", "new home sales", "global temperature",
    "national debt", "initial jobless", "consumer price",
    "fed funds", "building permits", "housing starts",
    # Crypto
    "bitcoin", "ethereum", "solana", "crypto", "xrp", "dogecoin",
    # Weather
    "temperature", "hurricane", "drought", "carbon",
    # Sports (check if IBKR added new)
    "mlb", "nfl", "nba", "nhl", "championship",
    # International
    "canada gdp", "eurozone", "japan", "uk inflation",
    # Business/awards
    "oscar", "grammy", "emmys",
    # New speculative categories
    "AI", "tariff", "immigration", "supreme court",
    "oil", "gold", "silver", "natural gas",
]


async def main():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL",
        "postgresql://arbiter:arbiter_secret@arbiter-postgres-prod:5432/arbiter_live"))

    cfg = ForecastExConfig()
    client = ForecastExClient(
        gateway_url=cfg.gateway_url,
        account_id=cfg.account_id,
        verify_ssl=cfg.verify_ssl,
        paper_trading=cfg.paper_trading,
    )
    await client._ensure_iserver_bridge()
    logger.info("Bridge ready: %s", client._bridge_ready)

    # Get all conids already in our DB
    existing_rows = await conn.fetch("""
        SELECT DISTINCT forecastex_contract_id
        FROM market_mappings
        WHERE forecastex_contract_id != '' AND forecastex_contract_id != '0'
    """)
    existing_conids = {r[0] for r in existing_rows}
    logger.info("Existing FX conids in DB: %d", len(existing_conids))

    # Search IBKR for all FORECASTX events
    discovered: dict[str, dict] = {}  # conid -> info dict
    keywords_per_conid: dict[str, list[str]] = defaultdict(list)

    logger.info("Searching %d keywords...", len(SEARCH_KEYWORDS))
    for i, keyword in enumerate(SEARCH_KEYWORDS):
        try:
            results = await client.search_contracts(keyword)
            for item in results:
                if not isinstance(item, dict):
                    continue
                # Only FORECASTX events
                header = str(item.get("companyHeader") or "")
                if "FORECASTX" not in header.upper():
                    continue
                conid = str(item.get("conid") or "").strip()
                if not conid or conid == "-1":
                    continue
                if conid not in discovered:
                    discovered[conid] = item
                keywords_per_conid[conid].append(keyword)
        except RuntimeError as exc:
            if "429" in str(exc):
                logger.warning("429 on search, backing off 10s...")
                await asyncio.sleep(10)
            else:
                logger.warning("Search failed for '%s': %s", keyword, exc)
        except Exception as exc:
            logger.warning("Search error for '%s': %s", keyword, exc)

        if (i + 1) % 10 == 0:
            logger.info("  Search progress: %d/%d keywords, %d FORECASTX events found",
                         i + 1, len(SEARCH_KEYWORDS), len(discovered))
        await asyncio.sleep(0.15)

    logger.info("Total FORECASTX events discovered: %d", len(discovered))

    # Classify as known vs new
    known = {}
    new = {}
    for conid, info in discovered.items():
        if conid in existing_conids:
            known[conid] = info
        else:
            new[conid] = info

    print(f"\n{'='*70}")
    print(f"  ForecastEx Discovery Results")
    print(f"{'='*70}")
    print(f"  Total FORECASTX events found:  {len(discovered)}")
    print(f"  Already in DB:                 {len(known)}")
    print(f"  NEW (not in DB):               {len(new)}")
    print(f"{'='*70}\n")

    if new:
        # Group new by symbol
        by_symbol: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for conid, info in new.items():
            sym = str(info.get("symbol") or "?")
            by_symbol[sym].append((conid, info))

        print(f"=== NEW ForecastEx events ({len(new)} total, {len(by_symbol)} symbols) ===\n")
        for sym in sorted(by_symbol.keys()):
            items = by_symbol[sym]
            for conid, info in items:
                header = str(info.get("companyHeader") or "")[:60]
                sec_type = str(info.get("secType") or "?")
                kws = ", ".join(keywords_per_conid.get(conid, [])[:3])
                print(f"  {conid:>12s}  {sym:10s}  {sec_type:4s}  {header}")
                print(f"               found via: {kws}")

    # Specifically check requested symbols
    print(f"\n{'='*70}")
    print(f"  Specific Symbol Checks")
    print(f"{'='*70}\n")

    for check_sym in ("RSM", "NHS", "GT", "ND"):
        matches = [
            (c, i) for c, i in discovered.items()
            if str(i.get("symbol") or "").upper() == check_sym
        ]
        new_matches = [
            (c, i) for c, i in matches if c not in existing_conids
        ]
        print(f"  {check_sym}: {len(matches)} total events, {len(new_matches)} new")
        for conid, info in new_matches[:5]:
            print(f"    NEW: {conid}  {info.get('companyHeader', '')[:60]}")

    await client.close()
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
