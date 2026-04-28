#!/usr/bin/env python3
"""Find specific matchable pairs by examining exact market names."""
import json
import re
import urllib.request
from pathlib import Path

BASE = "http://localhost:8080"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

prices = json.loads(urllib.request.urlopen(BASE + "/api/prices", timeout=30).read())

# Group Polymarket by sport+date
poly_by_sport_date = {}
for mid, p in prices.items():
    if p.get("platform") != "polymarket":
        continue
    raw = p.get("raw_market_id", "")
    parts = raw.split("-")
    if len(parts) >= 5:
        sport = parts[1]
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if dm:
            key = (sport, dm.group(1))
            if key not in poly_by_sport_date:
                poly_by_sport_date[key] = []
            poly_by_sport_date[key].append(raw)

# Check specific unmatched Kalshi tickers
MONTHS = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
          "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}

# EPL: EFLL1 -> epl
print("=== EPL matches ===")
epl_k = [mid for mid, p in prices.items() if p.get("platform")=="kalshi" and "EFLL1GAME" in p.get("raw_market_id","")]
for mid in epl_k:
    raw = prices[mid]["raw_market_id"]
    print("K: %s" % raw)
    dm = re.search(r"(\d{2})([A-Z]{3})(\d{2})", raw)
    if dm:
        iso = "20%s-%s-%s" % (dm.group(1), MONTHS.get(dm.group(2),"00"), dm.group(3))
        poly_matches = poly_by_sport_date.get(("epl", iso), [])
        print("  Date: %s, Poly EPL on this date: %s" % (iso, poly_matches))

# NHL: Check ANA-EDM and other upcoming
print("\n=== NHL matches ===")
nhl_k = [mid for mid, p in prices.items() if p.get("platform")=="kalshi" and "NHLGAME" in p.get("raw_market_id","")]
for mid in nhl_k:
    raw = prices[mid]["raw_market_id"]
    print("K: %s" % raw)
    dm = re.search(r"(\d{2})([A-Z]{3})(\d{2})", raw)
    if dm:
        iso = "20%s-%s-%s" % (dm.group(1), MONTHS.get(dm.group(2),"00"), dm.group(3))
        poly_matches = poly_by_sport_date.get(("nhl", iso), [])
        print("  Date: %s, Poly NHL on this date: %s" % (iso, poly_matches))

# NBA: Check if we can match any
print("\n=== NBA matches ===")
nba_poly = {k: v for k, v in poly_by_sport_date.items() if k[0] == "nba"}
print("Poly NBA markets by date:")
for (sport, date), slugs in sorted(nba_poly.items()):
    print("  %s: %s" % (date, slugs[:5]))

# Check if Kalshi has NBA game winner tickers
nba_k = [mid for mid, p in prices.items() if p.get("platform")=="kalshi" and re.match(r"KXNBA.*GAME-", p.get("raw_market_id",""))]
print("Kalshi NBA game tickers: %d" % len(nba_k))
for mid in nba_k[:5]:
    print("  %s" % prices[mid]["raw_market_id"])

# MLS upcoming
print("\n=== MLS matches ===")
mls_k = [mid for mid, p in prices.items() if p.get("platform")=="kalshi" and "MLSGAME" in p.get("raw_market_id","")]
for mid in mls_k:
    raw = prices[mid]["raw_market_id"]
    dm = re.search(r"(\d{2})([A-Z]{3})(\d{2})", raw)
    if dm:
        iso = "20%s-%s-%s" % (dm.group(1), MONTHS.get(dm.group(2),"00"), dm.group(3))
        poly_matches = poly_by_sport_date.get(("mls", iso), [])
        if poly_matches:
            print("K: %s" % raw)
            print("  Date: %s, Poly MLS: %s" % (iso, poly_matches[:3]))

# Serie A upcoming
print("\n=== Serie A matches ===")
sea_k = [mid for mid, p in prices.items() if p.get("platform")=="kalshi" and "SERIEAGAME" in p.get("raw_market_id","")]
for mid in sea_k:
    raw = prices[mid]["raw_market_id"]
    dm = re.search(r"(\d{2})([A-Z]{3})(\d{2})", raw)
    if dm:
        iso = "20%s-%s-%s" % (dm.group(1), MONTHS.get(dm.group(2),"00"), dm.group(3))
        poly_matches = poly_by_sport_date.get(("sea", iso), [])
        if poly_matches:
            print("K: %s" % raw)
            print("  Date: %s, Poly SEA: %s" % (iso, poly_matches[:3]))

# MLB upcoming
print("\n=== MLB matches ===")
mlb_k = [mid for mid, p in prices.items() if p.get("platform")=="kalshi" and "MLBGAME" in p.get("raw_market_id","")]
for mid in mlb_k:
    raw = prices[mid]["raw_market_id"]
    dm = re.search(r"(\d{2})([A-Z]{3})(\d{2})", raw)
    if dm:
        iso = "20%s-%s-%s" % (dm.group(1), MONTHS.get(dm.group(2),"00"), dm.group(3))
        poly_matches = poly_by_sport_date.get(("mlb", iso), [])
        if poly_matches:
            print("K: %s" % raw)
            print("  Date: %s, Poly MLB: %s" % (iso, poly_matches[:3]))
