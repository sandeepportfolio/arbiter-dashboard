#!/usr/bin/env python3
"""
Audit candidate mappings: find pairs with both platform prices,
compute edges, and flag mismatches for validation pipeline.
Output as JSON for sub-agent consumption.
"""
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

# Summary
print(f"Total price keys: {len(data)}")
print(f"Unique canonical_ids: {len(by_id)}")
print(f"With both platforms: {len(both)}")
print()

# Only show confirmed
print("=== CONFIRMED MAPPINGS ===")
for cid in sorted(both.keys()):
    if cid.startswith("AUTO_"):
        continue
    plats = both[cid]
    k = plats.get("kalshi", {})
    p = plats.get("polymarket", {})
    e1 = 1.0 - k.get("yes_price",0) - p.get("no_price",0)
    e2 = 1.0 - p.get("yes_price",0) - k.get("no_price",0)
    best = max(e1, e2)
    print(f"  {cid}: K_yes={k.get('yes_price',0):.3f} K_no={k.get('no_price',0):.3f} P_yes={p.get('yes_price',0):.3f} P_no={p.get('no_price',0):.3f} edge={best*100:.1f}c")

print()
print("=== AUTO CANDIDATES WITH POSITIVE EDGE (top 30) ===")
auto_edges = []
for cid in sorted(both.keys()):
    if not cid.startswith("AUTO_"):
        continue
    plats = both[cid]
    k = plats.get("kalshi", {})
    p = plats.get("polymarket", {})
    e1 = 1.0 - k.get("yes_price",0) - p.get("no_price",0)
    e2 = 1.0 - p.get("yes_price",0) - k.get("no_price",0)
    best = max(e1, e2)
    if best > 0.01:
        auto_edges.append({
            "canonical_id": cid,
            "kalshi_ticker": k.get("raw_market_id", ""),
            "poly_slug": p.get("raw_market_id", ""),
            "k_yes": k.get("yes_price", 0),
            "k_no": k.get("no_price", 0),
            "p_yes": p.get("yes_price", 0),
            "p_no": p.get("no_price", 0),
            "best_edge_cents": round(best * 100, 1),
        })

auto_edges.sort(key=lambda x: x["best_edge_cents"], reverse=True)
for item in auto_edges[:30]:
    print(f"  {item['canonical_id']}: edge={item['best_edge_cents']}c")
    print(f"    Kalshi: {item['kalshi_ticker']} (yes={item['k_yes']:.3f} no={item['k_no']:.3f})")
    print(f"    Poly:   {item['poly_slug']} (yes={item['p_yes']:.3f} no={item['p_no']:.3f})")

print()
print(f"Total auto candidates with >1c edge: {len(auto_edges)}")

# Dump all candidates to JSON for sub-agents
with open("/tmp/candidates_for_validation.json", "w") as f:
    json.dump(auto_edges, f, indent=2)
print(f"Wrote {len(auto_edges)} candidates to /tmp/candidates_for_validation.json")
