#!/usr/bin/env python3
"""Remove flipped-polarity mappings from market_seeds_auto.json.

Flipped polarity means Kalshi YES tracks the OPPOSITE outcome from Polymarket YES.
The scanner assumes same polarity, so these would produce WRONG edge calculations.

Affected:
- GAME_MLB_20260428_BAL: K YES=BAL wins, P YES=HOU wins (first team listed, no side)
- GAME_NHL_20260428_BUF: K YES=BUF wins, P YES=BOS wins (first team listed, no side)
"""
import json
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "arbiter" / "mapping" / "fixtures" / "market_seeds_auto.json"

seeds = json.loads(FIXTURE.read_text())

# Identify flipped-polarity game seeds
# These are where:
# 1. Polymarket slug has NO side suffix (aec-mlb-hou-bal-2026-04-28)
# 2. Kalshi side matches the SECOND team in the Polymarket slug

FLIPPED_IDS = set()
for s in seeds:
    if not s.get("canonical_id", "").startswith("GAME_"):
        continue
    poly = s.get("polymarket", "")
    kalshi = s.get("kalshi", "")
    parts = poly.split("-")
    # Check if slug has a side
    # aec-mlb-hou-bal-2026-04-28 = 7 parts (no side)
    # atc-bun-lev-rbl-2026-05-02-lev = 8 parts (has side)
    has_side = len(parts) >= 8
    if has_side:
        continue  # Side-specific slugs are fine

    # No side: YES = first team. Check if Kalshi side matches second team
    if len(parts) >= 6:
        p_team1 = parts[2]
        p_team2 = parts[3]
        # Extract Kalshi side from ticker
        k_parts = kalshi.split("-")
        if len(k_parts) >= 2:
            k_side = k_parts[-1].lower()
            if k_side == p_team2.lower():
                FLIPPED_IDS.add(s["canonical_id"])
                print("FLIPPED (removing): %s" % s["canonical_id"])
                print("  K: %s (side=%s)" % (kalshi, k_side))
                print("  P: %s (team1=%s, team2=%s, no side)" % (poly, p_team1, p_team2))
                print("  Problem: K YES=%s wins, P YES=%s wins (OPPOSITE)" % (k_side.upper(), p_team1.upper()))

# Remove flipped entries
filtered = [s for s in seeds if s["canonical_id"] not in FLIPPED_IDS]
removed = len(seeds) - len(filtered)

FIXTURE.write_text(json.dumps(filtered, indent=2))
print("\nRemoved %d flipped-polarity entries." % removed)
print("Remaining seeds: %d" % len(filtered))
print("Confirmed: %d" % len([s for s in filtered if s.get("status") == "confirmed"]))
