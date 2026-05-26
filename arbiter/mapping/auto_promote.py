"""
Auto-promote gate — 8-condition check before promoting a candidate mapping.

A mapping is only promoted to allow_auto_trade=True if all 8 gates pass.
Returns the first-failing reason or promoted=True.

Conditions (in order — first failing wins):
1. AUTO_PROMOTE_ENABLED=true
2. score >= AUTO_PROMOTE_MIN_SCORE  (env var; default 0.85 if unset)
3. Candidate was structurally parsed and matched
4. resolution_check(...) == IDENTICAL
5. LLM verifier returns YES
6. Both orderbooks have combined (bid + ask) depth >= PHASE5_MAX_ORDER_USD
7. resolution_date within 90 days
8. today_promoted_count < AUTO_PROMOTE_DAILY_CAP (default 20)
9. Cooling-off: first AUTO_PROMOTE_ADVISORY_SCANS after first-see are advisory-only

Tunable thresholds (read from env when not present in `settings`):
  AUTO_PROMOTE_MIN_SCORE          — Gate 2 minimum score (default 0.85)
  AUTO_PROMOTE_MAYBE_MIN_SCORE    — legacy setting, ignored; MAYBE fails closed
  AUTO_PROMOTE_PENDING_SCORE_BUMP — legacy setting, ignored; PENDING fails closed

Metrics: increments auto_promote_rejections_total{reason=...} counter when available.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Optional

from arbiter.mapping.resolution_check import MarketFacts, ResolutionMatch

logger = logging.getLogger("arbiter.mapping.auto_promote")


def _setting(settings: dict, *keys: str, default):
    for key in keys:
        if key in settings:
            return settings[key]
    return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# Per-category gate-2 floor. Sports markets are tight (parsed structurally,
# almost always score 0.85+); non-sports often score 0.55–0.75 because the
# structured-metadata gate is inherently looser for political/economic/crypto
# questions. We lower the score floor for those categories — the resolution
# IDENTICAL gate (#4) and the LLM YES gate (#5) still have to pass, so we are
# not relaxing safety, only allowing the better-judgment gates to do the work.
DEFAULT_CATEGORY_MIN_SCORE: dict[str, float] = {
    "sports": 0.85,
    "politics": 0.65,
    "crypto": 0.65,
    "economics": 0.65,
    "entertainment": 0.65,
    "finance": 0.65,
}


def _category_min_score(
    settings: dict,
    candidate: dict,
    base_default: float,
) -> float:
    """Return the score floor for this candidate's category.

    Lookup order (first hit wins):
      1. settings["AUTO_PROMOTE_MIN_SCORE_BY_CATEGORY"][category]
      2. DEFAULT_CATEGORY_MIN_SCORE[category]
      3. base_default  (the flat AUTO_PROMOTE_MIN_SCORE)
    """
    category = str(
        candidate.get("category")
        or candidate.get("kalshi_category")
        or ""
    ).strip().lower()
    if not category:
        return base_default
    by_cat = _setting(
        settings,
        "AUTO_PROMOTE_MIN_SCORE_BY_CATEGORY",
        "auto_promote_min_score_by_category",
        default=None,
    )
    if isinstance(by_cat, dict) and category in by_cat:
        try:
            return float(by_cat[category])
        except (TypeError, ValueError):
            pass
    if category in DEFAULT_CATEGORY_MIN_SCORE:
        return DEFAULT_CATEGORY_MIN_SCORE[category]
    return base_default


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
    """Result of the auto-promote gate evaluation.

    ``match_type`` records WHICH evidence path the candidate cleared:
      * ``"structural"`` — deterministic category-specific parser proved
        same event / outcome / date / type / source. Safe to flip to
        ``confirmed + allow_auto_trade=True`` without operator review.
      * ``"semantic"`` — only the high-confidence semantic similarity
        bypass cleared (no structural parser fired). Live-money trading
        from semantic-only matches has historically created bad arbs
        (winner vs spread, different settlement source, polarity flips
        the LLM missed). Callers MUST route these to operator review
        (``status="review"``, ``allow_auto_trade=False``) — never to
        ``confirmed+allow_auto_trade=True``. See ``apply_promotion`` for
        the caller-side cage.
      * ``""`` (empty) — gate failed; ``promoted=False``.
    """
    promoted: bool
    reason: str  # one of: "auto_promote_disabled", "score_low", "resolution_divergent",
                 #       "structural_unverified", "resolution_pending", "llm_no", "llm_maybe",
                 #       "liquidity_low", "date_out_of_window",
                 #       "daily_cap", "cooling_off", "promoted"
    match_type: str = ""

    @property
    def is_structural(self) -> bool:
        return self.promoted and self.match_type == "structural"

    @property
    def is_semantic(self) -> bool:
        return self.promoted and self.match_type == "semantic"


def apply_promotion(candidate: dict, result: PromotionResult) -> None:
    """Mutate a candidate dict in place per the cage policy.

    Structural matches go straight to ``confirmed+allow_auto_trade=True``.
    Semantic-only matches go to ``review+allow_auto_trade=False`` so an
    operator can inspect the pair before any live-money trade fires
    against it.  Failed promotions get a ``review_note`` documenting the
    reason but do not flip status (caller decides whether to leave them
    at ``candidate`` or surface to ``review`` based on the reason).

    Centralizing this in one helper means every promotion site uses the
    same cage — no caller can accidentally promote a semantic-only
    candidate to live-tradable.
    """
    if not result.promoted:
        candidate["allow_auto_trade"] = False
        candidate["review_note"] = f"Auto-promote gate: {result.reason}"
        return
    if result.is_structural:
        candidate["status"] = "confirmed"
        candidate["allow_auto_trade"] = True
        candidate["promotion_match_type"] = "structural"
        candidate["resolution_match_status"] = "identical"
        candidate["review_note"] = ""
        return
    # semantic-only — SAFETY CAGE: route to review, never live-tradable.
    candidate["status"] = "review"
    candidate["allow_auto_trade"] = False
    candidate["promotion_match_type"] = "semantic"
    # Surface why an operator should look — gate passed but evidence is
    # similarity-only, not structural proof.
    candidate["review_note"] = (
        "Semantic-only auto-promote (score ≥ SEMANTIC_PROMOTE_MIN_SCORE) "
        "cleared resolution + LLM gates but lacks structural parser proof. "
        "Operator must verify event identity before flipping allow_auto_trade."
    )


# ─── Depth calculation ────────────────────────────────────────────────────────

def _orderbook_depth_usd(orderbook: dict) -> float:
    """Sum USD depth across bids and asks: sum(price * qty) over all levels.

    Counts both sides (bid + ask/offer) because for cross-platform arb we may
    BUY at the ask on one venue and SELL at the bid on the other — the gate is
    a coarse "is this market actually trading" check, not a directional one.
    """
    total = 0.0
    for side_key in ("bids", "asks", "offers"):
        for level in orderbook.get(side_key, []) or []:
            try:
                px = float(level.get("px", 0) or 0)
                qty = float(level.get("qty", 0) or 0)
                total += px * qty
            except (TypeError, ValueError, AttributeError):
                continue
    return total


# ─── Resolution-facts extractor ───────────────────────────────────────────────

def _candidate_to_market_facts(candidate: dict, side: str) -> MarketFacts:
    """Build side-specific MarketFacts from a candidate dict for Layer 1 check."""
    prefix = f"{side}_"
    question = candidate.get("kalshi_title") if side == "kalshi" else candidate.get("poly_question")
    return MarketFacts(
        question=question or "",
        resolution_date=candidate.get(f"{prefix}resolution_date") or candidate.get("resolution_date"),
        resolution_source=candidate.get(f"{prefix}resolution_source") or candidate.get("resolution_source"),
        tie_break_rule=candidate.get(f"{prefix}tie_break_rule") or candidate.get("tie_break_rule"),
        category=candidate.get(f"{side}_category") or candidate.get("category"),
        outcome_set=tuple(
            candidate.get(f"{prefix}outcome_set")
            or candidate.get("outcome_set", ("Yes", "No"))
        ),
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
    if not _setting(settings, "AUTO_PROMOTE_ENABLED", "auto_promote_enabled", default=False):
        return _reject("auto_promote_disabled")

    # ── Gate 2: score >= configured threshold (per-category) ──────────────────
    # Settings dict (operator-runtime) wins; falls back to AUTO_PROMOTE_MIN_SCORE
    # env var (containers using env_file but no settings store), else 0.85.
    # Per-category floors (sports stays strict at 0.85, politics/crypto/economics
    # /entertainment/finance relax to 0.65) live in DEFAULT_CATEGORY_MIN_SCORE
    # and can be overridden via settings["AUTO_PROMOTE_MIN_SCORE_BY_CATEGORY"].
    base_min_score = float(_setting(
        settings, "AUTO_PROMOTE_MIN_SCORE", "auto_promote_min_score",
        default=_env_float("AUTO_PROMOTE_MIN_SCORE", 0.85),
    ))
    min_score = _category_min_score(settings, candidate, base_min_score)
    score = float(candidate.get("score", 0.0))
    if score < min_score:
        return _reject("score_low")

    # ── Gate 3: structural match OR high-confidence semantic match ──────────
    # Primary path: deterministic category-specific parser proves same
    # event/outcome/date/type/source.
    # Bypass path: for categories without parsers, allow promotion when
    # similarity_score >= SEMANTIC_PROMOTE_MIN_SCORE (default 0.92).
    # The bypass STILL requires Gates 4+5 (resolution IDENTICAL + LLM YES),
    # which provide equivalent safety to structural matching.
    semantic_min = float(_setting(
        settings, "SEMANTIC_PROMOTE_MIN_SCORE", "semantic_promote_min_score",
        default=_env_float("SEMANTIC_PROMOTE_MIN_SCORE", 0.92),
    ))
    is_structural = bool(candidate.get("structural_match"))
    is_semantic = score >= semantic_min and not is_structural
    if not is_structural and not is_semantic:
        return _reject("structural_unverified")

    # ── Gate 4: resolution_check must return IDENTICAL ───────────────────────
    # Live-money auto-trading requires confirmed identical resolution criteria.
    # DIVERGENT = confirmed mismatch; PENDING = insufficient structured data.
    # Both fail closed. LLM text equivalence is a second opinion, not a
    # substitute for structured resolution evidence.
    kalshi_facts = _candidate_to_market_facts(candidate, "kalshi")
    poly_facts = _candidate_to_market_facts(candidate, "polymarket")
    resolution_result = resolution_checker(kalshi_facts, poly_facts)
    if resolution_result == ResolutionMatch.DIVERGENT:
        return _reject("resolution_divergent")
    if resolution_result != ResolutionMatch.IDENTICAL:
        return _reject("resolution_pending")

    category = str(candidate.get("category") or candidate.get("kalshi_category") or "").lower()
    if category == "sports":
        polarity = str(candidate.get("polarity") or "").lower()
        if polarity == "flipped":
            return _reject("polarity_flipped")
        if polarity != "same":
            return _reject("polarity_unconfirmed")

    # ── Gate 5: LLM verifier must return YES ────────────────────────────────
    # MAYBE is ambiguity, not confirmation. Any non-YES verdict fails closed.
    kalshi_q = candidate.get("kalshi_verification_text") or candidate.get("kalshi_title", "")
    poly_q = (
        candidate.get("polymarket_verification_text")
        or candidate.get("poly_verification_text")
        or candidate.get("poly_question", "")
    )
    llm_result = await llm_verifier(kalshi_q, poly_q)
    if llm_result == "NO":
        return _reject("llm_no")
    if llm_result != "YES":
        return _reject("llm_maybe")

    # ── Gate 6: Liquidity depth ≥ PHASE5_MAX_ORDER_USD (combined bid+ask) ─────
    phase5_max = float(_setting(
        settings, "PHASE5_MAX_ORDER_USD", "phase5_max_order_usd",
        default=_env_float("PHASE5_MAX_ORDER_USD", 50.0),
    ))
    required_depth = phase5_max

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

    # ── Gate 7: resolution_date active and within AUTO_PROMOTE_MAX_DAYS ───────
    max_days = int(_setting(
        settings, "AUTO_PROMOTE_MAX_DAYS", "auto_promote_max_days",
        default=int(_env_float("AUTO_PROMOTE_MAX_DAYS", 90.0)),
    ))
    resolution_date_str = candidate.get("resolution_date") or candidate.get("kalshi_resolution_date") or candidate.get("polymarket_resolution_date")
    if resolution_date_str:
        try:
            res_date = date.fromisoformat(resolution_date_str)
            days_until = (res_date - date.today()).days
            if days_until < 0 or days_until > max_days:
                return _reject("date_out_of_window")
        except (ValueError, TypeError):
            # Can't parse date → treat as out of window (safe-fail)
            return _reject("date_out_of_window")
    # If no resolution date provided, skip this gate (insufficient data)

    # ── Gate 8: daily cap ─────────────────────────────────────────────────────
    daily_cap = int(_setting(
        settings, "AUTO_PROMOTE_DAILY_CAP", "auto_promote_daily_cap",
        default=int(_env_float("AUTO_PROMOTE_DAILY_CAP", 20.0)),
    ))
    if today_promoted_count >= daily_cap:
        return _reject("daily_cap")

    # ── Gate 9: cooling-off ───────────────────────────────────────────────────
    ticker = candidate.get("kalshi_ticker", "")
    advisory_scans = int(_setting(
        settings, "AUTO_PROMOTE_ADVISORY_SCANS", "auto_promote_advisory_scans",
        default=int(_env_float("AUTO_PROMOTE_ADVISORY_SCANS", 30.0)),
    ))
    if ticker and ticker in cooling_state:
        scans_so_far = int(cooling_state[ticker])
        if scans_so_far < advisory_scans:
            return _reject("cooling_off")

    # ── All gates passed ──────────────────────────────────────────────────────
    match_type = "structural" if is_structural else "semantic"
    logger.info("auto_promote PROMOTED candidate=%s score=%.3f match=%s", ticker, score, match_type)
    return PromotionResult(promoted=True, reason="promoted", match_type=match_type)
