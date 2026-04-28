"""
Auto-discovery pipeline — pulls all live markets from both platforms,
scores candidate pairs, and writes them to the mapping store.

Rate-limited to budget_rps (default 2.0 r/s) during discovery via asyncio.sleep.
Returns the count of candidates written to the store.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any

from arbiter.config.settings import normalize_market_text, similarity_score

logger = logging.getLogger("arbiter.mapping.auto_discovery")


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
    multi_prefixes = (
        "KXMVECROSSCATEGORY",
        "KXMVESPORTSMULTIGAME",
        "KXMVESPORTSMULTIGAMEEXTENDED",
    )
    if ticker.startswith(multi_prefixes) or event_ticker.startswith(multi_prefixes):
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
        }
        poly_entries.append(entry)
        entry_index = len(poly_entries) - 1
        for token in tokens:
            poly_index[token].add(entry_index)

    return poly_entries, poly_index


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
    return {
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


def _synthetic_kalshi_orderbook(market: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(market, dict):
        return {"bids": [], "asks": []}
    px = market.get("yes_bid") or market.get("bid") or market.get("last_price") or 0
    qty = market.get("yes_bid_size_fp") or market.get("yes_ask_size_fp") or market.get("volume") or 0
    try:
        px_f = float(px or 0)
        qty_f = float(qty or 0)
    except (TypeError, ValueError):
        return {"bids": [], "asks": []}
    if px_f > 1.0:
        px_f /= 100.0
    if px_f <= 0 or qty_f <= 0:
        return {"bids": [], "asks": []}
    return {"bids": [{"px": px_f, "qty": qty_f}], "asks": []}


def _normalize_kalshi_orderbook(payload: Any) -> dict[str, Any]:
    """Normalize Kalshi orderbook to {bids, asks} of {px, qty} levels.

    Kalshi exposes both sides of the YES contract under `orderbook.yes` (bids)
    and `orderbook.no` (which represents bids for NO, ≡ asks for YES). We treat
    both as activity for the liquidity gate.
    """
    orderbook = payload.get("orderbook", payload) if isinstance(payload, dict) else {}

    def _convert(levels: Any) -> list[dict[str, float]]:
        result: list[dict[str, float]] = []
        for level in levels or []:
            try:
                px = float(level[0])
                qty = float(level[1])
            except (IndexError, TypeError, ValueError):
                continue
            if px > 1.0:
                px /= 100.0
            if px <= 0 or qty <= 0:
                continue
            result.append({"px": px, "qty": qty})
        return result

    yes_levels = orderbook.get("yes", []) if isinstance(orderbook, dict) else []
    no_levels = orderbook.get("no", []) if isinstance(orderbook, dict) else []
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
) -> list[dict[str, Any]]:
    if not promotion_settings.get("auto_promote_enabled", False):
        return candidates

    from arbiter.mapping.auto_promote import maybe_promote
    from arbiter.mapping.llm_verifier import verify as llm_verify

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
                llm_verifier=llm_verify,
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

    logger.info(
        "auto_discovery: auto-promote summary — %d promoted, %d total, settings: min_score=%.2f max_days=%d advisory=%d, reasons: %s",
        promoted_this_pass, len(candidates), min_score, max_days, advisory_scans,
        ", ".join(f"{r}={c}" for r, c in sorted(reason_counts.items(), key=lambda x: -x[1])),
    )
    return candidates


async def _discover_from_kalshi_events(
    kalshi_client,
    kalshi_events: list[dict[str, Any]],
    poly_entries: list[dict[str, Any]],
    poly_index: dict[str, set[int]],
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

        candidate_indexes: set[int] = set()
        for token in event_tokens:
            candidate_indexes.update(poly_index.get(token, ()))
        if not candidate_indexes:
            continue

        for entry_index in candidate_indexes:
            pm_entry = poly_entries[entry_index]
            score = _candidate_score(
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

            for _, entry_index, _, _, _ in top_matches:
                pm_entry = poly_entries[entry_index]

                # ── Bracket-vs-binary guard (event path) ─────────────
                p_slug = str(pm_entry["market"].get("slug", "") or "")
                k_ticker = str(km.get("ticker", "") or "")
                if _is_bracket_vs_binary_mismatch(k_ticker, p_slug):
                    continue

                score = _candidate_score(
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
                candidates.append(
                    _candidate_payload(
                        km=km,
                        pm_entry=pm_entry,
                        k_text=k_text,
                        k_tokens=k_tokens,
                        k_category=k_category,
                        k_date=k_date,
                        score=score,
                    )
                )

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
        kalshi_markets = await kalshi_client.list_all_markets()
        t1 = time.monotonic()
        logger.info("auto_discovery: got %d Kalshi markets in %.2fs", len(kalshi_markets), t1 - t0)
    else:
        t1 = t0

    kalshi_events: list[dict] = []
    if event_discovery:
        await asyncio.sleep(request_interval)
        t_events = time.monotonic()
        logger.info("auto_discovery: fetching Kalshi events")
        kalshi_events = await kalshi_client.list_all_events()
        t1 = time.monotonic()
        logger.info("auto_discovery: got %d Kalshi events in %.2fs", len(kalshi_events), t1 - t_events)

    # Rate-limit sleep between platform and Polymarket calls
    await asyncio.sleep(request_interval)

    # ── Pull Polymarket US markets ─────────────────────────────────────────────
    logger.info("auto_discovery: fetching Polymarket US markets")
    poly_markets: list[dict] = []
    async for market in polymarket_us_client.list_markets(purpose="discovery"):
        poly_markets.append(market)
    t2 = time.monotonic()
    logger.info("auto_discovery: got %d Polymarket markets in %.2fs", len(poly_markets), t2 - t1)

    if (not kalshi_markets and not kalshi_events) or not poly_markets:
        logger.info("auto_discovery: no markets on one or both platforms — no candidates")
        return 0

    poly_entries, poly_index = _build_poly_entries(poly_markets)
    candidates: list[dict[str, Any]] = []
    scored_pairs = 0

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

            candidate_indexes: set[int] = set()
            for token in k_tokens:
                candidate_indexes.update(poly_index.get(token, ()))
            if not candidate_indexes:
                continue

            for entry_index in candidate_indexes:
                pm_entry = poly_entries[entry_index]

                # ── Bracket-vs-binary guard ───────────────────────────
                p_slug = str(pm_entry["market"].get("slug", "") or "")
                k_ticker = str(km.get("ticker", "") or "")
                if _is_bracket_vs_binary_mismatch(k_ticker, p_slug):
                    continue

                score = _candidate_score(
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

                candidates.append(
                    _candidate_payload(
                        km=km,
                        pm_entry=pm_entry,
                        k_text=k_text,
                        k_tokens=k_tokens,
                        k_category=k_category,
                        k_date=k_date,
                        score=score,
                    )
                )

    if event_discovery and kalshi_events:
        event_candidates, event_scored_pairs = await _discover_from_kalshi_events(
            kalshi_client,
            kalshi_events,
            poly_entries,
            poly_index,
            min_score=min_score,
            max_candidates=max_candidates,
        )
        candidates.extend(event_candidates)
        scored_pairs += event_scored_pairs

    candidates = _finalize_candidates(candidates, max_candidates=max_candidates)

    if promotion_settings is not None:
        candidates = await _apply_auto_promote(
            candidates,
            kalshi_client=kalshi_client,
            polymarket_us_client=polymarket_us_client,
            promotion_settings=promotion_settings,
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
            return await sync_candidates(candidates)
        return await mapping_store.write_candidates(candidates)

    return 0
