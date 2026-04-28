#!/usr/bin/env python3
"""
Match Kalshi binary game markets with Polymarket US game markets.

Identified matchable Kalshi market types:
- KXMLBGAME-{date}{away}{home}-{side} — MLB game winners
- KXNHLGAME-{date}{away}{home}-{side} — NHL game winners
- KXATPMATCH-{date}{p1}{p2}-{side} — ATP tennis match winners
- KXWTAMATCH-{date}{p1}{p2}-{side} — WTA tennis match winners
- KXITFMATCH-{date}{p1}{p2}-{side} — ITF tennis match winners
- KXMLBF5-{date}{away}{home}-{side}/{TIE} — MLB first 5 innings winner

Polymarket US slug format:
- {prefix}-{sport}-{team1}-{team2}-{date}[-{side}]
  where prefix is aec/tec/atc/tsc
"""
import json
import re
import sys
import urllib.request
from collections import defaultdict

BASE = "http://localhost:8080"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())

MONTHS = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
          "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}

def parse_kalshi_date(date_str):
    """Convert '26APR28' to '2026-04-28'."""
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", date_str)
    if m:
        return f"20{m.group(1)}-{MONTHS.get(m.group(2),'00')}-{m.group(3)}"
    return None

def parse_kalshi_game_ticker(ticker):
    """Parse KXMLBGAME-26APR281835HOUBAL-BAL format."""
    # Pattern: KX{SPORT}GAME-{YY}{MON}{DD}{TIME}{AWAY}{HOME}-{SIDE}
    # The time is 4 digits, teams are uppercase letter sequences
    m = re.match(
        r"KX(\w+?)GAME-(\d{2}[A-Z]{3}\d{2})(\d{4})([A-Z]+)-([A-Z]+)$",
        ticker
    )
    if m:
        sport = m.group(1).lower()
        date_raw = m.group(2)
        time_raw = m.group(3)
        teams_raw = m.group(4)
        side = m.group(5).lower()
        date_iso = parse_kalshi_date(date_raw)
        return {
            "sport": sport,
            "date": date_iso,
            "time": time_raw,
            "teams_raw": teams_raw.lower(),
            "side": side,
        }
    return None

def parse_kalshi_match_ticker(ticker):
    """Parse KXATPMATCH-26APR27POTRYB-RYB tennis format."""
    m = re.match(
        r"KX(\w+?)MATCH-(\d{2}[A-Z]{3}\d{2})([A-Z]+)-([A-Z]+)$",
        ticker
    )
    if m:
        sport = m.group(1).lower()
        date_raw = m.group(2)
        players_raw = m.group(3)
        side = m.group(4).lower()
        date_iso = parse_kalshi_date(date_raw)
        return {
            "sport": sport,
            "date": date_iso,
            "players_raw": players_raw.lower(),
            "side": side,
        }
    return None

def parse_poly_slug(slug):
    """Parse aec-nba-atl-ny-2026-04-28[-side] format."""
    # Some slugs have player names for tennis: aec-atp-adawal-jiecui-2026-04-26
    m = re.match(
        r"(?:aec|tec|atc|tsc)-(\w+)-([\w]+)-([\w]+)-(\d{4}-\d{2}-\d{2})(?:-([\w]+))?$",
        slug
    )
    if m:
        return {
            "sport": m.group(1),
            "id1": m.group(2),
            "id2": m.group(3),
            "date": m.group(4),
            "side": m.group(5) if m.group(5) else None,
        }
    return None

def teams_match(k_teams_raw, p_id1, p_id2):
    """Check if Kalshi team pair matches Polymarket team pair."""
    # Kalshi concatenates: HOUBAL = HOU + BAL
    # Polymarket has separate: hou, bal or bos, tor
    # Try splitting k_teams_raw to match p_id1 and p_id2
    combined = k_teams_raw.lower()
    t1 = p_id1.lower()
    t2 = p_id2.lower()

    # Try: combined = t1 + t2 or t2 + t1
    if combined == t1 + t2 or combined == t2 + t1:
        return True

    # Try partial matches (3-letter codes)
    if len(combined) >= 4:
        for split in range(2, len(combined) - 1):
            a = combined[:split]
            b = combined[split:]
            if (a == t1 and b == t2) or (a == t2 and b == t1):
                return True
            # Substring match
            if (t1.startswith(a) or a.startswith(t1)) and (t2.startswith(b) or b.startswith(t2)):
                if min(len(a), len(t1)) >= 2 and min(len(b), len(t2)) >= 2:
                    return True

    return False

def tennis_players_match(k_players_raw, p_id1, p_id2):
    """Check if Kalshi tennis player pair matches Polymarket pair."""
    # Kalshi: POTRYB = POT + RYB (last name abbreviations)
    # Polymarket: potost, rybbap (might be 3-letter abbreviations of last names)
    combined = k_players_raw.lower()
    t1 = p_id1.lower()[:3]  # First 3 chars
    t2 = p_id2.lower()[:3]

    # Try matching first N chars
    for n in range(3, min(6, len(combined))):
        a = combined[:n]
        b = combined[n:]
        if not b:
            continue
        if (t1.startswith(a[:3]) and t2.startswith(b[:3])) or \
           (t2.startswith(a[:3]) and t1.startswith(b[:3])):
            return True

    return False


def main():
    data = fetch("/api/prices")

    # Collect prices by platform
    kalshi = {}
    poly = {}
    for key, p in data.items():
        raw = p.get("raw_market_id", "")
        if not raw:
            continue
        if p.get("platform") == "kalshi":
            kalshi[raw] = p
        elif p.get("platform") == "polymarket":
            poly[raw] = p

    # Parse all Polymarket game slugs
    poly_parsed = {}
    for slug, p in poly.items():
        parsed = parse_poly_slug(slug)
        if parsed:
            poly_parsed[slug] = {**parsed, "price": p}

    # Index Polymarket by (sport, date) for fast lookup
    poly_by_sport_date = defaultdict(list)
    for slug, pp in poly_parsed.items():
        key = (pp["sport"], pp["date"])
        poly_by_sport_date[key].append((slug, pp))

    # Map Kalshi sport codes to Polymarket sport codes
    sport_map = {
        "mlb": "mlb",
        "nhl": "nhl",
        "nba": "nba",
        "atp": "atp",
        "wta": "wta",
        "itf": "itf",
        "itfw": "wta",
        "atpchallenger": "atp",
        "wtachallenger": "wta",
    }

    matched = []
    seen_pairs = set()

    # Match game-winner markets
    for ticker, kp in kalshi.items():
        parsed = parse_kalshi_game_ticker(ticker)
        if not parsed:
            parsed = parse_kalshi_match_ticker(ticker)
            if not parsed:
                continue

        k_sport = parsed.get("sport", "")
        p_sport = sport_map.get(k_sport, k_sport)
        k_date = parsed.get("date", "")
        k_side = parsed.get("side", "")

        # Look up Polymarket games for this sport+date
        candidates = poly_by_sport_date.get((p_sport, k_date), [])

        for p_slug, pp in candidates:
            p_side = pp.get("side")
            if not p_side:
                continue

            # Check if this side matches
            if not k_side.startswith(p_side[:3]) and not p_side.startswith(k_side[:3]):
                continue

            # Check if teams/players match
            k_teams = parsed.get("teams_raw", parsed.get("players_raw", ""))
            match_fn = tennis_players_match if p_sport in ("atp", "wta", "itf") else teams_match
            if not match_fn(k_teams, pp["id1"], pp["id2"]):
                continue

            pair_key = (ticker, p_slug)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            # Compute edge
            ky = kp.get("yes_price", 0)
            kn = kp.get("no_price", 0)
            py = pp["price"].get("yes_price", 0)
            pn = pp["price"].get("no_price", 0)
            e1 = 1.0 - ky - pn  # buy YES kalshi, buy NO poly
            e2 = 1.0 - py - kn  # buy YES poly, buy NO kalshi
            best_edge = max(e1, e2)
            direction = "K_YES+P_NO" if e1 > e2 else "P_YES+K_NO"

            matched.append({
                "kalshi_ticker": ticker,
                "poly_slug": p_slug,
                "sport": p_sport,
                "date": k_date,
                "side": k_side,
                "kalshi_yes": ky,
                "kalshi_no": kn,
                "poly_yes": py,
                "poly_no": pn,
                "edge_cents": round(best_edge * 100, 1),
                "direction": direction,
                "k_binary": abs(ky + kn - 1.0) < 0.05,
                "p_binary": abs(py + pn - 1.0) < 0.05,
            })

    # Sort by edge
    matched.sort(key=lambda x: x["edge_cents"], reverse=True)

    print(f"Kalshi markets: {len(kalshi)}")
    print(f"Polymarket US markets: {len(poly)}")
    print(f"Polymarket parsed game slugs: {len(poly_parsed)}")
    print(f"Matched cross-platform pairs: {len(matched)}")

    print(f"\n{'='*70}")
    print(f"VALIDATED CROSS-PLATFORM GAME PAIRS")
    print(f"{'='*70}")

    for mp in matched:
        binary_check = "OK" if mp["k_binary"] and mp["p_binary"] else "WARN-NOT-BINARY"
        print(f"\n  {mp['sport'].upper()} | {mp['date']} | Side: {mp['side'].upper()}")
        print(f"    Kalshi:     {mp['kalshi_ticker']}")
        print(f"      yes={mp['kalshi_yes']:.3f} no={mp['kalshi_no']:.3f} binary={mp['k_binary']}")
        print(f"    Polymarket: {mp['poly_slug']}")
        print(f"      yes={mp['poly_yes']:.3f} no={mp['poly_no']:.3f} binary={mp['p_binary']}")
        print(f"    Edge: {mp['edge_cents']:.1f}c ({mp['direction']}) [{binary_check}]")

    # Summary by sport
    by_sport = defaultdict(list)
    for mp in matched:
        by_sport[mp["sport"]].append(mp)
    print(f"\n{'='*70}")
    print("SUMMARY BY SPORT:")
    for sport in sorted(by_sport.keys()):
        items = by_sport[sport]
        pos_edge = [i for i in items if i["edge_cents"] > 0]
        print(f"  {sport.upper()}: {len(items)} pairs, {len(pos_edge)} with positive edge")

    with open("/tmp/matched_game_pairs.json", "w") as f:
        json.dump(matched, f, indent=2)
    print(f"\nSaved to /tmp/matched_game_pairs.json")


if __name__ == "__main__":
    main()
