#!/usr/bin/env python3
"""
Comprehensive cross-platform matcher.
Finds ALL genuine cross-platform pairs between Kalshi and Polymarket US.
Handles: MLB, NHL, NBA, MLS, Bundesliga, Serie A, La Liga, EPL, ATP, WTA, ITF tennis.
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

def kalshi_date(raw):
    """Convert '26APR28' to '2026-04-28'."""
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", raw)
    if m:
        return "20%s-%s-%s" % (m.group(1), MONTHS.get(m.group(2),"00"), m.group(3))
    return None

# ── Kalshi ticker parsers ──────────────────────────────────────────

def parse_kalshi_game(ticker):
    """Parse KXMLBGAME-26APR281835HOUBAL-BAL or KXBUNDESLIGAGAME-26MAY02LEVRBL-LEV."""
    # Sport prefix can be: MLB, NHL, BUNDESLIGA, MLS, SERIEA, LALIGA, EFLL1, AFL, etc.
    m = re.match(
        r"KX([A-Z0-9]+?)GAME-(\d{2}[A-Z]{3}\d{2})(\d*)([A-Z]+)-([A-Z]+)$",
        ticker
    )
    if m:
        sport = m.group(1).lower()
        date_raw = m.group(2)
        time_raw = m.group(3)
        teams_raw = m.group(4).lower()
        side = m.group(5).lower()
        return {
            "type": "game",
            "sport": sport,
            "date": kalshi_date(date_raw),
            "teams_raw": teams_raw,
            "side": side,
        }
    return None

def parse_kalshi_match(ticker):
    """Parse KXATPMATCH-26APR27POTRYB-RYB or KXATPCHALLENGERMATCH-26APR27..."""
    m = re.match(
        r"KX([A-Z0-9]+?)MATCH-(\d{2}[A-Z]{3}\d{2})([A-Z]+)-([A-Z]+)$",
        ticker
    )
    if m:
        sport = m.group(1).lower()
        date_raw = m.group(2)
        players_raw = m.group(3).lower()
        side = m.group(4).lower()
        return {
            "type": "match",
            "sport": sport,
            "date": kalshi_date(date_raw),
            "players_raw": players_raw,
            "side": side,
        }
    return None

# ── Polymarket slug parsers ────────────────────────────────────────

def parse_poly_slug(slug):
    """Parse aec-mlb-bos-tor-2026-04-28 or atc-bun-lev-rbl-2026-05-02-lev."""
    m = re.match(
        r"(aec|tec|atc|tsc|paccc)-(\w+)-([\w]+)-([\w]+)-(\d{4}-\d{2}-\d{2})(?:-([\w]+))?$",
        slug
    )
    if m:
        return {
            "prefix": m.group(1),
            "sport": m.group(2),
            "id1": m.group(3),
            "id2": m.group(4),
            "date": m.group(5),
            "side": m.group(6) if m.group(6) else None,
        }
    return None

# ── Sport code mapping ─────────────────────────────────────────────
# Kalshi sport prefix → Polymarket sport code
SPORT_MAP = {
    "mlb": "mlb",
    "nhl": "nhl",
    "mls": "mls",
    "bundesliga": "bun",
    "seriea": "sea",
    "laliga": "lal",
    "efll1": "epl",
    "atp": "atp",
    "atpchallenger": "atp",
    "wta": "wta",
    "wtachallenger": "wta",
    "itf": "itf",
    "itfw": "wta",
}

# ── Team abbreviation normalization ────────────────────────────────
TEAM_NORM = {
    # MLS
    "atl": "atl", "mtl": "mtl", "mim": "mim",
    "atx": "aus", "aus": "aus",  # Austin FC
    "stl": "stl",
    # MLB
    "hou": "hou", "bal": "bal", "det": "det", "nyy": "nyy",
    "tex": "tex", "wsh": "wsh", "nym": "nym", "tor": "tor",
    "min": "min", "az": "az", "mil": "mil", "bos": "bos",
    "sd": "sd", "chc": "chc",
    # NHL
    "buf": "buf", "ana": "ana", "edm": "edm",
    # Bundesliga
    "lev": "lev", "rbl": "rbl", "bmg": "bmg", "bvb": "bvb",
    # Serie A
    "ata": "ata", "gen": "gen", "bol": "bol", "cag": "cag",
    "rom": "rom", "fio": "fio",
    # La Liga
    "osa": "osa", "bar": "fcb", "fcb": "fcb",
    # EPL
    "ars": "ars", "ful": "ful", "ast": "ast", "tot": "tot",
    "bor": "bor", "cry": "cry", "bre": "bre", "whu": "whu",
    "cfc": "cfc", "not": "not",
    # NBA
    "atl": "atl", "ny": "ny",
}

def norm_team(t):
    t = t.lower()
    return TEAM_NORM.get(t, t)

def try_split_teams(combined, t1, t2):
    """Try to split 'HOUBAL' into ('hou','bal') matching t1,t2."""
    combined = combined.lower()
    t1 = norm_team(t1)
    t2 = norm_team(t2)

    for i in range(2, len(combined)):
        a = norm_team(combined[:i])
        b = norm_team(combined[i:])
        if (a == t1 and b == t2) or (a == t2 and b == t1):
            return True
        # Prefix match (3+ chars)
        if len(a) >= 2 and len(b) >= 2:
            if (a.startswith(t1[:3]) or t1.startswith(a[:3])) and \
               (b.startswith(t2[:3]) or t2.startswith(b[:3])):
                return True
            if (a.startswith(t2[:3]) or t2.startswith(a[:3])) and \
               (b.startswith(t1[:3]) or t1.startswith(b[:3])):
                return True
    return False

def try_match_players(combined, p1, p2):
    """Try to match Kalshi player concat with Polymarket player abbreviations."""
    combined = combined.lower()
    p1 = p1.lower()[:3]
    p2 = p2.lower()[:3]

    for i in range(2, len(combined)):
        a = combined[:i][:3]
        b = combined[i:][:3]
        if len(b) < 2:
            continue
        if (a == p1 and b == p2) or (a == p2 and b == p1):
            return True
    return False

def side_matches(k_side, p_side):
    """Check if Kalshi side matches Polymarket side."""
    if not p_side:
        return True  # Polymarket slug has no side — it's the base market
    ks = norm_team(k_side)
    ps = norm_team(p_side)
    if ks == ps:
        return True
    if ks[:3] == ps[:3]:
        return True
    # Handle TIE/DRAW
    if k_side.lower() == "tie" and p_side.lower() == "draw":
        return True
    if k_side.lower() == "draw" and p_side.lower() == "tie":
        return True
    return False


def main():
    data = fetch("/api/prices")

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

    # Parse all Polymarket slugs
    poly_parsed = {}
    for slug, p in poly.items():
        parsed = parse_poly_slug(slug)
        if parsed:
            poly_parsed[slug] = {**parsed, "price": p}

    # Index by (sport, date) for fast lookup
    poly_index = defaultdict(list)
    for slug, pp in poly_parsed.items():
        key = (pp["sport"], pp["date"])
        poly_index[key].append((slug, pp))

    matched = []
    seen = set()

    for ticker, kp in kalshi.items():
        # Try game parser first, then match parser
        parsed = parse_kalshi_game(ticker)
        is_tennis = False
        if not parsed:
            parsed = parse_kalshi_match(ticker)
            if parsed:
                is_tennis = True
        if not parsed:
            continue

        k_sport_raw = parsed["sport"]
        p_sport = SPORT_MAP.get(k_sport_raw)
        if not p_sport:
            continue

        k_date = parsed["date"]
        k_side = parsed["side"]
        k_teams = parsed.get("teams_raw", parsed.get("players_raw", ""))

        candidates = poly_index.get((p_sport, k_date), [])

        for p_slug, pp in candidates:
            p_side = pp.get("side")

            # Side must match
            if not side_matches(k_side, p_side):
                continue

            # Teams/players must match
            if is_tennis:
                if not try_match_players(k_teams, pp["id1"], pp["id2"]):
                    continue
            else:
                if not try_split_teams(k_teams, pp["id1"], pp["id2"]):
                    continue

            pair_key = (ticker, p_slug)
            if pair_key in seen:
                continue
            seen.add(pair_key)

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
                "k_sport_raw": k_sport_raw,
                "date": k_date,
                "k_side": k_side,
                "p_side": p_side or "N/A",
                "kalshi_yes": ky,
                "kalshi_no": kn,
                "poly_yes": py,
                "poly_no": pn,
                "edge_cents": round(best_edge * 100, 1),
                "direction": direction,
                "is_binary_k": abs(ky + kn - 1.0) < 0.06,
                "is_binary_p": abs(py + pn - 1.0) < 0.06,
            })

    matched.sort(key=lambda x: x["edge_cents"], reverse=True)

    print("=" * 80)
    print("COMPREHENSIVE CROSS-PLATFORM MATCHER RESULTS")
    print("=" * 80)
    print("Kalshi tickers: %d" % len(kalshi))
    print("Polymarket slugs: %d" % len(poly))
    print("Polymarket parsed: %d" % len(poly_parsed))
    print("Matched pairs: %d" % len(matched))
    print()

    # Group by sport
    by_sport = defaultdict(list)
    for mp in matched:
        by_sport[mp["sport"]].append(mp)

    for sport in sorted(by_sport.keys()):
        items = by_sport[sport]
        print("\n--- %s (%d pairs) ---" % (sport.upper(), len(items)))
        for mp in items:
            binary = "OK" if mp["is_binary_k"] and mp["is_binary_p"] else "WARN"
            print("  [%s] %s | %s" % (mp["date"], mp["k_side"].upper(), mp["p_side"].upper()))
            print("    K: %s  yes=%.3f no=%.3f" % (mp["kalshi_ticker"], mp["kalshi_yes"], mp["kalshi_no"]))
            print("    P: %s  yes=%.3f no=%.3f" % (mp["poly_slug"], mp["poly_yes"], mp["poly_no"]))
            print("    Edge: %.1fc (%s) [%s]" % (mp["edge_cents"], mp["direction"], binary))

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    pos = [m for m in matched if m["edge_cents"] > 0]
    binary_ok = [m for m in matched if m["is_binary_k"] and m["is_binary_p"]]
    print("Total matched pairs: %d" % len(matched))
    print("With positive edge: %d" % len(pos))
    print("Binary-OK: %d" % len(binary_ok))
    print("By sport:")
    for sport in sorted(by_sport.keys()):
        items = by_sport[sport]
        pos_items = [i for i in items if i["edge_cents"] > 0]
        print("  %s: %d pairs, %d positive edge" % (sport.upper(), len(items), len(pos_items)))

    with open("/tmp/comprehensive_matches.json", "w") as f:
        json.dump(matched, f, indent=2)
    print("\nSaved to /tmp/comprehensive_matches.json")


if __name__ == "__main__":
    main()
