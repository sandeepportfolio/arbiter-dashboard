#!/usr/bin/env python3
"""Verify the deployment after Docker rebuild."""
import json
import urllib.request
import time

BASE = "http://localhost:8080"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())

# Wait for startup
time.sleep(5)

h = fetch("/api/health")
scanner = h.get("scanner", {})
print("Status: %s, uptime: %.0fs" % (h.get("status"), h.get("uptime_seconds", 0)))
print("Scanner: %d scans, %d active, %d tradable" % (
    scanner.get("scan_count", 0),
    scanner.get("active_opportunities", 0),
    scanner.get("tradable_opportunities", 0),
))

readiness = h.get("readiness", {})
for check in readiness.get("checks", []):
    if check.get("key") == "auto_trade_mappings":
        ids = check.get("details", {}).get("canonical_ids", [])
        print("Confirmed auto-trade mappings: %d" % len(ids))
        det_found = any("DET" in x for x in ids)
        print("New DET mapping loaded: %s" % det_found)
        for cid in sorted(ids):
            print("  %s" % cid)
        break

opps = fetch("/api/opportunities")
opps = opps if isinstance(opps, list) else opps.get("opportunities", [])
confirmed = [o for o in opps if o.get("mapping_status") == "confirmed"]
print("\nConfirmed mapping opportunities: %d" % len(confirmed))
for o in sorted(confirmed, key=lambda x: x.get("net_edge_cents", 0), reverse=True):
    print("  %s: net=%.1fc gross=%.1fc status=%s conf=%.2f" % (
        o.get("canonical_id", "?")[:45],
        o.get("net_edge_cents", 0),
        o.get("gross_edge", 0) * 100,
        o.get("status", "?"),
        o.get("confidence", 0),
    ))
