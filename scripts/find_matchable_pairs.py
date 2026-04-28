#!/usr/bin/env python3
"""
Find genuinely matchable cross-platform pairs from live price data.

Approach:
1. Get all prices from the running Arbiter API
2. Parse Kalshi tickers and Polymarket US slugs to extract:
   - Sport/league
   - Teams involved
   - Date
   - Market type (moneyline/spread/total/prop)
3. Match only moneyline/game-winner markets across platforms
4. Output validated pairs for human review

Both platforms use structured slug formats:
- Kalshi: KXMVENBASINGLEGAME-26APR27ATLNY-ATL (NBA game, ATL vs NY, team ATL wins)
- Polymarket US: aec-nba-atl-ny-2026-04-28 (NBA game, ATL vs NY)
  or: tec-nba-atl-ny-2026-04-28 (alternate prefix)
"""
import json
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date

BASE = "http://localhost:8080"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


# ── Team abbreviation mapping ────────────────────────────────────────
# Map common abbreviations across platforms
TEAM_ALIASES = {
    # NBA
    "atl": "atl", "bos": "bos", "bkn": "bkn", "cha": "cha", "chi": "chi",
    "cle": "cle", "dal": "dal", "den": "den", "det": "det", "gs": "gsw",
    "gsw": "gsw", "hou": "hou", "ind": "ind", "lac": "lac", "lal": "lal",
    "mem": "mem", "mia": "mia", "mil": "mil", "min": "min", "no": "nop",
    "nop": "nop", "ny": "nyk", "nyk": "nyk", "okc": "okc", "orl": "orl",
    "phi": "phi", "phx": "phx", "por": "por", "sac": "sac", "sa": "sas",
    "sas": "sas", "sea": "sea", "tor": "tor", "uta": "uta", "was": "was",
    # NHL
    "ana": "ana", "ari": "ari", "buf": "buf", "car": "car", "cbj": "cbj",
    "col": "col", "dal": "dal", "edm": "edm", "fla": "fla", "la": "lak",
    "lak": "lak", "mtl": "mtl", "njd": "njd", "nsh": "nsh", "nyi": "nyi",
    "nyr": "nyr", "ott": "ott", "pit": "pit", "stl": "stl", "sj": "sjs",
    "sjs": "sjs", "tb": "tbl", "tbl": "tbl", "van": "van", "vgk": "vgk",
    "wpg": "wpg", "wsh": "wsh",
    # MLB
    "az": "az", "bal": "bal", "chc": "chc", "cin": "cin", "cws": "cws",
    "hou": "hou", "kc": "kc", "laa": "laa", "lad": "lad", "mil": "mil",
    "nym": "nym", "nyy": "nyy", "oak": "oak", "sd": "sdp", "sdp": "sdp",
    "sf": "sfg", "sfg": "sfg", "tex": "tex",
}

def normalize_team(abbr):
    return TEAM_ALIASES.get(abbr.lower(), abbr.lower())


def parse_kalshi_game_ticker(ticker):
    """Parse Kalshi single-game ticker.

    Examples:
    - KXMVENBASINGLEGAME-26APR27ATLNY-ATL → sport=nba, teams=(atl,ny), date=2026-04-27, side=atl
    - KXNHLAST-26APR26EDMANA-... → this is a prop, not a game
    """
    # Pattern: KXMVE{SPORT}SINGLEGAME-{DATE}{AWAY}{HOME}-{SIDE}
    m = re.match(
        r"KXMVE(NBA|MLB|NHL|NFL|MLS|EPL)SINGLEGAME-(\d{2})([A-Z]{3})(\d{2})([A-Z]+?)([A-Z]+?)-([A-Z]+)$",
        ticker, re.IGNORECASE
    )
    if m:
        sport = m.group(1).lower()
        day = m.group(2)
        month_str = m.group(3)
        year_suffix = m.group(4)
        # This doesn't easily split teams — try another approach
        pass

    # Try more specific patterns
    # KXMVENBASINGLEGAME-26APR28PHIBOS-BOS
    m = re.match(
        r"KXMVE(\w+?)SINGLEGAME[^-]*-(\d{2}[A-Z]{3}\d{2})(\w+)-(\w+)$",
        ticker, re.IGNORECASE
    )
    if m:
        sport_raw = m.group(1).lower()
        date_raw = m.group(2)
        teams_raw = m.group(3).lower()
        side = m.group(4).lower()
        return {
            "type": "game_winner",
            "sport": sport_raw.replace("singlegame", ""),
            "date_raw": date_raw,
            "teams_raw": teams_raw,
            "side": side,
            "ticker": ticker,
        }

    return None


def parse_poly_game_slug(slug):
    """Parse Polymarket US game-winner slug.

    Examples:
    - aec-nba-atl-ny-2026-04-28 → sport=nba, away=atl, home=ny, date=2026-04-28
    - tec-mlb-bos-tor-2026-04-28 → sport=mlb, away=bos, home=tor, date=2026-04-28
    - tec-nba-atl-ny-2026-04-28-atl → with team side specified
    """
    # Pattern: {prefix}-{sport}-{team1}-{team2}-{date}[-{side}]
    m = re.match(
        r"(?:aec|tec|atc|tsc)-(\w+)-(\w+)-(\w+)-(\d{4}-\d{2}-\d{2})(?:-(.+))?$",
        slug
    )
    if m:
        sport = m.group(1).lower()
        team1 = m.group(2).lower()
        team2 = m.group(3).lower()
        date_str = m.group(4)
        side = m.group(5).lower() if m.group(5) else None
        return {
            "type": "game_winner",
            "sport": sport,
            "team1": team1,
            "team2": team2,
            "date": date_str,
            "side": side,
            "slug": slug,
        }
    return None


def main():
    data = fetch("/api/prices")

    # Collect all Kalshi and Polymarket markets
    kalshi_markets = {}  # raw_market_id -> price data
    poly_markets = {}

    for key, p in data.items():
        raw = p.get("raw_market_id", "")
        if not raw:
            continue
        if p.get("platform") == "kalshi":
            kalshi_markets[raw] = p
        elif p.get("platform") == "polymarket":
            poly_markets[raw] = p

    print(f"Kalshi markets: {len(kalshi_markets)}")
    print(f"Polymarket US markets: {len(poly_markets)}")

    # Parse Polymarket game slugs
    poly_games = {}
    for slug, p in poly_markets.items():
        parsed = parse_poly_game_slug(slug)
        if parsed:
            poly_games[slug] = {**parsed, "price_data": p}

    print(f"Polymarket game-winner markets: {len(poly_games)}")

    # Group Polymarket games by (sport, team1, team2, date) for matching
    poly_by_game = defaultdict(list)
    for slug, pg in poly_games.items():
        t1 = normalize_team(pg["team1"])
        t2 = normalize_team(pg["team2"])
        game_key = (pg["sport"], tuple(sorted([t1, t2])), pg["date"])
        poly_by_game[game_key].append(pg)

    print(f"Unique Polymarket games: {len(poly_by_game)}")

    # Parse Kalshi game tickers and find matches
    matched_pairs = []
    for ticker, kp in kalshi_markets.items():
        parsed = parse_kalshi_game_ticker(ticker)
        if not parsed:
            continue

        # Try to match with Polymarket
        # Extract teams from Kalshi ticker
        k_sport = parsed.get("sport", "")
        k_side = normalize_team(parsed.get("side", ""))
        k_teams = parsed.get("teams_raw", "")
        k_date = parsed.get("date_raw", "")

        # For each poly game, check if sport and teams match
        for game_key, poly_entries in poly_by_game.items():
            p_sport, p_teams, p_date = game_key
            if k_sport != p_sport and k_sport.replace("singlegame", "") != p_sport:
                continue

            # Check if Kalshi side is one of the Polymarket teams
            if k_side not in p_teams:
                continue

            # Check date alignment (rough — Kalshi uses 26APR28 format)
            if k_date:
                try:
                    months = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
                              "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
                    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", k_date)
                    if m:
                        yr = "20" + m.group(1)
                        mn = months.get(m.group(2), "00")
                        dy = m.group(3)
                        k_date_str = f"{yr}-{mn}-{dy}"
                        if k_date_str != p_date:
                            continue
                except Exception:
                    pass

            # Find the matching Polymarket entry for this side
            for pe in poly_entries:
                p_side = pe.get("side")
                if p_side and normalize_team(p_side) == k_side:
                    matched_pairs.append({
                        "kalshi_ticker": ticker,
                        "kalshi_yes": kp.get("yes_price", 0),
                        "kalshi_no": kp.get("no_price", 0),
                        "poly_slug": pe["slug"],
                        "poly_yes": pe["price_data"].get("yes_price", 0),
                        "poly_no": pe["price_data"].get("no_price", 0),
                        "sport": p_sport,
                        "teams": list(p_teams),
                        "date": p_date,
                        "side": k_side,
                        "edge_k_yes_p_no": round((1.0 - kp.get("yes_price",0) - pe["price_data"].get("no_price",0)) * 100, 1),
                        "edge_p_yes_k_no": round((1.0 - pe["price_data"].get("yes_price",0) - kp.get("no_price",0)) * 100, 1),
                    })

    print(f"\n=== MATCHED CROSS-PLATFORM GAME PAIRS: {len(matched_pairs)} ===")
    for mp in sorted(matched_pairs, key=lambda x: max(x["edge_k_yes_p_no"], x["edge_p_yes_k_no"]), reverse=True)[:50]:
        best_edge = max(mp["edge_k_yes_p_no"], mp["edge_p_yes_k_no"])
        direction = "K_YES+P_NO" if mp["edge_k_yes_p_no"] > mp["edge_p_yes_k_no"] else "P_YES+K_NO"
        print(f"\n  {mp['sport'].upper()} | {mp['date']} | {mp['side'].upper()} wins")
        print(f"    Kalshi:     {mp['kalshi_ticker']} (yes={mp['kalshi_yes']:.3f} no={mp['kalshi_no']:.3f})")
        print(f"    Polymarket: {mp['poly_slug']} (yes={mp['poly_yes']:.3f} no={mp['poly_no']:.3f})")
        print(f"    Edge: {best_edge:.1f}c ({direction})")

    # Also check: are there Polymarket games with NO Kalshi match?
    matched_poly = {mp["poly_slug"] for mp in matched_pairs}
    unmatched_poly = [slug for slug in poly_games if slug not in matched_poly]
    print(f"\n=== STATS ===")
    print(f"  Matched pairs: {len(matched_pairs)}")
    print(f"  Unmatched Polymarket games: {len(unmatched_poly)}")

    # Save results
    with open("/tmp/matched_pairs.json", "w") as f:
        json.dump(matched_pairs, f, indent=2)
    print(f"\n  Saved to /tmp/matched_pairs.json")


if __name__ == "__main__":
    main()
