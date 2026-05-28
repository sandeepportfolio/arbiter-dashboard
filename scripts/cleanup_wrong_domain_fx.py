#!/usr/bin/env python3
"""Clear forecastex_contract_id on mappings bound to wrong-domain FORECASTX
events (CPI/GDP/weather/etc. matched to sports/political markets).

Live 2026-05-26 audit found 19 confirmed+allow_auto_trade mappings where
fuzzy discovery bound MLB/NFL/MLS canonical_ids to regional-CPI or daily-
temperature parents because they share a city token. The new domain guard
in forecastex_discovery._domain_compatible blocks fresh bindings, but the
stale rows still carry the wrong conid; the auto_executor's new
forecastex_not_available gate trips on these, but to keep ops.html honest
we also want the cached conid cleared.

Usage::

    python scripts/cleanup_wrong_domain_fx.py [--dry-run]

The wrong-conid list is derived from a live IBKR catalog probe — categories
``economics_*``, ``weather``, ``crypto``, ``travel``, ``business``. The
script then marks each affected row ``forecastex_not_available=true`` so
the resolver doesn't re-bind on the next discovery pass.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys

import aiohttp
import asyncpg

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from arbiter.mapping.forecastex_discovery import DEFAULT_SEED_KEYWORDS

WRONG_DOMAIN_CATEGORIES = {
    "economics_cpi",
    "economics_gdp",
    "economics_employment",
    "economics_inflation",
    "economics_housing",
    "economics_retail",
    "economics_fed",
    "weather",
    "crypto",
    "travel",
    "business",
}

CATEGORY_RULES = (
    ("political_state_general", ("general election",)),
    ("political_state_primary", ("primary",)),
    ("political_federal", ("senate majority", "house of representatives", "presidential")),
    ("political_municipal", ("mayoral", "ballot measure")),
    ("economics_cpi", ("cpi", "consumer price")),
    ("economics_gdp", ("gdp", "gross domestic")),
    ("economics_employment", ("unemployment", "payrolls", "jobs", "jolt", "labor")),
    ("economics_fed", ("fed ", "fomc", "rate cut", "federal funds", "fed funds", "fed decision")),
    ("economics_inflation", ("inflation", "pce", "ppi", "producer price")),
    ("economics_housing", ("housing", "mortgage")),
    ("economics_retail", ("retail sales",)),
    ("crypto", ("bitcoin", "ethereum", "solana", "xrp", "dogecoin", " btc ", " eth ")),
    ("weather", ("temperature", "hurricane", "drought", "climate", "carbon")),
    ("travel", ("air travel", "air fares", "freight")),
    ("sports", ("nfl", "nba", "nhl", "mlb", "ncaa", "championship", "super bowl", "playoff")),
    ("business", ("ceo", "merger", "tesla", "nvidia", "apple", "earnings")),
    ("entertainment", ("nobel", "oscar", "emmy", "grammy")),
    ("geopolitics", ("ukraine", "russia", "china", "iran", "israel", "tariff", "scotus")),
)


def classify(header: str) -> str:
    lower = header.lower()
    for label, hints in CATEGORY_RULES:
        for h in hints:
            if h in lower:
                return label
    return "other"


async def fetch_search(session, gateway, keyword):
    try:
        async with session.post(
            f"{gateway}/v1/api/iserver/secdef/search",
            json={"symbol": keyword, "name": True},
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=15.0),
        ) as resp:
            if resp.status >= 400:
                return []
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return []
    items = data if isinstance(data, list) else (
        data.get("items", []) if isinstance(data, dict) else []
    )
    return [i for i in items if isinstance(i, dict)]


async def enumerate_bad_conids(gateway):
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=4)
    bad: set[str] = set()
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(4)

        async def _one(kw):
            async with sem:
                for it in await fetch_search(session, gateway, kw):
                    header = str(it.get("companyHeader") or "")
                    if "FORECASTX" not in header.upper():
                        continue
                    conid = str(it.get("conid") or "").strip()
                    if not conid:
                        continue
                    if classify(header) in WRONG_DOMAIN_CATEGORIES:
                        bad.add(conid)

        await asyncio.gather(*(_one(kw) for kw in DEFAULT_SEED_KEYWORDS))
    return bad


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--gateway",
        default=os.getenv("IBKR_GATEWAY_URL", "https://localhost:5000").rstrip("/"),
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql://arbiter:arbiter_secret@localhost:5433/arbiter_live",
        ),
    )
    args = parser.parse_args()

    print(f"Enumerating bad conids from {args.gateway}...", file=sys.stderr)
    bad = await enumerate_bad_conids(args.gateway)
    print(f"Found {len(bad)} wrong-domain (CPI/GDP/weather/...) conids", file=sys.stderr)

    if not bad:
        print("Catalog probe returned no bad conids — refusing to run.", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(args.dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT canonical_id, forecastex_contract_id, status, allow_auto_trade
            FROM market_mappings
            WHERE forecastex_contract_id = ANY($1::text[])
            ORDER BY canonical_id
            """,
            list(bad),
        )
        print(f"\nFound {len(rows)} mapping(s) bound to wrong-domain conids:")
        for r in rows:
            tag = "  [ACTIVE]" if r["allow_auto_trade"] and r["status"] == "confirmed" else ""
            print(f"  {r['canonical_id']:50s} conid={r['forecastex_contract_id']:14s} status={r['status']}{tag}")

        if not rows:
            return 0
        if args.dry_run:
            print("\n[dry-run] No changes made.")
            return 0

        canon_ids = [r["canonical_id"] for r in rows]
        result = await conn.execute(
            """
            UPDATE market_mappings
            SET forecastex_contract_id = '',
                forecastex_not_available = TRUE,
                updated_at = now()
            WHERE canonical_id = ANY($1::text[])
            """,
            canon_ids,
        )
        print(f"\n{result}")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
