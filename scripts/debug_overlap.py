#!/usr/bin/env python3
"""Debug script to show actual market overlap between platforms."""
import json
import re
import sys
import urllib.request

BASE = "http://localhost:8080"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())

d = fetch("/api/prices")
kalshi = {v["raw_market_id"]: v for v in d.values() if v.get("platform") == "kalshi"}
poly = {v["raw_market_id"]: v for v in d.values() if v.get("platform") == "polymarket"}

def pp(ticker, v):
    y = v.get("yes_price", 0)
    n = v.get("no_price", 0)
    print("  %s  yes=%.3f no=%.3f" % (ticker, y, n))

# 1. MLB GAME tickers
print("=== KALSHI MLB GAME TICKERS ===")
for t in sorted(kalshi):
    if "MLBGAME" in t:
        pp(t, kalshi[t])

# 2. NHL GAME tickers
print("\n=== KALSHI NHL GAME TICKERS ===")
for t in sorted(kalshi):
    if "NHLGAME" in t:
        pp(t, kalshi[t])

# 3. NBA single game tickers (first 15)
print("\n=== KALSHI NBA SINGLEGAME TICKERS (first 15) ===")
nba_games = [t for t in sorted(kalshi) if "NBASINGLEGAME" in t]
for t in nba_games[:15]:
    pp(t, kalshi[t])

# 4. BUNDESLIGA tickers
print("\n=== KALSHI BUNDESLIGA GAME TICKERS ===")
for t in sorted(kalshi):
    if "BUNDESLIGA" in t:
        pp(t, kalshi[t])

# 5. MLS game tickers
print("\n=== KALSHI MLS GAME TICKERS ===")
for t in sorted(kalshi):
    if "MLSGAME" in t or "MLSSINGLEGAME" in t or "MVEMLS" in t:
        pp(t, kalshi[t])

# 6. ATP match tickers (first 15)
print("\n=== KALSHI ATP MATCH TICKERS (first 15) ===")
atp = [t for t in sorted(kalshi) if "ATPMATCH" in t or "ATPCHALLENGERMATCH" in t]
for t in atp[:15]:
    pp(t, kalshi[t])

# 7. Polymarket daily game slugs (aec-*)
print("\n=== POLYMARKET DAILY GAME SLUGS (aec-*) ===")
for s in sorted(poly):
    if s.startswith("aec-"):
        pp(s, poly[s])

# 8. Polymarket 3-way match slugs (atc-*)
print("\n=== POLYMARKET 3-WAY MATCH SLUGS (atc-*) ===")
for s in sorted(poly):
    if s.startswith("atc-"):
        pp(s, poly[s])

# Summary
print("\n=== OVERLAP SUMMARY ===")
k_sports = {}
for t in kalshi:
    for pattern, sport in [
        ("MLBGAME", "mlb-game"), ("NHLGAME", "nhl-game"),
        ("NBASINGLEGAME", "nba-game"), ("MLSGAME", "mls-game"),
        ("BUNDESLIGAGAME", "bundesliga-game"), ("SERIEA", "seriea-game"),
        ("LALIGA", "laliga-game"), ("EFLL1GAME", "epl-game"),
        ("ATPMATCH", "atp-match"), ("ATPCHALLENGERMATCH", "atp-challenger"),
        ("WTAMATCH", "wta-match"), ("WTACHALLENGERMATCH", "wta-challenger"),
        ("ITFMATCH", "itf-match"), ("ITFWMATCH", "itfw-match"),
    ]:
        if pattern in t:
            k_sports[sport] = k_sports.get(sport, 0) + 1
            break

p_sports = {}
for s in poly:
    parts = s.split("-")
    if len(parts) >= 3:
        key = parts[0] + "-" + parts[1]
        p_sports[key] = p_sports.get(key, 0) + 1

print("Kalshi game/match tickers by sport:")
for k, v in sorted(k_sports.items(), key=lambda x: -x[1]):
    print("  %s: %d" % (k, v))

print("\nPolymarket slugs by prefix-sport:")
for k, v in sorted(p_sports.items(), key=lambda x: -x[1]):
    print("  %s: %d" % (k, v))
