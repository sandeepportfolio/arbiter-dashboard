#!/usr/bin/env python3
"""
Validate matched pairs with correct polarity handling.

Critical insight:
- Polymarket WITH side (atc-bun-lev-rbl-2026-05-02-lev): YES = that side wins
- Polymarket WITHOUT side (aec-mlb-hou-bal-2026-04-28): YES = FIRST team (hou) wins
- Kalshi (KXMLBGAME-26APR281835HOUBAL-BAL): YES = side (BAL) wins

When Poly has no side and Kalshi side = Poly's SECOND team, YES/NO are FLIPPED.
"""
import json
import urllib.request

BASE = "http://localhost:8080"

TEAM_NORM = {
    "atl": "atl", "mtl": "mim", "mim": "mim",
    "atx": "aus", "aus": "aus", "stl": "stl",
    "hou": "hou", "bal": "bal", "det": "det", "nyy": "nyy",
    "tex": "tex", "wsh": "wsh", "nym": "nym", "tor": "tor",
    "min": "min", "az": "az", "mil": "mil", "bos": "bos",
    "sd": "sd", "chc": "chc",
    "buf": "buf", "ana": "ana", "edm": "edm",
    "lev": "lev", "rbl": "rbl", "bmg": "bmg", "bvb": "bvb",
    "ata": "ata", "gen": "gen", "bol": "bol", "cag": "cag",
    "rom": "rom", "fio": "fio",
    "osa": "osa", "bar": "fcb", "fcb": "fcb",
}
def norm_team(t):
    t = t.lower()
    return TEAM_NORM.get(t, t)

def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())

# Load comprehensive matches
with open("/tmp/comprehensive_matches.json") as f:
    matches = json.load(f)

print("=" * 80)
print("POLARITY-CORRECTED VALIDATION OF ALL %d MATCHED PAIRS" % len(matches))
print("=" * 80)

validated = []

for mp in matches:
    kt = mp["kalshi_ticker"]
    ps = mp["poly_slug"]
    ky = mp["kalshi_yes"]
    kn = mp["kalshi_no"]
    py = mp["poly_yes"]
    pn = mp["poly_no"]
    k_side = mp["k_side"].lower()
    p_side = mp["p_side"].lower() if mp["p_side"] != "N/A" else None

    # Determine what YES means on each platform
    # Kalshi: YES = k_side wins
    k_yes_means = "%s wins" % k_side.upper()

    # Parse poly slug to get team order
    parts = ps.split("-")
    # Format: prefix-sport-team1-team2-date[-side]
    p_team1 = parts[2].lower()
    p_team2 = parts[3].lower()

    if p_side:
        # Poly has explicit side: YES = p_side wins
        p_yes_means = "%s wins" % p_side.upper()
    else:
        # Poly has no side: YES = first team wins
        p_yes_means = "%s wins" % p_team1.upper()

    # Determine if YES tracks the same outcome
    # Normalize team names for comparison
    k_norm = norm_team(k_side)
    if p_side:
        p_norm = norm_team(p_side)
    else:
        p_norm_t1 = norm_team(p_team1)
        p_norm_t2 = norm_team(p_team2)

    # TIE and DRAW are the SAME outcome
    tie_draw = {k_side.lower(), (p_side or "").lower()}
    if tie_draw == {"tie", "draw"} or (k_side.lower() == "tie" and p_side and p_side.lower() == "draw"):
        same_polarity = True
    elif p_side:
        # Both have explicit sides — check if they match
        same_polarity = (k_norm == norm_team(p_side))
    else:
        # Poly has no side — YES = team1 wins
        # If Kalshi side = team1, same polarity
        # If Kalshi side = team2, FLIPPED polarity
        if k_norm == p_norm_t1 or k_side[:3] == p_team1[:3]:
            same_polarity = True
        elif k_norm == p_norm_t2 or k_side[:3] == p_team2[:3]:
            same_polarity = False
        else:
            print("\n  WARNING: Cannot determine polarity for %s vs %s" % (kt, ps))
            print("    k_side=%s, p_team1=%s, p_team2=%s" % (k_side, p_team1, p_team2))
            continue

    # Compute correct edge
    if same_polarity:
        # Both YES = same outcome
        # Strategy 1: Buy K YES + P NO → pays if k_side wins
        # Strategy 2: Buy P YES + K NO → pays if k_side wins (opposite)
        # Wait — this is wrong. Both strategies pay on ONE outcome.
        # Arb = buy YES cheapest + buy NO cheapest for the SAME event
        # e1: buy K YES (ky) + buy P NO (pn) = covers same_side_wins + same_side_loses
        # This works only if K YES and P NO are COMPLEMENTARY
        # K YES = side wins, P NO = side doesn't win (since P YES = same side wins)
        # So K YES + P NO covers both outcomes. Cost = ky + pn. Payout = 1.0.
        e1 = 1.0 - ky - pn  # buy K YES + P NO
        e2 = 1.0 - py - kn  # buy P YES + K NO
    else:
        # FLIPPED: K YES = opposite of P YES
        # K YES = k_side wins, P YES = other_team wins
        # So K YES + P YES covers both outcomes! Cost = ky + py. Payout = 1.0.
        e1 = 1.0 - ky - py  # buy K YES + P YES (covers both outcomes)
        e2 = 1.0 - kn - pn  # buy K NO + P NO (covers both outcomes)

    best = max(e1, e2)
    if same_polarity:
        dir_str = "K_YES+P_NO" if e1 > e2 else "P_YES+K_NO"
    else:
        dir_str = "K_YES+P_YES" if e1 > e2 else "K_NO+P_NO"

    validated.append({
        "kalshi_ticker": kt,
        "poly_slug": ps,
        "sport": mp["sport"],
        "date": mp["date"],
        "k_side": k_side,
        "p_side": p_side or "N/A",
        "same_polarity": same_polarity,
        "k_yes_means": k_yes_means,
        "p_yes_means": p_yes_means,
        "kalshi_yes": ky,
        "kalshi_no": kn,
        "poly_yes": py,
        "poly_no": pn,
        "edge_cents": round(best * 100, 1),
        "direction": dir_str,
        "is_binary_k": abs(ky + kn - 1.0) < 0.06,
        "is_binary_p": abs(py + pn - 1.0) < 0.06,
    })

validated.sort(key=lambda x: x["edge_cents"], reverse=True)

for v in validated:
    pol = "SAME" if v["same_polarity"] else "FLIPPED"
    binary = "OK" if v["is_binary_k"] and v["is_binary_p"] else "WARN"
    print("\n  %s | %s | Side: %s [%s polarity]" % (v["sport"].upper(), v["date"], v["k_side"].upper(), pol))
    print("    K: %s" % v["kalshi_ticker"])
    print("      YES = %s, yes=%.3f no=%.3f" % (v["k_yes_means"], v["kalshi_yes"], v["kalshi_no"]))
    print("    P: %s" % v["poly_slug"])
    print("      YES = %s, yes=%.3f no=%.3f" % (v["p_yes_means"], v["poly_yes"], v["poly_no"]))
    print("    EDGE: %.1fc (%s) [%s] [%s]" % (v["edge_cents"], v["direction"], binary, pol))

print("\n" + "=" * 80)
print("CORRECTED SUMMARY")
pos = [v for v in validated if v["edge_cents"] > 0]
print("Total validated: %d" % len(validated))
print("Positive edge: %d" % len(pos))
for v in pos:
    print("  %.1fc: %s vs %s (%s)" % (v["edge_cents"], v["kalshi_ticker"], v["poly_slug"], v["direction"]))

with open("/tmp/validated_pairs.json", "w") as f:
    json.dump(validated, f, indent=2)
print("\nSaved to /tmp/validated_pairs.json")
