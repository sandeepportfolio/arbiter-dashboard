"""
Auto-discovery pipeline — pulls all live markets from both platforms,
scores candidate pairs, and writes them to the mapping store.

Rate-limited to budget_rps (default 2.0 r/s) during discovery via asyncio.sleep.
Returns the count of candidates written to the store.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any, Callable

from arbiter.config.settings import normalize_market_text, similarity_score
from arbiter.mapping.event_fingerprint import (
    fingerprint_kalshi_event,
    fingerprint_kalshi_market,
    fingerprint_polymarket_market,
    structural_match,
)
from arbiter.mapping.sports_safety import (
    SUPPORTED_POLY_WINNER_PREFIXES,
    evaluate_sports_pair,
    parse_kalshi_sports_ticker,
)

logger = logging.getLogger("arbiter.mapping.auto_discovery")
ProgressCallback = Callable[[dict[str, Any]], None]


def _emit_progress(
    progress: ProgressCallback | None,
    phase: str,
    message: str,
    *,
    counts: dict[str, Any] | None = None,
    rejection_reasons: dict[str, int] | None = None,
    **extra: Any,
) -> None:
    if progress is None:
        return
    event: dict[str, Any] = {
        "phase": phase,
        "message": message,
        "timestamp": time.time(),
    }
    if counts is not None:
        event["counts"] = counts
    if rejection_reasons is not None:
        event["rejection_reasons"] = rejection_reasons
    event.update(extra)
    try:
        progress(event)
    except Exception:
        logger.debug("auto_discovery: progress callback failed", exc_info=True)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default

_COMMON_TOKENS = {
    "a", "an", "and", "are", "be", "for", "if", "in", "is", "of", "on", "or",
    "the", "to", "vs", "will", "win", "winner", "yes", "no",
}

# ── Bracket-vs-binary mismatch guard ─────────────────────────────────
# Kalshi offers bracket/seat-count markets (e.g., KXDSENATESEATS-27-47
# = "Democrats win exactly 47 seats") which look textually similar to
# binary control markets (CONTROLS-2026-D = "Democrats win the Senate").
# These are fundamentally different contracts: a seat-count bracket
# resolves on a specific number, while a control market resolves on
# majority. Pairing them creates phantom arbitrage (e.g., $0.03 YES on
# 47 seats vs $0.47 NO on overall control) that would guarantee losses.
_KALSHI_BRACKET_TICKERS = re.compile(
    r"^KX[DR](?:SENATE|HOUSE)SEATS",
    re.IGNORECASE,
)
_POLY_BINARY_CONTROL_SLUGS = re.compile(
    r"(?:midterms|control|usse|usho)",
    re.IGNORECASE,
)


def _is_bracket_vs_binary_mismatch(kalshi_ticker: str, poly_slug: str) -> bool:
    """Return True if a Kalshi bracket/seat-count market is paired with a
    Polymarket binary control market (or vice versa). These are structurally
    incompatible and must never be paired."""
    if _KALSHI_BRACKET_TICKERS.search(kalshi_ticker) and _POLY_BINARY_CONTROL_SLUGS.search(poly_slug):
        return True
    return False
_DATE_RE = re.compile(r"(20\d{2})[-_/](\d{2})[-_/](\d{2})")
_AUTO_CATEGORY_LABELS = {
    "politics",
    "sports",
    "economics",
    "finance",
    "crypto",
    "geopolitics",
    "tech",
    "weather",
    "culture",
}
_CATEGORY_ALIASES = {
    "elections": "politics",
    "election": "politics",
    "world": "geopolitics",
    "international": "geopolitics",
    "sport": "sports",
}


def _market_tokens(text: str) -> set[str]:
    tokens = {
        token
        for token in normalize_market_text(text).split()
        if token and (len(token) >= 3 or token.isdigit()) and token not in _COMMON_TOKENS
    }
    return tokens


def _candidate_indexes_from_tokens(
    tokens: set[str],
    poly_index: dict[str, set[int]],
    *,
    max_postings: int = 100,
    max_total: int = 75,
) -> set[int]:
    """Return Polymarket indexes for selective tokens only.

    Full-catalog discovery contains very broad tokens such as years, leagues,
    and generic market nouns. Expanding those postings creates a near-cartesian
    fuzzy match over tens of thousands of markets. Structural fingerprints carry
    exact matches; fuzzy fallback should only use reasonably selective tokens.
    """
    candidate_indexes: set[int] = set()
    token_postings = []
    for token in tokens:
        if len(token) == 4 and token.isdigit() and token.startswith("20"):
            continue
        postings = poly_index.get(token, ())
        if len(postings) > max_postings:
            continue
        token_postings.append((len(postings), token, postings))

    for _, _, postings in sorted(token_postings):
        candidate_indexes.update(postings)
        if len(candidate_indexes) >= max_total:
            return set(sorted(candidate_indexes)[:max_total])
    return candidate_indexes


def _coerce_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    match = _DATE_RE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


_ACTIVE_KALSHI_STATUSES = {"active", "open", "trading", "initialized"}
_INACTIVE_KALSHI_STATUSES = {"closed", "settled", "expired", "finalized", "resolved"}
_INACTIVE_POLYMARKET_STATES = {
    "closed", "resolved", "settled", "suspended", "halted", "expired", "finalized",
}


def _is_active_kalshi_item(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or item.get("market_status") or "").strip().lower()
    if status in _INACTIVE_KALSHI_STATUSES:
        return False
    if status in _ACTIVE_KALSHI_STATUSES:
        return True
    close_date = _coerce_date(item.get("close_time") or item.get("expiration_time"))
    if close_date is not None and close_date < date.today():
        return False
    # Keep unknown statuses because some Kalshi event payloads omit status.
    return not status


def _is_active_polymarket_item(item: dict[str, Any]) -> bool:
    state = str(item.get("state") or item.get("status") or "").strip().lower()
    if state in _INACTIVE_POLYMARKET_STATES:
        return False
    for flag in ("closed", "archived", "resolved", "settled", "expired"):
        if item.get(flag) is True:
            return False
    if item.get("active") is False:
        return False
    return True


async def _call_with_supported_kwargs(func: Callable[..., Any], **kwargs: Any) -> Any:
    """Call an async collector method while tolerating older test doubles."""
    try:
        return await func(**kwargs)
    except TypeError as exc:
        unexpected_kw = "unexpected keyword" in str(exc) or "got an unexpected" in str(exc)
        if not kwargs or not unexpected_kw:
            raise
        return await func()


def _normalize_category(value: Any) -> str:
    text = normalize_market_text(str(value or ""))
    if not text:
        return ""
    for token in text.split():
        canonical = _CATEGORY_ALIASES.get(token, token)
        if canonical in _AUTO_CATEGORY_LABELS:
            return canonical
    first = text.split()[0]
    return _CATEGORY_ALIASES.get(first, first)


def _dedupe_text_parts(parts: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for part in parts:
        normalized = normalize_market_text(part)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(part)
    return output


def _kalshi_text(market: dict[str, Any]) -> str:
    parts = _dedupe_text_parts([
        str(market.get(field, "") or "").strip()
        for field in ("title", "subtitle", "yes_sub_title", "no_sub_title")
        if str(market.get(field, "") or "").strip()
    ])
    if not parts:
        ticker = str(market.get("ticker", "") or "").strip()
        if ticker:
            parts.append(ticker)
    return " ".join(parts)


def _kalshi_event_text(event: dict[str, Any]) -> str:
    return " ".join(
        str(event.get(field, "") or "").strip()
        for field in ("title", "sub_title", "event_ticker", "series_ticker", "category")
        if str(event.get(field, "") or "").strip()
    )


def _looks_like_multi_leg_kalshi_market(market: dict[str, Any]) -> bool:
    ticker = str(market.get("ticker", "") or "").upper()
    event_ticker = str(market.get("event_ticker", "") or "").upper()
    title = str(market.get("title", "") or "")
    normalized = normalize_market_text(title)
    if ticker.startswith("KXMVE") or event_ticker.startswith("KXMVE"):
        return True
    if title.count(",") >= 2 and (normalized.count("yes ") + normalized.count("no ")) >= 2:
        return True
    return False


def _poly_text(market: dict[str, Any]) -> str:
    parts = _dedupe_text_parts([
        str(market.get(field, "") or "").strip()
        for field in ("question", "title", "description")
        if str(market.get(field, "") or "").strip()
    ])

    subject = market.get("subject") or {}
    if isinstance(subject, dict):
        for field in ("name",):
            value = str(subject.get(field, "") or "").strip()
            if value:
                parts.append(value)

    for side in market.get("marketSides") or []:
        if not isinstance(side, dict):
            continue
        for field in ("description",):
            value = str(side.get(field, "") or "").strip()
            if value:
                parts.append(value)
        team = side.get("team") or {}
        if isinstance(team, dict):
            for field in ("name", "alias", "displayAbbreviation", "league"):
                value = str(team.get(field, "") or "").strip()
                if value:
                    parts.append(value)

    parts = _dedupe_text_parts(parts)
    if not parts:
        slug = str(market.get("slug", "") or "").strip()
        if slug:
            parts.append(slug)
    return " ".join(parts)


def _build_poly_entries(poly_markets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, set[int]]]:
    poly_entries: list[dict[str, Any]] = []
    poly_index: dict[str, set[int]] = defaultdict(set)

    for pm in poly_markets:
        p_text = _poly_text(pm)
        if not p_text:
            continue
        tokens = _market_tokens(p_text)
        entry = {
            "market": pm,
            "text": p_text,
            "tokens": tokens,
            "category": _normalize_category(pm.get("category") or pm.get("groupItemTitle")),
            "date": _coerce_date(pm.get("closeTime") or pm.get("endDate") or pm.get("slug")),
            "fingerprint": fingerprint_polymarket_market(pm),
        }
        poly_entries.append(entry)
        entry_index = len(poly_entries) - 1
        for token in tokens:
            poly_index[token].add(entry_index)

    return poly_entries, poly_index


def _poly_fingerprint_index(poly_entries: list[dict[str, Any]]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = defaultdict(set)
    for entry_index, entry in enumerate(poly_entries):
        fingerprint = entry.get("fingerprint")
        market_key = getattr(fingerprint, "market_key", "")
        if market_key:
            index[market_key].add(entry_index)
    return index


def _poly_event_fingerprint_index(poly_entries: list[dict[str, Any]]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = defaultdict(set)
    for entry_index, entry in enumerate(poly_entries):
        fingerprint = entry.get("fingerprint")
        event_key = getattr(fingerprint, "event_key", "")
        if event_key:
            index[event_key].add(entry_index)
    return index


def _candidate_payload(
    *,
    km: dict[str, Any],
    pm_entry: dict[str, Any],
    k_text: str,
    k_tokens: set[str],
    k_category: str,
    k_date: date | None,
    score: float,
) -> dict[str, Any]:
    pm = pm_entry["market"]
    poly_date = pm_entry["date"].isoformat() if pm_entry["date"] is not None else None
    kalshi_date = k_date.isoformat() if k_date is not None else None
    poly_source = pm.get("resolutionSource")
    kalshi_source = km.get("settlement_source")
    poly_tie_break = pm.get("tieBreakRule")
    kalshi_tie_break = km.get("rules_primary")
    poly_outcomes = tuple(pm.get("outcomes") or ("Yes", "No"))
    kalshi_outcomes = ("Yes", "No")
    return {
        "kalshi_ticker": km.get("ticker", ""),
        "kalshi_title": str(km.get("title", "") or k_text),
        "poly_slug": pm.get("slug", ""),
        "poly_question": str(pm.get("question", "") or pm.get("title", "") or pm_entry["text"]),
        "description": str(pm.get("description", "") or pm.get("question", "") or pm_entry["text"]),
        "score": score,
        "status": "candidate",
        "category": k_category or pm_entry["category"],
        "kalshi_category": k_category,
        "poly_category": pm_entry["category"],
        "polymarket_category": pm_entry["category"],
        "resolution_date": kalshi_date or poly_date,
        "kalshi_resolution_date": kalshi_date,
        "polymarket_resolution_date": poly_date,
        "resolution_source": kalshi_source or poly_source,
        "kalshi_resolution_source": kalshi_source,
        "polymarket_resolution_source": poly_source,
        "tie_break_rule": kalshi_tie_break or poly_tie_break,
        "kalshi_tie_break_rule": kalshi_tie_break,
        "polymarket_tie_break_rule": poly_tie_break,
        "outcome_set": poly_outcomes,
        "kalshi_outcome_set": kalshi_outcomes,
        "polymarket_outcome_set": poly_outcomes,
        "shared_tokens": sorted(k_tokens & pm_entry["tokens"]),
        "__kalshi_market": km,
        "__polymarket_market": pm,
    }


def _finalize_candidates(candidates: list[dict[str, Any]], *, max_candidates: int) -> list[dict[str, Any]]:
    candidates.sort(
        key=lambda c: (
            c["score"],
            len(c.get("shared_tokens") or ()),
            bool(c.get("kalshi_resolution_date") and c.get("polymarket_resolution_date")),
        ),
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    used_kalshi: set[str] = set()
    used_poly: set[str] = set()
    for candidate in candidates:
        kalshi_ticker = str(candidate.get("kalshi_ticker", "") or "").strip()
        poly_slug = str(candidate.get("poly_slug", "") or "").strip()
        if not kalshi_ticker or not poly_slug:
            continue
        if kalshi_ticker in used_kalshi or poly_slug in used_poly:
            continue
        selected.append(candidate)
        used_kalshi.add(kalshi_ticker)
        used_poly.add(poly_slug)

    return selected[:max_candidates] if max_candidates > 0 else selected


def _candidate_resolution_criteria(candidate: dict[str, Any], *, operator_note: str) -> dict[str, Any]:
    criteria = {
        "kalshi": {
            "source": candidate.get("kalshi_resolution_source"),
            "rule": candidate.get("kalshi_tie_break_rule"),
            "settlement_date": candidate.get("kalshi_resolution_date"),
        },
        "polymarket": {
            "source": candidate.get("polymarket_resolution_source"),
            "rule": candidate.get("polymarket_tie_break_rule"),
            "settlement_date": candidate.get("polymarket_resolution_date"),
        },
        "criteria_match": "identical",
        "operator_note": operator_note,
    }
    if candidate.get("polarity") == "same":
        criteria["polarity"] = "same"
    return criteria


def _structured_canonical_id(candidate: dict[str, Any]) -> str:
    """Stable canonical ID for parser-proven recurring markets.

    The exact Kalshi/Polymarket pair remains the true uniqueness key, but a
    readable deterministic ID prevents new structurally matched rows from
    accumulating under opaque AUTO_* IDs as recurring sports/politics/econ
    catalogs rotate.
    """
    category = str(candidate.get("category") or "").strip().lower()
    event_key = str(candidate.get("event_fingerprint") or "")
    outcome = normalize_market_text(str(candidate.get("outcome") or "")).replace(" ", "_").upper()
    digest = hashlib.sha1(
        f"{candidate.get('kalshi_ticker')}|{candidate.get('poly_slug')}|{event_key}|{outcome}".encode("utf-8")
    ).hexdigest()[:8]

    parts = event_key.split(":")
    if category == "sports" and len(parts) >= 6:
        _, sport, _entities, event_date, metric, _threshold = parts[:6]
        if metric == "winner" and event_date:
            compact_date = event_date.replace("-", "")
            side = outcome or "YES"
            return f"GAME_{sport.upper()}_{compact_date}_{side}_{digest}"

    if category == "politics" and len(parts) >= 6:
        _, subcategory, entity, event_date, metric, threshold = parts[:6]
        compact_date = event_date.replace("-", "")
        side = outcome or str(candidate.get("outcome") or "YES").upper()
        return f"POL_{subcategory.upper()}_{entity.upper()}_{compact_date}_{metric.upper()}_{threshold.upper()}_{side}_{digest}"[:200]

    if category == "crypto" and len(parts) >= 6:
        _, asset, entity, event_date, metric, threshold = parts[:6]
        compact_date = event_date.replace("-", "")
        threshold_id = normalize_market_text(threshold).replace(" ", "_").upper()
        return f"CRYPT_{asset.upper()}_{entity.upper()}_{compact_date}_{metric.upper()}_{threshold_id}_{digest}"[:200]

    if category == "economics" and len(parts) >= 6:
        _, subcategory, entity, event_date, metric, threshold = parts[:6]
        compact_date = event_date.replace("-", "")
        entity_id = normalize_market_text(entity).replace(" ", "_").upper()[:50]
        threshold_id = normalize_market_text(threshold).replace(" ", "_").upper()[:32]
        return f"ECON_{subcategory.upper()}_{entity_id}_{compact_date}_{metric.upper()}_{threshold_id}_{digest}"[:200]

    base = normalize_market_text(event_key or str(candidate.get("kalshi_ticker") or "STRUCTURED")).replace(" ", "_").upper()
    return f"MAP_{base[:64]}_{digest}"


def _is_structured_sports_non_winner_pair(kalshi_ticker: str, poly_slug: str) -> bool:
    """Gate 0: never fuzzy-match sports moneyline tickers to spread/total slugs."""
    kalshi = parse_kalshi_sports_ticker(kalshi_ticker)
    if kalshi is None:
        return False
    match = re.match(r"^(?P<prefix>[a-z]+)-(?P<sport>[a-z0-9]+)-", str(poly_slug or "").strip().lower())
    if not match:
        return False
    if match.group("sport") != kalshi.poly_sport:
        return False
    return match.group("prefix") not in SUPPORTED_POLY_WINNER_PREFIXES


def _candidate_verification_pair(candidate: dict[str, Any]) -> tuple[str, str]:
    """Build LLM verification strings with parser context for structural pairs.

    Raw venue titles can be too terse for exact structured markets (e.g. a
    Kalshi politics outcome may be titled only "Which party will win the
    Senate?" while the party lives in the ticker suffix).  The structured
    parser has already passed exact market-key equality before auto-promotion,
    so include those extracted fields for the LLM while still showing raw IDs
    and text so conflicts can be caught.
    """
    kalshi_raw = str(candidate.get("kalshi_title", "") or "")
    poly_raw = str(candidate.get("poly_question", "") or "")
    if not candidate.get("structural_match"):
        return kalshi_raw, poly_raw

    shared = {
        "category": candidate.get("category"),
        "event_fingerprint": candidate.get("event_fingerprint"),
        "outcome_fingerprint": candidate.get("outcome_fingerprint"),
        "resolution_date": candidate.get("resolution_date"),
        "resolution_source": candidate.get("resolution_source"),
        "market_rule": candidate.get("tie_break_rule"),
        "polarity": candidate.get("polarity"),
    }

    def _format(side: str, raw: str, market_id: str, yes_side: str | None) -> str:
        lines = [
            f"{side} raw question/title: {raw}",
            f"{side} market id: {market_id}",
            "Parser-extracted structured criteria (must agree with raw text):",
        ]
        for key, value in shared.items():
            if value not in (None, "", ()):  # keep prompt concise
                lines.append(f"- {key}: {value}")
        if yes_side:
            lines.append(f"- yes_outcome_side: {yes_side}")
        return "\n".join(lines)

    kalshi_text = _format(
        "Kalshi",
        kalshi_raw,
        str(candidate.get("kalshi_ticker", "") or ""),
        str(candidate.get("kalshi_yes_side", "") or candidate.get("outcome", "") or "") or None,
    )
    poly_text = _format(
        "Polymarket",
        poly_raw,
        str(candidate.get("poly_slug", "") or ""),
        str(candidate.get("polymarket_yes_side", "") or candidate.get("outcome", "") or "") or None,
    )
    return kalshi_text, poly_text


def _synthetic_kalshi_orderbook(market: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(market, dict):
        return {"bids": [], "asks": []}

    def _level(price_keys: tuple[str, ...], qty_keys: tuple[str, ...]) -> dict[str, float] | None:
        px = next((market.get(key) for key in price_keys if market.get(key) not in (None, "", 0)), 0)
        qty = next((market.get(key) for key in qty_keys if market.get(key) not in (None, "", 0)), 0)
        try:
            px_f = float(px or 0)
            qty_f = float(qty or 0)
        except (TypeError, ValueError):
            return None
        if px_f > 1.0:
            px_f /= 100.0
        if px_f <= 0 or qty_f <= 0:
            return None
        return {"px": px_f, "qty": qty_f}

    # Use only visible top-of-book sizes. Historical volume is not executable
    # liquidity and must not satisfy an auto-trade promotion gate.
    bid = _level(("yes_bid", "bid", "yes_bid_dollars"), ("yes_bid_size_fp", "yes_bid_size"))
    ask = _level(("yes_ask", "ask", "yes_ask_dollars"), ("yes_ask_size_fp", "yes_ask_size"))
    return {
        "bids": [bid] if bid else [],
        "asks": [ask] if ask else [],
    }


def _normalize_kalshi_orderbook(payload: Any) -> dict[str, Any]:
    """Normalize Kalshi orderbooks to {bids, asks} of {px, qty} levels.

    Kalshi has emitted both integer-cent and fixed-point dollar shapes:
      * {"orderbook": {"yes": [[55, 100]], "no": [[44, 200]]}}
      * {"orderbook_fp": {"yes_dollars": [["0.55", "100"]], ...}}

    The NO side is a bid for NO, equivalent to visible activity on the YES ask
    side, so both sides count for the coarse liquidity gate.
    """
    if not isinstance(payload, dict):
        return {"bids": [], "asks": []}

    orderbook = payload.get("orderbook")
    orderbook_fp = payload.get("orderbook_fp")
    if not isinstance(orderbook, dict):
        orderbook = payload if any(key in payload for key in ("yes", "no", "yes_dollars", "no_dollars")) else {}
    if not isinstance(orderbook_fp, dict):
        orderbook_fp = {}

    def _level_values(level: Any) -> tuple[Any, Any]:
        if isinstance(level, dict):
            px_raw = level.get("px", level.get("price", level.get("yes_price")))
            if isinstance(px_raw, dict):
                px_raw = px_raw.get("value")
            qty_raw = level.get("qty", level.get("quantity", level.get("size")))
            return px_raw, qty_raw
        try:
            return level[0], level[1]
        except (IndexError, TypeError):
            return None, None

    def _convert(levels: Any) -> list[dict[str, float]]:
        result: list[dict[str, float]] = []
        for level in levels or []:
            px_raw, qty_raw = _level_values(level)
            try:
                px = float(px_raw or 0)
                qty = float(qty_raw or 0)
            except (TypeError, ValueError):
                continue
            if px > 1.0:
                px /= 100.0
            if px <= 0 or qty <= 0:
                continue
            result.append({"px": px, "qty": qty})
        return result

    yes_levels = (
        orderbook.get("yes")
        or orderbook.get("yes_dollars")
        or orderbook_fp.get("yes_dollars")
        or orderbook_fp.get("yes")
        or []
    )
    no_levels = (
        orderbook.get("no")
        or orderbook.get("no_dollars")
        or orderbook_fp.get("no_dollars")
        or orderbook_fp.get("no")
        or []
    )
    return {"bids": _convert(yes_levels), "asks": _convert(no_levels)}


def _normalize_polymarket_orderbook(payload: Any) -> dict[str, Any]:
    """Normalize Polymarket book API response to {bids, asks} of {px, qty} levels.

    The /markets/{slug}/book endpoint returns
    {"marketData": {"bids": [{"px": {"value": "0.50"}, "qty": "100"}], "offers": [...]}}.
    Prices are nested under `px.value` and quantities are strings.
    """
    if not isinstance(payload, dict):
        return {"bids": [], "asks": []}
    market_data = payload.get("marketData") if "marketData" in payload else payload
    if not isinstance(market_data, dict):
        return {"bids": [], "asks": []}

    def _convert(levels: Any) -> list[dict[str, float]]:
        result: list[dict[str, float]] = []
        for level in levels or []:
            if not isinstance(level, dict):
                continue
            px_raw = level.get("px")
            if isinstance(px_raw, dict):
                px_raw = px_raw.get("value")
            try:
                px = float(px_raw or 0)
                qty = float(level.get("qty", 0) or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0 or qty <= 0:
                continue
            result.append({"px": px, "qty": qty})
        return result

    return {
        "bids": _convert(market_data.get("bids")),
        "asks": _convert(market_data.get("offers") or market_data.get("asks")),
    }


async def _load_candidate_orderbooks(
    kalshi_client,
    polymarket_us_client,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    kalshi_market = candidate.get("__kalshi_market") or {}
    polymarket_market = candidate.get("__polymarket_market") or {}
    kalshi_orderbook = _synthetic_kalshi_orderbook(kalshi_market)
    poly_orderbook: dict[str, Any] = {"bids": [], "asks": []}

    kalshi_getter = getattr(kalshi_client, "get_orderbook", None)
    if callable(kalshi_getter):
        try:
            kalshi_orderbook = _normalize_kalshi_orderbook(
                await kalshi_getter(candidate.get("kalshi_ticker", ""), depth=100)
            )
        except Exception:
            pass

    poly_getter = getattr(polymarket_us_client, "get_orderbook", None)
    if callable(poly_getter):
        try:
            poly_orderbook = _normalize_polymarket_orderbook(
                await poly_getter(candidate.get("poly_slug", ""), depth=100)
            )
        except Exception:
            poly_orderbook = {"bids": [], "asks": []}

    if not poly_orderbook.get("bids") and not poly_orderbook.get("asks") and isinstance(polymarket_market, dict):
        poly_orderbook = _normalize_polymarket_orderbook(polymarket_market)

    return {
        "kalshi": kalshi_orderbook,
        "polymarket": poly_orderbook,
    }


async def _apply_auto_promote(
    candidates: list[dict[str, Any]],
    *,
    kalshi_client,
    polymarket_us_client,
    promotion_settings: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    if not promotion_settings.get("auto_promote_enabled", False):
        _emit_progress(
            progress,
            "validate_candidates",
            "Auto-promotion disabled; candidates remain queued for review",
            counts={"candidates_total": len(candidates), "promoted": 0},
            rejection_reasons={"auto_promote_disabled": len(candidates)} if candidates else {},
        )
        return candidates

    from arbiter.mapping.auto_promote import maybe_promote
    from arbiter.mapping.llm_verifier import verify as llm_verify
    from arbiter.mapping.llm_verifier import verify_batch as llm_verify_batch

    max_promotions_raw = promotion_settings.get("auto_promote_daily_cap", len(candidates))
    max_promotions = int(len(candidates) if max_promotions_raw is None else max_promotions_raw)
    concurrency_raw = promotion_settings.get("auto_promote_concurrency", 8)
    concurrency = max(1, min(24, int(8 if concurrency_raw is None else concurrency_raw)))
    semaphore = asyncio.Semaphore(concurrency)

    # Settings dict (operator-runtime) wins; falls back to env var
    # AUTO_PROMOTE_MIN_SCORE (containers using env_file but no settings
    # store), else 0.85.  Same fallback pattern for max_days and advisory.
    min_score_default = _env_float("AUTO_PROMOTE_MIN_SCORE", 0.85)
    min_score_raw = promotion_settings.get("auto_promote_min_score", min_score_default)
    min_score = float(min_score_default if min_score_raw is None else min_score_raw)
    max_days_default = int(_env_float("AUTO_PROMOTE_MAX_DAYS", 90.0))
    max_days_raw = promotion_settings.get("auto_promote_max_days", max_days_default)
    max_days = int(max_days_default if max_days_raw is None else max_days_raw)
    advisory_scans_default = int(_env_float("AUTO_PROMOTE_ADVISORY_SCANS", 30.0))
    advisory_scans_raw = promotion_settings.get("auto_promote_advisory_scans", advisory_scans_default)
    advisory_scans = int(advisory_scans_default if advisory_scans_raw is None else advisory_scans_raw)
    today = date.today()
    llm_results: dict[tuple[str, str], str] = {}
    llm_categories: dict[tuple[str, str], str] = {}

    preverify_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        score = float(candidate.get("score", 0.0) or 0.0)
        if score < min_score:
            continue
        resolution_date = _coerce_date(candidate.get("resolution_date"))
        if resolution_date is not None and (resolution_date - today).days > max_days:
            continue
        if int(candidate.get("advisory_scans", 0) or 0) < advisory_scans:
            continue
        preverify_candidates.append(candidate)

    _emit_progress(
        progress,
        "validate_candidates",
        "Preparing candidates for LLM and structured promotion gates",
        counts={
            "candidates_total": len(candidates),
            "preverify_candidates": len(preverify_candidates),
            "auto_promote_min_score": min_score,
            "auto_promote_max_days": max_days,
        },
    )

    if preverify_candidates:
        pairs_by_category: dict[str, list[tuple[dict[str, Any], tuple[str, str]]]] = defaultdict(list)
        for candidate in preverify_candidates:
            kalshi_verify_text, poly_verify_text = _candidate_verification_pair(candidate)
            candidate["kalshi_verification_text"] = kalshi_verify_text
            candidate["polymarket_verification_text"] = poly_verify_text
            category = str(candidate.get("category") or "").strip().lower()
            pair = (kalshi_verify_text, poly_verify_text)
            pairs_by_category[category].append((candidate, pair))
            llm_categories[pair] = category
        _emit_progress(
            progress,
            "llm_batch",
            "Running cached batch LLM verification",
            counts={"pairs": len(preverify_candidates), "categories": len(pairs_by_category)},
        )
        try:
            verdict_count = 0
            for category, items in pairs_by_category.items():
                pairs = [pair for _, pair in items]
                batch_results = await llm_verify_batch(pairs)
                for pair, result in zip(pairs, batch_results):
                    llm_results[pair] = result
                    verdict_count += 1
            _emit_progress(
                progress,
                "llm_batch",
                "Batch LLM verification complete",
                counts={"pairs": len(preverify_candidates), "verdicts": verdict_count},
            )
        except Exception as exc:
            logger.warning("auto_discovery: batch LLM verification failed closed: %s", exc)
            _emit_progress(
                progress,
                "llm_batch",
                "Batch LLM verification failed closed",
                counts={"pairs": len(preverify_candidates), "verdicts": 0},
                error=str(exc),
            )

    async def _cached_llm_verify(kalshi_q: str, poly_q: str) -> str:
        pair = (kalshi_q, poly_q)
        result = llm_results.get(pair)
        if result is not None:
            return result
        return await llm_verify(kalshi_q, poly_q)

    async def _evaluate(candidate: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        score = float(candidate.get("score", 0.0) or 0.0)
        if score < min_score:
            return candidate, type("PromotionResult", (), {"promoted": False, "reason": "score_low"})()

        resolution_date = _coerce_date(candidate.get("resolution_date"))
        # Allow candidates without explicit resolution dates — many valid
        # markets (politics, long-dated) lack structured date fields.  The
        # LLM verifier + score threshold are the real quality gates.
        if resolution_date is not None and (resolution_date - today).days > max_days:
            return candidate, type("PromotionResult", (), {"promoted": False, "reason": "resolution_too_far"})()

        if int(candidate.get("advisory_scans", 0) or 0) < advisory_scans:
            return candidate, type("PromotionResult", (), {"promoted": False, "reason": "advisory_pending"})()

        async with semaphore:
            orderbooks = await _load_candidate_orderbooks(kalshi_client, polymarket_us_client, candidate)
            result = await maybe_promote(
                candidate,
                settings=promotion_settings,
                orderbooks=orderbooks,
                llm_verifier=_cached_llm_verify,
                today_promoted_count=0,
                cooling_state={},
            )
            return candidate, result

    promoted_this_pass = 0
    reason_counts: dict[str, int] = {}
    for candidate, result in await asyncio.gather(*(_evaluate(candidate) for candidate in candidates)):
        candidate["auto_promote_reason"] = result.reason
        reason_counts[result.reason] = reason_counts.get(result.reason, 0) + 1
        if not result.promoted or promoted_this_pass >= max_promotions:
            if not result.promoted:
                candidate["allow_auto_trade"] = False
                candidate["review_note"] = f"Auto-promote gate: {result.reason}"
            continue

        promoted_this_pass += 1
        candidate["status"] = "confirmed"
        candidate["allow_auto_trade"] = True
        candidate["resolution_match_status"] = "identical"
        candidate["resolution_criteria"] = _candidate_resolution_criteria(
            candidate,
            operator_note="Auto-promoted by discovery engine after structured + LLM + liquidity checks.",
        )
        candidate["notes"] = "Auto-promoted by discovery engine."
        candidate["review_note"] = ""

    logger.info(
        "auto_discovery: auto-promote summary — %d promoted, %d total, settings: min_score=%.2f max_days=%d advisory=%d, reasons: %s",
        promoted_this_pass, len(candidates), min_score, max_days, advisory_scans,
        ", ".join(f"{r}={c}" for r, c in sorted(reason_counts.items(), key=lambda x: -x[1])),
    )
    _emit_progress(
        progress,
        "validate_candidates",
        "Auto-promotion gates complete",
        counts={
            "candidates_total": len(candidates),
            "promoted": promoted_this_pass,
            "evaluated": sum(reason_counts.values()),
        },
        rejection_reasons=reason_counts,
    )
    return candidates


async def _discover_from_kalshi_events(
    kalshi_client,
    kalshi_events: list[dict[str, Any]],
    poly_entries: list[dict[str, Any]],
    poly_index: dict[str, set[int]],
    poly_fingerprints: dict[str, set[int]],
    poly_event_fingerprints: dict[str, set[int]],
    *,
    min_score: float,
    max_candidates: int,
) -> tuple[list[dict[str, Any]], int]:
    event_candidates: dict[str, list[tuple[float, int, str, str, date | None]]] = defaultdict(list)
    scored_pairs = 0
    event_min_score = max(min_score - 0.10, 0.15)

    for idx, event in enumerate(kalshi_events, start=1):
        if idx % 100 == 0:
            await asyncio.sleep(0)

        event_ticker = str(event.get("event_ticker", "") or "").strip()
        if not event_ticker:
            continue

        event_text = _kalshi_event_text(event)
        if not event_text:
            continue

        event_tokens = _market_tokens(event_text)
        if not event_tokens:
            continue

        event_category = _normalize_category(event.get("category"))
        event_date = _coerce_date(
            event.get("close_time")
            or event.get("expiration_time")
            or event.get("sub_title")
            or event.get("title")
        )
        event_category_for_score = "" if event_category == "sports" and event_date is None else event_category

        event_fingerprint = fingerprint_kalshi_event(event)
        candidate_indexes: set[int] = set()
        if event_fingerprint is not None:
            candidate_indexes.update(poly_event_fingerprints.get(event_fingerprint.event_key, ()))
        else:
            candidate_indexes.update(
                _candidate_indexes_from_tokens(
                    event_tokens,
                    poly_index,
                    max_postings=50,
                    max_total=25,
                )
            )
        if not candidate_indexes:
            continue

        for entry_index in candidate_indexes:
            pm_entry = poly_entries[entry_index]
            poly_fingerprint = pm_entry.get("fingerprint")
            score = 0.9999 if (
                event_fingerprint is not None
                and poly_fingerprint is not None
                and event_fingerprint.event_key == poly_fingerprint.event_key
            ) else _candidate_score(
                    kalshi_text=event_text,
                    poly_text=pm_entry["text"],
                    kalshi_tokens=event_tokens,
                    poly_tokens=pm_entry["tokens"],
                    kalshi_category=event_category_for_score,
                    poly_category=pm_entry["category"],
                    kalshi_date=event_date,
                    poly_date=pm_entry["date"],
                )
            scored_pairs += 1
            if score < event_min_score:
                continue
            event_candidates[event_ticker].append((score, entry_index, event_text, event_category, event_date))

    if not event_candidates:
        return [], scored_pairs

    ranked_events = sorted(
        event_candidates.items(),
        key=lambda item: max(candidate[0] for candidate in item[1]),
        reverse=True,
    )[: min(max(max_candidates * 2, 100), 250)]

    candidates: list[dict[str, Any]] = []
    for idx, (event_ticker, matches) in enumerate(ranked_events, start=1):
        if idx % 25 == 0:
            await asyncio.sleep(0)

        event_markets = await kalshi_client.list_markets_for_event(event_ticker, limit=50)
        if not event_markets:
            continue

        top_matches = sorted(matches, key=lambda item: item[0], reverse=True)[:4]
        fallback_category = top_matches[0][3] if top_matches else ""
        fallback_date = top_matches[0][4] if top_matches else None

        for km in event_markets:
            if _looks_like_multi_leg_kalshi_market(km):
                continue

            k_text = _kalshi_text(km)
            if not k_text:
                continue

            k_tokens = _market_tokens(k_text)
            if not k_tokens:
                continue

            k_category = _normalize_category(km.get("category")) or fallback_category
            k_date = _coerce_date(km.get("close_time") or km.get("expiration_time")) or fallback_date

            candidate_indexes = {entry_index for _, entry_index, _, _, _ in top_matches}
            k_fingerprint = fingerprint_kalshi_market(km)
            if k_fingerprint is not None:
                candidate_indexes.update(poly_fingerprints.get(k_fingerprint.market_key, ()))

            for entry_index in candidate_indexes:
                pm_entry = poly_entries[entry_index]

                # ── Bracket-vs-binary guard (event path) ─────────────
                p_slug = str(pm_entry["market"].get("slug", "") or "")
                k_ticker = str(km.get("ticker", "") or "")
                if _is_bracket_vs_binary_mismatch(k_ticker, p_slug):
                    continue
                if _is_structured_sports_non_winner_pair(k_ticker, p_slug):
                    continue
                sports_safety = evaluate_sports_pair(k_ticker, p_slug)
                if sports_safety.known and not sports_safety.safe:
                    continue

                exact_match = structural_match(km, pm_entry["market"])
                score = 0.9999 if exact_match is not None else _candidate_score(
                    kalshi_text=k_text,
                    poly_text=pm_entry["text"],
                    kalshi_tokens=k_tokens,
                    poly_tokens=pm_entry["tokens"],
                    kalshi_category=k_category,
                    poly_category=pm_entry["category"],
                    kalshi_date=k_date,
                    poly_date=pm_entry["date"],
                )
                scored_pairs += 1
                if score < min_score:
                    continue
                candidate = _candidate_payload(
                    km=km,
                    pm_entry=pm_entry,
                    k_text=k_text,
                    k_tokens=k_tokens,
                    k_category=k_category,
                    k_date=k_date,
                    score=score,
                )
                if sports_safety.known:
                    candidate.update(sports_safety.candidate_fields())
                if exact_match is not None:
                    candidate.update(exact_match.candidate_fields())
                    candidate.setdefault("canonical_id", _structured_canonical_id(candidate))
                candidates.append(candidate)

    return candidates, scored_pairs


def _candidate_score(
    *,
    kalshi_text: str,
    poly_text: str,
    kalshi_tokens: set[str],
    poly_tokens: set[str],
    kalshi_category: str,
    poly_category: str,
    kalshi_date: date | None,
    poly_date: date | None,
) -> float:
    if kalshi_category and poly_category and kalshi_category != poly_category:
        return 0.0

    shared_tokens = kalshi_tokens & poly_tokens
    if not shared_tokens:
        return 0.0

    # Fast: token Jaccard overlap (set-based, order-independent)
    union_size = len(kalshi_tokens | poly_tokens)
    token_jaccard = len(shared_tokens) / max(union_size, 1)

    # Fast: meaningful token overlap (rare/domain-specific tokens matter more)
    meaningful_shared = shared_tokens - _COMMON_TOKENS
    meaningful_union = (kalshi_tokens | poly_tokens) - _COMMON_TOKENS
    meaningful_overlap = len(meaningful_shared) / max(len(meaningful_union), 1) if meaningful_union else 0.0

    # Quick score from fast metrics; only run expensive SequenceMatcher if
    # the fast score is promising enough to be a viable candidate.
    # Threshold 0.04 keeps scoring fast while allowing pairs with modest
    # token overlap (e.g. 2+ shared meaningful tokens) through to the
    # more accurate sequence comparison.
    quick_score = (0.50 * token_jaccard) + (0.50 * meaningful_overlap)
    if quick_score < 0.04:
        return 0.0  # Skip expensive SequenceMatcher for clearly-unrelated pairs

    # Slow: sequence similarity (order-sensitive, catches phrasing differences)
    seq_ratio = SequenceMatcher(None, kalshi_text.lower(), poly_text.lower()).ratio()

    # Combine all three signals
    score = (0.40 * seq_ratio) + (0.30 * token_jaccard) + (0.30 * meaningful_overlap)

    if kalshi_date is not None and poly_date is not None:
        day_delta = abs((kalshi_date - poly_date).days)
        if kalshi_category == "sports" and day_delta > 1:
            return 0.0
        if day_delta > 31:
            return 0.0
        if day_delta == 0:
            score += 0.20
        elif day_delta <= 1:
            score += 0.10
        elif day_delta <= 7:
            score += 0.04
    elif kalshi_category == "sports" and poly_category == "sports":
        return 0.0

    if kalshi_category and poly_category and kalshi_category == poly_category:
        score += 0.05

    return round(min(score, 0.9999), 4)


async def discover(
    kalshi_client,
    polymarket_us_client,
    mapping_store,
    budget_rps: float = 2.0,
    *,
    min_score: float = 0.25,
    max_candidates: int = 500,
    promotion_settings: dict[str, Any] | None = None,
    progress: ProgressCallback | None = None,
) -> int:
    """Pull all live markets from both platforms, score candidate pairs, write candidates.

    Parameters
    ----------
    kalshi_client:
        A Kalshi client with a ``list_all_markets()`` async method that returns
        a list of market dicts (each with 'ticker' and 'title'/'subtitle' keys).
    polymarket_us_client:
        A Polymarket US client with a ``list_markets()`` async generator that
        yields market dicts (each with 'slug' and 'question' keys).
    mapping_store:
        A mapping store with a ``write_candidates(candidates)`` async method.
    budget_rps:
        Discovery rate limit in requests per second. The pipeline makes exactly
        2 API calls (one per platform), sleeping between them to stay within budget.

    Returns
    -------
    int
        Number of candidate pairs written to the mapping store.
    """
    request_interval = 1.0 / budget_rps

    # ── Pull Kalshi catalogs ───────────────────────────────────────────────────
    # Rate-limit sleep before each platform call so both sleeps count.
    _emit_progress(
        progress,
        "fetch_kalshi_markets",
        "Fetching Kalshi active market catalog",
        counts={"kalshi_markets": 0},
    )
    await asyncio.sleep(request_interval)
    market_discovery = inspect.iscoroutinefunction(getattr(kalshi_client, "list_all_markets", None))
    event_discovery = (
        inspect.iscoroutinefunction(getattr(kalshi_client, "list_all_events", None))
        and inspect.iscoroutinefunction(getattr(kalshi_client, "list_markets_for_event", None))
    )

    t0 = time.monotonic()
    kalshi_markets: list[dict] = []
    if market_discovery:
        logger.info("auto_discovery: fetching Kalshi markets")
        kalshi_max_pages = int(_env_float("AUTO_DISCOVERY_KALSHI_MARKET_MAX_PAGES", 10))
        kalshi_markets = await _call_with_supported_kwargs(
            kalshi_client.list_all_markets,
            status="open",
            page_size=1000,
            max_pages=kalshi_max_pages,
        )
        raw_kalshi_markets = len(kalshi_markets)
        kalshi_markets = [m for m in kalshi_markets if _is_active_kalshi_item(m)]
        t1 = time.monotonic()
        logger.info(
            "auto_discovery: got %d/%d active Kalshi markets in %.2fs",
            len(kalshi_markets),
            raw_kalshi_markets,
            t1 - t0,
        )
        _emit_progress(
            progress,
            "fetch_kalshi_markets",
            "Fetched Kalshi active market catalog",
            counts={
                "kalshi_markets": len(kalshi_markets),
                "kalshi_markets_raw": raw_kalshi_markets,
            },
            elapsed_seconds=round(t1 - t0, 3),
        )
    else:
        t1 = t0

    kalshi_events: list[dict] = []
    if event_discovery:
        _emit_progress(
            progress,
            "fetch_kalshi_events",
            "Fetching Kalshi event hierarchy",
            counts={"kalshi_events": 0},
        )
        await asyncio.sleep(request_interval)
        t_events = time.monotonic()
        logger.info("auto_discovery: fetching Kalshi events")
        kalshi_event_max_pages = int(_env_float("AUTO_DISCOVERY_KALSHI_EVENT_MAX_PAGES", 25))
        kalshi_events = await _call_with_supported_kwargs(
            kalshi_client.list_all_events,
            status="open",
            page_size=200,
            max_pages=kalshi_event_max_pages,
        )
        raw_kalshi_events = len(kalshi_events)
        kalshi_events = [e for e in kalshi_events if _is_active_kalshi_item(e)]
        t1 = time.monotonic()
        logger.info(
            "auto_discovery: got %d/%d active Kalshi events in %.2fs",
            len(kalshi_events),
            raw_kalshi_events,
            t1 - t_events,
        )
        _emit_progress(
            progress,
            "fetch_kalshi_events",
            "Fetched Kalshi event hierarchy",
            counts={
                "kalshi_events": len(kalshi_events),
                "kalshi_events_raw": raw_kalshi_events,
            },
            elapsed_seconds=round(t1 - t_events, 3),
        )

    # Rate-limit sleep between platform and Polymarket calls
    await asyncio.sleep(request_interval)

    # ── Pull Polymarket US markets ─────────────────────────────────────────────
    _emit_progress(
        progress,
        "fetch_polymarket_markets",
        "Fetching Polymarket active market catalog",
        counts={"polymarket_markets": 0},
    )
    logger.info("auto_discovery: fetching Polymarket US markets")
    poly_markets: list[dict] = []
    raw_poly_markets = 0
    skipped_inactive_poly = 0
    polymarket_max_pages = int(_env_float("AUTO_DISCOVERY_POLYMARKET_MAX_PAGES", 40))
    async for market in polymarket_us_client.list_markets(
        purpose="discovery",
        max_pages=polymarket_max_pages,
        active=True,
        closed=False,
        archived=False,
    ):
        raw_poly_markets += 1
        if _is_active_polymarket_item(market):
            poly_markets.append(market)
        else:
            skipped_inactive_poly += 1
        if raw_poly_markets % 1000 == 0:
            _emit_progress(
                progress,
                "fetch_polymarket_markets",
                f"Fetched {len(poly_markets)} active Polymarket markets...",
                counts={
                    "polymarket_markets": len(poly_markets),
                    "polymarket_markets_raw": raw_poly_markets,
                    "polymarket_markets_skipped_inactive": skipped_inactive_poly,
                },
            )
    t2 = time.monotonic()
    logger.info(
        "auto_discovery: got %d/%d active Polymarket markets in %.2fs",
        len(poly_markets),
        raw_poly_markets,
        t2 - t1,
    )
    _emit_progress(
        progress,
        "fetch_polymarket_markets",
        "Fetched Polymarket active market catalog",
        counts={
            "polymarket_markets": len(poly_markets),
            "polymarket_markets_raw": raw_poly_markets,
            "polymarket_markets_skipped_inactive": skipped_inactive_poly,
        },
        elapsed_seconds=round(t2 - t1, 3),
    )

    if (not kalshi_markets and not kalshi_events) or not poly_markets:
        logger.info("auto_discovery: no markets on one or both platforms — no candidates")
        _emit_progress(
            progress,
            "persist_candidates",
            "No active markets available on one or both platforms",
            counts={"candidates_written": 0},
        )
        return 0

    poly_entries, poly_index = _build_poly_entries(poly_markets)
    poly_fingerprints = _poly_fingerprint_index(poly_entries)
    poly_event_fingerprints = _poly_event_fingerprint_index(poly_entries)
    _emit_progress(
        progress,
        "index_polymarket",
        "Indexed Polymarket text and canonical fingerprints",
        counts={
            "polymarket_indexed": len(poly_entries),
            "fingerprint_markets": len(poly_fingerprints),
            "fingerprint_events": len(poly_event_fingerprints),
        },
    )
    candidates: list[dict[str, Any]] = []
    scored_pairs = 0

    _emit_progress(
        progress,
        "score_candidates",
        "Scoring structural and fuzzy candidate pairs",
        counts={
            "kalshi_markets": len(kalshi_markets),
            "kalshi_events": len(kalshi_events),
            "polymarket_markets": len(poly_markets),
            "scored_pairs": 0,
        },
    )

    if market_discovery:
        for idx, km in enumerate(kalshi_markets, start=1):
            if idx % 100 == 0:
                await asyncio.sleep(0)

            if _looks_like_multi_leg_kalshi_market(km):
                continue

            k_text = _kalshi_text(km)
            if not k_text:
                continue

            k_tokens = _market_tokens(k_text)
            if not k_tokens:
                continue

            k_category = _normalize_category(km.get("category"))
            k_date = _coerce_date(km.get("close_time") or km.get("expiration_time"))

            candidate_indexes = _candidate_indexes_from_tokens(k_tokens, poly_index)
            k_fingerprint = fingerprint_kalshi_market(km)
            if k_fingerprint is not None:
                candidate_indexes.update(poly_fingerprints.get(k_fingerprint.market_key, ()))
            if not candidate_indexes:
                continue

            for entry_index in candidate_indexes:
                pm_entry = poly_entries[entry_index]

                # ── Bracket-vs-binary guard ───────────────────────────
                p_slug = str(pm_entry["market"].get("slug", "") or "")
                k_ticker = str(km.get("ticker", "") or "")
                if _is_bracket_vs_binary_mismatch(k_ticker, p_slug):
                    continue
                if _is_structured_sports_non_winner_pair(k_ticker, p_slug):
                    continue
                sports_safety = evaluate_sports_pair(k_ticker, p_slug)
                if sports_safety.known and not sports_safety.safe:
                    continue

                exact_match = structural_match(km, pm_entry["market"])
                score = 0.9999 if exact_match is not None else _candidate_score(
                    kalshi_text=k_text,
                    poly_text=pm_entry["text"],
                    kalshi_tokens=k_tokens,
                    poly_tokens=pm_entry["tokens"],
                    kalshi_category=k_category,
                    poly_category=pm_entry["category"],
                    kalshi_date=k_date,
                    poly_date=pm_entry["date"],
                )
                scored_pairs += 1
                if score < min_score:
                    continue

                candidate = _candidate_payload(
                    km=km,
                    pm_entry=pm_entry,
                    k_text=k_text,
                    k_tokens=k_tokens,
                    k_category=k_category,
                    k_date=k_date,
                    score=score,
                )
                if sports_safety.known:
                    candidate.update(sports_safety.candidate_fields())
                if exact_match is not None:
                    candidate.update(exact_match.candidate_fields())
                    candidate.setdefault("canonical_id", _structured_canonical_id(candidate))
                candidates.append(candidate)

    if event_discovery and kalshi_events:
        event_candidates, event_scored_pairs = await _discover_from_kalshi_events(
            kalshi_client,
            kalshi_events,
            poly_entries,
            poly_index,
            poly_fingerprints,
            poly_event_fingerprints,
            min_score=min_score,
            max_candidates=max_candidates,
        )
        candidates.extend(event_candidates)
        scored_pairs += event_scored_pairs

    candidates = _finalize_candidates(candidates, max_candidates=max_candidates)
    _emit_progress(
        progress,
        "score_candidates",
        "Candidate scoring complete",
        counts={
            "scored_pairs": scored_pairs,
            "candidates_selected": len(candidates),
        },
    )

    if promotion_settings is not None:
        candidates = await _apply_auto_promote(
            candidates,
            kalshi_client=kalshi_client,
            polymarket_us_client=polymarket_us_client,
            promotion_settings=promotion_settings,
            progress=progress,
        )

    logger.info(
        "auto_discovery: found %d candidate pairs after scoring %d filtered pairs (%d Kalshi markets + %d events × %d Polymarket indexed)",
        len(candidates),
        scored_pairs,
        len(kalshi_markets),
        len(kalshi_events),
        len(poly_entries),
    )

    # ── Write to store ─────────────────────────────────────────────────────────
    if candidates:
        sync_candidates = getattr(mapping_store, "sync_candidates", None)
        if sync_candidates is not None and asyncio.iscoroutinefunction(sync_candidates):
            written = await sync_candidates(candidates)
        else:
            written = await mapping_store.write_candidates(candidates)
        _emit_progress(
            progress,
            "persist_candidates",
            "Persisted discovery candidates",
            counts={
                "candidates_written": written,
                "candidates_selected": len(candidates),
                "scored_pairs": scored_pairs,
                "confirmed_in_batch": sum(1 for candidate in candidates if candidate.get("status") == "confirmed"),
            },
        )
        return written

    _emit_progress(
        progress,
        "persist_candidates",
        "No candidates met discovery thresholds",
        counts={
            "candidates_written": 0,
            "candidates_selected": 0,
            "scored_pairs": scored_pairs,
            "confirmed_in_batch": 0,
        },
    )
    return 0
