"""
Fee-aware cross-platform arbitrage scanner with persistence gating.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List

import re

from ..config.settings import (
    MARKET_MAP,
    ScannerConfig,
    kalshi_order_fee,
    polymarket_order_fee,
)
from ..utils.price_store import PricePoint, PriceStore

logger = logging.getLogger("arbiter.scanner")

# ── Bracket-vs-binary mismatch filter (defense-in-depth) ─────────────
# Even if auto-discovery lets a bad mapping through, the scanner must
# never generate opportunities for structurally incompatible pairs.
_BRACKET_TICKER_RE = re.compile(r"KX[DR](?:SENATE|HOUSE)SEATS", re.IGNORECASE)


def _mapping_is_bracket_mismatch(mapping: dict) -> bool:
    """Return True if a mapping pairs a Kalshi seat-count bracket market
    with a Polymarket binary control market."""
    kalshi = str(mapping.get("kalshi", "") or mapping.get("kalshi_market_id", "") or "")
    poly = str(mapping.get("polymarket", "") or mapping.get("polymarket_slug", "") or "")
    if _BRACKET_TICKER_RE.search(kalshi):
        if any(kw in poly.lower() for kw in ("midterms", "control", "usse", "usho")):
            return True
    return False


@dataclass
class ArbitrageOpportunity:
    canonical_id: str
    description: str
    yes_platform: str
    yes_price: float
    yes_fee: float
    yes_market_id: str
    no_platform: str
    no_price: float
    no_fee: float
    no_market_id: str
    gross_edge: float
    total_fees: float
    net_edge: float
    net_edge_cents: float
    suggested_qty: int = 0
    max_profit_usd: float = 0.0
    timestamp: float = 0.0
    confidence: float = 0.0
    arb_type: str = "cross_platform"
    status: str = "candidate"
    persistence_count: int = 1
    quote_age_seconds: float = 0.0
    min_available_liquidity: float = 0.0
    mapping_status: str = "candidate"
    mapping_score: float = 0.0
    requires_manual: bool = False
    fee_breakdown: Dict[str, float] = field(default_factory=dict)
    yes_fee_rate: float = 0.0
    no_fee_rate: float = 0.0
    # Side-specific outcome metadata (populated for alerting / audit so the
    # operator sees the SPECIFIC outcome being traded, not just the market
    # category). Falls back to empty string if the collector did not emit
    # a subtitle/question.
    yes_outcome_name: str = ""
    no_outcome_name: str = ""
    yes_question: str = ""
    no_question: str = ""
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    yes_quote_age_seconds: float = 0.0
    no_quote_age_seconds: float = 0.0

    def key(self) -> str:
        return f"{self.canonical_id}:{self.yes_platform}:{self.no_platform}:{self.yes_market_id}:{self.no_market_id}"

    def to_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "description": self.description,
            "yes_platform": self.yes_platform,
            "yes_price": round(self.yes_price, 4),
            "yes_fee": round(self.yes_fee, 4),
            "yes_market_id": self.yes_market_id,
            "no_platform": self.no_platform,
            "no_price": round(self.no_price, 4),
            "no_fee": round(self.no_fee, 4),
            "no_market_id": self.no_market_id,
            "gross_edge": round(self.gross_edge, 4),
            "total_fees": round(self.total_fees, 4),
            "net_edge": round(self.net_edge, 4),
            "net_edge_cents": round(self.net_edge_cents, 2),
            "suggested_qty": self.suggested_qty,
            "max_profit_usd": round(self.max_profit_usd, 2),
            "timestamp": self.timestamp,
            "confidence": round(self.confidence, 3),
            "arb_type": self.arb_type,
            "status": self.status,
            "persistence_count": self.persistence_count,
            "quote_age_seconds": round(self.quote_age_seconds, 2),
            "min_available_liquidity": round(self.min_available_liquidity, 2),
            "mapping_status": self.mapping_status,
            "mapping_score": round(self.mapping_score, 3),
            "requires_manual": self.requires_manual,
            "fee_breakdown": {name: round(value, 4) for name, value in self.fee_breakdown.items()},
            "yes_fee_rate": round(self.yes_fee_rate, 6),
            "no_fee_rate": round(self.no_fee_rate, 6),
            "yes_outcome_name": self.yes_outcome_name,
            "no_outcome_name": self.no_outcome_name,
            "yes_question": self.yes_question,
            "no_question": self.no_question,
            "yes_bid": round(self.yes_bid, 4),
            "yes_ask": round(self.yes_ask, 4),
            "no_bid": round(self.no_bid, 4),
            "no_ask": round(self.no_ask, 4),
            "yes_quote_age_seconds": round(self.yes_quote_age_seconds, 2),
            "no_quote_age_seconds": round(self.no_quote_age_seconds, 2),
        }

    def to_audit_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "description": self.description,
            "yes_platform": self.yes_platform,
            "yes_price": self.yes_price,
            "yes_fee": self.yes_fee,
            "yes_market_id": self.yes_market_id,
            "no_platform": self.no_platform,
            "no_price": self.no_price,
            "no_fee": self.no_fee,
            "no_market_id": self.no_market_id,
            "gross_edge": self.gross_edge,
            "total_fees": self.total_fees,
            "net_edge": self.net_edge,
            "net_edge_cents": self.net_edge_cents,
            "suggested_qty": self.suggested_qty,
            "max_profit_usd": self.max_profit_usd,
            "timestamp": self.timestamp,
            "confidence": self.confidence,
            "arb_type": self.arb_type,
            "status": self.status,
            "persistence_count": self.persistence_count,
            "quote_age_seconds": self.quote_age_seconds,
            "min_available_liquidity": self.min_available_liquidity,
            "mapping_status": self.mapping_status,
            "mapping_score": self.mapping_score,
            "requires_manual": self.requires_manual,
            "fee_breakdown": dict(self.fee_breakdown),
            "yes_fee_rate": self.yes_fee_rate,
            "no_fee_rate": self.no_fee_rate,
            "yes_outcome_name": self.yes_outcome_name,
            "no_outcome_name": self.no_outcome_name,
            "yes_question": self.yes_question,
            "no_question": self.no_question,
            "yes_bid": self.yes_bid,
            "yes_ask": self.yes_ask,
            "no_bid": self.no_bid,
            "no_ask": self.no_ask,
            "yes_quote_age_seconds": self.yes_quote_age_seconds,
            "no_quote_age_seconds": self.no_quote_age_seconds,
        }


def _flipped_view(price_point: PricePoint) -> PricePoint:
    """Return a shallow PricePoint with YES/NO sides swapped for polarity-flipped pairs.

    When a mapping has ``polarity_flipped=True`` the Polymarket YES outcome
    corresponds to the Kalshi NO outcome (and vice versa). Rather than
    branch every price comparison in the scanner, we present a swapped view
    of the Polymarket price point. The original PricePoint is not mutated.
    """
    return PricePoint(
        canonical_id=price_point.canonical_id,
        platform=price_point.platform,
        raw_market_id=price_point.raw_market_id,
        yes_price=price_point.no_price,
        no_price=price_point.yes_price,
        yes_volume=price_point.no_volume,
        no_volume=price_point.yes_volume,
        timestamp=price_point.timestamp,
        fee_rate=price_point.fee_rate,
        mapping_status=price_point.mapping_status,
        mapping_score=price_point.mapping_score,
        yes_bid=price_point.no_bid,
        yes_ask=price_point.no_ask,
        no_bid=price_point.yes_bid,
        no_ask=price_point.yes_ask,
        yes_market_id=price_point.no_market_id,
        no_market_id=price_point.yes_market_id,
        metadata=price_point.metadata,
    )


def extract_outcome_metadata(price_point: PricePoint, side: str) -> tuple[str, str]:
    """Return ``(outcome_name, question_text)`` for a side ("yes" or "no").

    The "outcome name" is the SPECIFIC choice being traded (e.g.
    "Democrats", "Republicans") — distinct from the canonical market
    category (e.g. "U.S Senate Midterm Winner"). Used by the Telegram
    alert so operators see what they're actually buying. Returns empty
    strings if the upstream collector did not populate metadata, in
    which case the alert gate will reject the opportunity.
    """
    md = price_point.metadata or {}
    if price_point.platform == "kalshi":
        # Kalshi event = umbrella, market = specific outcome. ``yes_sub_title``
        # / ``no_sub_title`` carry the per-side outcome name; ``market_title``
        # is the full question (e.g. "Will Democrats win Senate Majority…?").
        if side == "yes":
            outcome = str(md.get("yes_sub_title") or "").strip()
        else:
            outcome = str(md.get("no_sub_title") or "").strip()
        question = str(md.get("market_title") or "").strip()
        if not outcome:
            outcome = question
        return outcome, question
    if price_point.platform == "polymarket":
        # Polymarket binary markets encode the specific outcome inside the
        # ``question`` field (e.g. "Will Democrats win Senate Majority in 2026?")
        question = str(md.get("question") or "").strip()
        return question, question
    return "", ""


def compute_fee(platform: str, price: float, quantity: int, fee_rate: float = 0.0) -> float:
    if quantity <= 0:
        return 0.0
    if platform == "kalshi":
        return kalshi_order_fee(price, quantity=quantity)
    if platform == "polymarket":
        return polymarket_order_fee(price, quantity=quantity, fee_rate=fee_rate or None, category="politics")
    return 0.0


class ArbitrageScanner:
    """
    Scans across platform pairs, but only promotes opportunities after they
    survive multiple scans and pass stale-data and liquidity checks.
    """

    def __init__(self, config: ScannerConfig, price_store: PriceStore,
                 balance_provider=None):
        self.config = config
        self.store = price_store
        self._balance_provider = balance_provider  # callable returning {platform: balance}
        self._running = False
        self._paused = False
        self._opportunities: List[ArbitrageOpportunity] = []
        self._subscribers: List[asyncio.Queue] = []
        self._scan_count = 0
        self._last_scan_time = 0.0
        self._published_count = 0
        self._stable_keys: Dict[str, int] = {}
        self._recent_publish_time: Dict[str, float] = {}
        self._history: Deque[dict] = deque(maxlen=240)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        return queue

    async def scan_once(self) -> List[ArbitrageOpportunity]:
        opportunities: List[ArbitrageOpportunity] = []
        seen_keys: set[str] = set()
        self._scan_count += 1
        scan_start = time.time()

        for canonical_id, mapping in MARKET_MAP.items():
            if mapping.get("status") != "confirmed":
                continue

            # Defense-in-depth: skip structurally incompatible mappings
            if _mapping_is_bracket_mismatch(mapping):
                continue

            prices = await self.store.get_all_for_market(canonical_id)
            if len(prices) < 2:
                continue

            description = str(mapping.get("description", canonical_id))
            platforms = list(prices.keys())
            mapping_status = str(mapping.get("status", "candidate"))
            mapping_score = float(mapping.get("mapping_score", 0.0))
            # Polarity flip: when True, Polymarket YES = Kalshi NO. Present a
            # swapped view of the Polymarket price point so the rest of the
            # scanner pipeline can treat it identically to a normal mapping.
            polarity_flipped = bool(mapping.get("polarity_flipped", False))
            if polarity_flipped and "polymarket" in prices:
                prices = dict(prices)
                prices["polymarket"] = _flipped_view(prices["polymarket"])

            for yes_platform in platforms:
                for no_platform in platforms:
                    if yes_platform == no_platform:
                        continue

                    yes_price_point = prices[yes_platform]
                    no_price_point = prices[no_platform]
                    opportunity = self._build_cross_platform_opportunity(
                        canonical_id,
                        description,
                        mapping_status,
                        mapping_score,
                        yes_price_point,
                        no_price_point,
                    )
                    if not opportunity:
                        continue

                    seen_keys.add(opportunity.key())
                    opportunity.persistence_count = self._stable_keys.get(opportunity.key(), 0) + 1
                    self._stable_keys[opportunity.key()] = opportunity.persistence_count
                    opportunity.status = self._resolve_status(opportunity, mapping)
                    opportunities.append(opportunity)

                    if opportunity.status in {"tradable", "manual"} and self._should_publish(opportunity):
                        self._published_count += 1
                        self._recent_publish_time[opportunity.key()] = opportunity.timestamp
                        for subscriber in list(self._subscribers):
                            try:
                                subscriber.put_nowait(opportunity)
                            except asyncio.QueueFull:
                                logger.debug("Skipping slow opportunity subscriber")

        stale_keys = {key for key in self._stable_keys if key not in seen_keys}
        for stale_key in stale_keys:
            self._stable_keys.pop(stale_key, None)
        # Drop publish-time entries for opportunities we no longer see, so the
        # cache can't grow without bound across long-running scanner sessions.
        publish_stale = {
            key for key in self._recent_publish_time if key not in seen_keys
        }
        for stale_key in publish_stale:
            self._recent_publish_time.pop(stale_key, None)

        opportunities.sort(key=lambda item: (item.status == "tradable", item.net_edge_cents, item.confidence), reverse=True)
        self._opportunities = opportunities
        self._last_scan_time = time.time() - scan_start
        self._history.append(
            {
                "timestamp": time.time(),
                "scan_ms": round(self._last_scan_time * 1000.0, 2),
                "best_edge_cents": opportunities[0].net_edge_cents if opportunities else 0.0,
                "active": len(opportunities),
                "tradable": len([item for item in opportunities if item.status == "tradable"]),
                "manual": len([item for item in opportunities if item.status == "manual"]),
            }
        )

        if opportunities:
            best = opportunities[0]
            logger.info(
                "Scan #%s (%sms): %s opportunities, best=%s %s->%s net=%.2f¢ status=%s",
                self._scan_count,
                int(self._last_scan_time * 1000),
                len(opportunities),
                best.canonical_id,
                best.yes_platform,
                best.no_platform,
                best.net_edge_cents,
                best.status,
            )
        elif self._scan_count % 60 == 0:
            logger.info("Scan #%s: no opportunities above %.2f¢", self._scan_count, self.config.min_edge_cents)

        return opportunities

    def _build_cross_platform_opportunity(
        self,
        canonical_id: str,
        description: str,
        mapping_status: str,
        mapping_score: float,
        yes_price_point: PricePoint,
        no_price_point: PricePoint,
    ) -> ArbitrageOpportunity | None:
        # Use executable ASK prices (what you'd actually pay to enter)
        # Fall back to mid-quote only if ASK is unavailable
        yes_price = yes_price_point.yes_ask if yes_price_point.yes_ask > 0 else yes_price_point.yes_price
        no_price = no_price_point.no_ask if no_price_point.no_ask > 0 else no_price_point.no_price
        if yes_price <= 0 or no_price <= 0:
            return None

        # ── Non-binary market guard ───────────────────────────────────
        # A valid binary market has yes_price + no_price ≈ $1.00 on each
        # platform independently. If a platform's own yes+no deviates by
        # more than 15¢ from $1.00, it's likely a multi-outcome market
        # (e.g., "Who will win?" with many candidates) where buying YES
        # on one candidate + NO on another candidate is NOT a riskless arb.
        MAX_BINARY_DEVIATION = 0.15
        for pp, label in [(yes_price_point, "YES-source"), (no_price_point, "NO-source")]:
            platform_sum = pp.yes_price + pp.no_price
            if platform_sum > 0 and abs(platform_sum - 1.0) > MAX_BINARY_DEVIATION:
                logger.debug(
                    "Non-binary market skip: %s %s (%s) yes+no=$%.2f deviates from $1.00 by %.0f¢",
                    canonical_id, label, pp.platform, platform_sum, abs(platform_sum - 1.0) * 100,
                )
                return None

        gross_edge = 1.0 - yes_price - no_price
        if gross_edge <= 0:
            return None

        suggested_qty = self._compute_position_size(yes_price_point, no_price_point, yes_price, no_price)
        if suggested_qty <= 0:
            return None

        yes_fee_total = compute_fee(
            yes_price_point.platform,
            yes_price,
            suggested_qty,
            fee_rate=yes_price_point.fee_rate,
        )
        no_fee_total = compute_fee(
            no_price_point.platform,
            no_price,
            suggested_qty,
            fee_rate=no_price_point.fee_rate,
        )
        total_fees = (yes_fee_total + no_fee_total) / suggested_qty
        net_edge = gross_edge - total_fees
        net_edge_cents = net_edge * 100.0
        if net_edge_cents < self.config.min_edge_cents:
            return None

        quote_age_seconds = max(yes_price_point.age_seconds, no_price_point.age_seconds)
        min_available_liquidity = min(yes_price_point.yes_volume, no_price_point.no_volume)
        requires_manual = False
        confidence = self._compute_confidence(
            quote_age_seconds=quote_age_seconds,
            min_available_liquidity=min_available_liquidity,
            mapping_score=mapping_score,
            requires_manual=requires_manual,
        )

        yes_market_id = yes_price_point.yes_market_id or yes_price_point.raw_market_id
        no_market_id = no_price_point.no_market_id or no_price_point.raw_market_id
        yes_outcome_name, yes_question_text = extract_outcome_metadata(yes_price_point, "yes")
        no_outcome_name, no_question_text = extract_outcome_metadata(no_price_point, "no")
        return ArbitrageOpportunity(
            canonical_id=canonical_id,
            description=description,
            yes_platform=yes_price_point.platform,
            yes_price=yes_price,
            yes_fee=yes_fee_total / suggested_qty,
            yes_market_id=yes_market_id,
            no_platform=no_price_point.platform,
            no_price=no_price,
            no_fee=no_fee_total / suggested_qty,
            no_market_id=no_market_id,
            gross_edge=gross_edge,
            total_fees=total_fees,
            net_edge=net_edge,
            net_edge_cents=net_edge_cents,
            suggested_qty=suggested_qty,
            max_profit_usd=round(net_edge * suggested_qty, 4),
            timestamp=time.time(),
            confidence=confidence,
            status="candidate",
            quote_age_seconds=quote_age_seconds,
            min_available_liquidity=min_available_liquidity,
            mapping_status=mapping_status,
            mapping_score=mapping_score,
            requires_manual=requires_manual,
            fee_breakdown={
                "yes_total_fee": yes_fee_total,
                "no_total_fee": no_fee_total,
            },
            yes_fee_rate=yes_price_point.fee_rate,
            no_fee_rate=no_price_point.fee_rate,
            yes_outcome_name=yes_outcome_name,
            no_outcome_name=no_outcome_name,
            yes_question=yes_question_text,
            no_question=no_question_text,
            yes_bid=float(yes_price_point.yes_bid or 0.0),
            yes_ask=float(yes_price_point.yes_ask or 0.0),
            no_bid=float(no_price_point.no_bid or 0.0),
            no_ask=float(no_price_point.no_ask or 0.0),
            yes_quote_age_seconds=float(yes_price_point.age_seconds),
            no_quote_age_seconds=float(no_price_point.age_seconds),
        )

    def _resolve_status(self, opportunity: ArbitrageOpportunity, mapping: dict) -> str:
        # ══════════════════════════════════════════════════════════════
        # SAFETY-CRITICAL: Only CONFIRMED mappings with allow_auto_trade
        # can reach "tradable". Auto-discovered mappings MUST stay in
        # "review" regardless of edge size, because the auto-discovery
        # pipeline produces false matches (e.g., pairing a soccer draw
        # market with an NBA player prop). With real money at stake,
        # every mapping must be operator-verified before trading.
        # ══════════════════════════════════════════════════════════════
        is_confirmed = opportunity.mapping_status == "confirmed"
        is_auto_tradable = mapping.get("allow_auto_trade", False)

        # Every gate logs the reason it fired so the dashboard / log search
        # can answer "why didn't this opportunity trade?" without re-running it.
        if not is_confirmed:
            logger.debug(
                "scanner.skip canonical=%s reason=mapping_unconfirmed status=%s",
                opportunity.canonical_id, opportunity.mapping_status,
            )
            return "review"
        if not is_auto_tradable:
            logger.debug(
                "scanner.skip canonical=%s reason=mapping_not_auto_tradable",
                opportunity.canonical_id,
            )
            return "review"
        # Polarity-flipped mappings are valid (the math is correct) but the
        # operator must explicitly enable them via env flag. Default is
        # "manual" so the dashboard surfaces the edge without trading.
        if mapping.get("polarity_flipped"):
            allow_flipped = os.getenv("ENABLE_POLARITY_FLIPPED_AUTO_TRADE", "false").strip().lower() in {"1", "true", "yes", "on"}
            if not allow_flipped:
                logger.debug(
                    "scanner.skip canonical=%s reason=polarity_flipped_manual_only",
                    opportunity.canonical_id,
                )
                return "manual"
        res_status = str(mapping.get("resolution_match_status", "pending_operator_review")).lower()
        if res_status != "identical":
            logger.debug(
                "scanner.skip canonical=%s reason=resolution_match_status status=%s",
                opportunity.canonical_id, res_status,
            )
            return "review"
        if opportunity.quote_age_seconds > self.config.max_quote_age_seconds:
            logger.info(
                "scanner.skip canonical=%s reason=stale_quote age=%.1fs max=%.1fs",
                opportunity.canonical_id,
                opportunity.quote_age_seconds,
                self.config.max_quote_age_seconds,
            )
            return "stale"
        if opportunity.min_available_liquidity < self.config.min_liquidity:
            logger.info(
                "scanner.skip canonical=%s reason=illiquid liq=%.2f min=%.2f",
                opportunity.canonical_id,
                opportunity.min_available_liquidity,
                self.config.min_liquidity,
            )
            return "illiquid"
        if opportunity.persistence_count < self.config.persistence_scans:
            logger.debug(
                "scanner.skip canonical=%s reason=insufficient_persistence count=%d need=%d",
                opportunity.canonical_id,
                opportunity.persistence_count,
                self.config.persistence_scans,
            )
            return "candidate"
        if opportunity.requires_manual:
            return "manual"
        if opportunity.confidence < self.config.confidence_threshold:
            logger.debug(
                "scanner.skip canonical=%s reason=low_confidence confidence=%.2f threshold=%.2f",
                opportunity.canonical_id,
                opportunity.confidence,
                self.config.confidence_threshold,
            )
            return "candidate"
        # ── Net edge floor re-check (defense-in-depth) ────────────────
        # Catches stale-price drift between the time the opportunity was
        # built and now, and prevents trading on rounding-error edges.
        if opportunity.net_edge_cents < self.config.min_edge_cents:
            logger.debug(
                "scanner.skip canonical=%s reason=net_edge_below_floor net=%.2f¢ min=%.2f¢",
                opportunity.canonical_id,
                opportunity.net_edge_cents,
                self.config.min_edge_cents,
            )
            return "candidate"
        return "tradable"

    def _should_publish(self, opportunity: ArbitrageOpportunity) -> bool:
        last_published = self._recent_publish_time.get(opportunity.key(), 0.0)
        if not last_published:
            return True
        if opportunity.timestamp - last_published >= 30.0:
            return True
        return False

    def _compute_position_size(
        self,
        yes_price_point: PricePoint,
        no_price_point: PricePoint,
        yes_price: float,
        no_price: float,
    ) -> int:
        cost_per_pair = max(yes_price + no_price, 0.01)

        # Balance-proportioned sizing: use min of per-platform balance caps
        # so we never try to spend more than either platform has
        max_cap = self.config.max_position_usd
        if self._balance_provider:
            try:
                balances = self._balance_provider()
                yes_bal = balances.get(yes_price_point.platform, max_cap)
                no_bal = balances.get(no_price_point.platform, max_cap)
                # Cap per-leg spend to platform balance (leave 10% reserve)
                RESERVE_FRACTION = 0.10
                yes_cap = max(yes_bal * (1 - RESERVE_FRACTION), 0) / max(yes_price, 0.01)
                no_cap = max(no_bal * (1 - RESERVE_FRACTION), 0) / max(no_price, 0.01)
                balance_limited = int(min(yes_cap, no_cap))
            except Exception as exc:
                # Balance lookup is best-effort: fall back to the static cap so
                # a flaky balance source doesn't pause the scanner. We log the
                # reason once per scan so operators can spot persistent issues.
                logger.warning(
                    "scanner.balance_provider.error platform_yes=%s platform_no=%s err=%s",
                    yes_price_point.platform, no_price_point.platform, exc,
                )
                balance_limited = int(max_cap / cost_per_pair)
        else:
            balance_limited = int(max_cap / cost_per_pair)

        config_limited = int(max_cap / cost_per_pair)
        liquidity_limited = int(max(min(yes_price_point.yes_volume, no_price_point.no_volume), 1))
        size = max(1, min(config_limited, liquidity_limited, balance_limited))
        return max(size, 0)

    def _compute_confidence(
        self,
        quote_age_seconds: float,
        min_available_liquidity: float,
        mapping_score: float,
        requires_manual: bool,
    ) -> float:
        freshness_score = max(0.0, 1.0 - (quote_age_seconds / max(self.config.max_quote_age_seconds, 1.0)))
        liquidity_score = min(1.0, min_available_liquidity / max(self.config.min_liquidity * 4.0, 1.0))
        manual_penalty = 0.1 if requires_manual else 0.0
        confidence = (0.45 * freshness_score) + (0.35 * liquidity_score) + (0.20 * max(mapping_score, 0.25))
        return max(0.0, min(confidence - manual_penalty, 1.0))

    @property
    def current_opportunities(self) -> List[ArbitrageOpportunity]:
        return self._opportunities

    @property
    def history(self) -> List[dict]:
        return list(self._history)

    @property
    def stats(self) -> dict:
        tradable = len([item for item in self._opportunities if item.status == "tradable"])
        manual = len([item for item in self._opportunities if item.status == "manual"])
        return {
            "scan_count": self._scan_count,
            "last_scan_ms": round(self._last_scan_time * 1000.0, 1),
            "active_opportunities": len(self._opportunities),
            "tradable_opportunities": tradable,
            "manual_opportunities": manual,
            "best_edge_cents": round(self._opportunities[0].net_edge_cents, 2) if self._opportunities else 0.0,
            "persistence_scans": self.config.persistence_scans,
            "max_quote_age_seconds": self.config.max_quote_age_seconds,
            "min_liquidity": self.config.min_liquidity,
            "confidence_threshold": self.config.confidence_threshold,
            "published": self._published_count,
            "paused": self._paused,
        }

    def pause(self) -> None:
        """Pause scanning. The scanner loop stays alive but skips scan_once()."""
        self._paused = True
        logger.info("Scanner PAUSED by operator")

    def resume(self) -> None:
        """Resume scanning after a pause."""
        self._paused = False
        logger.info("Scanner RESUMED by operator")

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def run(self):
        self._running = True
        logger.info(
            "Arbitrage scanner started (interval=%ss, min_edge=%.2f¢, persistence=%s, dry_run=%s)",
            self.config.scan_interval,
            self.config.min_edge_cents,
            self.config.persistence_scans,
            self.config.dry_run,
        )

        while self._running:
            try:
                if not self._paused:
                    await self.scan_once()
                await asyncio.sleep(self.config.scan_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scanner error: %s", exc)
                await asyncio.sleep(2)

        logger.info("Arbitrage scanner stopped")

    async def stop(self):
        self._running = False
