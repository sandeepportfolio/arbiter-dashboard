#!/usr/bin/env python3
"""
Continuous cross-platform market discovery and validation.
Finds new game-winner pairs between Kalshi and Polymarket US,
validates them, and adds confirmed seeds to market_seeds_auto.json.

Run periodically (every 4-6 hours) to catch new daily games.
"""
import json
import hashlib
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from arbiter.mapping.team_aliases import (
    KNOWN_NON_MATCHES,
    detect_polarity,
    norm_team,
    same_team,
    try_split_teams,
)

BASE = "http://localhost:8080"
FIXTURE = Path(__file__).resolve().parent.parent / "arbiter" / "mapping" / "fixtures" / "market_seeds_auto.json"

MONTHS = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
          "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}

# ── Kalshi API for verification ─────────────────────────────────────
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())

def fetch_kalshi_market(ticker):
    """Fetch market title from Kalshi public API."""
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
    m = re.match(r"(aec|tec|atc|tsc|paccc)-(\w+)-([\w]+)-([\w]+)-(\d{4}-\d{2}-\d{2})(?:-([\w]+))?$", slug)
    if m:
        return {"prefix":m.group(1),"sport":m.group(2),"id1":m.group(3),
                "id2":m.group(4),"date":m.group(5),"side":m.group(6)}
    return None

# ── Sport mapping ───────────────────────────────────────────────────
SPORT_MAP = {
    "mlb":"mlb","nhl":"nhl","mls":"mls","bundesliga":"bun",
    "seriea":"sea","laliga":"lal","efll1":"epl",
    "atp":"atp","atpchallenger":"atp","wta":"wta",
    "wtachallenger":"wta","itf":"itf","itfw":"wta",
}

# Team normalization is centralized in arbiter/mapping/team_aliases.py.

def side_matches(k_side, p_side):
    if not p_side:
        return True
    return same_team(k_side, p_side)

SPORT_NAMES = {
    "mlb":"MLB","nhl":"NHL","mls":"MLS","bun":"Bundesliga",
    "sea":"Serie A","lal":"La Liga","epl":"EPL","atp":"ATP","wta":"WTA",
}

# ── Main discovery ──────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("CONTINUOUS DISCOVERY RUN — %s" % datetime.now().isoformat())
    print("=" * 70)

    data = fetch("/api/prices")
    kalshi = {v["raw_market_id"]: v for v in data.values() if v.get("platform") == "kalshi"}
    poly = {v["raw_market_id"]: v for v in data.values() if v.get("platform") == "polymarket"}

    # Parse Polymarket slugs
    poly_parsed = {}
    for slug, p in poly.items():
        parsed = parse_poly_slug(slug)
        if parsed:
            poly_parsed[slug] = {**parsed, "price": p}

    poly_index = defaultdict(list)
    for slug, pp in poly_parsed.items():
        poly_index[(pp["sport"], pp["date"])].append((slug, pp))

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
    seen = set()

    for ticker, kp in kalshi.items():
        parsed = parse_kalshi_game(ticker)
        is_tennis = False
        if not parsed:
            parsed = parse_kalshi_match(ticker)
            if parsed:
                is_tennis = True
        if not parsed:
            continue

        p_sport = SPORT_MAP.get(parsed["sport"])
        if not p_sport:
            continue

        candidates = poly_index.get((p_sport, parsed["date"]), [])
        k_teams = parsed.get("teams_raw", parsed.get("players_raw", ""))

        for p_slug, pp in candidates:
            if not try_split_teams(k_teams, pp["id1"], pp["id2"]):
                continue

            pair_key = (ticker, p_slug)
            if pair_key in seen or pair_key in existing_keys:
                continue
            seen.add(pair_key)

            # Known-trap guard (MTL/MIM and similar)
            k_side_lc = (parsed.get("side") or "").lower()
            p_side_lc = (pp.get("side") or "").lower()
            if (k_side_lc, p_side_lc) in KNOWN_NON_MATCHES or (
                p_side_lc, k_side_lc
            ) in KNOWN_NON_MATCHES:
                print("  SKIP (known non-match): %s vs %s" % (ticker, p_slug))
                continue

            polarity = detect_polarity(
                kalshi_side=parsed["side"],
                poly_side_suffix=pp.get("side"),
                poly_team1=pp["id1"],
                poly_team2=pp["id2"],
            )
            if polarity == "unrelated":
                continue
            polarity_flipped = polarity == "flipped"

            # Verify via Kalshi API
            km = fetch_kalshi_market(ticker)
            k_title = km.get("title", "")

            sport_full = SPORT_NAMES.get(p_sport, p_sport.upper())
            if parsed["side"] in ("tie", "draw"):
                desc = "%s: Draw/Tie on %s" % (sport_full, parsed["date"])
            else:
                desc = "%s: %s wins on %s" % (sport_full, parsed["side"].upper(), parsed["date"])

            h = hashlib.md5(("%s_%s" % (ticker, p_slug)).encode()).hexdigest()[:8]
            canonical_id = "GAME_%s_%s_%s_%s" % (
                p_sport.upper(), parsed["date"].replace("-",""),
                parsed["side"].upper(), h
            )

            tags = ["sports", p_sport, "game-winner", "auto-discovered"]
            if polarity_flipped:
                tags.append("polarity-flipped")

            entry = {
                "canonical_id": canonical_id,
                "description": desc,
                "kalshi": ticker,
                "polymarket": p_slug,
                "polymarket_question": desc,
                "category": "sports",
                "status": "confirmed",
                "allow_auto_trade": not polarity_flipped,
                "polarity_flipped": polarity_flipped,
                "tags": tags,
                "notes": "Auto-discovered %s. Kalshi title: %s%s" % (
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    k_title,
                    " (polarity flipped — operator review required)" if polarity_flipped else "",
                ),
                "resolution_criteria": {
                    "kalshi": {"source": "Kalshi", "rule": k_title or desc},
                    "polymarket": {"source": "Polymarket US", "rule": desc},
                    "criteria_match": "identical",
                },
                "resolution_match_status": "identical",
            }
            new_pairs.append(entry)
            tag = "NEW (FLIPPED)" if polarity_flipped else "NEW"
            print("  %s: %s — %s" % (tag, canonical_id, desc))

    # Merge new pairs into fixture
    if new_pairs:
        for entry in new_pairs:
            existing.append(entry)
            existing_keys.add((entry["kalshi"], entry["polymarket"]))
        FIXTURE.write_text(json.dumps(existing, indent=2))

    print("\n--- SUMMARY ---")
    print("Kalshi: %d, Polymarket: %d parsed" % (len(kalshi), len(poly_parsed)))
    print("New pairs found: %d" % len(new_pairs))
    print("Total seeds in fixture: %d" % len(existing))
    print("Confirmed: %d" % len([s for s in existing if s.get("status") == "confirmed"]))

    if new_pairs:
        print("\nNEW PAIRS ADDED — rebuild Docker to deploy:")
        print("  docker compose -f docker-compose.prod.yml --env-file .env.production build arbiter-api-prod")
        print("  docker compose -f docker-compose.prod.yml --env-file .env.production up -d arbiter-api-prod")

    return len(new_pairs)


if __name__ == "__main__":
    n = main()
    sys.exit(0 if n >= 0 else 1)
