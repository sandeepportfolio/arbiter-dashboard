#!/usr/bin/env python3
"""Check live opportunities for confirmed mappings."""
import json
import urllib.request

BASE = "http://localhost:8080"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())

d = fetch("/api/opportunities")
opps = d if isinstance(d, list) else d.get("opportunities", [])
confirmed = [o for o in opps if o.get("mapping_status") == "confirmed"]
print("Total opportunities: %d" % len(opps))
print("Confirmed mapping opportunities: %d" % len(confirmed))
for o in sorted(confirmed, key=lambda x: x.get("net_edge_cents", 0), reverse=True):
    print("  %s: edge=%.1fc status=%s conf=%.2f" % (
        o.get("canonical_id", "?"),
        o.get("net_edge_cents", 0),
        o.get("status", "?"),
        o.get("confidence", 0),
    ))

# Check scanner health
h = fetch("/api/health")
s = h.get("scanner", {})
print("\nScanner: %d scans, %d active, %d tradable, best=%.1fc" % (
    s.get("scan_count", 0),
    s.get("active_opportunities", 0),
    s.get("tradable_opportunities", 0),
    s.get("best_edge_cents", 0),
))
