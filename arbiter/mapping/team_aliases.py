"""Canonical aliases for teams/entities used in market mapping.

The mapping layer uses these codes as structural identifiers.  Keep aliases
conservative: an alias should only collapse when the two codes truly refer to
the same team/entity in the relevant venue feeds.
"""
from __future__ import annotations

TEAM_ALIASES: dict[str, str] = {
    # Draw/tie outcomes.
    "draw": "tie",
    "tie": "tie",
    # MLS.
    "atx": "aus",
    "aus": "aus",
    "atl": "atl",
    "chi": "chi",
    "clb": "clb",
    "cin": "cin",
    "col": "col",
    "dal": "dal",
    "dc": "dc",
    "hou": "hou",
    "lafc": "lafc",
    "lag": "lag",
    "mia": "mia",
    "mim": "mim",
    "min": "min",
    "mtl": "mtl",
    "nyc": "nyc",
    "nyrb": "nyrb",
    "orl": "orl",
    "phi": "phi",
    "por": "por",
    "rsl": "rsl",
    "sea": "sea",
    "sj": "sj",
    "skc": "skc",
    "stl": "stl",
    "tor": "tor",
    "van": "van",
    # MLB.
    "ari": "ari",
    "az": "ari",
    "bal": "bal",
    "bos": "bos",
    "chc": "chc",
    "cws": "cws",
    "cle": "cle",
    "det": "det",
    "kc": "kc",
    "laa": "laa",
    "lad": "lad",
    "mia": "mia",
    "mil": "mil",
    "nym": "nym",
    "nyy": "nyy",
    "oak": "oak",
    "phi": "phi",
    "pit": "pit",
    "sd": "sd",
    "sdp": "sd",
    "sea": "sea",
    "sf": "sf",
    "stl": "stl",
    "tb": "tb",
    "tex": "tex",
    "wsh": "wsh",
    # NHL.
    "ana": "ana",
    "buf": "buf",
    "car": "car",
    "cbj": "cbj",
    "cgy": "cgy",
    "col": "col",
    "dal": "dal",
    "det": "det",
    "edm": "edm",
    "fla": "fla",
    "lak": "lak",
    "min": "min",
    "mtl": "mtl",
    "nj": "nj",
    "nsh": "nsh",
    "nyi": "nyi",
    "nyr": "nyr",
    "ott": "ott",
    "phi": "phi",
    "pit": "pit",
    "sea": "sea",
    "sj": "sj",
    "stl": "stl",
    "tbl": "tb",
    "tor": "tor",
    "uta": "uta",
    "vgk": "vgk",
    "wpg": "wpg",
    "wsh": "wsh",
    # NBA.
    "atl": "atl",
    "bkn": "bkn",
    "cha": "cha",
    "chi": "chi",
    "cle": "cle",
    "dal": "dal",
    "den": "den",
    "gs": "gsw",
    "gsw": "gsw",
    "hou": "hou",
    "ind": "ind",
    "lac": "lac",
    "lal": "lal",
    "mem": "mem",
    "mia": "mia",
    "mil": "mil",
    "min": "min",
    "nop": "nop",
    "ny": "nyk",
    "nyk": "nyk",
    "okc": "okc",
    "orl": "orl",
    "phi": "phi",
    "phx": "phx",
    "por": "por",
    "sac": "sac",
    "sas": "sas",
    "tor": "tor",
    "uta": "uta",
    "wsh": "wsh",
    # NFL.
    "ari": "ari",
    "atl": "atl",
    "bal": "bal",
    "buf": "buf",
    "car": "car",
    "chi": "chi",
    "cin": "cin",
    "cle": "cle",
    "dal": "dal",
    "den": "den",
    "det": "det",
    "gb": "gb",
    "hou": "hou",
    "ind": "ind",
    "jac": "jac",
    "jax": "jac",
    "kc": "kc",
    "lv": "lv",
    "lac": "lac",
    "lar": "lar",
    "mia": "mia",
    "min": "min",
    "ne": "ne",
    "no": "no",
    "nyg": "nyg",
    "nyj": "nyj",
    "phi": "phi",
    "pit": "pit",
    "sea": "sea",
    "sf": "sf",
    "tb": "tb",
    "ten": "ten",
    "wsh": "wsh",
    # European soccer aliases observed across Kalshi/Polymarket.
    "ars": "ars",
    "ast": "ast",
    "ata": "ata",
    "bar": "fcb",
    "bfc": "bol",
    "bmg": "bmg",
    "bol": "bol",
    "bor": "bor",
    "bre": "bre",
    "bvb": "bvb",
    "cag": "cag",
    "cfc": "cfc",
    "cry": "cry",
    "fcb": "fcb",
    "fio": "fio",
    "ful": "ful",
    "gen": "gen",
    "lev": "lev",
    "not": "not",
    "osa": "osa",
    "rbl": "rbl",
    "rom": "rom",
    "tot": "tot",
    "whu": "whu",
}


def normalize_entity_code(value: str | None) -> str:
    code = str(value or "").strip().lower()
    return TEAM_ALIASES.get(code, code)


def split_compound_code(value: str | None) -> tuple[str, str] | None:
    """Split Kalshi's concatenated participant code into two canonical codes."""
    text = str(value or "").strip().lower()
    if len(text) < 4:
        return None

    candidates: list[tuple[str, str]] = []
    known_codes = set(TEAM_ALIASES)
    for idx in range(2, len(text) - 1):
        left_raw = text[:idx]
        right_raw = text[idx:]
        left = normalize_entity_code(left_raw)
        right = normalize_entity_code(right_raw)
        if left_raw in known_codes and right_raw in known_codes:
            candidates.append((left, right))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: (len(pair[0]) + len(pair[1]), pair[0], pair[1]))
    return candidates[0]


def canonical_pair(a: str, b: str) -> str:
    left = normalize_entity_code(a)
    right = normalize_entity_code(b)
    return "-".join(sorted((left, right)))
