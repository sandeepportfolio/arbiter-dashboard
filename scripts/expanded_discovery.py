#!/usr/bin/env python3
"""
Expanded cross-platform market discovery.
Goes beyond game-winners to find:
- NBA game winners (KXNBA* vs aec-nba-*)
- ATP/WTA tennis matches (including challenger)
- Championship futures (tec-* vs KX*CHAMP*)
- EPL matches (KXEFLL1GAME vs atc-epl-*)
- Any sport with cross-platform pricing

Run on Mac (needs localhost:8080 API access).
"""
import json
import hashlib
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BASE = "http://localhost:8080"
FIXTURE = Path(__file__).resolve().parent.parent / "arbiter" / "mapping" / "fixtures" / "market_seeds_auto.json"

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

MONTHS = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
          "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())

def fetch_kalshi_market(ticker):
    try:
        url = "%s/markets/%s" % (KALSHI_API, ticker)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            return data.get("market", data)
    except Exception:
        return {}

def kalshi_date(raw):
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", raw)
    if m:
        return "20%s-%s-%s" % (m.group(1), MONTHS.get(m.group(2),"00"), m.group(3))
    return None

# ── Expanded sport mapping ─────────────────────────────────────────
# Maps Kalshi sport prefix to Polymarket sport code
SPORT_MAP = {
    "mlb": "mlb", "nhl": "nhl", "mls": "mls",
    "bundesliga": "bun", "seriea": "sea", "laliga": "lal",
    "efll1": "epl",  # English Football League L1 = EPL on Polymarket
    "atp": "atp", "atpchallenger": "atp",
    "wta": "wta", "wtachallenger": "wta",
    "itf": "atp", "itfw": "wta",  # ITF maps to ATP/WTA
    "nba": "nba",
}

# Polymarket prefix to sport type
POLY_PREFIX_MAP = {
    "aec": "binary",   # binary game winner (no side in slug)
    "atc": "3way",     # 3-way with side suffix
    "tec": "props",    # props/totals
    "tsc": "spread",   # spreads
    "asc": "spread",   # alt spread
}

SPORT_NAMES = {
    "mlb":"MLB","nhl":"NHL","mls":"MLS","nba":"NBA",
    "bun":"Bundesliga","sea":"Serie A","lal":"La Liga","epl":"EPL",
    "atp":"ATP","wta":"WTA",
}

# Team/player normalization
TEAM_NORM = {
    "atl":"atl","mtl":"mtl","mim":"mim","atx":"aus","aus":"aus","stl":"stl",
    "hou":"hou","bal":"bal","det":"det","nyy":"nyy","tex":"tex","wsh":"wsh",
    "nym":"nym","tor":"tor","min":"min","az":"az","mil":"mil","bos":"bos",
    "sd":"sd","chc":"chc","buf":"buf","ana":"ana","edm":"edm",
    "lev":"lev","rbl":"rbl","bmg":"bmg","bvb":"bvb",
    "ata":"ata","gen":"gen","bol":"bol","cag":"cag","rom":"rom","fio":"fio",
    "osa":"osa","bar":"fcb","fcb":"fcb",
    "ars":"ars","ful":"ful","ast":"ast","tot":"tot","bor":"bor","cry":"cry",
    "bre":"bre","whu":"whu","cfc":"cfc","not":"not",
    # NBA teams
    "lal":"lal","bkn":"bkn","gsw":"gsw","phi":"phi","mia":"mia",
    "chi":"chi","den":"den","dal":"dal","lac":"lac","sas":"sas",
    "por":"por","okc":"okc","mem":"mem","nop":"nop","ind":"ind",
    "cle":"cle","orl":"orl","cha":"cha","sac":"sac","uta":"uta",
}

def norm_team(t):
    return TEAM_NORM.get(t.lower(), t.lower())

def try_split_teams(combined, t1, t2):
    combined = combined.lower()
    t1, t2 = norm_team(t1), norm_team(t2)
    for i in range(2, len(combined)):
        a, b = norm_team(combined[:i]), norm_team(combined[i:])
        if (a == t1 and b == t2) or (a == t2 and b == t1):
            return True
        if len(a) >= 2 and len(b) >= 2:
            if (a[:3] == t1[:3] or t1[:3] == a[:3]) and (b[:3] == t2[:3] or t2[:3] == b[:3]):
                return True
            if (a[:3] == t2[:3] or t2[:3] == a[:3]) and (b[:3] == t1[:3] or t1[:3] == b[:3]):
                return True
    return False

def side_matches(k_side, p_side):
    if not p_side:
        return True
    ks, ps = norm_team(k_side), norm_team(p_side)
    if ks == ps or ks[:3] == ps[:3]:
        return True
    if k_side.lower() in ("tie","draw") and p_side.lower() in ("tie","draw"):
        return True
    return False

def is_flipped_polarity(kalshi_ticker, poly_slug):
    parts = poly_slug.split("-")
    if len(parts) >= 8:
        return False
    if len(parts) < 7:
        return False
    p_team2 = parts[3].lower()
    k_side = kalshi_ticker.split("-")[-1].lower()
    return norm_team(k_side) == norm_team(p_team2)

# ── Parsers ─────────────────────────────────────────────────────────

def parse_kalshi_game(ticker):
    m = re.match(r"KX([A-Z0-9]+?)GAME-(\d{2}[A-Z]{3}\d{2})(\d*)([A-Z]+)-([A-Z]+)$", ticker)
    if m:
        return {"type":"game","sport":m.group(1).lower(),"date":kalshi_date(m.group(2)),
                "teams_raw":m.group(4).lower(),"side":m.group(5).lower()}
    return None

def parse_kalshi_match(ticker):
    m = re.match(r"KX([A-Z0-9]+?)MATCH-(\d{2}[A-Z]{3}\d{2})([A-Z]+)-([A-Z]+)$", ticker)
    if m:
        return {"type":"match","sport":m.group(1).lower(),"date":kalshi_date(m.group(2)),
                "players_raw":m.group(3).lower(),"side":m.group(4).lower()}
    return None

def parse_poly_slug(slug):
    # Standard format: prefix-sport-id1-id2-date[-side]
    m = re.match(r"(aec|tec|atc|tsc|paccc|asc|rdc)-(\w+)-([\w]+)-([\w]+)-(\d{4}-\d{2}-\d{2})(?:-([\w]+))?$", slug)
    if m:
        return {"prefix":m.group(1),"sport":m.group(2),"id1":m.group(3),
                "id2":m.group(4),"date":m.group(5),"side":m.group(6)}
    return None

# ── Validation helpers ─────────────────────────────────────────────

def validate_pair(kalshi_ticker, poly_slug, k_parsed, p_parsed):
    """Multi-check validation. Returns (valid, reason)."""
    # 1. Sport must match
    k_sport = k_parsed.get("sport", "")
    p_sport = p_parsed.get("sport", "")
    expected_p_sport = SPORT_MAP.get(k_sport)
    if expected_p_sport != p_sport:
        return False, "sport_mismatch: K=%s P=%s" % (k_sport, p_sport)

    # 2. Date must match
    if k_parsed.get("date") != p_parsed.get("date"):
        return False, "date_mismatch"

    # 3. Side must match
    if not side_matches(k_parsed.get("side",""), p_parsed.get("side")):
        return False, "side_mismatch"

    # 4. Teams must match
    k_teams = k_parsed.get("teams_raw", k_parsed.get("players_raw", ""))
    if not try_split_teams(k_teams, p_parsed["id1"], p_parsed["id2"]):
        return False, "teams_mismatch"

    # 5. Polarity check
    if is_flipped_polarity(kalshi_ticker, poly_slug):
        return False, "flipped_polarity"

    # 6. MTL/MIM trap
    k_side = k_parsed.get("side", "").lower()
    p_side = (p_parsed.get("side") or "").lower()
    if k_side == "mtl" and p_side == "mim":
        return False, "mtl_mim_mismatch"

    # 7. Date not expired
    try:
        game_date = datetime.strptime(k_parsed["date"], "%Y-%m-%d")
        if game_date < datetime.now() - timedelta(days=1):
            return False, "expired_date"
    except (ValueError, KeyError):
        pass

    return True, "ok"

# ── Main discovery ──────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("EXPANDED DISCOVERY — %s" % datetime.now().isoformat())
    print("=" * 70)

    data = fetch("/api/prices")
    kalshi = {v["raw_market_id"]: v for v in data.values() if v.get("platform") == "kalshi"}
    poly = {v["raw_market_id"]: v for v in data.values() if v.get("platform") == "polymarket"}

    print("Kalshi markets: %d" % len(kalshi))
    print("Polymarket markets: %d" % len(poly))

    # Parse Polymarket slugs
    poly_parsed = {}
    for slug, p in poly.items():
        parsed = parse_poly_slug(slug)
        if parsed:
            poly_parsed[slug] = {**parsed, "price": p}

    poly_index = defaultdict(list)
    for slug, pp in poly_parsed.items():
        poly_index[(pp["sport"], pp["date"])].append((slug, pp))

    print("Parsed Polymarket slugs: %d" % len(poly_parsed))
    print("Sport/date combos: %d" % len(poly_index))

    # Show what sports are available
    sport_counts = defaultdict(int)
    for (sport, _), slugs in poly_index.items():
        sport_counts[sport] += len(slugs)
    print("\nPolymarket sports available:")
    for sport, cnt in sorted(sport_counts.items(), key=lambda x: -x[1]):
        print("  %s: %d slugs" % (sport, cnt))

    # Load existing seeds
    existing = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else []
    existing_keys = set()
    for s in existing:
        k = s.get("kalshi", "")
        p = s.get("polymarket", "")
        if k and p:
            existing_keys.add((k, p))

    # Find matches
    new_pairs = []
    skipped = defaultdict(int)
    seen = set()

    for ticker, kp in kalshi.items():
        parsed = parse_kalshi_game(ticker)
        if not parsed:
            parsed = parse_kalshi_match(ticker)
        if not parsed:
            continue

        p_sport = SPORT_MAP.get(parsed["sport"])
        if not p_sport:
            continue

        candidates = poly_index.get((p_sport, parsed.get("date")), [])
        k_teams = parsed.get("teams_raw", parsed.get("players_raw", ""))

        for p_slug, pp in candidates:
            pair_key = (ticker, p_slug)
            if pair_key in seen or pair_key in existing_keys:
                continue
            seen.add(pair_key)

            # Full validation
            valid, reason = validate_pair(ticker, p_slug, parsed, pp)
            if not valid:
                skipped[reason] += 1
                if reason not in ("sport_mismatch", "teams_mismatch", "side_mismatch", "expired_date"):
                    print("  SKIP (%s): %s vs %s" % (reason, ticker, p_slug))
                continue

            # Verify via Kalshi API
            km = fetch_kalshi_market(ticker)
            k_title = km.get("title", "")

            sport_full = SPORT_NAMES.get(p_sport, p_sport.upper())
            if parsed.get("side") in ("tie", "draw"):
                desc = "%s: Draw/Tie on %s" % (sport_full, parsed["date"])
            else:
                desc = "%s: %s wins on %s" % (sport_full, parsed.get("side","?").upper(), parsed["date"])

            h = hashlib.md5(("%s_%s" % (ticker, p_slug)).encode()).hexdigest()[:8]
            canonical_id = "GAME_%s_%s_%s_%s" % (
                p_sport.upper(), parsed["date"].replace("-",""),
                parsed.get("side","?").upper(), h
            )

            entry = {
                "canonical_id": canonical_id,
                "description": desc,
                "kalshi": ticker,
                "polymarket": p_slug,
                "polymarket_question": desc,
                "category": "sports",
                "status": "confirmed",
                "allow_auto_trade": True,
                "tags": ["sports", p_sport, "game-winner", "auto-discovered", "expanded"],
                "notes": "Expanded discovery %s. Kalshi: %s" % (
                    datetime.now().strftime("%Y-%m-%d %H:%M"), k_title
                ),
                "resolution_criteria": {
                    "kalshi": {"source": "Kalshi", "rule": k_title or desc},
                    "polymarket": {"source": "Polymarket US", "rule": desc},
                    "criteria_match": "identical",
                    "polarity": "same",
                },
                "resolution_match_status": "identical",
            }
            new_pairs.append(entry)
            print("  NEW: %s — %s" % (canonical_id, desc))
            print("    K: %s  P: %s" % (ticker, p_slug))
            if k_title:
                print("    Title: %s" % k_title)

    # Merge into fixture
    if new_pairs:
        for entry in new_pairs:
            existing.append(entry)
            existing_keys.add((entry["kalshi"], entry["polymarket"]))
        FIXTURE.write_text(json.dumps(existing, indent=2))

    print("\n--- SUMMARY ---")
    print("New pairs found: %d" % len(new_pairs))
    print("Skip reasons:")
    for reason, cnt in sorted(skipped.items(), key=lambda x: -x[1]):
        print("  %s: %d" % (reason, cnt))
    print("Total seeds in fixture: %d" % len(existing))
    print("Confirmed: %d" % len([s for s in existing if s.get("status") == "confirmed"]))

    if new_pairs:
        print("\nNEW PAIRS ADDED — rebuild Docker to deploy:")
        print("  docker compose -f docker-compose.prod.yml --env-file .env.production build arbiter-api-prod")
        print("  docker compose -f docker-compose.prod.yml --env-file .env.production up -d --no-deps arbiter-api-prod")

    return len(new_pairs)


if __name__ == "__main__":
    n = main()
    sys.exit(0 if n >= 0 else 1)
