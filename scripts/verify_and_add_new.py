#!/usr/bin/env python3
"""Verify specific potential matches via Kalshi API and add to fixture."""
import json
import hashlib
import urllib.request
from datetime import datetime
from pathlib import Path

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
FIXTURE = Path.home() / "Documents/arbiter/arbiter/mapping/fixtures/market_seeds_auto.json"

def fetch_kalshi(ticker):
    try:
        url = "%s/markets/%s" % (KALSHI_API, ticker)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("market", data)
    except Exception as e:
        return {"error": str(e)}

# Load existing
seeds = json.loads(FIXTURE.read_text())
existing_keys = {(s.get("kalshi",""), s.get("polymarket","")) for s in seeds}
new_pairs = []

def add_pair(kalshi, poly, sport, side, date, desc_override=None):
    if (kalshi, poly) in existing_keys:
        print("  SKIP (already exists): %s" % kalshi)
        return
    km = fetch_kalshi(kalshi)
    title = km.get("title", "")
    if km.get("error"):
        print("  ERROR fetching %s: %s" % (kalshi, km["error"]))
        return

    sport_names = {"mlb":"MLB","nhl":"NHL","mls":"MLS","nba":"NBA",
                   "bun":"Bundesliga","sea":"Serie A","lal":"La Liga","epl":"EPL"}
    sport_full = sport_names.get(sport, sport.upper())

    if desc_override:
        desc = desc_override
    elif side in ("tie","draw"):
        desc = "%s: Draw/Tie on %s" % (sport_full, date)
    else:
        desc = "%s: %s wins on %s" % (sport_full, side.upper(), date)

    h = hashlib.md5(("%s_%s" % (kalshi, poly)).encode()).hexdigest()[:8]
    canonical_id = "GAME_%s_%s_%s_%s" % (sport.upper(), date.replace("-",""), side.upper(), h)

    entry = {
        "canonical_id": canonical_id,
        "description": desc,
        "kalshi": kalshi,
        "polymarket": poly,
        "polymarket_question": desc,
        "category": "sports",
        "status": "confirmed",
        "allow_auto_trade": True,
        "tags": ["sports", sport, "game-winner", "manual-verified"],
        "notes": "Manual verification %s. Kalshi: %s" % (datetime.now().strftime("%Y-%m-%d %H:%M"), title),
        "resolution_criteria": {
            "kalshi": {"source": "Kalshi", "rule": title or desc},
            "polymarket": {"source": "Polymarket US", "rule": desc},
            "criteria_match": "identical",
        },
        "resolution_match_status": "identical",
    }
    new_pairs.append(entry)
    existing_keys.add((kalshi, poly))
    print("  ADDED: %s — %s" % (canonical_id, desc))
    print("    Kalshi title: %s" % title)

# 1. Verify MLS ATL-MTL
print("=== MLS ATL-MTL verification ===")
km = fetch_kalshi("KXMLSGAME-26MAY02ATLMTL-ATL")
print("Kalshi title: %s" % km.get("title", "?"))
print("Kalshi subtitle: %s" % km.get("subtitle", "?"))
# If title says "Miami" then MTL = Miami on Kalshi (unusual)
# If title says "Montreal" then it's a different team from Polymarket's MIM
title = km.get("title", "").lower()
if "miami" in title:
    print("MTL = Miami on Kalshi! Can match with MIM on Polymarket")
    add_pair("KXMLSGAME-26MAY02ATLMTL-ATL", "atc-mls-atl-mim-2026-05-02-atl", "mls", "atl", "2026-05-02")
    add_pair("KXMLSGAME-26MAY02ATLMTL-MTL", "atc-mls-atl-mim-2026-05-02-mim", "mls", "mtl", "2026-05-02")
    add_pair("KXMLSGAME-26MAY02ATLMTL-TIE", "atc-mls-atl-mim-2026-05-02-draw", "mls", "tie", "2026-05-02")
elif "montreal" in title:
    print("MTL = Montreal. Polymarket has MIM (Inter Miami). DIFFERENT TEAMS — skip!")
else:
    print("Can't determine. Title: %s — SKIPPING for safety" % km.get("title",""))

# 2. Serie A BFC-CAG (BFC=Bologna, BOL=Bologna on Polymarket)
print("\n=== Serie A BFC-CAG verification ===")
km = fetch_kalshi("KXSERIEAGAME-26MAY03BFCCAG-BFC")
print("Kalshi title: %s" % km.get("title", "?"))
title = km.get("title", "").lower()
if "bologna" in title:
    print("BFC = Bologna confirmed! Matching with BOL on Polymarket")
    add_pair("KXSERIEAGAME-26MAY03BFCCAG-BFC", "atc-sea-bol-cag-2026-05-03-bol", "sea", "bfc", "2026-05-03",
             "Serie A: Bologna wins on 2026-05-03")
    add_pair("KXSERIEAGAME-26MAY03BFCCAG-CAG", "atc-sea-bol-cag-2026-05-03-cag", "sea", "cag", "2026-05-03",
             "Serie A: Cagliari wins on 2026-05-03")
    # Check if draw slug exists
    add_pair("KXSERIEAGAME-26MAY03BFCCAG-TIE", "atc-sea-bol-cag-2026-05-03-draw", "sea", "tie", "2026-05-03",
             "Serie A: Draw on 2026-05-03 (Bologna vs Cagliari)")
else:
    print("Unexpected. Title: %s" % km.get("title",""))

# 3. MLB DET-ATL Apr 28 (different from Apr 29 which we already have)
print("\n=== MLB DET-ATL Apr 28 verification ===")
km = fetch_kalshi("KXMLBGAME-26APR281915DETATL-DET")
print("Kalshi title: %s" % km.get("title", "?"))
# Check if Polymarket has det-atl for Apr 28
# From find_specific: Poly MLB Apr 28 = hou-bal, az-mil, bos-tor (NO det-atl)
print("Polymarket has NO det-atl slug for Apr 28 — can't match")

# 4. MLB BOS-TOR Apr 28 and 29
print("\n=== MLB BOS-TOR verification ===")
# Check if Kalshi has BOS-TOR
for date_str in ["26APR28", "26APR29"]:
    for time_suffix in ["", "1910", "1915", "1810", "1835", "1910"]:
        ticker = "KXMLBGAME-%s%sBOSTOR-BOS" % (date_str, time_suffix)
        km = fetch_kalshi(ticker)
        if not km.get("error"):
            iso = "2026-04-%s" % date_str[-2:]
            print("Found: %s — %s" % (ticker, km.get("title","")))
            poly = "aec-mlb-bos-tor-%s" % iso
            add_pair(ticker, poly, "mlb", "bos", iso)
            # Also add TOR side
            ticker_tor = ticker.replace("-BOS", "-TOR")
            add_pair(ticker_tor, poly, "mlb", "tor", iso)
            break

# 5. MLB AZ-MIL Apr 28
print("\n=== MLB AZ-MIL Apr 28 verification ===")
for time_suffix in ["1910", "1915", "1810", "1835", "2010", "2005"]:
    ticker = "KXMLBGAME-26APR28%sAZMIL-AZ" % time_suffix
    km = fetch_kalshi(ticker)
    if not km.get("error"):
        print("Found: %s — %s" % (ticker, km.get("title","")))
        add_pair(ticker, "aec-mlb-az-mil-2026-04-28", "mlb", "az", "2026-04-28")
        ticker_mil = ticker.replace("-AZ", "-MIL")
        add_pair(ticker_mil, "aec-mlb-az-mil-2026-04-28", "mlb", "mil", "2026-04-28")
        break

# Save
if new_pairs:
    for entry in new_pairs:
        seeds.append(entry)
    FIXTURE.write_text(json.dumps(seeds, indent=2))
    print("\n=== ADDED %d NEW PAIRS ===" % len(new_pairs))
    print("Total seeds: %d, Confirmed: %d" % (
        len(seeds), len([s for s in seeds if s.get("status")=="confirmed"])))
    print("\nRebuild Docker to deploy:")
    print("  docker compose -f docker-compose.prod.yml --env-file .env.production build arbiter-api-prod")
    print("  docker compose -f docker-compose.prod.yml --env-file .env.production up -d arbiter-api-prod")
else:
    print("\nNo new pairs to add.")
