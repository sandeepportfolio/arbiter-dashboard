"""
Centralized team-name normalization across Kalshi and Polymarket tickers.

Both venues abbreviate teams differently. Kalshi uses 3-letter codes inside
ticker stems (e.g. ``KXMLBGAME-26APR29HOUBAL-BAL``); Polymarket uses
3-letter slugs in URL parts (``aec-mlb-hou-bal-2026-04-29``). The two
catalogs disagree on dozens of teams, which silently breaks ``try_split_teams``
during discovery — every disagreement is a missed mapping.

This module maintains the canonical alias table per league. Add a new league
or alias by editing the per-league dict; the public helpers ``norm_team`` and
``same_team`` use the merged table at import time.

Trap: MTL (Montreal Canadiens / Expos) ≠ MIM (Inter Miami CF). Both share the
"Mi" prefix but are wholly different franchises in different sports — the
existing ``mtl_mim_mismatch`` guard in ``expanded_discovery.py`` is correct
and is preserved here as ``KNOWN_NON_MATCHES``.
"""
from __future__ import annotations

from typing import Iterable

# ─── League-by-league canonical aliases ────────────────────────────────────────
# Each entry maps a venue abbreviation (lowercase) → canonical key (lowercase).
# Multiple keys can canonicalize to the same value, which is how we declare an
# alias. Canonical keys are arbitrary but should match the more common spelling
# across both venues (so existing seeds keep working).

_MLB: dict[str, str] = {
    # AL East
    "bal": "bal", "bos": "bos", "nyy": "nyy", "tb": "tb", "tbr": "tb",
    "tor": "tor",
    # AL Central
    "cws": "cws", "chw": "cws", "cle": "cle", "det": "det", "kc": "kc",
    "kcr": "kc", "min": "min",
    # AL West
    "hou": "hou", "laa": "laa", "ana": "laa", "ath": "ath", "oak": "ath",
    "sea": "sea", "tex": "tex",
    # NL East
    "atl": "atl", "mia": "mia", "fla": "mia", "nym": "nym", "phi": "phi",
    "wsh": "wsh", "was": "wsh",
    # NL Central
    "chc": "chc", "chn": "chc", "cin": "cin", "mil": "mil", "pit": "pit",
    "stl": "stl",
    # NL West
    "ari": "ari", "az": "ari", "col": "col", "lad": "lad", "la": "lad",
    "sd": "sd", "sdp": "sd", "sf": "sf", "sfg": "sf",
    # Historical
    "mtl": "mtl",
}

_NBA: dict[str, str] = {
    # East
    "atl": "atl", "bos": "bos", "bkn": "bkn", "brk": "bkn", "cha": "cha",
    "chi": "chi", "cle": "cle", "det": "det", "ind": "ind", "mia": "mia",
    "mil": "mil", "nyk": "nyk", "ny": "nyk", "orl": "orl", "phi": "phi",
    "tor": "tor", "wsh": "wsh", "was": "wsh",
    # West
    "dal": "dal", "den": "den", "gs": "gsw", "gsw": "gsw", "hou": "hou",
    "lac": "lac", "lal": "lal", "mem": "mem", "min": "min", "nop": "nop",
    "no": "nop", "nor": "nop", "okc": "okc", "phx": "phx", "phn": "phx",
    "por": "por", "sac": "sac", "sas": "sas", "sa": "sas", "uta": "uta",
    "utah": "uta",
}

_NHL: dict[str, str] = {
    "ana": "ana", "ari": "uta", "bos": "bos", "buf": "buf", "cgy": "cgy",
    "car": "car", "chi": "chi", "col": "col", "cbj": "cbj", "dal": "dal",
    "det": "det", "edm": "edm", "fla": "fla", "lak": "lak", "la": "lak",
    "min": "min", "mtl": "mtl", "nsh": "nsh", "nj": "nj", "njd": "nj",
    "nyi": "nyi", "nyr": "nyr", "ott": "ott", "phi": "phi", "pit": "pit",
    "sj": "sj", "sjs": "sj", "sea": "sea", "stl": "stl", "tb": "tb",
    "tbl": "tb", "tor": "tor", "uta": "uta", "vnc": "van", "van": "van",
    "vgk": "vgk", "wsh": "wsh", "was": "wsh", "wpg": "wpg",
}

_MLS: dict[str, str] = {
    "atl": "atl", "atx": "aus", "aus": "aus", "cha": "cha", "chi": "chi",
    "cin": "cin", "clb": "clb", "col": "col", "dc": "dc", "dcu": "dc",
    "dal": "dal", "fcd": "dal", "hou": "hou", "lafc": "lafc", "la": "lafc",
    "lag": "lag", "lou": "lou", "mim": "mim", "mia": "mim", "ifc": "mim",
    "min": "min", "mtl": "mtl", "nsh": "nsh", "nyc": "nyc", "ne": "ne",
    "ner": "ne", "nyrb": "rbn", "rbn": "rbn", "orl": "orl", "phi": "phi",
    "por": "por", "rsl": "rsl", "sd": "sd", "sj": "sj", "sea": "sea",
    "skc": "skc", "stl": "stl", "tor": "tor", "van": "van",
}

_EPL: dict[str, str] = {
    "ars": "ars", "ava": "ast", "ast": "ast", "bou": "bou", "bre": "bre",
    "bha": "bha", "bre": "bre", "che": "che", "cfc": "che",
    "cry": "cry", "cpa": "cry", "eve": "eve", "ful": "ful", "ips": "ips",
    "lei": "lei", "lci": "lei", "liv": "liv", "lfc": "liv", "mci": "mci",
    "mun": "mun", "mufc": "mun", "new": "new", "nuf": "new", "not": "not",
    "nfo": "not", "sou": "sou", "tot": "tot", "thf": "tot", "whu": "whu",
    "wol": "wol", "wlv": "wol",
    # Championship & EFL teams that appear in KXEFLL1GAME tickers
    "bor": "bor", "bbr": "bor",
}

_LALIGA: dict[str, str] = {
    "alm": "alm", "alc": "alc", "alh": "alh", "ath": "ath", "atb": "atm",
    "atm": "atm", "bar": "fcb", "fcb": "fcb", "bet": "bet", "rbb": "bet",
    "cad": "cad", "cdc": "cad", "cel": "cel", "rcc": "cel", "esp": "esp",
    "rce": "esp", "get": "get", "gci": "get", "gir": "gir", "lpa": "lpa",
    "leg": "leg", "lvt": "lvn", "lvn": "lvn", "may": "may", "rcm": "may",
    "osa": "osa", "ovi": "ovi", "rs": "rs", "rss": "rs", "rcr": "rcr",
    "ray": "ray", "rmu": "rmu", "rea": "rma", "rma": "rma",
    "sev": "sev", "sfc": "sev", "val": "val", "vcf": "val", "vil": "vil",
    "vcd": "vil",
}

_BUNDESLIGA: dict[str, str] = {
    "auo": "fca", "fca": "fca", "bay": "fcb", "fcb_de": "fcb", "bmg": "bmg",
    "mgl": "bmg", "bvb": "bvb", "dor": "bvb", "bre": "wer", "wer": "wer",
    "fcs": "fcs", "ssv": "fcs", "fra": "sge", "sge": "sge", "fre": "scf",
    "scf": "scf", "ham": "hsv", "hsv": "hsv", "hei": "fch", "fch": "fch",
    "hof": "tsg", "tsg": "tsg", "kol": "fck", "fck": "fck", "lev": "lev",
    "b04": "lev", "mai": "m05", "m05": "m05", "rbl": "rbl",
    "leip": "rbl", "stu": "vfb", "vfb": "vfb", "uni": "fcu", "fcu": "fcu",
    "wol": "wob", "wob": "wob",
}

_SERIEA: dict[str, str] = {
    "ata": "ata", "bol": "bol", "bfc": "bol", "cag": "cag", "cca": "cag",
    "com": "com", "ccc": "com", "cre": "cre", "uss": "cre", "emp": "emp",
    "fio": "fio", "acf": "fio", "gen": "gen", "cfc_it": "gen", "ham": "ham",
    "her": "ver", "ver": "ver", "ina": "int", "int": "int", "juv": "juv",
    "juventus": "juv", "laz": "laz", "ssl": "laz", "lec": "lec", "use": "lec",
    "mil": "mil_it", "acm": "mil_it", "mon": "mnz", "mnz": "mnz", "nap": "nap",
    "ssc": "nap", "par": "par", "pcl": "par", "pis": "pis", "rom": "rom",
    "asr": "rom", "sas": "sas", "uss_it": "sas", "tor": "tor_it", "tfc": "tor_it",
    "udi": "udi", "uca": "udi", "ven": "ven", "vfc": "ven",
}

_LIGUE1: dict[str, str] = {
    "ang": "ang", "sco": "ang", "auc": "auc", "ajx": "auc", "bre": "sb29",
    "sb29": "sb29", "lva": "lva", "rcl": "lva", "len": "rcl", "lil": "los",
    "los": "los", "lor": "fcl", "fcl": "fcl", "lyo": "ol", "ol": "ol",
    "mar": "om", "om": "om", "mon": "asm", "asm": "asm", "mtp": "mhsc",
    "mhsc": "mhsc", "nan": "fcn", "fcn": "fcn", "nic": "ogn", "ogn": "ogn",
    "par": "psg", "psg": "psg", "rms": "sdr", "sdr": "sdr", "ren": "srfc",
    "srfc": "srfc", "ste": "asse", "asse": "asse", "str": "rcsa", "rcsa": "rcsa",
    "tou": "tfc", "tfc_fr": "tfc",
}

_NFL: dict[str, str] = {
    "ari": "ari", "atl": "atl", "bal": "bal", "buf": "buf", "car": "car",
    "chi": "chi", "cin": "cin", "cle": "cle", "dal": "dal", "den": "den",
    "det": "det", "gb": "gb", "gnb": "gb", "hou": "hou", "ind": "ind",
    "jax": "jax", "jac": "jax", "kc": "kc", "lac": "lac", "lar": "lar",
    "ram": "lar", "lv": "lv", "lvr": "lv", "oak": "lv", "mia": "mia",
    "min": "min", "ne": "ne", "nwe": "ne", "no": "no", "nor": "no",
    "nyg": "nyg", "nyj": "nyj", "phi": "phi", "pit": "pit", "sf": "sf",
    "sea": "sea", "tb": "tb", "tam": "tb", "ten": "ten", "wsh": "wsh",
    "was": "wsh",
}

# ATP/WTA player codes are dynamic (Grand Slams, every week). We can't
# enumerate them — instead we treat the raw 3-letter code as canonical and
# rely on the fact both Kalshi and Polymarket use the player's last-name
# prefix. Same approach for UFC fighter codes.

# Canonical merge order: when a code exists in multiple leagues with
# DIFFERENT canonical keys, the last league wins. NHL "ari" → "uta" must
# precede MLS "atl" so MLB "atl" still maps to "atl". Sports-shared codes
# (e.g. ATL = Atlanta in many leagues) mostly agree.
_ALL: dict[str, str] = {}
for league in (_LIGUE1, _SERIEA, _BUNDESLIGA, _LALIGA, _EPL, _MLS, _NHL, _NBA, _NFL, _MLB):
    _ALL.update(league)

# Pairs that look similar but are KNOWN to be different markets (different
# franchises, different sports). The validator must always reject these
# regardless of overlap heuristics.
KNOWN_NON_MATCHES: frozenset[tuple[str, str]] = frozenset({
    ("mtl", "mim"),   # Montreal Canadiens (NHL) vs Inter Miami CF (MLS)
    ("mim", "mtl"),
    ("la", "lac"),    # LA Lakers (NBA) ≠ LA Clippers — same city, diff team
    ("la", "lal"),
    ("ny", "nyy"),    # NY Knicks (NBA) ≠ NY Yankees (MLB)
    ("ny", "nym"),
    ("ny", "nyi"),
    ("ny", "nyr"),
})


def norm_team(code: str) -> str:
    """Return the canonical alias for a venue-specific team code.

    Unknown codes pass through lowercase untouched — the alias table is
    advisory; a code we don't know about still has to be matched against
    the other venue's code, which means raw equality is the fallback.
    """
    if not code:
        return ""
    raw = str(code).strip().lower()
    return _ALL.get(raw, raw)


def same_team(a: str, b: str) -> bool:
    """Return True if two venue codes refer to the same team."""
    if not a or not b:
        return False
    na, nb = norm_team(a), norm_team(b)
    if na == nb:
        return True
    # KNOWN non-matches override prefix heuristics: catch MTL/MIM trap
    if (a.lower(), b.lower()) in KNOWN_NON_MATCHES:
        return False
    if (na, nb) in KNOWN_NON_MATCHES:
        return False
    # Allow 3-letter prefix match as a last resort (covers player codes in ATP/WTA)
    if len(na) >= 3 and len(nb) >= 3 and na[:3] == nb[:3]:
        return True
    return False


def try_split_teams(combined: str, t1: str, t2: str) -> bool:
    """Try every split point in ``combined`` and check whether the two halves
    canonicalize to ``t1`` / ``t2`` in either order.

    Used to validate that a Kalshi ticker stem like ``HOUBAL`` corresponds
    to a Polymarket pair ``(hou, bal)``. The split must produce
    canonically-equal halves on both sides — order doesn't matter.
    """
    if not combined or not t1 or not t2:
        return False
    combined = combined.lower()
    nt1, nt2 = norm_team(t1), norm_team(t2)
    for i in range(2, len(combined)):
        a, b = combined[:i], combined[i:]
        if same_team(a, nt1) and same_team(b, nt2):
            return True
        if same_team(a, nt2) and same_team(b, nt1):
            return True
    return False


def detect_polarity(
    *,
    kalshi_side: str,
    poly_side_suffix: str | None,
    poly_team1: str,
    poly_team2: str,
) -> str:
    """Classify the polarity relationship between a Kalshi ticker and a
    Polymarket slug.

    Returns one of:
      - ``"same"``       — Kalshi YES and Polymarket YES point to the same outcome.
      - ``"flipped"``    — same underlying market, but Polymarket YES is the
                            OPPOSITE outcome to Kalshi YES (e.g. binary game-winner
                            slugs default YES = team1; if the Kalshi side is team2
                            then YES means opposite teams).
      - ``"unrelated"``  — the two outcomes are not the same market (e.g. one
                            side is "draw"/"tie" while the other is a team).

    A flipped pair is still a valid arbitrage candidate; the scanner just
    needs to swap which Polymarket side it pairs with Kalshi YES.
    """
    ks = (kalshi_side or "").lower().strip()
    ps = (poly_side_suffix or "").lower().strip()
    pt1 = (poly_team1 or "").lower().strip()
    pt2 = (poly_team2 or "").lower().strip()

    # Tie/draw handling — both sides must explicitly agree they're the
    # tie outcome. A team-vs-tie comparison is not the same market.
    k_is_tie = ks in {"tie", "draw"}
    p_is_tie = ps in {"tie", "draw"}
    if k_is_tie and p_is_tie:
        return "same"
    if k_is_tie or p_is_tie:
        return "unrelated"

    if ps:
        # Polymarket slug carries an explicit side suffix → 3-way market
        # (e.g. soccer with home/draw/away). Compare directly to the suffix.
        if same_team(ks, ps):
            return "same"
        # Same fixture, opposite team picked — true polarity flip.
        if (same_team(ks, pt1) or same_team(ks, pt2)) and (
            same_team(ps, pt1) or same_team(ps, pt2)
        ):
            return "flipped"
        return "unrelated"

    # No side suffix → binary slug. Polymarket convention: YES = team1.
    if same_team(ks, pt1):
        return "same"
    if same_team(ks, pt2):
        return "flipped"
    return "unrelated"


def all_known_codes() -> Iterable[str]:
    """Iterate every alias key currently in the table (mostly for tests)."""
    return _ALL.keys()
