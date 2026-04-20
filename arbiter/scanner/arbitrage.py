"""
Fee-aware cross-platform arbitrage scanner with persistence gating.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List

from ..config.settings import (
    MARKET_MAP,
    ScannerConfig,
    kalshi_order_fee,
    polymarket_order_fee,
)
from ..utils.price_store import PricePoint, PriceStore

logger = logging.getLogger("arbiter.scanner")


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
        }


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

    def __init__(self, config: ScannerConfig, price_store: PriceStore):
        self.config = config
        self.store = price_store
        self._running = False
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
            if mapping.get("status") == "disabled":
                continue

            prices = await self.store.get_all_for_market(canonical_id)
            if len(prices) < 2:
                continue

            description = str(mapping.get("description", canonical_id))
            platforms = list(prices.keys())
            mapping_status = str(mapping.get("status", "candidate"))
            mapping_score = float(mapping.get("mapping_score", 0.0))

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
        yes_price = yes_price_point.yes_price
        no_price = no_price_point.no_price
        if yes_price <= 0 or no_price <= 0:
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
        )

    def _resolve_status(self, opportunity: ArbitrageOpportunity, mapping: dict) -> str:
        if opportunity.mapping_status != "confirmed":
            return "review"
        if opportunity.quote_age_seconds > self.config.max_quote_age_seconds:
            return "stale"
        if opportunity.min_available_liquidity < self.config.min_liquidity:
            return "illiquid"
        if opportunity.persistence_count < self.config.persistence_scans:
            return "candidate"
        if opportunity.requires_manual:
            return "manual"
        if not mapping.get("allow_auto_trade", False):
            return "review"
        if opportunity.confidence < self.config.confidence_threshold:
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
        capital_limited = int(self.config.max_position_usd / cost_per_pair)
        liquidity_limited = int(max(min(yes_price_point.yes_volume, no_price_point.no_volume), 1))
        size = max(1, min(capital_limited, liquidity_limited))
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
        }

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
