#!/usr/bin/env python3
"""Check why confirmed mappings aren't generating tradable opportunities."""
import json
import urllib.request

BASE = "http://localhost:8080"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())

# Get all opportunities
data = fetch("/api/opportunities")
opps = data if isinstance(data, list) else data.get("opportunities", [])

# Get prices
prices = fetch("/api/prices")

confirmed_ids = [
    "GAME_BUN_20260503_BMG_de1ef745",
    "DEM_HOUSE_2026",
    "GOP_HOUSE_2026",
    "GAME_MLB_20260428_HOU_4c6f8dd1",
    "GAME_SEA_20260502_ATA_5923d22a",
    "GAME_LAL_20260502_BAR_58ad4ae6",
]

print("=== CONFIRMED MAPPING OPPORTUNITY STATUS ===")
for cid in confirmed_ids:
    found = [o for o in opps if o.get("canonical_id") == cid]
    if found:
        o = found[0]
        print("%s:" % cid)
        print("  net=%.2fc status=%s conf=%.3f liq=%.0f persist=%d qa=%.0fs" % (
            o.get("net_edge_cents", 0), o.get("status"), o.get("confidence", 0),
            o.get("min_available_liquidity", 0), o.get("persistence_count", 0),
            o.get("quote_age_seconds", 0),
        ))
        print("  yes: %s @ $%.4f | no: %s @ $%.4f" % (
            o.get("yes_platform", "?"), o.get("yes_price", 0),
            o.get("no_platform", "?"), o.get("no_price", 0),
        ))
    else:
        print("%s: NOT IN OPPORTUNITIES" % cid)

print("\n=== PRICE DATA FOR CONFIRMED MAPPINGS ===")
if isinstance(prices, dict):
    for cid in confirmed_ids:
        if cid in prices:
            p = prices[cid]
            print("%s: %s" % (cid, json.dumps(p, indent=2)[:200]))
        else:
            print("%s: NO PRICE DATA" % cid)
elif isinstance(prices, list):
    print("Prices is a list of %d items" % len(prices))
    for p in prices[:3]:
        print("  Sample: %s" % json.dumps(p)[:200])
