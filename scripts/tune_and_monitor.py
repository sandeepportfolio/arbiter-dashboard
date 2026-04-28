#!/usr/bin/env python3
"""
Analyze current opportunities, tune scanner thresholds for profitability,
and monitor the system state.
"""
import json
import urllib.request
from datetime import datetime

BASE = "http://localhost:8080"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())

def post_json(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def patch_json(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, method="PATCH",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

print("=" * 70)
print("ARBITER SYSTEM ANALYSIS — %s" % datetime.now().isoformat())
print("=" * 70)

# 1. Health check
h = fetch("/api/health")
scanner = h.get("scanner", {})
print("\n--- HEALTH ---")
print("Status: %s, uptime: %.0fs" % (h.get("status"), h.get("uptime_seconds", 0)))
print("Scanner: %d scans, %d active, %d tradable" % (
    scanner.get("scan_count", 0),
    scanner.get("active_opportunities", 0),
    scanner.get("tradable_opportunities", 0),
))

# 2. Current settings
settings = fetch("/api/settings")
sc = settings.get("scanner", {})
print("\n--- CURRENT SCANNER SETTINGS ---")
print("  min_edge_cents: %.2f" % sc.get("min_edge_cents", 0))
print("  confidence_threshold: %.2f" % sc.get("confidence_threshold", 0))
print("  min_liquidity: %.1f" % sc.get("min_liquidity", 0))
print("  persistence_scans: %d" % sc.get("persistence_scans", 0))
print("  max_quote_age_seconds: %.0f" % sc.get("max_quote_age_seconds", 0))
print("  dry_run: %s" % sc.get("dry_run", "?"))

# 3. All opportunities with positive net edge
opps = fetch("/api/opportunities")
opps = opps if isinstance(opps, list) else opps.get("opportunities", [])

print("\n--- ALL OPPORTUNITIES WITH POSITIVE NET EDGE ---")
positive = [o for o in opps if o.get("net_edge_cents", 0) > 0]
positive.sort(key=lambda x: -x.get("net_edge_cents", 0))

for o in positive[:30]:
    print("  %s" % o.get("canonical_id", "?")[:50])
    print("    status=%s net=%.2fc gross=%.2fc conf=%.3f persist=%d" % (
        o.get("status", "?"),
        o.get("net_edge_cents", 0),
        o.get("gross_edge", 0) * 100,
        o.get("confidence", 0),
        o.get("persistence_count", 0),
    ))
    print("    mapping=%s auto_trade=%s liq=%.1f quote_age=%.0fs" % (
        o.get("mapping_status", "?"),
        o.get("allow_auto_trade", "?"),
        o.get("min_available_liquidity", 0),
        o.get("quote_age_seconds", 0),
    ))

# 4. Show opportunities that WOULD become tradable with lower thresholds
print("\n--- NEAR-TRADABLE ANALYSIS ---")
print("Opps that would be tradable with confidence_threshold=0.5 and min_liquidity=10:")
near_tradable = []
for o in opps:
    if o.get("net_edge_cents", 0) <= 0:
        continue
    ms = o.get("mapping_status", "")
    if ms != "confirmed":
        continue
    if not o.get("allow_auto_trade"):
        continue
    conf = o.get("confidence", 0)
    liq = o.get("min_available_liquidity", 0)
    persist = o.get("persistence_count", 0)
    qa = o.get("quote_age_seconds", 0)

    blockers = []
    if conf < 0.8:
        blockers.append("conf=%.2f<0.8" % conf)
    if liq < 25:
        blockers.append("liq=%.0f<25" % liq)
    if persist < 3:
        blockers.append("persist=%d<3" % persist)
    if qa > 120:
        blockers.append("quote_age=%.0f>120" % qa)

    if blockers:
        near_tradable.append((o, blockers))
        print("  %s: net=%.2fc %s" % (
            o.get("canonical_id", "?")[:45],
            o.get("net_edge_cents", 0),
            " | ".join(blockers),
        ))

if not near_tradable:
    print("  (none)")

# 5. Confirmed mappings summary
confirmed = [o for o in opps if o.get("mapping_status") == "confirmed"]
print("\n--- CONFIRMED MAPPING SUMMARY ---")
print("Total confirmed: %d" % len(confirmed))
for o in sorted(confirmed, key=lambda x: -x.get("net_edge_cents", 0)):
    edge = o.get("net_edge_cents", 0)
    if edge != 0:
        print("  %s: net=%.2fc status=%s" % (
            o.get("canonical_id", "?")[:45],
            edge,
            o.get("status", "?"),
        ))

# 6. P&L snapshot
pnl = fetch("/api/pnl")
print("\n--- P&L ---")
for k in ["total_pnl_usd", "realized_pnl_usd", "unrealized_pnl_usd",
          "total_fees_usd", "total_trades"]:
    print("  %s: %s" % (k, pnl.get(k, "?")))

# 7. Balance
bal = fetch("/api/balances")
print("\n--- BALANCES ---")
for plat, info in bal.items():
    if isinstance(info, dict):
        print("  %s: $%.2f" % (plat, info.get("balance", 0)))

print("\n" + "=" * 70)
