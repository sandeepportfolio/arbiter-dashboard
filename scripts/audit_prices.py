#!/usr/bin/env python3
"""Audit current prices and candidate mappings for validation."""
import json
import sys
import urllib.request
from collections import defaultdict

BASE = "http://localhost:8080"

def fetch(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Error fetching {path}: {e}", file=sys.stderr)
        return {}

# Get prices
data = fetch("/api/prices")
by_id = defaultdict(dict)
for key, p in data.items():
    cid = p.get("canonical_id", "")
    plat = p.get("platform", "")
    by_id[cid][plat] = p

both = {k: v for k, v in by_id.items() if len(v) >= 2}
print(f"Total price keys: {len(data)}")
print(f"Unique canonical_ids: {len(by_id)}")
print(f"With both platforms: {len(both)}")
print()

print("=== Markets with prices on BOTH platforms ===")
for cid in sorted(both.keys()):
    plats = both[cid]
    k = plats.get("kalshi", {})
    p = plats.get("polymarket", {})
    ky = k.get("yes_price", 0)
    kn = k.get("no_price", 0)
    py = p.get("yes_price", 0)
    pn = p.get("no_price", 0)
    kr = k.get("raw_market_id", "")
    pr = p.get("raw_market_id", "")
    # Compute edge
    # Cross-platform: buy YES cheapest, buy NO cheapest
    edges = []
    # YES on kalshi, NO on polymarket
    e1 = 1.0 - ky - pn
    # YES on polymarket, NO on kalshi
    e2 = 1.0 - py - kn
    best = max(e1, e2)
    direction = "K_yes+P_no" if e1 > e2 else "P_yes+K_no"
    print(f"  {cid}")
    print(f"    Kalshi:      yes={ky:.3f} no={kn:.3f} raw={kr}")
    print(f"    Polymarket:  yes={py:.3f} no={pn:.3f} raw={pr}")
    print(f"    Best edge:   {best*100:.1f}c ({direction})")
    print()
