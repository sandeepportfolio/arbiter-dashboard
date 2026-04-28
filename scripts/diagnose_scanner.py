#!/usr/bin/env python3
"""Diagnose why confirmed mappings aren't generating scanner opportunities."""
import json
import urllib.request
import time

BASE = "http://localhost:8080"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())

# Get ALL prices
prices = fetch("/api/prices")
print("Total price entries:", len(prices))

# Check confirmed markets
confirmed = [
    "DEM_HOUSE_2026", "GOP_HOUSE_2026", "DEM_SENATE_2026", "GOP_SENATE_2026",
    "GAME_BUN_20260503_BMG_de1ef745", "GAME_BUN_20260503_BVB_52af5fbf",
    "GAME_SEA_20260502_ATA_5923d22a", "GAME_MLB_20260428_HOU_4c6f8dd1",
]

now = time.time()
print("\n=== CONFIRMED MARKET PRICES ===")
for cid in confirmed:
    kalshi_key = "price:kalshi:" + cid
    poly_key = "price:polymarket:" + cid
    k = prices.get(kalshi_key)
    p = prices.get(poly_key)

    has_both = k is not None and p is not None
    if k:
        k_age = now - k.get("timestamp", 0)
    if p:
        p_age = now - p.get("timestamp", 0)

    print("%s:" % cid)
    if k:
        print("  Kalshi: YES=$%.4f NO=$%.4f age=%.0fs" % (k["yes_price"], k["no_price"], k_age))
    else:
        print("  Kalshi: NO DATA")
    if p:
        print("  Polymarket: YES=$%.4f NO=$%.4f age=%.0fs" % (p["yes_price"], p["no_price"], p_age))
    else:
        print("  Polymarket: NO DATA")

    if has_both:
        # Compute edge like the scanner would
        yes_buy_k = k["yes_price"]  # Buy YES on Kalshi
        no_buy_p = p["no_price"]    # Buy NO on Polymarket
        gross1 = 1.0 - yes_buy_k - no_buy_p

        yes_buy_p = p["yes_price"]  # Buy YES on Polymarket
        no_buy_k = k["no_price"]    # Buy NO on Kalshi
        gross2 = 1.0 - yes_buy_p - no_buy_k

        print("  Edge (K-YES + P-NO): %.4f (%.2fc)" % (gross1, gross1 * 100))
        print("  Edge (P-YES + K-NO): %.4f (%.2fc)" % (gross2, gross2 * 100))
    else:
        print("  MISSING DATA - can't compute edge")

# Check opportunities for these
opps = fetch("/api/opportunities")
opps = opps if isinstance(opps, list) else opps.get("opportunities", [])
print("\n=== OPPORTUNITY CHECK ===")
for cid in confirmed:
    found = [o for o in opps if o.get("canonical_id") == cid]
    if found:
        for o in found:
            print("%s: status=%s net=%.2fc" % (cid, o["status"], o.get("net_edge_cents", 0)))
    else:
        print("%s: NOT IN OPPORTUNITIES" % cid)
