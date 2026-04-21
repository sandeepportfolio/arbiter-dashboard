"""
Auto-promote gate — 8-condition check before promoting a candidate mapping.

A mapping is only promoted to allow_auto_trade=True if all 8 gates pass.
Returns the first-failing reason or promoted=True.

Conditions (in order — first failing wins):
1. AUTO_PROMOTE_ENABLED=true
2. score >= 0.85
3. resolution_check(...) == IDENTICAL
4. LLM verifier returns YES
5. Both orderbooks have depth >= PHASE5_MAX_ORDER_USD × 2
6. resolution_date within 90 days
7. today_promoted_count < AUTO_PROMOTE_DAILY_CAP (default 20)
8. Cooling-off: first AUTO_PROMOTE_ADVISORY_SCANS after first-see are advisory-only

Metrics: increments auto_promote_rejections_total{reason=...} counter when available.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Optional

from arbiter.mapping.resolution_check import MarketFacts, ResolutionMatch

logger = logging.getLogger("arbiter.mapping.auto_promote")

# ─── Try to import Prometheus counter (optional — no-op if unavailable) ───────
try:
    from prometheus_client import Counter as _Counter
    _REJECTIONS = _Counter(
        "auto_promote_rejections_total",
        "Auto-promote gate rejection count by reason",
        ["reason"],
    )
    def _inc_rejection(reason: str) -> None:
        _REJECTIONS.labels(reason=reason).inc()
except Exception:
    def _inc_rejection(reason: str) -> None:  # type: ignore[misc]
        pass


# ─── Public types ─────────────────────────────────────────────────────────────

@dataclass
class PromotionResult:
    """Result of the auto-promote gate evaluation."""
    promoted: bool
    reason: str  # one of: "auto_promote_disabled", "score_low", "resolution_divergent",
                 #       "llm_no", "liquidity_low", "date_out_of_window",
                 #       "daily_cap", "cooling_off", "promoted"


# ─── Depth calculation ────────────────────────────────────────────────────────

def _orderbook_depth_usd(orderbook: dict) -> float:
    """Sum USD depth from the bids side: sum(price * qty) for each bid level."""
    total = 0.0
    for bid in orderbook.get("bids", []):
        try:
            px = float(bid.get("px", 0) or 0)
            qty = float(bid.get("qty", 0) or 0)
            total += px * qty
        except (TypeError, ValueError):
            continue
    return total


# ─── Resolution-facts extractor ───────────────────────────────────────────────

def _candidate_to_market_facts(candidate: dict) -> MarketFacts:
    """Build a MarketFacts from a candidate dict for Layer 1 check."""
    return MarketFacts(
        question=candidate.get("kalshi_title") or candidate.get("poly_question") or "",
        resolution_date=candidate.get("resolution_date"),
        resolution_source=candidate.get("resolution_source"),
        tie_break_rule=candidate.get("tie_break_rule"),
        category=candidate.get("category"),
        outcome_set=tuple(candidate.get("outcome_set", ("Yes", "No"))),
    )


# ─── Core gate function ───────────────────────────────────────────────────────

async def maybe_promote(
    candidate: dict,
    *,
    settings: dict,
    orderbooks: dict,
    llm_verifier,
    today_promoted_count: int,
    cooling_state: dict,
    resolution_checker: Optional[Callable] = None,
) -> PromotionResult:
    """Run all 8 gates; return first-failing reason OR promoted=True.

    Parameters
    ----------
    candidate:
        Dict with keys: kalshi_ticker, kalshi_title, poly_slug, poly_question,
        score, status, resolution_date (optional), category (optional), etc.
    settings:
        Dict with keys: AUTO_PROMOTE_ENABLED (bool), PHASE5_MAX_ORDER_USD (float),
        AUTO_PROMOTE_DAILY_CAP (int), AUTO_PROMOTE_ADVISORY_SCANS (int).
    orderbooks:
        Dict with keys 'kalshi' and 'polymarket', each a dict with 'bids' list.
    llm_verifier:
        Async callable(kalshi_q: str, poly_q: str) -> Literal["YES","NO","MAYBE"].
    today_promoted_count:
        Number of mappings already promoted today.
    cooling_state:
        Dict mapping kalshi_ticker → scan_count since first seen. If ticker is
        absent, this candidate has not been in the cooling-off period.
    resolution_checker:
        Optional callable(MarketFacts, MarketFacts) -> ResolutionMatch.
        Defaults to check_resolution_equivalence from resolution_check module.
    """
    if resolution_checker is None:
        from arbiter.mapping.resolution_check import check_resolution_equivalence
        resolution_checker = check_resolution_equivalence

    def _reject(reason: str) -> PromotionResult:
        _inc_rejection(reason)
        logger.debug("auto_promote REJECTED candidate=%s reason=%s", candidate.get("kalshi_ticker"), reason)
        return PromotionResult(promoted=False, reason=reason)

    # ── Gate 1: AUTO_PROMOTE_ENABLED ──────────────────────────────────────────
    if not settings.get("AUTO_PROMOTE_ENABLED", False):
        return _reject("auto_promote_disabled")

    # ── Gate 2: score >= 0.85 ─────────────────────────────────────────────────
    score = float(candidate.get("score", 0.0))
    if score < 0.85:
        return _reject("score_low")

    # ── Gate 3: resolution_check == IDENTICAL ─────────────────────────────────
    kalshi_facts = _candidate_to_market_facts(candidate)
    # Use poly_question as the "b" side facts for structured comparison
    poly_facts = MarketFacts(
        question=candidate.get("poly_question") or "",
        resolution_date=candidate.get("resolution_date"),
        resolution_source=candidate.get("resolution_source"),
        tie_break_rule=candidate.get("tie_break_rule"),
        category=candidate.get("category"),
        outcome_set=tuple(candidate.get("outcome_set", ("Yes", "No"))),
    )
    resolution_result = resolution_checker(kalshi_facts, poly_facts)
    if resolution_result != ResolutionMatch.IDENTICAL:
        return _reject("resolution_divergent")

    # ── Gate 4: LLM verifier == YES ───────────────────────────────────────────
    kalshi_q = candidate.get("kalshi_title", "")
    poly_q = candidate.get("poly_question", "")
    llm_result = await llm_verifier(kalshi_q, poly_q)
    if llm_result != "YES":
        return _reject("llm_no")

    # ── Gate 5: Liquidity depth ≥ PHASE5_MAX_ORDER_USD × 2 ────────────────────
    phase5_max = float(settings.get("PHASE5_MAX_ORDER_USD", 50.0))
    required_depth = phase5_max * 2.0

    kalshi_ob = orderbooks.get("kalshi", {})
    poly_ob = orderbooks.get("polymarket", {})

    kalshi_depth = _orderbook_depth_usd(kalshi_ob)
    poly_depth = _orderbook_depth_usd(poly_ob)

    if kalshi_depth < required_depth or poly_depth < required_depth:
        logger.debug(
            "auto_promote liquidity_low: kalshi=%.2f poly=%.2f required=%.2f",
            kalshi_depth, poly_depth, required_depth,
        )
        return _reject("liquidity_low")

    # ── Gate 6: resolution_date within 90 days ────────────────────────────────
    resolution_date_str = candidate.get("resolution_date")
    if resolution_date_str:
        try:
            res_date = date.fromisoformat(resolution_date_str)
            days_until = (res_date - date.today()).days
            if days_until > 90:
                return _reject("date_out_of_window")
        except (ValueError, TypeError):
            # Can't parse date → treat as out of window (safe-fail)
            return _reject("date_out_of_window")
    # If no resolution date provided, skip this gate (insufficient data)

    # ── Gate 7: daily cap ─────────────────────────────────────────────────────
    daily_cap = int(settings.get("AUTO_PROMOTE_DAILY_CAP", 20))
    if today_promoted_count >= daily_cap:
        return _reject("daily_cap")

    # ── Gate 8: cooling-off ───────────────────────────────────────────────────
    ticker = candidate.get("kalshi_ticker", "")
    advisory_scans = int(settings.get("AUTO_PROMOTE_ADVISORY_SCANS", 30))
    if ticker and ticker in cooling_state:
        scans_so_far = int(cooling_state[ticker])
        if scans_so_far < advisory_scans:
            return _reject("cooling_off")

    # ── All gates passed ──────────────────────────────────────────────────────
    logger.info("auto_promote PROMOTED candidate=%s score=%.3f", ticker, score)
    return PromotionResult(promoted=True, reason="promoted")
