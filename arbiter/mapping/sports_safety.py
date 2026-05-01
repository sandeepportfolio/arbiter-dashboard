"""Sports mapping safety helpers.

These helpers only cover outcome-level game-winner style markets where the
venue identifiers encode the teams and outcome side. Unknown formats are not
declared safe; callers can still keep them as review candidates, but must not
auto-trade them without a separate manual polarity confirmation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from arbiter.mapping.team_aliases import TEAM_ALIASES, normalize_entity_code

MONTHS = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}

KALSHI_SPORT_TO_POLY = {
    "mlb": "mlb",
    "nhl": "nhl",
    "nba": "nba",
    "mls": "mls",
    "bundesliga": "bun",
    "seriea": "sea",
    "laliga": "lal",
    "efll1": "epl",
    "atp": "atp",
    "atpchallenger": "atp",
    "wta": "wta",
    "wtachallenger": "wta",
    "itf": "itf",
    "itfw": "wta",
}

# Prefixes whose Polymarket slugs encode same-event winner / draw outcomes.
# Spread/total/range/futures markets (for example `asc-*` alternate spreads,
# `tsc-*` totals, or `tec-*` championship futures) must not be treated as
# moneyline game/match winners even when the first slug segments look similar.
SUPPORTED_POLY_WINNER_PREFIXES = {"aec", "atc"}

_KALSHI_GAME_RE = re.compile(
    r"^KX([A-Z0-9]+?)GAME-(\d{2})([A-Z]{3})(\d{2})(\d*)([A-Z]+)-([A-Z]+)$"
)
_KALSHI_MATCH_RE = re.compile(
    r"^KX([A-Z0-9]+?)MATCH-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)-([A-Z]+)$"
)
_POLY_SPORTS_PREFIXES = ("aec", "tec", "atc", "tsc", "asc", "rdc")
_POLY_SPORTS_RE = re.compile(
    r"^(aec|tec|atc|tsc|asc|rdc)-([a-z0-9]+)-([a-z0-9]+)-([a-z0-9]+)-"
    r"(\d{4}-\d{2}-\d{2})(?:-([a-z0-9]+))?$"
)


@dataclass(frozen=True)
class KalshiSportsMarket:
    sport: str
    poly_sport: str
    date: str
    participants_raw: str
    side: str


@dataclass(frozen=True)
class PolymarketSportsSlug:
    prefix: str
    sport: str
    team1: str
    team2: str
    date: str
    side: str | None


@dataclass(frozen=True)
class SportsPairSafety:
    known: bool
    safe: bool
    reason: str
    polarity: str = "unknown"
    kalshi_side: str = ""
    polymarket_yes_side: str = ""

    def candidate_fields(self) -> dict[str, str]:
        fields = {
            "polarity": self.polarity,
            "polarity_status": self.reason,
        }
        if self.kalshi_side:
            fields["kalshi_yes_side"] = self.kalshi_side
        if self.polymarket_yes_side:
            fields["polymarket_yes_side"] = self.polymarket_yes_side
        return fields


def normalize_team_code(value: str | None) -> str:
    return normalize_entity_code(value)


def _kalshi_date(year: str, month: str, day: str) -> str | None:
    month_number = MONTHS.get(month.upper())
    if not month_number:
        return None
    return f"20{year}-{month_number}-{day}"


def parse_kalshi_sports_ticker(ticker: str) -> KalshiSportsMarket | None:
    text = str(ticker or "").strip().upper()
    match = _KALSHI_GAME_RE.match(text)
    if match:
        sport = match.group(1).lower()
        date = _kalshi_date(match.group(2), match.group(3), match.group(4))
        poly_sport = KALSHI_SPORT_TO_POLY.get(sport)
        if not date or not poly_sport:
            return None
        return KalshiSportsMarket(
            sport=sport,
            poly_sport=poly_sport,
            date=date,
            participants_raw=match.group(6).lower(),
            side=match.group(7).lower(),
        )

    match = _KALSHI_MATCH_RE.match(text)
    if match:
        sport = match.group(1).lower()
        date = _kalshi_date(match.group(2), match.group(3), match.group(4))
        poly_sport = KALSHI_SPORT_TO_POLY.get(sport)
        if not date or not poly_sport:
            return None
        return KalshiSportsMarket(
            sport=sport,
            poly_sport=poly_sport,
            date=date,
            participants_raw=match.group(5).lower(),
            side=match.group(6).lower(),
        )
    return None


def parse_polymarket_sports_slug(slug: str) -> PolymarketSportsSlug | None:
    match = _POLY_SPORTS_RE.match(str(slug or "").strip().lower())
    if not match:
        return None
    return PolymarketSportsSlug(
        prefix=match.group(1),
        sport=match.group(2),
        team1=match.group(3),
        team2=match.group(4),
        date=match.group(5),
        side=match.group(6),
    )


def _sports_prefix(slug: str) -> str | None:
    text = str(slug or "").strip().lower()
    prefix = text.split("-", 1)[0]
    return prefix if prefix in _POLY_SPORTS_PREFIXES else None


def is_sports_like_polymarket_slug(slug: str) -> bool:
    return _sports_prefix(slug) is not None


def is_supported_polymarket_winner_slug(slug: str) -> bool:
    parsed = parse_polymarket_sports_slug(slug)
    return parsed is not None and parsed.prefix in SUPPORTED_POLY_WINNER_PREFIXES


def is_sports_like_kalshi_ticker(ticker: str) -> bool:
    text = str(ticker or "").strip().upper()
    if parse_kalshi_sports_ticker(text) is not None:
        return True
    if not text.startswith("KX"):
        return False
    sport_markers = (
        "MLB", "NBA", "NFL", "NHL", "MLS", "BUNDESLIGA", "SERIEA",
        "LALIGA", "EPL", "ATP", "WTA", "ITF", "UFC",
    )
    market_type_markers = (
        "GAME", "MATCH", "SPREAD", "TOTAL", "OVERTIME", "SINGLEGAME",
        "MULTIGAME", "PLAYER", "RUNS", "POINTS", "GOALS", "CHAMP",
    )
    return any(marker in text for marker in sport_markers) and any(
        marker in text for marker in market_type_markers
    )


def is_supported_kalshi_winner_ticker(ticker: str) -> bool:
    return parse_kalshi_sports_ticker(ticker) is not None


def unsupported_sports_pair_reason(kalshi_ticker: str, poly_slug: str) -> str | None:
    """Fail closed on obvious sports market-type mismatches at Gate 0."""
    kalshi_sports = is_sports_like_kalshi_ticker(kalshi_ticker)
    poly_sports = is_sports_like_polymarket_slug(poly_slug)
    if not kalshi_sports and not poly_sports:
        return None
    if kalshi_sports and not is_supported_kalshi_winner_ticker(kalshi_ticker):
        return "unsupported_kalshi_sports_market_type"
    if poly_sports and not is_supported_polymarket_winner_slug(poly_slug):
        return "unsupported_polymarket_sports_market_type"
    return None


def evaluate_sports_pair(kalshi_ticker: str, poly_slug: str) -> SportsPairSafety:
    kalshi = parse_kalshi_sports_ticker(kalshi_ticker)
    poly = parse_polymarket_sports_slug(poly_slug)
    unsupported_reason = unsupported_sports_pair_reason(kalshi_ticker, poly_slug)
    if unsupported_reason is not None:
        return SportsPairSafety(
            known=True,
            safe=False,
            reason=unsupported_reason,
        )
    if kalshi is None or poly is None:
        return SportsPairSafety(
            known=False,
            safe=True,
            reason="not_structured_sports_pair",
        )

    kalshi_side = normalize_team_code(kalshi.side)
    poly_team1 = normalize_team_code(poly.team1)
    poly_team2 = normalize_team_code(poly.team2)
    poly_yes_side = normalize_team_code(poly.side) if poly.side else poly_team1

    if kalshi.poly_sport != poly.sport:
        return SportsPairSafety(
            known=True,
            safe=False,
            reason="sport_mismatch",
            kalshi_side=kalshi_side,
            polymarket_yes_side=poly_yes_side,
        )

    if kalshi.date != poly.date:
        return SportsPairSafety(
            known=True,
            safe=False,
            reason="date_mismatch",
            kalshi_side=kalshi_side,
            polymarket_yes_side=poly_yes_side,
        )

    if {kalshi.side.lower(), (poly.side or "").lower()} == {"mtl", "mim"}:
        return SportsPairSafety(
            known=True,
            safe=False,
            reason="mtl_mim_mismatch",
            kalshi_side=kalshi_side,
            polymarket_yes_side=poly_yes_side,
        )

    if kalshi_side == poly_yes_side:
        return SportsPairSafety(
            known=True,
            safe=True,
            reason="same_polarity",
            polarity="same",
            kalshi_side=kalshi_side,
            polymarket_yes_side=poly_yes_side,
        )

    if not poly.side and kalshi_side == poly_team2:
        return SportsPairSafety(
            known=True,
            safe=False,
            reason="flipped_polarity",
            polarity="flipped",
            kalshi_side=kalshi_side,
            polymarket_yes_side=poly_yes_side,
        )

    return SportsPairSafety(
        known=True,
        safe=False,
        reason="polarity_unconfirmed",
        kalshi_side=kalshi_side,
        polymarket_yes_side=poly_yes_side,
    )
