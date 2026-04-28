#!/usr/bin/env python3
"""
Verify team name equivalences by checking Kalshi API market descriptions.
Uses the Arbiter's /api/prices endpoint to get canonical_id and market metadata.
Also queries Kalshi's /v2/markets/{ticker} for full market details.
"""
import json
import os
import sys
import urllib.request

BASE = "http://localhost:8080"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())

def fetch_kalshi_market(ticker):
    """Fetch market details from Kalshi API."""
    try:
        api_base = "https://api.elections.kalshi.com/trade-api/v2"
        url = "%s/markets/%s" % (api_base, ticker)
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        # Try without auth first (public endpoint)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("market", data)
    except Exception as e:
        return {"error": str(e)}

# Load validated pairs
with open("/tmp/validated_pairs.json") as f:
    pairs = json.load(f)

# Get full price data from Arbiter
prices = fetch("/api/prices")

print("=" * 80)
print("TEAM NAME VERIFICATION FOR %d MATCHED PAIRS" % len(pairs))
print("=" * 80)

# Check each pair
issues = []
verified = []

for p in pairs:
    kt = p["kalshi_ticker"]
    ps = p["poly_slug"]
    sport = p["sport"]

    # Get Kalshi market details from API
    km = fetch_kalshi_market(kt)

    k_title = km.get("title", km.get("subtitle", "UNKNOWN"))
    k_event = km.get("event_ticker", "")
    k_yes_sub = km.get("yes_sub_title", "")
    k_no_sub = km.get("no_sub_title", "")

    # Get Polymarket data from Arbiter prices
    p_data = None
    for key, v in prices.items():
        if v.get("raw_market_id") == ps:
            p_data = v
            break

    p_title = ""
    p_question = ""
    if p_data:
        p_title = p_data.get("title", p_data.get("description", ""))
        p_question = p_data.get("question", "")

    print("\n--- %s: %s vs %s ---" % (sport.upper(), kt, ps))
    print("  Kalshi title: %s" % k_title)
    print("  Kalshi yes_sub: %s" % k_yes_sub)
    print("  Kalshi event: %s" % k_event)
    if km.get("error"):
        print("  Kalshi API error: %s" % km["error"])
    print("  Poly title: %s" % p_title)
    print("  Poly question: %s" % p_question)
    print("  K side: %s, P side: %s" % (p["k_side"], p["p_side"]))
    print("  Same polarity: %s" % p["same_polarity"])
    print("  Edge: %.1fc (%s)" % (p["edge_cents"], p["direction"]))

    # Flag potential issues
    issue = None
    if p["k_side"].lower() == "mtl" and p["p_side"].lower() == "mim":
        issue = "MTL (Montreal) vs MIM (Inter Miami?) — POTENTIAL MISMATCH"

    if issue:
        print("  *** ISSUE: %s ***" % issue)
        issues.append({"pair": p, "issue": issue})
    else:
        verified.append(p)

print("\n" + "=" * 80)
print("VERIFICATION RESULTS")
print("=" * 80)
print("Total pairs: %d" % len(pairs))
print("Verified OK: %d" % len(verified))
print("Issues found: %d" % len(issues))
for i in issues:
    print("  ISSUE: %s" % i["issue"])
    print("    K: %s" % i["pair"]["kalshi_ticker"])
    print("    P: %s" % i["pair"]["poly_slug"])

# Save verified pairs
with open("/tmp/verified_pairs.json", "w") as f:
    json.dump(verified, f, indent=2)
print("\nSaved %d verified pairs to /tmp/verified_pairs.json" % len(verified))

# Save issues
with open("/tmp/pair_issues.json", "w") as f:
    json.dump(issues, f, indent=2)
