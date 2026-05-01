"""Canonical event/outcome fingerprints for cross-platform market mapping."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from arbiter.config.settings import normalize_market_text
from arbiter.mapping.sports_safety import (
    KALSHI_SPORT_TO_POLY,
    SUPPORTED_POLY_WINNER_PREFIXES,
    parse_kalshi_sports_ticker,
    parse_polymarket_sports_slug,
)
from arbiter.mapping.team_aliases import (
    canonical_pair,
    normalize_entity_code,
    split_compound_code,
)

_POLITICS_KALSHI_CONTROL_RE = re.compile(r"^CONTROL([HS])-(20\d{2})-([DR])$")
_POLITICS_POLY_CONTROL_RE = re.compile(
    r"^paccc-us(ho|se)-midterms-(20\d{2}-\d{2}-\d{2})-(dem|rep)$"
)
_CRYPTO_KALSHI_RE = re.compile(
    r"^KX(?P<asset>BTC|ETH|XRP|SOL|DOGE)[A-Z]*-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})(?P<hh>\d{2})?-T(?P<threshold>[0-9.]+)$"
)
_CRYPTO_POLY_REACH_RE = re.compile(
    r"^(?:will-)?(?P<asset>bitcoin|btc|ethereum|eth|xrp|solana|sol|dogecoin|doge)"
    r"(?:-[a-z0-9]+)*-reach-(?P<threshold>[0-9]+(?:pt[0-9]+)?)"
    r"-by-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>20\d{2})$"
)
_GDP_KALSHI_RE = re.compile(r"^KXGDP-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})-T(?P<threshold>[0-9.]+)$")
_GDP_POLY_RE = re.compile(
    r"^will-us-gdp-growth-in-(?P<period>q[1-4]-20\d{2}|20\d{2})-be-"
    r"(?P<direction>greater-than|less-than|between)-(?P<threshold>[0-9]+pt[0-9]+)"
    r"(?:-and-(?P<threshold2>[0-9]+pt[0-9]+))?$"
)
_FED_KALSHI_RE = re.compile(
    r"^KXFED(?:DECISION|RATE|FOMC)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})-"
    r"(?P<outcome>HOLD|C25P|C25|H25P|H25)$"
)
_FED_POLY_RE = re.compile(
    r"^rdc-usfed-fomc-(?P<date>20\d{2}-\d{2}-\d{2})-"
    r"(?P<outcome>maintains|cut25bps|cutgt25bps|hike25bps|hikegt25bps)$"
)
_CPI_KALSHI_RE = re.compile(
    r"^KXCPI(?P<basis>YOY|MOM)?-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})-"
    r"(?P<bucket>(?:GTE|LTE|T)?[0-9]+(?:\.[0-9]+)?PCT?)$"
)
_CPI_POLY_RE = re.compile(
    r"^cpic-uscpi-(?P<period>[a-z]{3}20\d{2})(?P<basis>yoy|mom)-"
    r"(?P<release>20\d{2}-\d{2}-\d{2})-"
    r"(?P<bucket>(?:gte|lte)?[0-9]+pt[0-9]+pct)$"
)
_UNEMPLOYMENT_KALSHI_RE = re.compile(
    r"^KXUNEMP(?:LOYMENT)?(?P<basis>RATE)?-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})-"
    r"(?P<bucket>(?:GTE|LTE|T)?[0-9]+(?:\.[0-9]+)?PCT?)$"
)
_UNEMPLOYMENT_POLY_RE = re.compile(
    r"^(?:uec|unemp)-usunemployment-(?P<period>[a-z]{3}20\d{2})-"
    r"(?P<release>20\d{2}-\d{2}-\d{2})-"
    r"(?P<bucket>(?:gte|lte)?[0-9]+pt[0-9]+pct)$"
)
_KALSHI_SPORT_EVENT_RE = re.compile(
    r"^KX([A-Z0-9]+?)(?:GAME|MATCH)-(\d{2})([A-Z]{3})(\d{2})(\d*)([A-Z]+)$"
)

_MONTHS = {
    "jan": "01",
    "january": "01",
    "feb": "02",
    "february": "02",
    "mar": "03",
    "march": "03",
    "apr": "04",
    "april": "04",
    "may": "05",
    "jun": "06",
    "june": "06",
    "jul": "07",
    "july": "07",
    "aug": "08",
    "august": "08",
    "sep": "09",
    "september": "09",
    "oct": "10",
    "october": "10",
    "nov": "11",
    "november": "11",
    "dec": "12",
    "december": "12",
}
_KALSHI_MONTHS = {key[:3].upper(): value for key, value in _MONTHS.items()}
_ASSET_ALIASES = {
    "bitcoin": "btc",
    "btc": "btc",
    "ethereum": "eth",
    "eth": "eth",
    "xrp": "xrp",
    "ripple": "xrp",
    "solana": "sol",
    "sol": "sol",
    "dogecoin": "doge",
    "doge": "doge",
}


@dataclass(frozen=True)
class MarketFingerprint:
    category: str
    subcategory: str
    entity: str
    date: str
    metric: str
    threshold: str
    outcome: str
    direction: str = "yes"
    source: str = "official_result"

    @property
    def event_key(self) -> str:
        return ":".join(
            (
                self.category,
                self.subcategory,
                self.entity,
                self.date,
                self.metric,
                self.threshold,
            )
        )

    @property
    def market_key(self) -> str:
        return f"{self.event_key}:{self.direction}:{self.outcome}"


@dataclass(frozen=True)
class StructuralMatch:
    category: str
    event_key: str
    market_key: str
    outcome: str
    polarity: str
    resolution_date: str
    resolution_source: str
    rule: str

    def candidate_fields(self) -> dict[str, Any]:
        return {
            "structural_match": True,
            "event_fingerprint": self.event_key,
            "outcome_fingerprint": self.market_key,
            "outcome": self.outcome,
            "category": self.category,
            "kalshi_category": self.category,
            "polymarket_category": self.category,
            "polarity": self.polarity,
            "resolution_date": self.resolution_date,
            "kalshi_resolution_date": self.resolution_date,
            "polymarket_resolution_date": self.resolution_date,
            "resolution_source": self.resolution_source,
            "kalshi_resolution_source": self.resolution_source,
            "polymarket_resolution_source": self.resolution_source,
            "tie_break_rule": self.rule,
            "kalshi_tie_break_rule": self.rule,
            "polymarket_tie_break_rule": self.rule,
            "outcome_set": ("Yes", "No"),
            "kalshi_outcome_set": ("Yes", "No"),
            "polymarket_outcome_set": ("Yes", "No"),
        }


def fingerprint_kalshi_market(market: dict) -> MarketFingerprint | None:
    return (
        _fingerprint_kalshi_sports(market)
        or _fingerprint_kalshi_politics(market)
        or _fingerprint_kalshi_crypto(market)
        or _fingerprint_kalshi_gdp(market)
        or _fingerprint_kalshi_fed_decision(market)
        or _fingerprint_kalshi_cpi(market)
        or _fingerprint_kalshi_unemployment(market)
    )


def fingerprint_kalshi_event(event: dict) -> MarketFingerprint | None:
    ticker = str(event.get("event_ticker", "") or "").strip().upper()
    match = _KALSHI_SPORT_EVENT_RE.match(ticker)
    if not match:
        return None
    sport = match.group(1).lower()
    poly_sport = KALSHI_SPORT_TO_POLY.get(sport)
    event_date = _kalshi_date(match.group(2), match.group(3), match.group(4))
    participants = split_compound_code(match.group(6))
    if not poly_sport or not event_date or participants is None:
        return None
    return MarketFingerprint(
        category="sports",
        subcategory=poly_sport,
        entity=canonical_pair(*participants),
        date=event_date,
        metric="winner",
        threshold="moneyline",
        outcome="*",
        direction="event",
        source="official_sports_result",
    )


def fingerprint_polymarket_market(market: dict) -> MarketFingerprint | None:
    return (
        _fingerprint_poly_sports(market)
        or _fingerprint_poly_politics(market)
        or _fingerprint_poly_crypto(market)
        or _fingerprint_poly_gdp(market)
        or _fingerprint_poly_fed_decision(market)
        or _fingerprint_poly_cpi(market)
        or _fingerprint_poly_unemployment(market)
    )


def structural_match(kalshi_market: dict, polymarket_market: dict) -> StructuralMatch | None:
    kalshi = fingerprint_kalshi_market(kalshi_market)
    poly = fingerprint_polymarket_market(polymarket_market)
    if kalshi is None or poly is None:
        return None
    if kalshi.market_key != poly.market_key:
        return None
    if kalshi.source != poly.source:
        return None
    return StructuralMatch(
        category=kalshi.category,
        event_key=kalshi.event_key,
        market_key=kalshi.market_key,
        outcome=kalshi.outcome,
        polarity="same",
        resolution_date=kalshi.date,
        resolution_source=kalshi.source,
        rule=f"{kalshi.category}:{kalshi.metric}:{kalshi.threshold}:{kalshi.outcome}",
    )


def _fingerprint_kalshi_sports(market: dict) -> MarketFingerprint | None:
    parsed = parse_kalshi_sports_ticker(str(market.get("ticker", "") or ""))
    if parsed is None:
        return None
    participants = split_compound_code(parsed.participants_raw)
    if participants is None:
        return None
    outcome = normalize_entity_code(parsed.side)
    return MarketFingerprint(
        category="sports",
        subcategory=parsed.poly_sport,
        entity=canonical_pair(*participants),
        date=parsed.date,
        metric="winner",
        threshold="moneyline",
        outcome=outcome,
        source="official_sports_result",
    )


def _fingerprint_poly_sports(market: dict) -> MarketFingerprint | None:
    parsed = parse_polymarket_sports_slug(str(market.get("slug", "") or ""))
    if parsed is None or parsed.prefix not in SUPPORTED_POLY_WINNER_PREFIXES:
        return None
    outcome = normalize_entity_code(parsed.side) if parsed.side else normalize_entity_code(parsed.team1)
    return MarketFingerprint(
        category="sports",
        subcategory=parsed.sport,
        entity=canonical_pair(parsed.team1, parsed.team2),
        date=parsed.date,
        metric="winner",
        threshold="moneyline",
        outcome=outcome,
        source="official_sports_result",
    )


def _fingerprint_kalshi_politics(market: dict) -> MarketFingerprint | None:
    match = _POLITICS_KALSHI_CONTROL_RE.match(str(market.get("ticker", "") or "").upper())
    if not match:
        return None
    chamber = "house" if match.group(1) == "H" else "senate"
    year = match.group(2)
    party = "dem" if match.group(3) == "D" else "rep"
    return MarketFingerprint(
        category="politics",
        subcategory="us",
        entity=chamber,
        date=f"{year}-11-03",
        metric="party-control",
        threshold="majority",
        outcome=party,
        source="official_us_election_result",
    )


def _fingerprint_poly_politics(market: dict) -> MarketFingerprint | None:
    match = _POLITICS_POLY_CONTROL_RE.match(str(market.get("slug", "") or "").lower())
    if not match:
        return None
    chamber = "house" if match.group(1) == "ho" else "senate"
    return MarketFingerprint(
        category="politics",
        subcategory="us",
        entity=chamber,
        date=match.group(2),
        metric="party-control",
        threshold="majority",
        outcome=match.group(3),
        source="official_us_election_result",
    )


def _fingerprint_kalshi_crypto(market: dict) -> MarketFingerprint | None:
    ticker = str(market.get("ticker", "") or "").upper()
    match = _CRYPTO_KALSHI_RE.match(ticker)
    if not match:
        return None
    month = _KALSHI_MONTHS.get(match.group("mon"))
    if not month:
        return None
    asset = _ASSET_ALIASES.get(match.group("asset").lower(), match.group("asset").lower())
    threshold = _normalize_decimal(match.group("threshold"))
    return MarketFingerprint(
        category="crypto",
        subcategory=asset,
        entity="price",
        date=f"20{match.group('yy')}-{month}-{match.group('dd')}",
        metric="above",
        threshold=threshold,
        outcome="yes",
        source="cf_benchmarks",
    )


def _fingerprint_poly_crypto(market: dict) -> MarketFingerprint | None:
    slug = str(market.get("slug", "") or "").lower()
    match = _CRYPTO_POLY_REACH_RE.match(slug)
    if not match:
        return None
    month = _MONTHS.get(match.group("month"))
    if not month:
        return None
    asset = _ASSET_ALIASES.get(match.group("asset"), match.group("asset"))
    return MarketFingerprint(
        category="crypto",
        subcategory=asset,
        entity="price",
        date=f"{match.group('year')}-{month}-{int(match.group('day')):02d}",
        metric="above",
        threshold=_normalize_decimal(match.group("threshold")),
        outcome="yes",
        source="cf_benchmarks",
    )


def _fingerprint_kalshi_gdp(market: dict) -> MarketFingerprint | None:
    match = _GDP_KALSHI_RE.match(str(market.get("ticker", "") or "").upper())
    if not match:
        return None
    title = normalize_market_text(
        " ".join(str(market.get(field, "") or "") for field in ("title", "subtitle", "rules_primary"))
    )
    period_match = re.search(r"\b(q[1-4])\s*(20\d{2})\b", title)
    period = f"{period_match.group(1)}-{period_match.group(2)}" if period_match else f"20{match.group('yy')}"
    direction = "above"
    if "less than" in title or "below" in title:
        direction = "below"
    return MarketFingerprint(
        category="economics",
        subcategory="us",
        entity=f"gdp-growth-{period}",
        date=_kalshi_date(match.group("yy"), match.group("mon"), match.group("dd")) or "",
        metric=direction,
        threshold=_normalize_decimal(match.group("threshold")),
        outcome="yes",
        source="bea",
    )


def _fingerprint_poly_gdp(market: dict) -> MarketFingerprint | None:
    match = _GDP_POLY_RE.match(str(market.get("slug", "") or "").lower())
    if not match:
        return None
    direction_raw = match.group("direction")
    if direction_raw == "between":
        direction = "between"
        threshold = f"{_normalize_decimal(match.group('threshold'))}-{_normalize_decimal(match.group('threshold2') or '')}"
    else:
        direction = "above" if direction_raw == "greater-than" else "below"
        threshold = _normalize_decimal(match.group("threshold"))
    period = match.group("period")
    return MarketFingerprint(
        category="economics",
        subcategory="us",
        entity=f"gdp-growth-{period}",
        date=_gdp_release_date_for_period(period) or str(market.get("endDate") or market.get("closeTime") or ""),
        metric=direction,
        threshold=threshold,
        outcome="yes",
        source="bea",
    )


def _fingerprint_kalshi_fed_decision(market: dict) -> MarketFingerprint | None:
    ticker = str(market.get("ticker", "") or "").upper()
    match = _FED_KALSHI_RE.match(ticker)
    if not match:
        return None
    text = normalize_market_text(" ".join(str(market.get(field, "") or "") for field in ("title", "subtitle", "rules_primary")))
    if "dissent" in text or (text and "fed" not in text and "fomc" not in text and "federal reserve" not in text):
        return None
    outcome = _normalize_fed_outcome(match.group("outcome"))
    return MarketFingerprint(
        category="economics",
        subcategory="us",
        entity="fomc-rate-decision",
        date=_kalshi_date(match.group("yy"), match.group("mon"), match.group("dd")) or "",
        metric="fed-rate-decision",
        threshold="target-range",
        outcome=outcome,
        source="federal_reserve",
    )


def _fingerprint_poly_fed_decision(market: dict) -> MarketFingerprint | None:
    match = _FED_POLY_RE.match(str(market.get("slug", "") or "").lower())
    if not match:
        return None
    return MarketFingerprint(
        category="economics",
        subcategory="us",
        entity="fomc-rate-decision",
        date=match.group("date"),
        metric="fed-rate-decision",
        threshold="target-range",
        outcome=_normalize_fed_outcome(match.group("outcome")),
        source="federal_reserve",
    )


def _fingerprint_kalshi_cpi(market: dict) -> MarketFingerprint | None:
    match = _CPI_KALSHI_RE.match(str(market.get("ticker", "") or "").upper())
    if not match:
        return None
    basis = (match.group("basis") or "YOY").lower()
    release_date = _kalshi_date(match.group("yy"), match.group("mon"), match.group("dd")) or ""
    return MarketFingerprint(
        category="economics",
        subcategory="us",
        entity=f"cpi-{basis}-{_period_from_text_or_release(market, release_date)}",
        date=release_date,
        metric="cpi",
        threshold=_normalize_percent_bucket(match.group("bucket")),
        outcome="yes",
        source="bls",
    )


def _fingerprint_poly_cpi(market: dict) -> MarketFingerprint | None:
    match = _CPI_POLY_RE.match(str(market.get("slug", "") or "").lower())
    if not match:
        return None
    return MarketFingerprint(
        category="economics",
        subcategory="us",
        entity=f"cpi-{match.group('basis')}-{match.group('period')}",
        date=match.group("release"),
        metric="cpi",
        threshold=_normalize_percent_bucket(match.group("bucket")),
        outcome="yes",
        source="bls",
    )


def _fingerprint_kalshi_unemployment(market: dict) -> MarketFingerprint | None:
    match = _UNEMPLOYMENT_KALSHI_RE.match(str(market.get("ticker", "") or "").upper())
    if not match:
        return None
    release_date = _kalshi_date(match.group("yy"), match.group("mon"), match.group("dd")) or ""
    return MarketFingerprint(
        category="economics",
        subcategory="us",
        entity=f"unemployment-rate-{_period_from_text_or_release(market, release_date)}",
        date=release_date,
        metric="unemployment-rate",
        threshold=_normalize_percent_bucket(match.group("bucket")),
        outcome="yes",
        source="bls",
    )


def _fingerprint_poly_unemployment(market: dict) -> MarketFingerprint | None:
    match = _UNEMPLOYMENT_POLY_RE.match(str(market.get("slug", "") or "").lower())
    if not match:
        return None
    return MarketFingerprint(
        category="economics",
        subcategory="us",
        entity=f"unemployment-rate-{match.group('period')}",
        date=match.group("release"),
        metric="unemployment-rate",
        threshold=_normalize_percent_bucket(match.group("bucket")),
        outcome="yes",
        source="bls",
    )


def _kalshi_date(year: str, month: str, day: str) -> str | None:
    month_number = _KALSHI_MONTHS.get(month.upper())
    if not month_number:
        return None
    return f"20{year}-{month_number}-{day}"


def _normalize_decimal(value: str) -> str:
    text = str(value or "").lower().replace("pt", ".")
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _normalize_fed_outcome(value: str) -> str:
    return {
        "hold": "maintains",
        "maintains": "maintains",
        "c25": "cut25bps",
        "c25p": "cutgt25bps",
        "cut25bps": "cut25bps",
        "cutgt25bps": "cutgt25bps",
        "h25": "hike25bps",
        "h25p": "hikegt25bps",
        "hike25bps": "hike25bps",
        "hikegt25bps": "hikegt25bps",
    }.get(str(value or "").lower(), str(value or "").lower())


def _normalize_percent_bucket(value: str) -> str:
    text = str(value or "").lower().replace("pct", "").replace("%", "")
    prefix = ""
    for candidate in ("gte", "lte", "t"):
        if text.startswith(candidate):
            prefix = candidate
            text = text[len(candidate):]
            break
    normalized = _normalize_decimal(text)
    return f"{prefix}{normalized}" if prefix else normalized


def _gdp_release_date_for_period(period: str) -> str | None:
    # First advance GDP estimate release dates are deterministic enough for
    # structural matching; exact source still comes from BEA on both venues.
    match = re.match(r"q([1-4])-(20\d{2})$", str(period or "").lower())
    if not match:
        return None
    quarter, year = int(match.group(1)), int(match.group(2))
    return {
        1: f"{year}-04-30",
        2: f"{year}-07-30",
        3: f"{year}-10-29",
        4: f"{year + 1}-01-29",
    }[quarter]


def _period_from_text_or_release(market: dict, release_date: str) -> str:
    text = normalize_market_text(" ".join(str(market.get(field, "") or "") for field in ("title", "subtitle", "rules_primary", "description")))
    match = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(20\d{2})\b",
        text,
    )
    if match:
        month = _MONTHS.get(match.group(1))
        if month:
            # Slugs use three-letter month abbreviations, e.g. apr2026.
            abbrev = next(k for k, v in _MONTHS.items() if len(k) == 3 and v == month)
            return f"{abbrev}{match.group(2)}"

    try:
        year_s, month_s, _ = release_date.split("-", 2)
        year = int(year_s)
        month = int(month_s) - 1
        if month == 0:
            month = 12
            year -= 1
        abbrev = next(k for k, v in _MONTHS.items() if len(k) == 3 and v == f"{month:02d}")
        return f"{abbrev}{year}"
    except Exception:
        return "unknown"
