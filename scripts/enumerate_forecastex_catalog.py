#!/usr/bin/env python3
"""Enumerate the FULL FORECASTX catalog via IBKR Client Portal API.

Hits ``/iserver/secdef/search`` once per seed keyword, dedupes by conid,
classifies each parent by description heuristics, and prints a summary
table. Operator-on-demand diagnostic — run by hand when you want to see
what FX inventory IBKR exposes today.

Limitation surfaced live 2026-05-26: binary political parents (SENM,
HORC, AXX*, MXX*, etc.) expose tradeable children via ``secdef/strikes``
+ ``secdef/info`` (strike=1.0/2.0). Multi-outcome parents (CPI, FEDRC,
GDP, BTC threshold ladders) do NOT — IBKR's Client Portal API simply
does not return their child OPT conids. The collector therefore cannot
publish quotes for the multi-outcome bucket; the resolver auto-
quarantines those parents after 3 empty cycles.

Usage::

    python scripts/enumerate_forecastex_catalog.py \\
        [--gateway https://localhost:5000] \\
        [--all]    # also print every individual conid

Exit code is 0 unless the gateway is unreachable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
from collections import defaultdict
from typing import Any

import aiohttp

# Reuse the same seed list that production discovery uses so this
# diagnostic stays in sync with what the runtime actually probes.
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from arbiter.mapping.forecastex_discovery import DEFAULT_SEED_KEYWORDS


CATEGORY_RULES = (
    # Order matters — first match wins. Most specific first.
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
    """Bucket a FORECASTX event header into an operator-readable category."""
    lower = header.lower()
    for label, hints in CATEGORY_RULES:
        for h in hints:
            if h in lower:
                return label
    return "other"


async def fetch_search(session: aiohttp.ClientSession, gateway: str, keyword: str) -> list[dict[str, Any]]:
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


async def enumerate_catalog(gateway: str) -> dict[str, dict[str, Any]]:
    """Sweep every seed keyword and dedupe FORECASTX hits by conid."""
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=4)

    out: dict[str, dict[str, Any]] = {}
    async with aiohttp.ClientSession(connector=connector) as session:
        # Throttle to ~4 concurrent calls so we don't trigger IBKR's
        # tight 429 bucket on /secdef/search.
        sem = asyncio.Semaphore(4)

        async def _one(kw: str):
            async with sem:
                items = await fetch_search(session, gateway, kw)
                for it in items:
                    header = str(it.get("companyHeader") or "")
                    if "FORECASTX" not in header.upper():
                        continue
                    conid = str(it.get("conid") or "").strip()
                    if not conid or conid == "-1" or conid in out:
                        continue
                    out[conid] = {
                        "conid": conid,
                        "symbol": str(it.get("symbol") or ""),
                        "header": header,
                        "category": classify(header),
                    }

        await asyncio.gather(*(_one(kw) for kw in DEFAULT_SEED_KEYWORDS))
    return out


def render_summary(catalog: dict[str, dict[str, Any]], show_all: bool = False) -> None:
    print(f"\n=== FORECASTX CATALOG (live IBKR Client Portal probe) ===")
    print(f"Total unique events surfaced: {len(catalog)}\n")

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for v in catalog.values():
        by_category[v["category"]].append(v)

    for cat in sorted(by_category.keys(), key=lambda c: (-len(by_category[c]), c)):
        rows = by_category[cat]
        print(f"  {cat:30s}  {len(rows):4d}")
    print()

    if show_all:
        print("=== INDIVIDUAL EVENTS ===")
        for cat in sorted(by_category.keys()):
            print(f"\n--- {cat} ({len(by_category[cat])}) ---")
            for v in sorted(by_category[cat], key=lambda x: x["symbol"]):
                print(f"  {v['symbol']:8s}  {v['conid']:14s}  {v['header']}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--gateway",
        default=os.getenv("IBKR_GATEWAY_URL", "https://localhost:5000").rstrip("/"),
        help="IBKR Client Portal gateway base URL (default from IBKR_GATEWAY_URL env)",
    )
    parser.add_argument("--all", action="store_true", help="List every event individually")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of summary")
    args = parser.parse_args()

    # /v1/api is appended by fetch_search; strip if user passed it
    gw = args.gateway.replace("/v1/api", "").rstrip("/")
    print(f"Probing gateway: {gw}", file=sys.stderr)

    catalog = await enumerate_catalog(gw)
    if not catalog:
        print("ERROR: gateway unreachable or no FORECASTX hits returned.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(catalog, indent=2))
    else:
        render_summary(catalog, show_all=args.all)


if __name__ == "__main__":
    asyncio.run(main())
