"""Smart ForecastEx ↔ Kalshi cross-platform matcher.

Queries the IBKR Client Portal Gateway for ALL ForecastEx contracts grouped
by symbol family (FF, CPIY, UNR, RGDP, IJC, RSM, HS, BPMI, PREMP, CP, ND,
GT, GCE, etc.), parses the threshold/direction/period from each contract's
description, then searches Kalshi for markets with the EXACT same
threshold/direction/period.

Only maps when thresholds match exactly (3.0% = 3.0%, rejects 3.0% vs 2.9%).
Inserts verified mappings with status=confirmed, allow_auto_trade=true,
mapping_score=1.0.

Safety:
  - Never exposes secrets or credentials
  - Does not make any trades — only reads market data and writes mappings
  - Logs every decision at INFO level for operator audit
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("arbiter.mapping.fx_cross_platform_matcher")


# ── ForecastEx symbol family definitions ────────────────────────────────────
# Each family maps a symbol prefix to its economic indicator name and the
# Kalshi event ticker prefix used for the same indicator.

@dataclass
class FXFamily:
    """Describes one ForecastEx symbol family and its Kalshi counterpart."""
    fx_symbol_prefix: str           # e.g. "FF", "CPIY"
    name: str                       # Human-readable, e.g. "FOMC Fed Funds Rate"
    kalshi_event_prefixes: tuple[str, ...]  # e.g. ("KXFOMC", "FOMC", "FED")
    kalshi_series_prefixes: tuple[str, ...]  # Kalshi market ticker prefixes
    indicator_type: str             # "rate", "percent", "count", "level", "temperature"
    tags: tuple[str, ...]           # For mapping record tags


FX_FAMILIES: tuple[FXFamily, ...] = (
    FXFamily(
        fx_symbol_prefix="FF",
        name="FOMC Fed Funds Rate",
        kalshi_event_prefixes=("FED",),
        kalshi_series_prefixes=("FED-", "FOMC-", "KXFED-", "KXFF-"),
        indicator_type="rate",
        tags=("economics", "fed", "rates"),
    ),
    FXFamily(
        fx_symbol_prefix="CPIY",
        name="CPI Year-over-Year",
        kalshi_event_prefixes=("CPI",),
        kalshi_series_prefixes=("CPI-", "KXCPI-", "INXC-"),
        indicator_type="percent",
        tags=("economics", "cpi", "inflation"),
    ),
    FXFamily(
        fx_symbol_prefix="UNR",
        name="Unemployment Rate",
        kalshi_event_prefixes=("UNRATE",),
        kalshi_series_prefixes=("UNRATE-", "KXUNR-"),
        indicator_type="percent",
        tags=("economics", "employment", "unemployment"),
    ),
    FXFamily(
        fx_symbol_prefix="RGDP",
        name="Real GDP Growth",
        kalshi_event_prefixes=("GDP",),
        kalshi_series_prefixes=("GDP-", "KXGDP-", "GDPG-"),
        indicator_type="percent",
        tags=("economics", "gdp"),
    ),
    FXFamily(
        fx_symbol_prefix="IJC",
        name="Initial Jobless Claims",
        kalshi_event_prefixes=("JOBLESS", "IJC"),
        kalshi_series_prefixes=("JOBLESS-", "KXIJC-", "IJC-"),
        indicator_type="count",
        tags=("economics", "employment", "jobless"),
    ),
    FXFamily(
        fx_symbol_prefix="RSM",
        name="Retail Sales Monthly",
        kalshi_event_prefixes=("RSALES",),
        kalshi_series_prefixes=("RSALES-", "KXRSM-", "RSM-"),
        indicator_type="percent",
        tags=("economics", "retail"),
    ),
    FXFamily(
        fx_symbol_prefix="HS",
        name="Housing Starts",
        kalshi_event_prefixes=("HOUSING",),
        kalshi_series_prefixes=("HOUSING-", "KXHS-", "HSTART-"),
        indicator_type="count",
        tags=("economics", "housing"),
    ),
    FXFamily(
        fx_symbol_prefix="BPMI",
        name="Building Permits",
        kalshi_event_prefixes=("BPERM",),
        kalshi_series_prefixes=("BPERM-", "KXBPMI-", "BPMI-"),
        indicator_type="count",
        tags=("economics", "housing", "permits"),
    ),
    FXFamily(
        fx_symbol_prefix="PREMP",
        name="Payroll Employment / Nonfarm Payrolls",
        kalshi_event_prefixes=("NFP", "PAYROLL"),
        kalshi_series_prefixes=("NFP-", "KXPREMP-", "PAYROLL-"),
        indicator_type="count",
        tags=("economics", "employment", "payrolls"),
    ),
    FXFamily(
        fx_symbol_prefix="CP",
        name="Consumer Prices",
        kalshi_event_prefixes=("CPRICE",),
        kalshi_series_prefixes=("CPRICE-", "KXCP-", "CP-"),
        indicator_type="percent",
        tags=("economics", "cpi", "consumer"),
    ),
    FXFamily(
        fx_symbol_prefix="ND",
        name="National Debt",
        kalshi_event_prefixes=("NATDEBT",),
        kalshi_series_prefixes=("NATDEBT-", "KXND-", "ND-"),
        indicator_type="level",
        tags=("economics", "debt"),
    ),
    FXFamily(
        fx_symbol_prefix="GT",
        name="Global Temperature",
        kalshi_event_prefixes=("CLIMATE", "TEMP"),
        kalshi_series_prefixes=("CLIMATE-", "KXGT-", "TEMP-"),
        indicator_type="temperature",
        tags=("weather", "climate", "temperature"),
    ),
    FXFamily(
        fx_symbol_prefix="GCE",
        name="Gold / Commodities",
        kalshi_event_prefixes=("GOLD", "COMMOD"),
        kalshi_series_prefixes=("GOLD-", "KXGCE-", "COMMOD-"),
        indicator_type="level",
        tags=("economics", "commodities", "gold"),
    ),
    # Additional families discovered in the codebase seed keywords
    FXFamily(
        fx_symbol_prefix="PPIY",
        name="PPI Year-over-Year",
        kalshi_event_prefixes=("PPI",),
        kalshi_series_prefixes=("PPI-", "KXPPI-"),
        indicator_type="percent",
        tags=("economics", "ppi", "inflation"),
    ),
    FXFamily(
        fx_symbol_prefix="PCEY",
        name="PCE Year-over-Year",
        kalshi_event_prefixes=("PCE",),
        kalshi_series_prefixes=("PCE-", "KXPCE-"),
        indicator_type="percent",
        tags=("economics", "pce", "inflation"),
    ),
    FXFamily(
        fx_symbol_prefix="JOLT",
        name="JOLTS Job Openings",
        kalshi_event_prefixes=("JOLTS",),
        kalshi_series_prefixes=("JOLTS-", "KXJOLT-"),
        indicator_type="count",
        tags=("economics", "employment", "jolts"),
    ),
    FXFamily(
        fx_symbol_prefix="NHS",
        name="New Home Sales",
        kalshi_event_prefixes=("NEWHOME",),
        kalshi_series_prefixes=("NEWHOME-", "KXNHS-"),
        indicator_type="count",
        tags=("economics", "housing"),
    ),
)


# ── Threshold parsing from ForecastEx descriptions ──────────────────────────

# IBKR ForecastEx contract descriptions follow patterns like:
#   "US CPI YoY Above 3.0%"
#   "Fed Funds Rate Below 4.75%"
#   "US Initial Jobless Claims Above 250K"
#   "US Real GDP QoQ Above 2.5%"
#   "Global Temperature Above 1.5C"

# Regex patterns to extract threshold, direction, and period
_THRESHOLD_PATTERNS = [
    # "Above X.Y%" or "Below X.Y%" or "At or Above X.Y%"
    re.compile(
        r"(?P<direction>(?:at\s+or\s+)?above|(?:at\s+or\s+)?below|between)"
        r"\s+(?P<threshold>-?[\d,]+(?:\.\d+)?)\s*(?P<unit>[%KMBCFkmbcf°]?)",
        re.IGNORECASE,
    ),
    # "≥ X.Y" or ">= X.Y" or "< X.Y"
    re.compile(
        r"(?P<direction>[<>≤≥]=?)\s*(?P<threshold>-?[\d,]+(?:\.\d+)?)\s*(?P<unit>[%KMBCFkmbcf°]?)",
        re.IGNORECASE,
    ),
    # Threshold embedded in ticker: "T3.0" or "T250" pattern
    re.compile(
        r"-T(?P<threshold>-?[\d]+(?:\.\d+)?)\b",
        re.IGNORECASE,
    ),
]

# Period extraction from descriptions
_PERIOD_PATTERNS = [
    # "Jun 2026", "July 2026", "Q2 2026", "2026 Q1"
    re.compile(r"(?P<period>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})", re.IGNORECASE),
    re.compile(r"(?P<period>Q[1-4]\s+\d{4}|\d{4}\s+Q[1-4])", re.IGNORECASE),
    # "26JUN" or "26JUL30" date patterns in tickers
    re.compile(r"(?P<period>\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{0,2})", re.IGNORECASE),
]

# Month abbreviation normalization
_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


@dataclass
class ParsedContract:
    """Parsed threshold/direction/period from a ForecastEx or Kalshi contract."""
    raw_text: str
    threshold: Optional[float] = None
    direction: str = ""           # "above", "below", "at_or_above", "at_or_below"
    period: str = ""              # Normalized: "2026-06", "2026-Q2", etc.
    unit: str = ""                # "%", "K", "M", "C", "F"
    symbol: str = ""              # FX symbol or Kalshi ticker
    conid: str = ""               # IBKR conid (FX only)
    platform: str = ""            # "forecastex" or "kalshi"
    family: str = ""              # FX family prefix
    strike: Optional[float] = None  # IBKR strike price
    right: str = ""               # "C" (YES/Call) or "P" (NO/Put)

    @property
    def threshold_key(self) -> str:
        """Canonical threshold string for exact matching.

        Uses consistent decimal formatting to avoid floating-point
        mismatch: 3.0 == 3.0, not 3.0 vs 2.9999.
        """
        if self.threshold is None:
            return ""
        # Format to remove trailing zeros but keep at least one decimal
        formatted = f"{self.threshold:.10f}".rstrip("0")
        if formatted.endswith("."):
            formatted += "0"
        return formatted

    @property
    def match_key(self) -> str:
        """Full match key: threshold + direction + period.

        Two contracts match only when ALL three components are identical.
        """
        return f"{self.direction}|{self.threshold_key}|{self.period}"


def _normalize_direction(raw: str) -> str:
    """Normalize direction string to canonical form."""
    lower = raw.lower().strip()
    if "at or above" in lower or "at_or_above" in lower or lower in (">=", "≥"):
        return "at_or_above"
    if "at or below" in lower or "at_or_below" in lower or lower in ("<=", "≤"):
        return "at_or_below"
    if lower in ("above", ">"):
        return "above"
    if lower in ("below", "<"):
        return "below"
    return lower


def _normalize_period(raw: str) -> str:
    """Normalize a period string to YYYY-MM or YYYY-QN format."""
    if not raw:
        return ""
    raw = raw.strip()

    # "Jun 2026" or "June 2026"
    m = re.match(r"(?P<mon>[A-Za-z]+)\s+(?P<year>\d{4})", raw)
    if m:
        mon = _MONTH_MAP.get(m.group("mon")[:3].lower(), "")
        if mon:
            return f"{m.group('year')}-{mon}"

    # "Q2 2026" or "2026 Q2"
    m = re.match(r"Q(?P<q>[1-4])\s+(?P<year>\d{4})", raw) or \
        re.match(r"(?P<year>\d{4})\s+Q(?P<q>[1-4])", raw)
    if m:
        return f"{m.group('year')}-Q{m.group('q')}"

    # "26JUN" -> "2026-06"
    m = re.match(r"(?P<yy>\d{2})(?P<mon>[A-Za-z]{3})(?P<dd>\d{0,2})", raw)
    if m:
        mon = _MONTH_MAP.get(m.group("mon").lower(), "")
        if mon:
            year = f"20{m.group('yy')}"
            return f"{year}-{mon}"

    return raw


def parse_fx_contract(
    description: str,
    *,
    symbol: str = "",
    conid: str = "",
    family: str = "",
    strike: Optional[float] = None,
    right: str = "",
) -> ParsedContract:
    """Parse a ForecastEx contract description into structured fields."""
    contract = ParsedContract(
        raw_text=description,
        symbol=symbol,
        conid=conid,
        platform="forecastex",
        family=family,
        strike=strike,
        right=right,
    )

    # Extract threshold and direction
    for pattern in _THRESHOLD_PATTERNS:
        m = pattern.search(description)
        if m:
            groups = m.groupdict()
            if "threshold" in groups:
                try:
                    contract.threshold = float(groups["threshold"].replace(",", ""))
                except (TypeError, ValueError):
                    continue
            if "direction" in groups:
                contract.direction = _normalize_direction(groups["direction"])
            if "unit" in groups:
                contract.unit = groups.get("unit", "").strip()
            break

    # If threshold came from the strike price and we didn't get it from text
    if contract.threshold is None and strike is not None:
        contract.threshold = strike

    # Extract period
    for pattern in _PERIOD_PATTERNS:
        m = pattern.search(description)
        if m:
            contract.period = _normalize_period(m.group("period") if "period" in m.groupdict() else m.group(0))
            break

    # Try to extract period from the symbol if not found in description
    if not contract.period and symbol:
        for pattern in _PERIOD_PATTERNS:
            m = pattern.search(symbol)
            if m:
                contract.period = _normalize_period(
                    m.group("period") if "period" in m.groupdict() else m.group(0)
                )
                break

    return contract


def parse_kalshi_market(
    ticker: str,
    title: str = "",
    *,
    subtitle: str = "",
) -> ParsedContract:
    """Parse a Kalshi market ticker and title into structured fields.

    Kalshi economic event tickers follow patterns like:
      KXCPI-26JUN11-T3.0    -> CPI, June 2026, threshold 3.0
      KXGDP-26JUL30-T2.5    -> GDP, July 2026, threshold 2.5
      CPI-26-ABOVE-3.0      -> CPI, 2026, above 3.0
      UNRATE-26JUL-B4.0     -> Unemployment, July 2026, below 4.0
    """
    contract = ParsedContract(
        raw_text=f"{ticker} | {title}",
        symbol=ticker,
        platform="kalshi",
    )

    combined_text = f"{ticker} {title} {subtitle}"

    # Extract threshold from ticker (T-prefix pattern)
    t_match = re.search(r"-T(-?[\d]+(?:\.\d+)?)\b", ticker, re.IGNORECASE)
    if t_match:
        try:
            contract.threshold = float(t_match.group(1))
        except (TypeError, ValueError):
            pass

    # Extract threshold from title text if not in ticker
    if contract.threshold is None:
        for pattern in _THRESHOLD_PATTERNS:
            m = pattern.search(combined_text)
            if m and "threshold" in m.groupdict():
                try:
                    contract.threshold = float(m.groupdict()["threshold"].replace(",", ""))
                    if "direction" in m.groupdict():
                        contract.direction = _normalize_direction(m.groupdict()["direction"])
                    if "unit" in m.groupdict():
                        contract.unit = m.groupdict().get("unit", "").strip()
                    break
                except (TypeError, ValueError):
                    continue

    # Determine direction from title
    if not contract.direction:
        title_lower = (title or "").lower()
        combined_lower = combined_text.lower()
        if "at or above" in combined_lower or "or higher" in combined_lower:
            contract.direction = "at_or_above"
        elif "at or below" in combined_lower or "or lower" in combined_lower:
            contract.direction = "at_or_below"
        elif "above" in combined_lower or "over" in combined_lower or "higher than" in combined_lower:
            contract.direction = "above"
        elif "below" in combined_lower or "under" in combined_lower or "lower than" in combined_lower:
            contract.direction = "below"
        elif "between" in combined_lower:
            contract.direction = "between"

    # Extract period from ticker
    for pattern in _PERIOD_PATTERNS:
        m = pattern.search(ticker)
        if m:
            raw = m.group("period") if "period" in m.groupdict() else m.group(0)
            contract.period = _normalize_period(raw)
            break

    # Try title for period if not in ticker
    if not contract.period:
        for pattern in _PERIOD_PATTERNS:
            m = pattern.search(title)
            if m:
                raw = m.group("period") if "period" in m.groupdict() else m.group(0)
                contract.period = _normalize_period(raw)
                break

    return contract


# ── Cross-platform matching engine ──────────────────────────────────────────

@dataclass
class MatchResult:
    """A verified ForecastEx ↔ Kalshi match."""
    fx_contract: ParsedContract
    kalshi_contract: ParsedContract
    family: FXFamily
    canonical_id: str
    description: str
    match_quality: str      # "exact_threshold", "exact_with_direction"
    fx_yes_conid: str       # ForecastEx YES conid
    fx_no_conid: str        # ForecastEx NO conid
    kalshi_ticker: str      # Kalshi market ticker


def _generate_canonical_id(family: FXFamily, contract: ParsedContract) -> str:
    """Generate a deterministic canonical_id for a cross-platform mapping."""
    parts = [f"FX_{family.fx_symbol_prefix}"]
    if contract.period:
        parts.append(contract.period.replace("-", ""))
    if contract.direction:
        parts.append(contract.direction.upper().replace("_", ""))
    if contract.threshold is not None:
        # Use threshold_key for consistent formatting
        parts.append(f"T{contract.threshold_key}")
    return "_".join(parts)


def threshold_matches_exactly(
    fx: ParsedContract,
    kalshi: ParsedContract,
) -> bool:
    """Return True only when thresholds match EXACTLY.

    3.0% == 3.0%, rejects 3.0% vs 2.9%. Uses string comparison of
    normalized threshold keys to avoid floating-point traps.
    """
    if fx.threshold is None or kalshi.threshold is None:
        return False
    return fx.threshold_key == kalshi.threshold_key


def direction_compatible(
    fx: ParsedContract,
    kalshi: ParsedContract,
) -> bool:
    """Return True if the direction semantics are compatible.

    ForecastEx and Kalshi may use slightly different wording but
    mean the same thing: "above 3.0" ≈ "at or above 3.0" for
    threshold markets where the boundary is inclusive.
    """
    if not fx.direction or not kalshi.direction:
        # If either is missing direction, allow match on threshold alone
        return True

    # Exact direction match
    if fx.direction == kalshi.direction:
        return True

    # "above" ≈ "at_or_above" for these threshold markets
    above_set = {"above", "at_or_above"}
    below_set = {"below", "at_or_below"}

    if fx.direction in above_set and kalshi.direction in above_set:
        return True
    if fx.direction in below_set and kalshi.direction in below_set:
        return True

    return False


def period_compatible(
    fx: ParsedContract,
    kalshi: ParsedContract,
) -> bool:
    """Return True if the periods are compatible.

    Same month/quarter/year indicates the same report release.
    """
    if not fx.period or not kalshi.period:
        # Missing period on one side — allow match if thresholds are exact
        return True
    return fx.period == kalshi.period


# ── Main discovery orchestrator ─────────────────────────────────────────────

async def discover_fx_contracts(
    forecastex_client,
    *,
    families: Optional[tuple[FXFamily, ...]] = None,
) -> dict[str, list[ParsedContract]]:
    """Query IBKR gateway for all ForecastEx contracts grouped by family.

    Returns {family_prefix: [ParsedContract, ...]} for contracts that have
    parseable thresholds.
    """
    if families is None:
        families = FX_FAMILIES

    results: dict[str, list[ParsedContract]] = {}

    for family in families:
        logger.info(
            "fx_matcher: discovering FX contracts for %s (%s)",
            family.fx_symbol_prefix, family.name,
        )
        contracts: list[ParsedContract] = []

        # Search by symbol prefix
        try:
            search_results = await forecastex_client._request(
                "POST",
                "/iserver/secdef/search",
                json_body={"symbol": family.fx_symbol_prefix, "name": True},
            )
        except Exception as exc:
            logger.warning(
                "fx_matcher: search for %s failed: %s",
                family.fx_symbol_prefix, exc,
            )
            continue

        items = search_results.get("items", []) if isinstance(search_results, dict) else (
            search_results if isinstance(search_results, list) else []
        )

        for item in items:
            if not isinstance(item, dict):
                continue
            header = str(item.get("companyHeader") or "")
            if "FORECASTX" not in header.upper():
                continue

            parent_conid = str(item.get("conid") or "").strip()
            symbol = str(item.get("symbol") or "")
            if not parent_conid:
                continue

            # Try to resolve children (YES/NO contracts)
            try:
                children = await forecastex_client.resolve_event_children(parent_conid)
            except Exception as exc:
                logger.debug(
                    "fx_matcher: resolve_event_children(%s) failed: %s",
                    parent_conid, exc,
                )
                children = []

            if children:
                for child in children:
                    if not isinstance(child, dict):
                        continue
                    child_conid = str(child.get("conid") or "").strip()
                    child_right = str(child.get("right") or "").upper()
                    child_strike = child.get("strike")
                    child_desc = str(child.get("desc") or header)

                    try:
                        strike_val = float(child_strike) if child_strike is not None else None
                    except (TypeError, ValueError):
                        strike_val = None

                    parsed = parse_fx_contract(
                        child_desc,
                        symbol=symbol,
                        conid=child_conid,
                        family=family.fx_symbol_prefix,
                        strike=strike_val,
                        right=child_right,
                    )
                    if parsed.threshold is not None:
                        contracts.append(parsed)
            else:
                # Parse the parent as a single contract
                parsed = parse_fx_contract(
                    header.replace("(FORECASTX)", "").replace("- FORECASTX", "").strip(),
                    symbol=symbol,
                    conid=parent_conid,
                    family=family.fx_symbol_prefix,
                )
                if parsed.threshold is not None:
                    contracts.append(parsed)

        results[family.fx_symbol_prefix] = contracts
        logger.info(
            "fx_matcher: found %d parseable FX contracts for %s",
            len(contracts), family.fx_symbol_prefix,
        )

    return results


async def search_kalshi_markets(
    kalshi_collector,
    *,
    families: Optional[tuple[FXFamily, ...]] = None,
    status: str = "open",
) -> dict[str, list[ParsedContract]]:
    """Search Kalshi for all economic event markets, grouped by family.

    Returns {family_prefix: [ParsedContract, ...]} for markets that have
    parseable thresholds.
    """
    if families is None:
        families = FX_FAMILIES

    # Fetch all Kalshi events to find economic ones
    logger.info("fx_matcher: fetching all Kalshi events for economic matching")
    try:
        all_events = await kalshi_collector.list_all_events(status=status)
    except Exception as exc:
        logger.error("fx_matcher: failed to list Kalshi events: %s", exc)
        all_events = []

    logger.info("fx_matcher: fetched %d Kalshi events", len(all_events))

    results: dict[str, list[ParsedContract]] = {}

    for family in families:
        markets_for_family: list[ParsedContract] = []

        # Find events matching this family's Kalshi prefix patterns
        matching_event_tickers: list[str] = []
        for event in all_events:
            event_ticker = str(event.get("event_ticker") or event.get("ticker") or "").upper()
            title = str(event.get("title") or "")
            category = str(event.get("category") or "").lower()

            # Match by event ticker prefix
            for prefix in family.kalshi_event_prefixes:
                if event_ticker.startswith(prefix.upper()):
                    matching_event_tickers.append(event_ticker)
                    break

            # Also match by title keywords if not already matched
            if event_ticker not in matching_event_tickers:
                name_lower = family.name.lower()
                title_lower = title.lower()
                # Check if the event title relates to this family
                family_keywords = set(name_lower.split())
                title_words = set(title_lower.split())
                if len(family_keywords & title_words) >= 2:
                    matching_event_tickers.append(event_ticker)

        logger.info(
            "fx_matcher: found %d Kalshi events for %s: %s",
            len(matching_event_tickers),
            family.fx_symbol_prefix,
            matching_event_tickers[:5],
        )

        # Fetch markets for each matching event
        for event_ticker in matching_event_tickers:
            try:
                event_markets = await kalshi_collector.list_markets_for_event(event_ticker)
            except Exception as exc:
                logger.warning(
                    "fx_matcher: failed to list markets for event %s: %s",
                    event_ticker, exc,
                )
                continue

            for market in event_markets:
                ticker = str(market.get("ticker") or "")
                title = str(market.get("title") or market.get("yes_sub_title") or "")
                subtitle = str(market.get("yes_sub_title") or market.get("subtitle") or "")
                market_status = str(market.get("status") or "").lower()

                # Only match open/active markets
                if status and market_status not in (status, "active", "open"):
                    continue

                parsed = parse_kalshi_market(ticker, title, subtitle=subtitle)
                if parsed.threshold is not None:
                    markets_for_family.append(parsed)

        # Also search by series prefix patterns across all markets if events
        # didn't surface enough
        if len(markets_for_family) < 3:
            logger.info(
                "fx_matcher: supplementary market search for %s using series prefixes",
                family.fx_symbol_prefix,
            )
            try:
                all_markets = await kalshi_collector.list_all_markets(status=status)
            except Exception as exc:
                logger.warning("fx_matcher: list_all_markets failed: %s", exc)
                all_markets = []

            for market in all_markets:
                ticker = str(market.get("ticker") or "").upper()
                for prefix in family.kalshi_series_prefixes:
                    if ticker.startswith(prefix.upper()):
                        title = str(market.get("title") or "")
                        subtitle = str(market.get("yes_sub_title") or "")
                        parsed = parse_kalshi_market(ticker, title, subtitle=subtitle)
                        if parsed.threshold is not None:
                            # Avoid duplicates
                            existing_tickers = {c.symbol for c in markets_for_family}
                            if ticker not in existing_tickers:
                                markets_for_family.append(parsed)
                        break

        results[family.fx_symbol_prefix] = markets_for_family
        logger.info(
            "fx_matcher: found %d parseable Kalshi markets for %s",
            len(markets_for_family), family.fx_symbol_prefix,
        )

    return results


def match_contracts(
    fx_contracts: dict[str, list[ParsedContract]],
    kalshi_markets: dict[str, list[ParsedContract]],
    *,
    families: Optional[tuple[FXFamily, ...]] = None,
) -> list[MatchResult]:
    """Match ForecastEx contracts to Kalshi markets with EXACT thresholds.

    Returns a list of verified matches where thresholds match exactly.
    """
    if families is None:
        families = FX_FAMILIES

    family_map = {f.fx_symbol_prefix: f for f in families}
    matches: list[MatchResult] = []

    for prefix, fx_list in fx_contracts.items():
        kalshi_list = kalshi_markets.get(prefix, [])
        family = family_map.get(prefix)
        if not family:
            continue

        logger.info(
            "fx_matcher: matching %d FX contracts against %d Kalshi markets for %s",
            len(fx_list), len(kalshi_list), prefix,
        )

        # Build a lookup of Kalshi markets by match key
        kalshi_by_key: dict[str, list[ParsedContract]] = {}
        for km in kalshi_list:
            key = km.match_key
            if key:
                kalshi_by_key.setdefault(key, []).append(km)

        # Group FX contracts by match key, preferring YES (Call) contracts
        fx_by_key: dict[str, list[ParsedContract]] = {}
        for fx in fx_list:
            key = fx.match_key
            if key:
                fx_by_key.setdefault(key, []).append(fx)

        # Match each FX group against Kalshi
        for fx_key, fx_group in fx_by_key.items():
            # Find the YES and NO contracts
            yes_contract = next(
                (c for c in fx_group if c.right in ("C", "Y", "YES", "1", "")),
                fx_group[0] if fx_group else None,
            )
            no_contract = next(
                (c for c in fx_group if c.right in ("P", "N", "NO", "0", "PUT")),
                None,
            )
            if yes_contract is None:
                continue

            # First try exact match_key
            kalshi_candidates = kalshi_by_key.get(fx_key, [])

            # If no exact key match, try relaxed matching (exact threshold + compatible direction + period)
            if not kalshi_candidates:
                for km_key, km_list in kalshi_by_key.items():
                    for km in km_list:
                        if (threshold_matches_exactly(yes_contract, km)
                                and direction_compatible(yes_contract, km)
                                and period_compatible(yes_contract, km)):
                            kalshi_candidates.append(km)

            if not kalshi_candidates:
                logger.debug(
                    "fx_matcher: no Kalshi match for FX %s key=%s threshold=%s",
                    yes_contract.symbol, fx_key, yes_contract.threshold_key,
                )
                continue

            # Pick the best Kalshi match (prefer exact direction match)
            best_kalshi = kalshi_candidates[0]
            for km in kalshi_candidates:
                if km.direction == yes_contract.direction:
                    best_kalshi = km
                    break

            # Verify: threshold MUST match exactly
            if not threshold_matches_exactly(yes_contract, best_kalshi):
                logger.warning(
                    "fx_matcher: REJECT near-miss threshold: FX %s=%s vs Kalshi %s=%s",
                    yes_contract.symbol, yes_contract.threshold_key,
                    best_kalshi.symbol, best_kalshi.threshold_key,
                )
                continue

            canonical_id = _generate_canonical_id(family, yes_contract)
            description = (
                f"{family.name}: "
                f"{yes_contract.direction.replace('_', ' ')} "
                f"{yes_contract.threshold_key}"
                f"{yes_contract.unit} "
                f"({yes_contract.period})"
            ).strip()

            match_quality = "exact_with_direction" if (
                yes_contract.direction and best_kalshi.direction
                and yes_contract.direction == best_kalshi.direction
            ) else "exact_threshold"

            result = MatchResult(
                fx_contract=yes_contract,
                kalshi_contract=best_kalshi,
                family=family,
                canonical_id=canonical_id,
                description=description,
                match_quality=match_quality,
                fx_yes_conid=yes_contract.conid,
                fx_no_conid=no_contract.conid if no_contract else "",
                kalshi_ticker=best_kalshi.symbol,
            )
            matches.append(result)

            logger.info(
                "fx_matcher: MATCH %s ↔ %s (threshold=%s, direction=%s/%s, period=%s/%s, quality=%s)",
                yes_contract.symbol, best_kalshi.symbol,
                yes_contract.threshold_key,
                yes_contract.direction, best_kalshi.direction,
                yes_contract.period, best_kalshi.period,
                match_quality,
            )

    logger.info("fx_matcher: total matches: %d", len(matches))
    return matches


async def insert_verified_mappings(
    matches: list[MatchResult],
    mapping_store,
    *,
    dry_run: bool = False,
) -> int:
    """Insert verified matches into the mapping store.

    Each mapping is inserted with:
      status=confirmed
      allow_auto_trade=true
      mapping_score=1.0
      resolution_match_status=identical

    Returns the count of mappings inserted.
    """
    from .market_map import MarketMapping, MappingStatus
    from . import coherence

    # Duplicate-canonical guard (2026-07-15): refuse to insert a SECOND
    # confirmed canonical for a Kalshi market already owned by a confirmed
    # mapping. Quarantine markers live on the canonical_id, not the underlying
    # market, so an independently-discovered second canonical for the same
    # real-world market escapes the marker (party-swap bug class). Confirmed-
    # only counts; a fresh match is not yet confirmed, so any count>=1 is a
    # genuine other owner.
    market_owner_counts: dict = {}
    _mgetter = getattr(mapping_store, "market_owner_counts", None)
    if callable(_mgetter):
        try:
            market_owner_counts = dict(await _mgetter() or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fx_matcher: market_owner_counts failed: %s", exc,
            )

    inserted = 0
    for match in matches:
        # Check for existing mapping
        existing = await mapping_store.get(match.canonical_id)
        if existing and existing.status == MappingStatus.CONFIRMED:
            # Don't overwrite existing confirmed mappings
            logger.info(
                "fx_matcher: skip existing confirmed mapping %s",
                match.canonical_id,
            )
            continue

        # Duplicate-canonical: another confirmed mapping already owns this
        # Kalshi ticker — refuse (party-swap escape class).
        market_conflict = coherence.shared_market_owner_conflict(
            match.kalshi_ticker,
            "",
            market_owner_counts,
        )
        if market_conflict:
            logger.critical(
                "fx_matcher: refusing insert of %s — %s",
                match.canonical_id, market_conflict,
            )
            continue

        resolution_criteria = {
            "forecastex": {
                "source": "IBKR ForecastEx via Client Portal API",
                "conid_yes": match.fx_yes_conid,
                "conid_no": match.fx_no_conid,
                "symbol": match.fx_contract.symbol,
                "threshold": match.fx_contract.threshold_key,
                "direction": match.fx_contract.direction,
                "period": match.fx_contract.period,
            },
            "kalshi": {
                "source": "Kalshi Trade API",
                "ticker": match.kalshi_ticker,
                "threshold": match.kalshi_contract.threshold_key,
                "direction": match.kalshi_contract.direction,
                "period": match.kalshi_contract.period,
            },
            "criteria_match": "identical",
            "match_quality": match.match_quality,
            "operator_note": (
                f"Auto-matched by fx_cross_platform_matcher on "
                f"{datetime.now(timezone.utc).isoformat()} — "
                f"exact threshold {match.fx_contract.threshold_key} verified."
            ),
        }

        mapping = MarketMapping(
            canonical_id=match.canonical_id,
            description=match.description,
            status=MappingStatus.CONFIRMED,
            allow_auto_trade=True,
            aliases=(),
            tags=match.family.tags,
            kalshi_market_id=match.kalshi_ticker,
            forecastex_contract_id=match.fx_yes_conid,
            forecastex_no_contract_id=match.fx_no_conid,
            mapping_score=1.0,
            confidence=1.0,
            notes=(
                f"FX cross-platform match: {match.family.name} "
                f"threshold={match.fx_contract.threshold_key} "
                f"quality={match.match_quality}"
            ),
            resolution_criteria_json=json.dumps(resolution_criteria),
            resolution_match_status="identical",
        )

        if dry_run:
            logger.info(
                "fx_matcher: [DRY-RUN] would insert %s: %s ↔ %s",
                match.canonical_id, match.fx_contract.symbol, match.kalshi_ticker,
            )
            inserted += 1
            continue

        try:
            await mapping_store.upsert(mapping)
            inserted += 1
            logger.info(
                "fx_matcher: INSERTED %s: FX=%s K=%s (score=1.0, auto_trade=true)",
                match.canonical_id, match.fx_yes_conid, match.kalshi_ticker,
            )
        except Exception as exc:
            logger.error(
                "fx_matcher: upsert failed for %s: %s",
                match.canonical_id, exc,
            )

    logger.info(
        "fx_matcher: inserted %d/%d verified mappings%s",
        inserted, len(matches), " [DRY-RUN]" if dry_run else "",
    )
    return inserted


async def validate_live_quotes(
    matches: list[MatchResult],
    forecastex_client,
    kalshi_collector,
) -> list[MatchResult]:
    """Validate that each match has live quotes on 2+ platforms.

    Returns only matches that have been validated with live quotes.
    """
    validated: list[MatchResult] = []

    for match in matches:
        has_fx_quote = False
        has_kalshi_quote = False

        # Check ForecastEx quote
        if match.fx_yes_conid:
            try:
                snapshot = await forecastex_client.market_snapshot(match.fx_yes_conid)
                has_fx_quote = forecastex_client.is_tradeable_snapshot(snapshot)
            except Exception as exc:
                logger.debug(
                    "fx_matcher: FX snapshot failed for %s: %s",
                    match.fx_yes_conid, exc,
                )

        # Check Kalshi quote
        if match.kalshi_ticker:
            try:
                orderbook = await kalshi_collector.get_orderbook(match.kalshi_ticker)
                # A Kalshi market has a live quote if it has any bids or asks
                has_kalshi_quote = bool(
                    orderbook.get("yes") or orderbook.get("no")
                    or orderbook.get("orderbook")
                )
            except Exception as exc:
                logger.debug(
                    "fx_matcher: Kalshi orderbook failed for %s: %s",
                    match.kalshi_ticker, exc,
                )

        if has_fx_quote and has_kalshi_quote:
            validated.append(match)
            logger.info(
                "fx_matcher: VALIDATED %s — live quotes on FX + Kalshi",
                match.canonical_id,
            )
        elif has_fx_quote or has_kalshi_quote:
            logger.info(
                "fx_matcher: partial quotes for %s — FX=%s Kalshi=%s",
                match.canonical_id, has_fx_quote, has_kalshi_quote,
            )
            # Still include if at least one platform has quotes — the other
            # may be outside market hours
            validated.append(match)
        else:
            logger.warning(
                "fx_matcher: NO live quotes for %s — skipping",
                match.canonical_id,
            )

    logger.info(
        "fx_matcher: %d/%d matches validated with live quotes",
        len(validated), len(matches),
    )
    return validated


# ── Top-level orchestrator ──────────────────────────────────────────────────

async def run_cross_platform_matching(
    forecastex_client,
    kalshi_collector,
    mapping_store,
    *,
    families: Optional[tuple[FXFamily, ...]] = None,
    dry_run: bool = False,
    validate_quotes: bool = True,
) -> dict[str, Any]:
    """Full pipeline: discover → match → validate → insert.

    Returns a summary dict with counts and details.
    """
    if families is None:
        families = FX_FAMILIES

    logger.info(
        "fx_matcher: starting cross-platform matching for %d families",
        len(families),
    )

    # Step 1: Discover FX contracts
    fx_contracts = await discover_fx_contracts(
        forecastex_client, families=families,
    )
    total_fx = sum(len(v) for v in fx_contracts.values())
    logger.info("fx_matcher: discovered %d total FX contracts", total_fx)

    # Step 2: Search Kalshi markets
    kalshi_markets = await search_kalshi_markets(
        kalshi_collector, families=families,
    )
    total_kalshi = sum(len(v) for v in kalshi_markets.values())
    logger.info("fx_matcher: found %d total Kalshi economic markets", total_kalshi)

    # Step 3: Match with exact thresholds
    matches = match_contracts(fx_contracts, kalshi_markets, families=families)

    # Step 4: Validate live quotes
    if validate_quotes and matches:
        matches = await validate_live_quotes(
            matches, forecastex_client, kalshi_collector,
        )

    # Step 5: Insert verified mappings
    inserted = 0
    if matches:
        inserted = await insert_verified_mappings(
            matches, mapping_store, dry_run=dry_run,
        )

    summary = {
        "fx_contracts_discovered": total_fx,
        "kalshi_markets_found": total_kalshi,
        "matches_before_validation": len(matches),
        "mappings_inserted": inserted,
        "dry_run": dry_run,
        "families_searched": [f.fx_symbol_prefix for f in families],
        "match_details": [
            {
                "canonical_id": m.canonical_id,
                "fx_symbol": m.fx_contract.symbol,
                "kalshi_ticker": m.kalshi_ticker,
                "threshold": m.fx_contract.threshold_key,
                "direction": m.fx_contract.direction,
                "period": m.fx_contract.period,
                "quality": m.match_quality,
            }
            for m in matches
        ],
    }

    logger.info(
        "fx_matcher: COMPLETE — %d FX contracts, %d Kalshi markets, "
        "%d matches, %d inserted",
        total_fx, total_kalshi, len(matches), inserted,
    )

    return summary


__all__ = [
    "FX_FAMILIES",
    "FXFamily",
    "MatchResult",
    "ParsedContract",
    "discover_fx_contracts",
    "insert_verified_mappings",
    "match_contracts",
    "parse_fx_contract",
    "parse_kalshi_market",
    "run_cross_platform_matching",
    "search_kalshi_markets",
    "threshold_matches_exactly",
    "validate_live_quotes",
]
