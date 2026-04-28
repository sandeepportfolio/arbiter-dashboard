#!/usr/bin/env python3
"""Find untapped cross-platform market opportunities."""
import json
import re
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = "http://localhost:8080"

prices = json.loads(urllib.request.urlopen(BASE + "/api/prices", timeout=30).read())

# Load existing seeds
fixture = Path.home() / "Documents/arbiter/arbiter/mapping/fixtures/market_seeds_auto.json"
seeds = json.loads(fixture.read_text())
matched_kalshi = {s.get("kalshi", "") for s in seeds if s.get("kalshi")}

# Unmatched Kalshi game/match tickers
kalshi_unmatched = []
for mid, p in prices.items():
    if p.get("platform") != "kalshi":
        continue
    raw = p.get("raw_market_id", "")
    if raw in matched_kalshi:
        continue
    m = re.match(r"KX([A-Z0-9]+?)(?:GAME|MATCH)-(\d{2}[A-Z]{3}\d{2})", raw)
    if m:
        kalshi_unmatched.append((m.group(1), m.group(2), raw))

sport_groups = defaultdict(list)
for sport, date, ticker in kalshi_unmatched:
    sport_groups[sport].append((date, ticker))

print("Unmatched Kalshi game/match tickers by sport:")
for sport, tickers in sorted(sport_groups.items(), key=lambda x: -len(x[1])):
    print("  %s: %d tickers" % (sport, len(tickers)))
    for date, ticker in tickers[:3]:
        print("    %s" % ticker)
    if len(tickers) > 3:
        print("    ... and %d more" % (len(tickers) - 3))

# Polymarket future sports
today = datetime.now().strftime("%Y-%m-%d")
poly_future = defaultdict(list)
for mid, p in prices.items():
    if p.get("platform") != "polymarket":
        continue
    raw = p.get("raw_market_id", "")
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if m and m.group(1) >= today:
        parts = raw.split("-")
        if len(parts) >= 3:
            prefix = parts[0] + "-" + parts[1]
            poly_future[prefix].append(raw)

print("\nPolymarket future sports markets:")
for prefix, slugs in sorted(poly_future.items(), key=lambda x: -len(x[1])):
    print("  %s: %d slugs" % (prefix, len(slugs)))
    for s in slugs[:2]:
        print("    %s" % s)

# Cross-reference: find date overlaps between unmatched Kalshi and Polymarket
MONTHS = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
          "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}

SPORT_MAP = {
    "mlb":"mlb","nhl":"nhl","mls":"mls","bundesliga":"bun",
    "seriea":"sea","laliga":"lal","efll1":"epl",
    "atp":"atp","atpchallenger":"atp","wta":"wta","wtachallenger":"wta",
    "itf":"atp","itfw":"wta","nba":"nba",
}

print("\nPotential new overlaps (unmatched Kalshi with Poly on same date):")
# Build Poly index by sport+date
poly_by_sd = defaultdict(list)
for mid, p in prices.items():
    if p.get("platform") != "polymarket":
        continue
    raw = p.get("raw_market_id", "")
    parts = raw.split("-")
    if len(parts) >= 5:
        sport = parts[1]
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if dm:
            poly_by_sd[(sport, dm.group(1))].append(raw)

overlaps = 0
for sport, date_raw, ticker in kalshi_unmatched:
    dm = re.match(r"(\d{2})([A-Z]{3})(\d{2})", date_raw)
    if not dm:
        continue
    iso_date = "20%s-%s-%s" % (dm.group(1), MONTHS.get(dm.group(2), "00"), dm.group(3))
    if iso_date < today:
        continue
    p_sport = SPORT_MAP.get(sport.lower())
    if not p_sport:
        continue
    candidates = poly_by_sd.get((p_sport, iso_date), [])
    if candidates:
        overlaps += 1
        if overlaps <= 20:
            print("  K: %s" % ticker)
            print("  P candidates: %s" % ", ".join(candidates[:3]))
            print()

print("Total potential overlaps: %d" % overlaps)
