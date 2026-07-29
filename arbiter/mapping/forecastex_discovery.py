"""ForecastEx auto-discovery — populate ``forecastex_contract_id`` on confirmed
Kalshi/Polymarket mappings by fuzzy-matching IBKR ForecastEx event titles.

Pattern mirrors :mod:`arbiter.mapping.auto_discovery` (the Kalshi↔Polymarket
discoverer) but runs against the IBKR Client Portal Gateway instead. The
gateway exposes ForecastEx event contracts under ``/iserver/secdef/search``
with ``companyHeader`` containing the literal ``FORECASTX`` marker. The
function enumerates those events via a small set of seed keywords (IBKR's
secdef search does not support a "list all by exchange" call from the
Client Portal API), then walks every confirmed mapping that is still
missing a ``forecastex_contract_id`` and attaches the best fuzzy match.

The matching is intentionally conservative — a 3-way arbitrage that uses a
wrong ForecastEx contract bleeds capital silently — so the default
``min_score`` is set high and operators can review every attach via the
mapping log line emitted at INFO level.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional

from ..collectors.forecastex import is_valid_conid
from ..config.settings import normalize_market_text, similarity_score
from .market_map import MarketMapping, MarketMappingStore

logger = logging.getLogger("arbiter.mapping.forecastex_discovery")

# Tokens that add no semantic signal — filter before similarity scoring so
# titles like "US House of Representatives" and "U.S House" don't lose
# points over connective words.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "for", "from", "in", "of", "on", "or", "s", "the",
    "to", "u", "us", "vs", "with",
})


def _tokenize(text: str) -> set[str]:
    return {
        t for t in normalize_market_text(text).split()
        if t and t not in _STOPWORDS and len(t) > 1
    }


def _overlap_score(a: str, b: str) -> float:
    """Overlap coefficient: |A∩B| / min(|A|,|B|).

    Less punishing than Jaccard when the FORECASTX title is wordier than the
    Kalshi/Polymarket description (which it usually is — "US House of
    Representatives Control" vs "US House Midterm Winner").
    """
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if not inter:
        return 0.0
    return round(len(inter) / min(len(ta), len(tb)), 4)


# Seed keywords for enumerating FORECASTX inventory. IBKR's secdef/search
# is keyword-driven and capped at ~15 results per query, so we issue
# multiple queries and dedupe by conid. Add new keywords as ForecastEx
# expands beyond elections into sports / weather / etc.
# ── Aggressive FX catalog enumeration (2026-05-26) ────────────────────
# IBKR's /iserver/secdef/search is keyword-driven and caps at ~15 results
# per query. Live probe against host.docker.internal:5000 enumerated 323
# unique FORECASTX events across politics / economics / crypto / weather
# / sports / business / awards. The literal symbol-prefix keywords below
# guarantee each parent gets surfaced by at least ONE search call so the
# discover() pass sees the full catalog (dedupe by conid is automatic).
#
# IMPORTANT: enumerating the parent ≠ resolving children. Binary
# political events (SENM, HORC, AXX*, MXX*, etc.) follow the
# strike=1.0/2.0 pattern and resolve cleanly via secdef/info — these
# trade. Multi-outcome events (CPI, FEDRC, GDP, BTC threshold ladders)
# have parent IND conids but IBKR's Client Portal API does NOT return
# their child OPT conids via secdef/strikes — that's a confirmed
# upstream limitation. Those parents enter the resolver, return
# no_children for 3 cycles, and get auto-quarantined by the existing
# _NO_CHILDREN_MARK_THRESHOLD logic.
_FX_SYMBOL_SEEDS: tuple[str, ...] = (
    # Political — US federal
    "horc", "senm", "pcd", "pcr", "prpa", "presh", "prest",
    "prazh", "prgah", "prmih", "prnch", "prnvh", "prpah", "prwih",
    "pnfed",
    # State Senate general (AXX***)
    "axxak", "axxga", "axxky", "axxla", "axxme", "axxmi", "axxmt",
    "axxnc", "axxne", "axxnh", "axxnj", "axxnm", "axxok", "axxtx",
    "axxva", "axxwv",
    # State Governor general (MXX***)
    "mxxak", "mxxaz", "mxxca", "mxxco", "mxxfl", "mxxga", "mxxil",
    "mxxks", "mxxmi", "mxxmn", "mxxnh", "mxxnm", "mxxnv", "mxxny",
    "mxxpa", "mxxri", "mxxsd", "mxxtn", "mxxtx", "mxxwi", "mxdec",
    # Governor party primaries (GP***)
    "gpakr", "gpazr", "gpcad", "gpfld", "gpflr", "gpgad", "gpgar",
    "gpksd", "gpksr", "gpmdr", "gpmed", "gpmer", "gpmid", "gpmir",
    "gpnvd", "gpnyd", "gpnyr", "gpohd", "gpohr", "gppar", "gptnr",
    "gptxd", "gptxr", "gpvad", "gpvar", "gpwid", "gpwir",
    # Senate primaries (BXX***, OXX***, SR***, CXX***)
    "bxxfl", "bxxia", "bxxmi", "bxxwv",
    "oxxco", "oxxct", "oxxia", "oxxma", "oxxmn", "oxxor", "oxxri", "oxxvt",
    "srazl", "srflm", "srmir", "srmts", "srneo", "srnvb", "srohm",
    "srpam", "srptx", "srwih",
    "cxxga", "cxxva",
    # House district primaries (Hxx**, Ixx**)
    "h02ne", "h03pa", "h06ky", "h08oh", "h13ga",
    "i01ga", "i11ga",
    "ky04r",
    # State Senate (other patterns) + others
    "xxxla", "xxxma", "xxxme", "xxxmi", "xxxnh", "xxxok", "xxxtx", "xxxva",
    "zxxaz", "zxxnv", "zxxpa", "zxxwi",
    # General (G***)
    "g01ga", "g01sd", "g02al", "g02mn", "g02nv", "g10pa", "g11nj", "g16fl",
    # Justice primaries / others
    "j04ca", "j04wa", "j11ca", "j22ca", "j40ca",
    "exxfl", "exxoh",
    # International political
    "prech", "preco", "premp",
    # Mayoral / municipal
    "mlaxg", "mnycd", "mnycg",
    # Economics — Fed / rates / CPI / GDP / PPI / employment
    "cpi", "cpic", "cpiy", "cpcey", "pcey", "ppiy", "rgdp", "gdp",
    "unr", "rsm", "hs", "nhs", "inflm", "jolt", "ff", "ffdec", "fedrc",
    "fedlg", "fedro", "dissa", "dissn", "uscci",
    # 2026-06-02 expansion — FX symbols confirmed live but absent from seeds:
    # IJC (US Initial Jobless Claims, conid 626425574),
    # ND (US National Debt, conid 712856715),
    # GT (Global Temperature, conid 712856720 + monthly variants).
    "ijc", "nd", "gt",
    # Retail sales sub-categories
    "ur443", "ur444", "ur451", "ur453", "uspsr", "usip",
    # International economics
    "cagdp", "canhi", "caunr", "ezgdp", "ezunr", "gdpau", "gdpge", "gdpjp",
    "hkgdp", "sggdp", "cpige", "cpiin", "cpijp", "cpisp",
    # Regional US CPI (R***)
    "rcatl", "rcbos", "rcchi", "rcdet", "rclax", "rcmia", "rcmsp",
    "rcmwe", "rcnyc", "rcpac", "rcphl", "rcsal", "rcsth", "rcwdc", "rcwst",
    # Regional US GDP (GDQ**)
    "gdqca", "gdqfl", "gdqga", "gdqil", "gdqnj", "gdqny", "gdqoh",
    "gdqpa", "gdqtx", "gdqwa",
    # Crypto thresholds
    "cbbtc", "cbeth", "cbsol", "cbxrp",
    "cfbtc", "cfdog", "cfsol", "cfxrp",
    "cte2a", "cte2b", "cte2c",
    "yxhbt", "yxhdg", "yxhet", "yxhso", "yxhxr",
    "yxlbt", "yxldg", "yxlet", "yxlso", "yxlxr",
    "hleth", "hletl",
    # Weather — daily temp highs (UH***), drought, hurricanes, carbon
    "uhatl", "uhbna", "uhbos", "uhdca", "uhdfw", "uhhou", "uhlax",
    "uhlga", "uhmdw", "uhmia", "uhokc", "uhphx", "uhsea", "uhsfo",
    "ullga", "ulokc", "utcbo", "uthvt", "utlao", "uttai", "utumo",
    "dhokc", "dlokc", "taokc", "ustm", "usdr", "usce", "hcab", "hlf", "hlfre",
    # Sports — championship futures
    # (NFL/MLB/NHL/NBA championship variants exist on K side as KX***,
    # but the only FX champ markets surfaced were NFL/NCAAF — keep the
    # current binary champion mappings on the K↔P pair side. No FX
    # symbols below; the resolver still handles those via K mappings.)
    # Air travel / fuel / shipping
    "airtd", "airti", "airtt", "airus", "chicc", "macd", "ma",
    "ipi",
    # Business / corporate events
    "cenvd", "itnvd", "mtssx",
    # Misc — entertainment, science, geopolitics
    "peace", "uvr1p", "scttt", "euaia", "fedlg", "ustrf",
    "puseg", "renya", "renyc", "retxc", "reusa", "reusc",
    "rpnyc", "recac", "trfls", "ngp", "gsl", "gce", "ccs", "acd",
    "bpmi", "spifl", "spiga", "spioh", "spipa",
    "frerc", "eucb", "jpusd", "skdec",
    "mort1", "mort2",
    "cmcac", "cmcan", "codts",
    "emcac", "emcar", "emcax",
    "chde1", "chde5",
    "groh", "grva",
    "senc",
    "gcyco", "gcyrm", "gcywh",
    "cs", "op",
    # State Ballot Measures
    "bamca",
    "hookc",
)


DEFAULT_SEED_KEYWORDS: tuple[str, ...] = (
    # Political (current FORECASTX inventory)
    "election", "senate", "house", "governor", "primary", "general",
    "control", "majority", "midterm", "race", "president",
    "republican", "democrat", "congressional",
    # Elections 2026 specifics
    "2026", "midterm 2026", "senate 2026", "house 2026",
    "gubernatorial", "ballot", "referendum", "recall",
    # Sports — covered if/when IBKR adds the inventory
    "mlb", "nfl", "nba", "nhl", "soccer", "tournament", "championship",
    "world cup", "super bowl", "playoff", "world series",
    # Economics / macro
    "cpi", "gdp", "unemployment", "rate", "fed", "fomc", "inflation",
    "recession", "jobs", "payrolls",
    "fed funds", "federal funds", "interest rate", "rate hike", "rate cut",
    "core cpi", "core pce", "pce", "ppi", "retail sales",
    "nonfarm", "jobless", "consumer", "housing starts",
    # Crypto / finance
    "bitcoin", "ethereum", "crypto", "spx", "sp500", "nasdaq",
    # Crypto price levels (BTC/ETH/XRP targets)
    "btc", "eth", "xrp", "solana", "sol", "dogecoin", "doge",
    "bitcoin 100k", "bitcoin 150k", "bitcoin 200k",
    "ethereum 5k", "ethereum 10k", "xrp 5", "xrp 10",
    "btc target", "eth target", "ath", "all-time high",
    # Company earnings
    "earnings", "eps", "revenue", "guidance", "tesla earnings",
    "apple earnings", "nvidia earnings", "microsoft earnings",
    "google earnings", "amazon earnings", "meta earnings",
    "q1 earnings", "q2 earnings", "q3 earnings", "q4 earnings",
    # Tech milestones
    "ai", "gpt", "agi", "openai", "anthropic", "claude", "gemini",
    "tesla", "spacex", "starship", "nvidia", "chip", "semiconductor",
    "iphone", "model release", "product launch",
    # Climate / weather
    "hurricane", "temperature", "climate", "drought", "snowfall",
    # Science / health
    "vaccine", "drug", "approval", "fda", "trial", "study",
    "launch", "rocket", "mission",
    # Entertainment / culture / awards
    "oscar", "emmy", "grammy", "box", "movie", "award",
    "oscars", "academy award", "best picture", "best actor",
    "best actress", "best director", "grammys", "song of the year",
    "album of the year", "emmys", "outstanding drama", "golden globe",
    # Legal / policy / SCOTUS
    "scotus", "court", "ruling", "verdict", "indictment",
    "supreme court", "scotus decision", "scotus ruling",
    "scotus opinion", "appeal", "appellate", "doj", "justice department",
    # Geopolitics
    "russia", "ukraine", "china", "taiwan", "israel", "iran",
    "north korea", "nato", "un", "united nations", "g7", "g20",
    "summit", "ceasefire", "treaty", "sanction", "tariff", "trade war",
    "peace deal", "border", "invasion",
    # ── 2026-05-26 Phase 3 expansion: state-level political races ──
    # FORECASTX's political coverage has been expanding state-by-state
    # but our previous seed list relied on top-level keywords (election,
    # senate, governor) which IBKR's search caps at ~15 results per
    # query. Naming the high-volume races explicitly produces 60+ extra
    # hits per pass with no duplicate cost (dedup by conid). Targets
    # the 11 confirmed candidate political_event conids that show up
    # in MARKET_MAP but aren't yet bound to a Kalshi/PMUS mapping.
    "ohio", "texas", "florida", "arizona", "pennsylvania", "michigan",
    "wisconsin", "georgia", "nevada", "north carolina", "virginia",
    "new york", "california", "illinois", "minnesota",
    "ohio senate", "ohio governor",
    "texas senate", "texas governor",
    "florida senate", "florida governor",
    "arizona senate", "arizona governor",
    "pennsylvania senate", "pennsylvania governor",
    "michigan senate", "michigan governor",
    "wisconsin senate", "wisconsin governor",
    "georgia senate", "georgia governor",
    "nevada senate", "nevada governor",
    "north carolina senate", "north carolina governor",
    "virginia senate", "virginia governor",
    # Production / fundraising events
    "production", "factory", "shipments", "deliveries",
    "tesla deliveries", "tesla production", "f150", "model y",
    "iphone shipments", "iphone sales",
    "solana 200", "solana 300", "solana 500",
    "xrp 3", "xrp 5",
    "btc 200k", "btc 250k", "btc 300k", "eth 8k", "eth 15k",
)


def _dedupe_seeds(seeds: tuple[str, ...], extras: tuple[str, ...]) -> tuple[str, ...]:
    """Order-preserving dedup so the seed list stays diff-stable."""
    seen: set[str] = set()
    out: list[str] = []
    for s in seeds + extras:
        k = s.lower().strip()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return tuple(out)


# 2026-05-26 full catalog sweep: append every FORECASTX SYMBOL the live
# IBKR probe surfaced (323 unique events). Symbol-prefix queries
# guarantee IBKR's search returns the exact parent row even when
# keyword similarity fails. Discovery's conid-keyed dedup makes
# duplicate enumerations free; here we still dedupe at the seed level
# so each query happens exactly once per pass.
DEFAULT_SEED_KEYWORDS = _dedupe_seeds(DEFAULT_SEED_KEYWORDS, _FX_SYMBOL_SEEDS)

# Conservative threshold. Below this, log the near-miss but do not write.
# 0.30 picks up "U.S House Midterm Winner" ↔ "US House Control" (3-of-9
# overlap) while the domain-routing guard below blocks the obvious false
# positives (sports mappings ↔ FORECASTX geographic primaries that share
# only city names).
DEFAULT_MIN_SCORE = 0.30

# Tokens that mark a mapping (or event) as a sports market. When the mapping
# carries any of these and the FORECASTX event title does not, we refuse to
# match — geographic overlap alone ("New York Yankees" vs "New York Governor")
# is a false-positive trap.
#
# 2026-06-02 expansion: Kalshi-channel canonical_ids ("CHAMP_KXNCAAF_27_OKLA",
# "GAME_MLB_20260516_TEX_..." etc.) tokenize to fragments that did not match
# the original set, so 22 sports mappings got wrong-domain conids bound.
# Adding the prefix tokens and Kalshi-channel slugs catches them.
_SPORTS_HINT_TOKENS: frozenset[str] = frozenset({
    "mlb", "nfl", "nba", "nhl", "mls", "epl", "uefa", "fifa", "wnba",
    "game", "vs", "yankees", "mets", "dodgers", "celtics", "lakers",
    "sox", "cubs", "padres", "rangers", "phillies", "athletic",
    "fc", "soccer", "football", "basketball", "baseball", "hockey",
    "tournament", "playoff", "championship", "match", "tie",
    # Kalshi-channel slugs (CHAMP_/GAME_ canonical_id family)
    "champ", "afcchamp", "nfcchamp", "kxncaaf", "kxnfl", "kxnflafcchamp",
    "kxnflnfcchamp", "kxmlb", "kxnba", "kxnhl", "kxmls", "kxepl", "kxwc",
    "kxnflweek", "kxnflsb", "afc", "nfc", "ncaaf", "ncaab", "fbs", "fcs",
    "ncaa", "draft", "super", "bowl", "series", "finals", "trophy",
    # Team mascots + locations frequent in canonical_ids
    "okla", "osu", "lsu", "fla", "mich", "ind", "ore", "uga", "usc",
    "bal", "buf", "cin", "cle", "den", "hou", "jac", "kc", "lac", "lv",
    "mia", "ne", "nyj", "nyg", "pit", "ten", "ari", "atl", "car", "chi",
    "dal", "det", "gb", "lar", "min", "no", "phi", "sea", "sf", "tb",
    "was", "wsh", "tex", "phl", "col",
})

# Tokens that mark a mapping (or event) as political.
_POLITICAL_HINT_TOKENS: frozenset[str] = frozenset({
    "election", "senate", "house", "governor", "primary", "general",
    "control", "majority", "midterm", "president", "presidential",
    "republican", "democrat", "democratic", "congressional", "gop",
    "dem", "party",
})

# Tokens that mark a FORECASTX event as a regional/macro economic
# indicator (CPI, GDP, PCE, retail sales, etc.). 2026-05-25 saw 39
# wrong-domain attachments where Kalshi MLB game mappings were bound
# to regional-CPI FORECASTX events because they shared a city name
# token (NYC, ATL, etc.). Treating these as a distinct domain — and
# refusing to pair them with sports or political mappings — closes
# that hole. Two failures suffice: (a) the discovery scorer drops
# the pairing to 0, and (b) the resolver's mark-unavailable path
# already auto-clears any wrong-domain attachment that's already on
# disk.
_ECONOMIC_HINT_TOKENS: frozenset[str] = frozenset({
    "cpi", "gdp", "pce", "ppi", "fomc", "inflation",
    "unemployment", "payrolls", "nonfarm", "jobless", "retail",
    "rates", "rate", "fed", "feds", "powell",
    "ism", "pmi", "manufacturing", "housing", "starts",
    "permits", "claims", "consumer", "sentiment", "michigan",
})

# Weather/climate FORECASTX events ("Daily Temperature High UHSFO",
# "Hurricane Season Storm Count", etc.) frequently share a city token
# with sports mappings (San Francisco 49ers ↔ San Francisco Temperature)
# and got bound to NFL/MLB rows 2026-05-26. Treating weather as a
# distinct domain — like the economic carve-out above — blocks the
# pairing whenever the mapping is clearly sports or political and the
# event is clearly weather.
_WEATHER_HINT_TOKENS: frozenset[str] = frozenset({
    "temperature", "temp", "high", "low", "rainfall", "snow", "snowfall",
    "hurricane", "tropical", "storm", "tornado", "drought", "climate",
    "carbon", "wind",
})

# Marker IBKR uses in companyHeader to identify ForecastEx event listings.
FORECASTX_MARKER = "FORECASTX"


# Conid-level blocklist — these IBKR conids were observed live (2026-06-02)
# being attached to multiple unrelated sports mappings (sign of a parent
# event that fuzzy-matches sports geography). They are permanently barred
# from auto-binding regardless of what discovery scores them against.
# Operators can clear the blocklist manually if a conid is later confirmed
# to be a legitimate match.
_BLOCKLISTED_FORECASTEX_CONIDS: frozenset[str] = frozenset({
    # 2026-06-02 sports-mis-bind cleanup: each conid below was attached to
    # ≥2 sports mappings (NFL/NCAAF/MLB) at the same time, which is a
    # structural impossibility for a binary FORECASTX contract.
    "860934996",   # bound to KXNCAAF_27_OKLA + KXNFLAFCCHAMP_27_KC + MLB games
    "882351367",   # bound to NYJ + NO + NYG simultaneously
    "831072023",   # bound to OSU
    "831072372",   # bound to 5 different MLB TEX/HOU games
    "831077453",   # bound to ATL
    "831078727",   # bound to NE
    "841511353",   # bound to WAS
    "853400777",   # bound to MLB PHI
    "853400816",   # bound to MLB SF
    "866500896",   # bound to MLB MIA
    "881980037",   # bound to MLB MIA late
    "881980047",   # bound to MLB KC late
    "882350597",   # bound to CHI
})


def is_blocklisted_forecastex_conid(conid: str) -> bool:
    """Return True if the conid is on the permanent wrong-domain blocklist."""
    return str(conid or "").strip() in _BLOCKLISTED_FORECASTEX_CONIDS


def _strip_marker(title: str) -> str:
    """Remove the ``(FORECASTX)`` / ``- FORECASTX`` suffix from an event title."""
    cleaned = title.replace("(FORECASTX)", "").replace("- FORECASTX", "")
    return cleaned.strip(" -")


def _domain_compatible(event_title: str, mapping: MarketMapping) -> bool:
    """Block cross-domain matches.

    A FORECASTX event titled "New York Governor Republican Primary" should
    never bind to a Kalshi MLB mapping ("New York Yankees vs Mets") even
    though they share the "new york" tokens. The check fires only when the
    mapping clearly belongs to one domain (sports) and the event clearly
    belongs to another (political), so it stays conservative and won't
    reject mappings whose domain we can't infer.
    """
    mapping_text = " ".join(
        s for s in (
            mapping.canonical_id,
            mapping.description,
            mapping.polymarket_question,
        ) if s
    )
    mapping_tokens = _tokenize(mapping_text)
    event_tokens = _tokenize(event_title)

    mapping_is_sports = bool(mapping_tokens & _SPORTS_HINT_TOKENS)
    event_is_political = bool(event_tokens & _POLITICAL_HINT_TOKENS)
    event_is_sports = bool(event_tokens & _SPORTS_HINT_TOKENS)
    mapping_is_political = bool(mapping_tokens & _POLITICAL_HINT_TOKENS)
    event_is_economic = bool(event_tokens & _ECONOMIC_HINT_TOKENS)
    mapping_is_economic = bool(mapping_tokens & _ECONOMIC_HINT_TOKENS)
    event_is_weather = bool(event_tokens & _WEATHER_HINT_TOKENS)
    mapping_is_weather = bool(mapping_tokens & _WEATHER_HINT_TOKENS)

    if mapping_is_sports and event_is_political and not event_is_sports:
        return False
    if mapping_is_political and event_is_sports and not event_is_political:
        return False
    # Sports↔economic and political↔economic crossovers. Geographic
    # overlap ("New York Yankees" vs "New York regional CPI") drove
    # 39 wrong-domain attachments live 2026-05-25 — every cycle of
    # the resolver burned IBKR rate-limit budget probing these dead
    # ends. Reject any pairing where one side is clearly economic
    # and the other clearly isn't.
    if mapping_is_sports and event_is_economic and not event_is_sports:
        return False
    if mapping_is_economic and event_is_sports and not event_is_economic:
        return False
    if mapping_is_political and event_is_economic and not event_is_political:
        return False
    if mapping_is_economic and event_is_political and not event_is_economic:
        return False
    # Weather carve-out — same shape as the economic one.
    if mapping_is_sports and event_is_weather and not event_is_sports:
        return False
    if mapping_is_weather and event_is_sports and not event_is_weather:
        return False
    if mapping_is_political and event_is_weather and not event_is_political:
        return False
    if mapping_is_weather and event_is_political and not event_is_weather:
        return False
    return True


def _score_event_against_mapping(event_title: str, mapping: MarketMapping) -> float:
    """Best similarity between the FORECASTX event title and any descriptive
    field on the mapping (description, polymarket question, aliases).

    Combines two signals and keeps the higher one:
      * overlap coefficient — robust when FORECASTX titles are wordier
      * Jaccard (similarity_score) — penalises spurious matches by union size

    Both run with the same stopword-filtered token set. Cross-domain
    pairings (sports mapping ↔ political event, or vice versa) short-circuit
    to 0 so geographic overlap can't carry a match.
    """
    if not _domain_compatible(event_title, mapping):
        return 0.0
    candidates = (
        mapping.description,
        mapping.polymarket_question,
        " ".join(mapping.aliases) if mapping.aliases else "",
    )
    best = 0.0
    for cand in candidates:
        if not cand:
            continue
        s_overlap = _overlap_score(event_title, cand)
        s_jaccard = similarity_score(event_title, cand)
        score = max(s_overlap, s_jaccard)
        if score > best:
            best = score
    return best


async def _search_forecastx_events(
    client, keyword: str,
) -> list[dict[str, Any]]:
    """Issue a single secdef/search call and return only FORECASTX hits."""
    try:
        payload = await client._request(
            "POST",
            "/iserver/secdef/search",
            json_body={"symbol": keyword, "name": True},
        )
    except Exception as exc:  # noqa: BLE001 — broad catch so one bad keyword can't kill the whole pass
        logger.warning(
            "forecastex_discovery: search '%s' failed: %s", keyword, exc,
        )
        return []

    # _request normalises bare lists to ``{"items": [...]}`` so callers can
    # always treat the response as a dict.
    items: Iterable[Any]
    if isinstance(payload, dict):
        items = payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return []

    hits: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        header = str(item.get("companyHeader") or "")
        if FORECASTX_MARKER not in header.upper():
            continue
        conid = str(item.get("conid") or "").strip()
        # is_valid_conid, not truthiness: IBKR search payloads carry the
        # literal "0" (and serializers "None"), which are truthy strings —
        # filtering only at read time let them persist and be re-probed
        # forever (the 2026-07 /iserver/secdef/strikes 429 storm).
        if not is_valid_conid(conid):
            continue
        hits.append(
            {
                "conid": conid,
                "title": _strip_marker(header),
                "symbol": str(item.get("symbol") or ""),
                "raw_header": header,
            }
        )
    return hits


async def enumerate_forecastex_events(
    client, *, keywords: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """Walk a seed keyword list and return deduped FORECASTX events."""
    kws = tuple(keywords) if keywords is not None else DEFAULT_SEED_KEYWORDS
    seen: dict[str, dict[str, Any]] = {}
    for kw in kws:
        for hit in await _search_forecastx_events(client, kw):
            seen.setdefault(hit["conid"], hit)
    return list(seen.values())


async def discover(
    forecastex_client,
    mapping_store: MarketMappingStore,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    keywords: Optional[Iterable[str]] = None,
    dry_run: bool = False,
) -> int:
    """Attach ``forecastex_contract_id`` to confirmed mappings missing one.

    Returns the count of mappings that were updated (or would be, in dry-run).
    """
    if forecastex_client is None:
        logger.info("forecastex_discovery: client unavailable — skipping")
        return 0

    events = await enumerate_forecastex_events(
        forecastex_client, keywords=keywords,
    )
    logger.info(
        "forecastex_discovery: enumerated %d unique FORECASTX events", len(events),
    )
    # Even when events is empty we still want to run the NO-conid backfill
    # pass below — gateway flakes shouldn't block backfilling a mapping
    # whose YES was attached on a healthy cycle.

    # Gather confirmed mappings that haven't been bound to a ForecastEx
    # contract yet. iter_confirmed is an async generator returning
    # (canonical_id, MarketMapping). Skip rows already negative-cached
    # via `forecastex_not_available` — those have been searched in a prior
    # pass and confirmed to have no FORECASTX equivalent, so re-querying
    # IBKR every cycle is wasted rate budget on the long tail of
    # localised sports/cultural markets.
    #
    # Also collect a separate `no_backfill_targets` list: mappings that
    # already have YES bound but are missing NO. Without this pass, the
    # NO conid never gets attached (the resolver and discovery loops both
    # skipped any mapping with YES already set, leaving 0/N populated NO
    # conids live 2026-06-02).
    targets: list[MarketMapping] = []
    no_backfill_targets: list[MarketMapping] = []
    skipped_negative_cache = 0
    async for _canonical_id, mapping in mapping_store.iter_confirmed():
        # The quarantine check MUST come first. It used to sit after the
        # `has_yes` branch below, whose `continue` made it unreachable for
        # any row with a YES conid — so quarantined rows kept getting probed
        # on the NO-backfill path every cycle.
        if getattr(mapping, "forecastex_not_available", False):
            skipped_negative_cache += 1
            continue
        # Use is_valid_conid, not plain truthiness: IBKR payloads persist the
        # literal strings "0" and "None" as conids, and bool("0") is True.
        # Treating those as "YES already bound" sent them to the NO-backfill
        # pass, which called resolve_event_children("0") and burned the
        # /iserver/secdef/strikes 429 budget on every discovery cycle.
        has_yes = is_valid_conid(mapping.forecastex_contract_id)
        has_no = is_valid_conid(getattr(mapping, "forecastex_no_contract_id", ""))
        if has_yes:
            if not has_no:
                no_backfill_targets.append(mapping)
            continue
        targets.append(mapping)

    logger.info(
        "forecastex_discovery: %d confirmed mapping(s) missing forecastex_contract_id "
        "(skipped %d previously-negative-cached), %d need NO-backfill",
        len(targets),
        skipped_negative_cache,
        len(no_backfill_targets),
    )

    matched = 0
    unavailable_marked = 0
    # Skip the main matching loop when no events came back — but DO still
    # run the NO-backfill pass for mappings already bound to YES.
    targets_to_iterate = targets if events else []
    for mapping in targets_to_iterate:
        # Build a SORTED list of all candidates above zero so blocklist
        # rejection can fall back to the next-best instead of skipping
        # the mapping entirely. The previous one-shot selection allowed a
        # blocklisted top-scorer to bury a legitimate runner-up.
        scored: list[tuple[float, int, dict[str, Any]]] = []
        any_event_scored = False
        for event in events:
            score = _score_event_against_mapping(event["title"], mapping)
            if score <= 0:
                continue
            any_event_scored = True
            event_token_count = len(_tokenize(event["title"]))
            scored.append((score, event_token_count, event))
        # Sort by score desc, then by token_count asc (tiebreaker — shorter
        # titles are typically the umbrella event).
        scored.sort(key=lambda t: (-t[0], t[1]))
        best_event: Optional[dict[str, Any]] = None
        best_score = 0.0
        # Walk candidates in rank order; first non-blocklisted wins.
        for score, _tok, event in scored:
            if is_blocklisted_forecastex_conid(event["conid"]):
                logger.debug(
                    "forecastex_discovery: skip blocklisted candidate "
                    "%s (conid=%s) for %s; trying next-best",
                    event["title"], event["conid"], mapping.canonical_id,
                )
                continue
            best_event = event
            best_score = score
            break

        if best_event is None:
            # Two cases:
            #   (a) no event scored above zero (any_event_scored=False) —
            #       the genuine negative-cache case for sports/cultural
            #       mappings with no FORECASTX equivalent.
            #   (b) all positive-scoring candidates were blocklisted
            #       (any_event_scored=True) — we should still mark
            #       unavailable so discovery doesn't burn rate budget on
            #       this mapping every cycle.
            if not dry_run:
                try:
                    await mapping_store.mark_forecastex_unavailable(
                        mapping.canonical_id
                    )
                    unavailable_marked += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "forecastex_discovery: mark_forecastex_unavailable "
                        "failed for %s: %s",
                        mapping.canonical_id, exc,
                    )
            continue

        if best_score < min_score:
            logger.debug(
                "forecastex_discovery: %s — best near-miss '%s' (conid=%s) score=%.2f < %.2f",
                mapping.canonical_id, best_event["title"], best_event["conid"],
                best_score, min_score,
            )
            continue

        # Resolve YES/NO child conids if the attached parent is an event.
        # IBKR's FORECASTX EC endpoints are flaky (503 on weekends, often
        # unsupported) so the resolver returns [] gracefully when it can't
        # enumerate children — we then fall back to the parent conid and
        # let the collector's parent-detection logic skip it gracefully.
        resolved_conid = best_event["conid"]
        resolved_no_conid = ""
        try:
            children = await forecastex_client.resolve_event_children(
                best_event["conid"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "forecastex_discovery: resolve_event_children failed for %s: %s",
                best_event["conid"], exc,
            )
            children = []
        if children:
            # Pick the YES side by convention (right in {"Y","C","1"}); if no
            # right info is present, fall back to the first child.
            yes_child = next(
                (c for c in children
                 if str(c.get("right", "")).upper() in ("Y", "C", "1", "YES")),
                children[0],
            )
            resolved_conid = yes_child["conid"]
            # Blocklist check on the resolved YES child conid too, in case
            # the parent passed but the actual child option is flagged.
            if is_blocklisted_forecastex_conid(resolved_conid):
                logger.warning(
                    "forecastex_discovery: REFUSE blocklisted YES child %s "
                    "for %s (parent=%s)",
                    resolved_conid, mapping.canonical_id, best_event["conid"],
                )
                if not dry_run:
                    try:
                        await mapping_store.mark_forecastex_unavailable(
                            mapping.canonical_id
                        )
                        unavailable_marked += 1
                    except Exception:
                        pass
                continue
            # Also pick the NO sibling at the same strike (right=P/N/0/NO).
            # ForecastEx binary contracts have SEPARATE IBKR conids for YES
            # (Call) and NO (Put) under the same parent event; storing only
            # YES was the root cause of the ARB-000695/699 phantom-trade
            # incident — see arbiter/collectors/forecastex._build_price_point.
            yes_strike = yes_child.get("strike")
            try:
                yes_strike_f = float(yes_strike) if yes_strike is not None else None
            except (TypeError, ValueError):
                yes_strike_f = None
            for child in children:
                if not isinstance(child, dict):
                    continue
                if str(child.get("right", "")).upper() not in ("P", "N", "0", "NO", "PUT"):
                    continue
                if yes_strike_f is not None:
                    try:
                        child_strike = float(child.get("strike") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if abs(child_strike - yes_strike_f) > 1e-6:
                        continue
                no_candidate = str(child.get("conid") or "").strip()
                if no_candidate and no_candidate != resolved_conid:
                    resolved_no_conid = no_candidate
                    break
            logger.info(
                "forecastex_discovery: resolved %s parent %s -> YES=%s NO=%s "
                "(yes_right=%s, src=%s)",
                mapping.canonical_id, best_event["conid"],
                resolved_conid, resolved_no_conid or "(unresolved)",
                yes_child.get("right"), yes_child.get("source"),
            )

        logger.info(
            "forecastex_discovery: MATCH %s ↔ conid=%s '%s' (score=%.2f)%s",
            mapping.canonical_id,
            resolved_conid,
            best_event["title"],
            best_score,
            " [dry-run]" if dry_run else "",
        )

        if dry_run:
            matched += 1
            continue

        mapping.forecastex_contract_id = resolved_conid
        if resolved_no_conid:
            mapping.forecastex_no_contract_id = resolved_no_conid
        try:
            await mapping_store.upsert(mapping)
            matched += 1
        except Exception as exc:  # noqa: BLE001 — never block discovery on one row
            logger.error(
                "forecastex_discovery: upsert failed for %s: %s",
                mapping.canonical_id, exc,
            )

    logger.info(
        "forecastex_discovery: attached %d/%d mappings "
        "(negative-cached %d with no FORECASTX overlap)",
        matched, len(targets), unavailable_marked,
    )

    # ── NO-conid backfill ─────────────────────────────────────────────
    # Mappings already bound to a YES conid but missing the NO conid get
    # processed here. The previous code path bailed out before this loop
    # because both `discover()` and the resolver candidate filter required
    # YES to be missing — leaving NO conids permanently unpopulated. The
    # phantom-trade postmortem (ARB-000695/699, commit d157cb6) made NO
    # conids a hard requirement for FX-on-the-NO-side execution; without
    # them, FX never trades on the NO leg.
    backfilled = 0
    for mapping in no_backfill_targets:
        yes_conid = (mapping.forecastex_contract_id or "").strip()
        if not yes_conid:
            continue
        if is_blocklisted_forecastex_conid(yes_conid):
            # Don't try to resolve a NO sibling under a blocklisted YES.
            continue
        # Try resolving siblings under the YES child conid itself (if YES
        # was already a child OPT). When YES is the parent EC, the
        # collector layer has the parent_id; we can't rebuild that here
        # so we just probe YES — if it returns no siblings the resolver
        # background loop will eventually handle it.
        try:
            siblings = await forecastex_client.resolve_event_children(yes_conid)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "forecastex_discovery: NO-backfill resolve_event_children(%s) "
                "failed for %s: %s",
                yes_conid, mapping.canonical_id, exc,
            )
            continue
        if not siblings:
            continue
        # Find the YES child in siblings to read its strike, then pick the
        # matching Put sibling at the same strike.
        yes_strike_f = None
        for s in siblings:
            if not isinstance(s, dict):
                continue
            if str(s.get("conid") or "").strip() == yes_conid:
                try:
                    yes_strike_f = float(s.get("strike") or 0.0)
                except (TypeError, ValueError):
                    yes_strike_f = None
                break
        no_candidate = ""
        for s in siblings:
            if not isinstance(s, dict):
                continue
            if str(s.get("right", "")).upper() not in ("P", "N", "0", "NO", "PUT"):
                continue
            if yes_strike_f is not None:
                try:
                    cs = float(s.get("strike") or 0.0)
                except (TypeError, ValueError):
                    continue
                if abs(cs - yes_strike_f) > 1e-6:
                    continue
            cand = str(s.get("conid") or "").strip()
            if cand and cand != yes_conid and not is_blocklisted_forecastex_conid(cand):
                no_candidate = cand
                break
        if not no_candidate:
            continue
        if dry_run:
            backfilled += 1
            continue
        mapping.forecastex_no_contract_id = no_candidate
        try:
            await mapping_store.upsert(mapping)
            backfilled += 1
            logger.info(
                "forecastex_discovery: NO-backfilled %s YES=%s NO=%s",
                mapping.canonical_id, yes_conid, no_candidate,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "forecastex_discovery: NO-backfill upsert failed for %s: %s",
                mapping.canonical_id, exc,
            )
    if no_backfill_targets:
        logger.info(
            "forecastex_discovery: NO-backfilled %d/%d mappings with YES-only conid",
            backfilled, len(no_backfill_targets),
        )
    return matched


__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULT_SEED_KEYWORDS",
    "discover",
    "enumerate_forecastex_events",
]
