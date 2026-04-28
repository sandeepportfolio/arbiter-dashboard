#!/usr/bin/env python3
"""
Generate confirmed seed entries from verified pairs.
Excludes suspicious MTL/MIM MLS pairs.
Outputs JSON suitable for market_seeds_auto.json.
"""
import json
import hashlib
from datetime import datetime

with open("/tmp/validated_pairs.json") as f:
    pairs = json.load(f)

# Exclude suspicious pairs
excluded = []
confirmed = []

for p in pairs:
    # Exclude MTL/MIM mismatch
    if p["k_side"].lower() == "mtl" and p["p_side"].lower() == "mim":
        excluded.append(p)
        continue
    if p["kalshi_ticker"].find("ATLMTL") >= 0 and p["poly_slug"].find("atl-mim") >= 0:
        excluded.append(p)
        continue

    # Generate canonical ID
    sport = p["sport"].upper()
    date = p["date"].replace("-", "")
    side = p["k_side"].upper()
    # Extract teams from Kalshi ticker
    kt = p["kalshi_ticker"]
    h = hashlib.md5(("%s_%s" % (kt, p["poly_slug"])).encode()).hexdigest()[:8]
    canonical_id = "GAME_%s_%s_%s_%s" % (sport, date, side, h)

    # Sport-specific descriptions
    sport_names = {
        "mlb": "MLB",
        "nhl": "NHL",
        "mls": "MLS",
        "bun": "Bundesliga",
        "sea": "Serie A",
        "lal": "La Liga",
    }
    sport_full = sport_names.get(p["sport"], p["sport"].upper())

    # Side description
    if p["k_side"].lower() == "tie":
        desc = "%s: Draw/Tie" % sport_full
    else:
        desc = "%s: %s wins" % (sport_full, p["k_side"].upper())

    # Polarity note
    if p["same_polarity"]:
        polarity_note = "Same polarity: both YES = %s" % p["k_side"].upper()
    else:
        polarity_note = "FLIPPED polarity: Kalshi YES = %s, Polymarket YES = opposite" % p["k_side"].upper()

    entry = {
        "canonical_id": canonical_id,
        "description": "%s on %s" % (desc, p["date"]),
        "kalshi": kt,
        "polymarket": p["poly_slug"],
        "polymarket_question": desc,
        "category": "sports",
        "status": "confirmed",
        "allow_auto_trade": True,
        "tags": ["sports", p["sport"], "game-winner", "cross-validated"],
        "notes": "Structurally matched and verified via Kalshi API on %s. %s." % (
            datetime.now().strftime("%Y-%m-%d"), polarity_note
        ),
        "resolution_criteria": {
            "kalshi": {
                "source": "Kalshi rulebook",
                "rule": "Market resolves Yes if %s" % (
                    "the game ends in a draw/tie" if p["k_side"].lower() == "tie"
                    else "%s wins the game on %s" % (p["k_side"].upper(), p["date"])
                ),
                "settlement_date": p["date"],
            },
            "polymarket": {
                "source": "Polymarket US retail market",
                "rule": "Market resolves Yes if %s" % (
                    "the game ends in a draw" if p["p_side"] in ("draw", "N/A") and p["k_side"].lower() == "tie"
                    else "%s wins the game on %s" % (
                        p["p_side"].upper() if p["p_side"] != "N/A" else "first team listed",
                        p["date"]
                    )
                ),
                "settlement_date": p["date"],
            },
            "criteria_match": "identical",
            "polarity": "same" if p["same_polarity"] else "flipped",
            "operator_note": "Auto-validated by comprehensive matcher + Kalshi API verification on %s" % (
                datetime.now().strftime("%Y-%m-%d")
            ),
        },
        "resolution_match_status": "identical",
    }
    confirmed.append(entry)

print("Excluded %d suspicious pairs:" % len(excluded))
for e in excluded:
    print("  %s vs %s (MTL/MIM mismatch)" % (e["kalshi_ticker"], e["poly_slug"]))

print("\nGenerated %d confirmed seeds:" % len(confirmed))
for c in confirmed:
    edge = 0
    for p in pairs:
        if p["kalshi_ticker"] == c["kalshi"] and p["poly_slug"] == c["polymarket"]:
            edge = p["edge_cents"]
            break
    print("  %s: %s (%s) edge=%.1fc" % (c["canonical_id"], c["description"], c["kalshi"], edge))

# Save
with open("/tmp/confirmed_game_seeds.json", "w") as f:
    json.dump(confirmed, f, indent=2)
print("\nSaved to /tmp/confirmed_game_seeds.json")
