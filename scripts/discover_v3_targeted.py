#!/usr/bin/env python3
"""
V3 targeted discovery: directly maps known Kalshi championship/award event series
to Polymarket slugs using known patterns, team abbreviations, and direct API lookups.

This is the high-quality seed file generator — produces confirmed-grade mappings
for well-understood market categories.

Runs in ~30 seconds.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("discover_v3")

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "scripts" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def _load_env() -> None:
    for candidate in [REPO_ROOT / ".env", Path("/Users/rentamac/Documents/arbiter/.env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            break

_load_env()

_KEY_CANDIDATES = [
    REPO_ROOT / "keys" / "kalshi_private.pem",
    Path("/Users/rentamac/Documents/arbiter/keys/kalshi_private.pem"),
]
KEY_PATH = next((p for p in _KEY_CANDIDATES if p.exists()), _KEY_CANDIDATES[0])
KALSHI_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GW = "https://gateway.polymarket.us"

# ─── Auth ─────────────────────────────────────────────────────────────────────

_PRIV = None

def _pk():
    global _PRIV
    if _PRIV is None:
        from cryptography.hazmat.primitives import serialization
        _PRIV = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    return _PRIV

def kh(method: str, path: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = int(time.time() * 1000)
    msg = f"{ts}{method}{path}".encode()
    sig = _pk().sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KALSHI_KEY_ID, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(), "KALSHI-ACCESS-TIMESTAMP": str(ts), "Accept": "application/json"}

# ─── Text utils ───────────────────────────────────────────────────────────────

_NORM = re.compile(r"[^a-z0-9]+")

def norm(t: str) -> str:
    return _NORM.sub(" ", str(t).lower()).strip()

def sim(a: str, b: str) -> float:
    an, bn = norm(a), norm(b)
    if not an or not bn: return 0.0
    if an == bn: return 1.0
    at, bt = set(an.split()), set(bn.split())
    j = len(at & bt) / max(len(at | bt), 1)
    return round(j, 4)

# ─── Kalshi fetchers ──────────────────────────────────────────────────────────

SEM = asyncio.Semaphore(8)

async def kalshi_markets(session: aiohttp.ClientSession, event_ticker: str) -> list[dict]:
    async with SEM:
        try:
            async with session.get(
                f"{BASE}/markets",
                params={"event_ticker": event_ticker, "limit": "200"},
                headers=kh("GET", "/trade-api/v2/markets"),
            ) as r:
                if r.status != 200: return []
                return list((await r.json()).get("markets") or [])
        except Exception as e:
            logger.debug("markets fetch error %s: %s", event_ticker, e)
            return []

async def kalshi_markets_by_series(session: aiohttp.ClientSession, series_ticker: str) -> list[dict]:
    """Fetch all markets for a series ticker (multiple events)."""
    all_events = json.loads(Path("/tmp/kalshi_events.json").read_text())
    event_tickers = [e["event_ticker"] for e in all_events if e.get("series_ticker") == series_ticker]
    results = []
    for et in event_tickers:
        mkts = await kalshi_markets(session, et)
        results.extend(mkts)
        if mkts:
            await asyncio.sleep(0.12)
    return results

# ─── Known series → Polymarket slug pattern mappings ─────────────────────────

# Format: (kalshi_series_ticker, polymarket_slug_prefix, category, description_template)
KNOWN_SERIES = [
    # ── Sports Championships ──────────────────────────────────────────────────
    ("KXNBA",       "tec-nba-champ-2026-07-01",      "sports",  "2026 NBA Champion"),
    ("KXNBAEAST",   "tec-nba-east-conf-2026-07-01",   "sports",  "2026 Eastern Conference Champion"),
    ("KXNBAWEST",   "tec-nba-west-conf-2026-07-01",   "sports",  "2026 Western Conference Champion"),
    ("KXNHL",       "tec-nhl-scw-2026-06-30",         "sports",  "2026 NHL Stanley Cup Champion"),
    ("KXNHLEAST",   "tec-nhl-east-conf-2026-06-30",   "sports",  "2026 NHL Eastern Conference Champion"),
    ("KXNHLWEST",   "tec-nhl-west-conf-2026-06-30",   "sports",  "2026 NHL Western Conference Champion"),
    ("KXMLB",       "tec-mlb-champ-2026-09-27",       "sports",  "2026 MLB World Series Champion"),
    ("KXMLBAL",     "tec-mlb-alchamp-2026-09-27",     "sports",  "2026 MLB American League Champion"),
    ("KXMLBNL",     "tec-mlb-nlchamp-2026-09-27",     "sports",  "2026 MLB National League Champion"),
    ("KXMLSCUP",    "tec-mls-winner-2026-11-07",      "sports",  "2026 MLS Cup Champion"),
    ("KXPREMIERLEAGUE", "tec-epl-winner-2025-05-25",  "sports",  "English Premier League Champion"),
    ("KXUCL",       "tec-ucl-winner-2026-05-31",      "sports",  "UEFA Champions League Winner"),
    ("KXMENWORLDCUP", "tec-wcmen-winner-2026-07-19",  "sports",  "2026 FIFA World Cup Winner"),
    ("KXSB",        "tec-nfl-champ-2027-02-08",       "sports",  "2027 NFL Super Bowl Champion"),
    ("KXNFLAFCCHAMP", "tec-nfl-afc-2027-02-08",       "sports",  "2027 NFL AFC Champion"),
    ("KXNFLNFCCHAMP", "tec-nfl-nfc-2027-02-08",       "sports",  "2027 NFL NFC Champion"),
    ("KXMARMAD",    "tec-cbb-champ-2026-04-04",       "sports",  "2026 NCAA Basketball Champion"),
    ("KXNCAAF",     "tec-cfb-champ-2026-01-19",       "sports",  "College Football National Championship Winner"),
    ("KXWNBA",      "tec-wnba-champ-2026-10-15",      "sports",  "2026 WNBA Champion"),
    # ── Golf ─────────────────────────────────────────────────────────────────
    ("KXPGATOUR",   "tec-pga-champ-2026-05-18",       "sports",  "2026 PGA Championship Winner"),
    ("KXLPGATOUR",  "tec-chevron-champ-2026-04-26",   "sports",  "2026 Chevron Championship Winner"),
    # ── Formula 1 ─────────────────────────────────────────────────────────────
    ("KXF1",        "tec-f1-champ-2026-11-29",        "sports",  "2026 F1 World Drivers Champion"),
    ("KXF1CONSTRUCTORS", "tec-f1-constr-2026-11-29",  "sports",  "2026 F1 Constructors Champion"),
    # ── European Soccer ───────────────────────────────────────────────────────
    ("KXUEL",       "tec-uel-winner-2026-05-20",      "sports",  "2026 Europa League Champion"),
    ("KXUECL",      "tec-uecl-winner-2026-05-13",     "sports",  "2026 UEFA Conference League Champion"),
    ("KXDFBPOKAL",  "tec-dfb-pokal-2026-05-30",       "sports",  "DFB Pokal Champion"),
    ("KXCOPADELREY","tec-copa-del-rey-2026-05-23",     "sports",  "Copa del Rey Champion"),
    ("KXCOPPAITALIA","tec-coppa-italia-2026-05-13",    "sports",  "Coppa Italia Champion"),
    ("KXBRASILEIRO","tec-brasileirao-2026-12-01",      "sports",  "Brasileirao Series A Champion"),
    # ── Cricket / IPL ─────────────────────────────────────────────────────────
    ("KXIPL",       "tec-ipl-winner-2026-06-01",      "sports",  "2026 IPL Champion"),
    # ── Tennis ────────────────────────────────────────────────────────────────
    # US Open, Wimbledon etc. have Poly equivalents
]

# Team abbreviation maps: Kalshi ticker suffix → Polymarket slug suffix
# These will be used for direct matching when exact ticker matches are unknown
TEAM_MAPS = {
    "nba": {
        "ATL": "atl", "BOS": "bos", "BKN": "bkn", "CHA": "cha", "CHI": "chi",
        "CLE": "cle", "DAL": "dal", "DEN": "den", "DET": "det", "GSW": "gs",
        "HOU": "hou", "IND": "ind", "LAC": "lac", "LAL": "lal", "MEM": "mem",
        "MIA": "mia", "MIL": "mil", "MIN": "min", "NOP": "no", "NYK": "ny",
        "OKC": "okc", "ORL": "orl", "PHI": "phi", "PHX": "phx", "POR": "por",
        "SAC": "sac", "SAS": "sas", "TOR": "tor", "UTA": "uta", "WAS": "was",
    },
    "nhl": {
        "ANA": "ana", "ARI": "ari", "BOS": "bos", "BUF": "buf", "CAR": "car",
        "CBJ": "cbj", "CGY": "cgy", "CHI": "chi", "COL": "col", "DAL": "dal",
        "DET": "det", "EDM": "edm", "FLA": "fla", "LAK": "lak", "MIN": "min",
        "MTL": "mtl", "NJD": "njd", "NSH": "nsh", "NYI": "nyi", "NYR": "nyr",
        "OTT": "ott", "PHI": "phi", "PIT": "pit", "SEA": "sea", "SJS": "sjs",
        "STL": "stl", "TBL": "tbl", "TOR": "tor", "UTA": "uta", "VAN": "van",
        "VGK": "vgk", "WPG": "wpg", "WSH": "wsh",
    },
    "mlb": {
        "ATL": "atl", "ARI": "ari", "BAL": "bal", "BOS": "bos", "CHC": "chc",
        "CIN": "cin", "CLE": "cle", "COL": "col", "CWS": "cws", "DET": "det",
        "HOU": "hou", "KCR": "kc", "LAA": "laa", "LAD": "lad", "MIA": "mia",
        "MIL": "mil", "MIN": "min", "NYM": "nym", "NYY": "nyy", "OAK": "oak",
        "PHI": "phi", "PIT": "pit", "SDP": "sdp", "SEA": "sea", "SFG": "sf",
        "STL": "stl", "TBR": "tb", "TEX": "tex", "TOR": "tor", "WSN": "was",
    },
    "nfl": {
        "ARI": "ari", "ATL": "atl", "BAL": "bal", "BUF": "buf", "CAR": "car",
        "CHI": "chi", "CIN": "cin", "CLE": "cle", "DAL": "dal", "DEN": "den",
        "DET": "det", "GB": "gb", "HOU": "hou", "IND": "ind", "JAX": "jax",
        "KC": "kc", "LA": "lar", "LAC": "lac", "LV": "lv", "MIA": "mia",
        "MIN": "min", "NE": "ne", "NO": "no", "NYG": "nyg", "NYJ": "nyj",
        "PHI": "phi", "PIT": "pit", "SF": "sf", "SEA": "sea", "TB": "tb",
        "TEN": "ten", "WAS": "was",
    },
}

# ─── Direct pair matching (specific, hand-verified) ───────────────────────────

# Pairs where we know for sure there's a direct Kalshi market → Polymarket slug match
DIRECT_PAIRS = [
    # ── Climate/Science long-term ──────────────────────────────────────────────
    {
        "kalshi_ticker": "KXWARMING-50",
        "kalshi_event_ticker": "KXWARMING-50",
        "kalshi_title": "Will the world pass 2 degrees Celsius over pre-industrial levels before 2050?",
        "poly_slug": "will-the-world-breach-2-c-of-warming-before-2050",
        "poly_question": "Will the world breach 2°C of warming before 2050?",
        "category": "climate",
        "kalshi_category": "Climate and Weather",
        "score": 0.82,
        "description": "Global warming 2°C breach before 2050",
        "kalshi_resolution_date": "2050-12-31",
        "notes": "Long-term climate milestone market, both platforms resolve on same event",
    },
    {
        "kalshi_ticker": "USCLIMATE",
        "kalshi_event_ticker": "USCLIMATE",
        "kalshi_title": "US meets its climate goals?",
        "poly_slug": "will-the-us-meet-its-2030-climate-goals",
        "poly_question": "Will the US meet its 2030 climate goals?",
        "category": "climate",
        "kalshi_category": "Climate and Weather",
        "score": 0.75,
        "description": "US 2030 climate goals",
        "kalshi_resolution_date": "2030-12-31",
        "notes": "Both platforms track US Paris Agreement / clean energy targets",
    },
    # ── Science/Tech ─────────────────────────────────────────────────────────
    {
        "kalshi_ticker": "AITURING",
        "kalshi_event_ticker": "AITURING",
        "kalshi_title": "AI passes Turing test before 2030?",
        "poly_slug": "will-ai-pass-a-turing-test-before-2030",
        "poly_question": "Will AI pass a Turing test before 2030?",
        "category": "science",
        "kalshi_category": "Science and Technology",
        "score": 0.80,
        "description": "AI passes Turing test before 2030",
        "kalshi_resolution_date": "2030-01-01",
        "notes": "Both platforms define this as AGI-level conversational test",
    },
    {
        "kalshi_ticker": "KXMARSVRAIL-50",
        "kalshi_event_ticker": "KXMARSVRAIL-50",
        "kalshi_title": "Will a human land on Mars before California starts high-speed rail?",
        "poly_slug": "human-on-mars-before-california-high-speed-rail",
        "poly_question": "Will a human land on Mars before California high-speed rail is operational?",
        "category": "science",
        "kalshi_category": "Science and Technology",
        "score": 0.78,
        "description": "Human on Mars vs CA high-speed rail",
        "kalshi_resolution_date": "2050-01-01",
        "notes": "Famous Kalshi novelty science/infrastructure bet",
    },
    {
        "kalshi_ticker": "KXCOLONIZEMARS-50",
        "kalshi_event_ticker": "KXCOLONIZEMARS-50",
        "kalshi_title": "Will humans colonize Mars before 2050?",
        "poly_slug": "will-humans-colonize-mars-before-2050",
        "poly_question": "Will humans colonize Mars before 2050?",
        "category": "science",
        "kalshi_category": "Science and Technology",
        "score": 0.85,
        "description": "Human Mars colonization before 2050",
        "kalshi_resolution_date": "2050-01-01",
        "notes": "Near-identical question on both platforms",
    },
    {
        "kalshi_ticker": "KXELONMARS-99",
        "kalshi_event_ticker": "KXELONMARS-99",
        "kalshi_title": "Will Elon Musk visit Mars in his lifetime?",
        "poly_slug": "will-elon-musk-visit-mars-in-his-lifetime",
        "poly_question": "Will Elon Musk go to Mars in his lifetime?",
        "category": "science",
        "kalshi_category": "World",
        "score": 0.80,
        "description": "Elon Musk visits Mars in his lifetime",
        "kalshi_resolution_date": "2099-01-01",
        "notes": "Long-dated science/celebrity novelty market",
    },
    # ── Culture/Entertainment ─────────────────────────────────────────────────
    # Grammy nominations - these are multi-choice, skip for now
    # Oscar categories - will be handled by series matching
]

# ─── Player award events (matched by yes_sub_title → 6-char Poly player code) ─

# Format: (kalshi_event_tickers, poly_prefix, category, description)
# Polymarket 6-char code = first3_of_firstname + first3_of_lastname (lowercase)
PLAYER_AWARD_EVENTS = [
    # NHL Hart Trophy
    (["KXNHLHART-26"], "tec-nhl-hart-2026-06-30", "sports", "NHL Hart Trophy Winner"),
    # NBA MVP
    (["KXNBAMVP-26"], "tec-nba-mvp-2026-06-10", "sports", "NBA MVP Award"),
    # Masters round leaders
    (["KXPGAR1LEAD-MAST26"], "tec-masters-round1leader-2026-04-12", "sports", "2026 Masters Round 1 Leader"),
    (["KXPGAR2LEAD-MAST26"], "tec-masters-round2leader-2026-04-12", "sports", "2026 Masters Round 2 Leader"),
    (["KXPGAR3LEAD-MAST26"], "tec-masters-round3leader-2026-04-12", "sports", "2026 Masters Round 3 Leader"),
    # RBC Heritage round leaders
    (["KXPGAR1LEAD-RBH26"], "tec-pga-rbcheri-2026-04-19-round1leader", "sports", "2026 RBC Heritage Round 1 Leader"),
    (["KXPGAR2LEAD-RBH26"], "tec-pga-rbcheri-2026-04-19-round2leader", "sports", "2026 RBC Heritage Round 2 Leader"),
    (["KXPGAR3LEAD-RBH26"], "tec-pga-rbcheri-2026-04-19-round3leader", "sports", "2026 RBC Heritage Round 3 Leader"),
]

_SUFFIX_RE = re.compile(r"\s+(jr\.?|sr\.?|i{1,3}|iv|v)$", re.IGNORECASE)
_PARTICLES = {"de", "van", "der", "el", "la", "le", "von"}


def player_code(full_name: str) -> str:
    """Generate Polymarket 6-char player code from full name."""
    name = _SUFFIX_RE.sub("", full_name.strip())
    parts = [p for p in name.lower().split() if p not in _PARTICLES]
    if len(parts) >= 2:
        return parts[0][:3] + parts[-1][:3]
    return parts[0][:6] if parts else ""


# ─── API calls ────────────────────────────────────────────────────────────────

async def fetch_series_markets_all(session: aiohttp.ClientSession, series_ticker: str) -> list[dict]:
    """Get all markets across all events in a series."""
    events = json.loads(Path("/tmp/kalshi_events.json").read_text())
    event_tickers = [e["event_ticker"] for e in events if e.get("series_ticker") == series_ticker]
    if not event_tickers:
        # Try by event_ticker prefix
        event_tickers = [e["event_ticker"] for e in events
                         if str(e.get("event_ticker","")).startswith(series_ticker)]

    all_mkts: list[dict] = []
    tasks = [kalshi_markets(session, et) for et in event_tickers[:50]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, list):
            all_mkts.extend(r)
    return all_mkts


async def fetch_poly_series(session: aiohttp.ClientSession, slug_prefix: str) -> list[dict]:
    """Fetch Polymarket markets matching a slug prefix from cached data."""
    poly_all = json.loads((OUTPUT_DIR / "polymarket_raw.json").read_text())
    return [m for m in poly_all if str(m.get("slug","")).startswith(slug_prefix.rstrip("-"))]


# ─── Matching utils ───────────────────────────────────────────────────────────

def extract_team_from_kalshi_ticker(ticker: str) -> str:
    """Extract team abbreviation from Kalshi ticker like KXNBA-BOS → BOS."""
    parts = ticker.split("-")
    return parts[-1].upper() if parts else ""


def extract_team_from_poly_slug(slug: str, slug_prefix: str) -> str:
    """Extract team code from poly slug like tec-nba-champ-2026-07-01-bos → bos."""
    suffix = slug[len(slug_prefix):].lstrip("-")
    return suffix.lower()


def canonical_id(kalshi_ticker: str, poly_slug: str) -> str:
    digest = hashlib.sha1(f"{kalshi_ticker}|{poly_slug}".encode()).hexdigest()[:8]
    base = re.sub(r"[^A-Z0-9]", "", kalshi_ticker.upper()[:20])
    return f"AUTO_{base}_{digest}"


# ─── Series matcher ───────────────────────────────────────────────────────────

async def match_series(
    session: aiohttp.ClientSession,
    kalshi_series: str,
    poly_prefix: str,
    category: str,
    description: str,
) -> list[dict]:
    """Match Kalshi series markets to Polymarket markets by name similarity."""
    # Fetch Kalshi markets for this series
    kalshi_mkts = await fetch_series_markets_all(session, kalshi_series)
    if not kalshi_mkts:
        logger.info("  No Kalshi markets found for series %s", kalshi_series)
        return []

    # Filter out multi-leg
    kalshi_mkts = [m for m in kalshi_mkts
                   if not str(m.get("ticker","")).startswith(("KXMVE", "KXCROSS"))]

    # Load matching poly markets
    poly_mkts = await fetch_poly_series(session, poly_prefix)
    if not poly_mkts:
        logger.info("  No Polymarket markets found for prefix %s", poly_prefix)
        return []

    logger.info("  Series %s: %d Kalshi markets × %d Poly markets",
                kalshi_series, len(kalshi_mkts), len(poly_mkts))

    # Build Poly lookup: team code → market
    poly_by_code: dict[str, dict] = {}
    for pm in poly_mkts:
        slug = pm.get("slug", "")
        code = extract_team_from_poly_slug(slug, poly_prefix)
        if code:
            poly_by_code[code] = pm

    candidates: list[dict] = []
    used_kalshi: set[str] = set()
    used_poly: set[str] = set()

    # Team abbreviation map for this sport
    sport = kalshi_series.lower()
    team_map: dict[str, str] = {}
    for sport_key, tmap in TEAM_MAPS.items():
        if sport_key in sport or sport in sport_key:
            team_map = tmap
            break

    for km in kalshi_mkts:
        ticker = km.get("ticker", "")
        if ticker in used_kalshi:
            continue

        k_title = str(km.get("title", "") or km.get("yes_sub_title", "") or "")
        k_norm = norm(k_title)

        best_pm = None
        best_score = 0.0

        # Try team abbrev map first
        k_team = extract_team_from_kalshi_ticker(ticker)
        if k_team and k_team in team_map:
            poly_code = team_map[k_team]
            if poly_code in poly_by_code:
                best_pm = poly_by_code[poly_code]
                best_score = 0.88

        # Fall back to title similarity
        if best_pm is None:
            for pm in poly_mkts:
                if pm.get("slug", "") in used_poly:
                    continue
                p_title = str(pm.get("question", "") or pm.get("title", "") or "")
                s = sim(k_title, p_title)
                if s > best_score:
                    best_score = s
                    best_pm = pm

        if best_pm is None or best_score < 0.25:
            # Try matching by token overlap with poly_by_code
            k_words = set(k_norm.split())
            for code, pm in poly_by_code.items():
                if pm.get("slug", "") in used_poly:
                    continue
                if code in k_words or k_team.lower() in code or code in k_team.lower():
                    best_pm = pm
                    best_score = 0.70
                    break

        if best_pm is None or best_score < 0.20:
            continue

        if best_pm.get("slug", "") in used_poly:
            continue

        p_q = str(best_pm.get("question", "") or best_pm.get("title", "") or "")
        p_slug = best_pm.get("slug", "")
        poly_date = None
        k_date = None
        for field in ("closeTime", "endDate"):
            v = best_pm.get(field)
            if v:
                try:
                    poly_date = datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
                    break
                except:
                    pass

        candidates.append({
            "kalshi_ticker": ticker,
            "kalshi_event_ticker": km.get("event_ticker", ""),
            "kalshi_title": k_title,
            "kalshi_category": "Sports",
            "kalshi_resolution_date": k_date.isoformat() if k_date else None,
            "poly_slug": p_slug,
            "poly_question": p_q,
            "poly_category": str(best_pm.get("category", "")),
            "poly_resolution_date": poly_date.isoformat() if poly_date else None,
            "poly_resolution_source": best_pm.get("resolutionSource"),
            "score": best_score,
            "description": f"{description}: {k_title}",
            "category": category,
            "resolution_date": (poly_date.isoformat() if poly_date else None),
            "series": kalshi_series,
            "notes": f"V3 targeted: {kalshi_series} ↔ {poly_prefix}",
        })
        used_kalshi.add(ticker)
        used_poly.add(p_slug)

    logger.info("  → %d pairs matched for %s", len(candidates), kalshi_series)
    return candidates


# ─── Oscar/Award specialized matcher ─────────────────────────────────────────

async def match_oscars(session: aiohttp.ClientSession) -> list[dict]:
    """Match Kalshi Oscar events to Polymarket Oscar markets by category."""
    events = json.loads(Path("/tmp/kalshi_events.json").read_text())
    poly_all = json.loads((OUTPUT_DIR / "polymarket_raw.json").read_text())

    # Kalshi Oscar events
    oscar_events = [e for e in events if "KXOSCAR" in str(e.get("series_ticker","")).upper()
                    or "KXOSCAR" in str(e.get("event_ticker","")).upper()]
    # Polymarket Oscar markets
    poly_oscars = [m for m in poly_all if str(m.get("slug","")).startswith("tac-osc-")]

    logger.info("Oscars: %d Kalshi events, %d Poly markets", len(oscar_events), len(poly_oscars))

    # Group Poly by category code: bpic, bdir, bact, bact2, bsup, bsup2
    poly_by_cat: dict[str, list[dict]] = defaultdict(list)
    for pm in poly_oscars:
        slug = pm.get("slug", "")
        parts = slug.split("-")
        if len(parts) >= 4:
            cat_code = parts[2]  # e.g., "bpic"
            poly_by_cat[cat_code].append(pm)

    # Map Kalshi Oscar event to Poly category
    oscar_cat_map = {
        "KXOSCARBP": "bpic",   # Best Picture
        "KXOSCARBDIR": "bdir", # Best Director
        "KXOSCARBA": "bact",   # Best Actor
        "KXOSCARBAS": "bact2", # Best Supporting Actor
        "KXOSCARBAS2": "bsup", # Best Supporting Actress
        "KXOSCARBAA": "bact2", # Best Actress
        "KXOSCARANIMATED": "banim",
    }

    # Try to match by Oscar category in title
    cat_keyword_map = {
        "best picture": "bpic",
        "best director": "bdir",
        "best actor": "bact",
        "best actress": "bact2",
        "best supporting actor": "bsup",
        "best supporting actress": "bsup2",
        "animated feature": "banim",
        "original song": "bsong",
        "original score": "bscor",
        "documentary": "bdoc",
        "international": "bintl",
        "costume": "bcos",
        "visual effects": "bvis",
    }

    candidates: list[dict] = []
    used_k: set[str] = set()
    used_p: set[str] = set()

    for ev in oscar_events:
        et = ev.get("event_ticker", "")
        title = str(ev.get("title", "")).lower()

        # Find which Poly category this matches
        poly_cat = None
        for kw, cat in cat_keyword_map.items():
            if kw in title:
                poly_cat = cat
                break

        if poly_cat is None:
            continue

        poly_for_cat = poly_by_cat.get(poly_cat, [])
        if not poly_for_cat:
            continue

        # For each Poly market in this category, try to match by nominee name
        # Actually the best match is: each Kalshi event_ticker has sub_title with nominee name
        # and each Poly slug has a nominee code

        # Build lookup: normalized nominee name → poly market
        for pm in poly_for_cat:
            slug = pm.get("slug", "")
            if slug in used_p:
                continue
            p_title = str(pm.get("question", "") or pm.get("title", "") or slug)

            # Score based on event title vs poly question
            s = sim(ev.get("title",""), p_title)

            if s < 0.3:
                continue
            if et in used_k:
                continue

            candidates.append({
                "kalshi_ticker": et,
                "kalshi_event_ticker": et,
                "kalshi_title": ev.get("title", ""),
                "kalshi_category": "Entertainment",
                "kalshi_resolution_date": None,
                "poly_slug": slug,
                "poly_question": p_title,
                "poly_category": "culture",
                "poly_resolution_date": None,
                "score": s,
                "description": f"2026 Oscar: {ev.get('title','')}",
                "category": "culture",
                "notes": "V3 Oscar targeted match",
            })
            used_k.add(et)
            used_p.add(slug)
            break

    logger.info("Oscars: %d candidate pairs", len(candidates))
    return candidates


# ─── Billboard chart matcher ──────────────────────────────────────────────────

async def match_billboard(session: aiohttp.ClientSession) -> list[dict]:
    """Match Kalshi Billboard chart events to Polymarket Billboard markets."""
    events = json.loads(Path("/tmp/kalshi_events.json").read_text())
    poly_all = json.loads((OUTPUT_DIR / "polymarket_raw.json").read_text())

    bb_events = [e for e in events if "KXTOPSONG" in str(e.get("event_ticker",""))
                 or "KXTOPALBUM" in str(e.get("event_ticker",""))
                 or "KXBILLBOARD" in str(e.get("event_ticker",""))]

    poly_bb = [m for m in poly_all if "billboard" in str(m.get("slug","")).lower()
               or "hot 100" in str(m.get("question","")).lower()
               or "billboard 200" in str(m.get("question","")).lower()]

    logger.info("Billboard: %d Kalshi events, %d Poly markets", len(bb_events), len(poly_bb))

    if not poly_bb:
        return []

    candidates: list[dict] = []
    used_k: set[str] = set()
    used_p: set[str] = set()

    for ev in bb_events:
        et = ev.get("event_ticker", "")
        if et in used_k:
            continue
        e_title = ev.get("title", "")

        best_pm = None
        best_score = 0.0
        for pm in poly_bb:
            if pm.get("slug","") in used_p:
                continue
            s = sim(e_title, str(pm.get("question","") or pm.get("title","")))
            if s > best_score:
                best_score = s
                best_pm = pm

        if best_pm and best_score >= 0.35:
            candidates.append({
                "kalshi_ticker": et,
                "kalshi_event_ticker": et,
                "kalshi_title": e_title,
                "kalshi_category": "Entertainment",
                "poly_slug": best_pm.get("slug",""),
                "poly_question": str(best_pm.get("question","") or best_pm.get("title","")),
                "poly_category": "culture",
                "score": best_score,
                "description": f"Billboard chart: {e_title}",
                "category": "culture",
                "notes": "V3 Billboard targeted match",
            })
            used_k.add(et)
            used_p.add(best_pm.get("slug",""))

    logger.info("Billboard: %d candidate pairs", len(candidates))
    return candidates


# ─── World Cup group matches ───────────────────────────────────────────────────

async def match_world_cup(session: aiohttp.ClientSession) -> list[dict]:
    """Match FIFA World Cup group and knockout markets."""
    events = json.loads(Path("/tmp/kalshi_events.json").read_text())
    poly_all = json.loads((OUTPUT_DIR / "polymarket_raw.json").read_text())

    wc_events = [e for e in events if "KXWC" in str(e.get("series_ticker",""))
                 or "KXMENWORLDCUP" in str(e.get("series_ticker",""))]

    # World cup on Polymarket
    poly_wc = [m for m in poly_all
               if "world-cup" in str(m.get("slug","")) or "worldcup" in str(m.get("slug",""))
               or "world cup" in str(m.get("question","")).lower()]

    logger.info("World Cup: %d Kalshi events, %d Poly markets", len(wc_events), len(poly_wc))
    if not poly_wc:
        return []

    candidates: list[dict] = []
    used_k: set[str] = set()
    used_p: set[str] = set()

    for ev in wc_events:
        et = ev.get("event_ticker","")
        if et in used_k:
            continue
        e_title = ev.get("title","")

        best_pm = None
        best_score = 0.0
        for pm in poly_wc:
            if pm.get("slug","") in used_p:
                continue
            s = sim(e_title, str(pm.get("question","") or pm.get("title","")))
            if s > best_score:
                best_score = s
                best_pm = pm

        if best_pm and best_score >= 0.30:
            candidates.append({
                "kalshi_ticker": et,
                "kalshi_event_ticker": et,
                "kalshi_title": e_title,
                "kalshi_category": "Sports",
                "poly_slug": best_pm.get("slug",""),
                "poly_question": str(best_pm.get("question","") or best_pm.get("title","")),
                "poly_category": "sports",
                "score": best_score,
                "description": f"World Cup: {e_title}",
                "category": "sports",
                "notes": "V3 World Cup targeted match",
            })
            used_k.add(et)
            used_p.add(best_pm.get("slug",""))

    logger.info("World Cup: %d candidate pairs", len(candidates))
    return candidates


# ─── Player award event matcher ───────────────────────────────────────────────

async def match_player_awards_event(
    session: aiohttp.ClientSession,
    kalshi_event_tickers: list[str],
    poly_prefix: str,
    category: str,
    description: str,
) -> list[dict]:
    """Match Kalshi player-award markets to Polymarket via yes_sub_title → 6-char code."""
    poly_all = json.loads((OUTPUT_DIR / "polymarket_raw.json").read_text())
    poly_mkts = [m for m in poly_all if str(m.get("slug", "")).startswith(poly_prefix.rstrip("-"))]
    if not poly_mkts:
        logger.info("  No Poly markets for prefix %s", poly_prefix)
        return []

    # Build Poly lookup: 6-char code → market
    poly_by_code: dict[str, dict] = {}
    for pm in poly_mkts:
        slug = pm.get("slug", "")
        code = slug[len(poly_prefix):].lstrip("-")
        if code:
            poly_by_code[code] = pm

    candidates: list[dict] = []
    used_k: set[str] = set()
    used_p: set[str] = set()

    for event_ticker in kalshi_event_tickers:
        mkts = await kalshi_markets(session, event_ticker)
        if not mkts:
            logger.info("  No Kalshi markets for %s", event_ticker)
            continue

        logger.info("  %s: %d Kalshi × %d Poly markets", event_ticker, len(mkts), len(poly_mkts))

        for km in mkts:
            ticker = km.get("ticker", "")
            if ticker in used_k:
                continue

            player_name = str(km.get("yes_sub_title") or km.get("no_sub_title") or "")
            if not player_name:
                continue

            code = player_code(player_name)
            pm = poly_by_code.get(code)

            if pm is None:
                # Try alternate: swap first/last parts (e.g. "McIlroy Rory" vs "Rory McIlroy")
                parts = player_name.lower().split()
                if len(parts) >= 2:
                    alt_code = parts[-1][:3] + parts[0][:3]
                    pm = poly_by_code.get(alt_code)

            if pm is None:
                logger.debug("  No Poly match for %s (code=%s)", player_name, code)
                continue

            p_slug = pm.get("slug", "")
            if p_slug in used_p:
                continue

            p_q = str(pm.get("question", "") or pm.get("title", "") or "")
            poly_date = None
            for field in ("closeTime", "endDate"):
                v = pm.get(field)
                if v:
                    try:
                        poly_date = datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
                        break
                    except Exception:
                        pass

            candidates.append({
                "kalshi_ticker": ticker,
                "kalshi_event_ticker": event_ticker,
                "kalshi_title": km.get("title", ""),
                "kalshi_subtitle": player_name,
                "kalshi_category": "Sports",
                "kalshi_resolution_date": None,
                "poly_slug": p_slug,
                "poly_question": p_q,
                "poly_category": str(pm.get("category", "")),
                "poly_resolution_date": poly_date.isoformat() if poly_date else None,
                "poly_resolution_source": pm.get("resolutionSource"),
                "score": 0.85,
                "description": f"{description}: {player_name}",
                "category": category,
                "resolution_date": (poly_date.isoformat() if poly_date else None),
                "series": kalshi_event_tickers[0],
                "notes": f"V3 player award: {event_ticker} ↔ {poly_prefix} (player_code={code})",
            })
            used_k.add(ticker)
            used_p.add(p_slug)

    logger.info("  → %d pairs for %s", len(candidates), poly_prefix)
    return candidates


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main() -> list[dict]:
    logger.info("=== V3 Targeted Discovery ===")

    timeout = aiohttp.ClientTimeout(total=20, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        all_candidates: list[dict] = []

        # 1. Direct hand-verified pairs
        logger.info("Loading %d direct pairs ...", len(DIRECT_PAIRS))
        all_candidates.extend(DIRECT_PAIRS)

        # 2. Known series matching
        logger.info("Matching %d known series ...", len(KNOWN_SERIES))
        series_tasks = [
            match_series(session, k_series, p_prefix, cat, desc)
            for k_series, p_prefix, cat, desc in KNOWN_SERIES
        ]
        series_results = await asyncio.gather(*series_tasks, return_exceptions=True)
        for result in series_results:
            if isinstance(result, list):
                all_candidates.extend(result)
            elif isinstance(result, Exception):
                logger.warning("Series match error: %s", result)

        # 3. Oscar matching
        oscar_pairs = await match_oscars(session)
        all_candidates.extend(oscar_pairs)

        # 4. Billboard matching
        bb_pairs = await match_billboard(session)
        all_candidates.extend(bb_pairs)

        # 5. World Cup matching
        wc_pairs = await match_world_cup(session)
        all_candidates.extend(wc_pairs)

        # 6. Player award event matching (NHL Hart, NBA MVP, golf round leaders)
        logger.info("Matching %d player award event series ...", len(PLAYER_AWARD_EVENTS))
        award_tasks = [
            match_player_awards_event(session, et, pf, cat, desc)
            for et, pf, cat, desc in PLAYER_AWARD_EVENTS
        ]
        award_results = await asyncio.gather(*award_tasks, return_exceptions=True)
        for result in award_results:
            if isinstance(result, list):
                all_candidates.extend(result)
            elif isinstance(result, Exception):
                logger.warning("Player award match error: %s", result)

    # Dedup by kalshi_ticker + poly_slug
    seen_k: set[str] = set()
    seen_p: set[str] = set()
    deduped: list[dict] = []
    all_candidates.sort(key=lambda c: c.get("score", 0), reverse=True)
    for c in all_candidates:
        kt = str(c.get("kalshi_ticker",""))
        ps = str(c.get("poly_slug",""))
        if not kt or not ps:
            continue
        if kt in seen_k or ps in seen_p:
            continue
        deduped.append(c)
        seen_k.add(kt)
        seen_p.add(ps)

    logger.info("Total V3 candidates: %d", len(deduped))

    # Stats
    from collections import Counter
    by_cat = Counter(c.get("category","?") for c in deduped)
    by_score = Counter("high" if c.get("score",0) >= 0.7 else "med" if c.get("score",0) >= 0.4 else "low" for c in deduped)
    logger.info("By category: %s", dict(by_cat.most_common()))
    logger.info("By confidence: %s", dict(by_score))

    # Write
    out = OUTPUT_DIR / "market_candidates_v3.json"
    out.write_text(json.dumps(deduped, indent=2, default=str))
    logger.info("Wrote %d candidates to %s", len(deduped), out)

    # Print top 50
    print("\n=== TOP 50 V3 CANDIDATES ===")
    for c in deduped[:50]:
        print(f"[{c.get('score',0):.3f}] {str(c.get('kalshi_ticker','')):<45s} <-> {c.get('poly_slug','')}")
        print(f"       K: {str(c.get('kalshi_title',''))[:80]}")
        print(f"       P: {str(c.get('poly_question',''))[:80]}")
        print()

    return deduped


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    asyncio.run(main())
