"""
ARBITER — Cross-Platform Arbitrage Scanner
Scans all N² platform pairs for fee-adjusted arbitrage opportunities.

Core arbitrage logic:
  Buy YES on Platform A at price Y_a
  Buy NO on Platform B at price N_b
  If Y_a + N_b < 1.00 (after fees), guaranteed profit regardless of outcome.

  Profit = $1.00 - Y_a - N_b - fee(Y_a) - fee(N_b)

Also detects within-platform mispricing:
  If YES + NO < 1.00 on same platform (rare but happens with stale books)
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..config.settings import (
    ScannerConfig, MARKET_MAP,
    kalshi_fee, polymarket_fee, predictit_fee,
)
from ..utils.price_store import PricePoint, PriceStore

logger = logging.getLogger("arbiter.scanner")


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity."""
    canonical_id: str
    description: str
    # Leg A: buy YES
    yes_platform: str
    yes_price: float
    yes_fee: float
    yes_market_id: str
    # Leg B: buy NO
    no_platform: str
    no_price: float
    no_fee: float
    no_market_id: str
    # Computed
    gross_edge: float       # 1.0 - yes - no (before fees)
    total_fees: float       # fee_a + fee_b
    net_edge: float         # gross_edge - total_fees
    net_edge_cents: float   # net_edge * 100
    # Position sizing
    suggested_qty: int = 0
    max_profit_usd: float = 0.0
    # Metadata
    timestamp: float = 0.0
    confidence: float = 0.0  # 0-1, based on volume/liquidity
    arb_type: str = "cross_platform"  # or "within_platform"

    def to_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "description": self.description,
            "yes_platform": self.yes_platform,
            "yes_price": round(self.yes_price, 4),
            "no_platform": self.no_platform,
            "no_price": round(self.no_price, 4),
            "gross_edge": round(self.gross_edge, 4),
            "total_fees": round(self.total_fees, 4),
            "net_edge": round(self.net_edge, 4),
            "net_edge_cents": round(self.net_edge_cents, 2),
            "suggested_qty": self.suggested_qty,
            "max_profit_usd": round(self.max_profit_usd, 2),
            "confidence": round(self.confidence, 2),
            "arb_type": self.arb_type,
            "timestamp": self.timestamp,
        }


def compute_fee(platform: str, price: float) -> float:
    """Compute fee for a single leg based on platform."""
    if platform == "kalshi":
        return kalshi_fee(price)
    elif platform == "polymarket":
        return polymarket_fee(price, category="politics")
    elif platform == "predictit":
        # PredictIt fee is on profit, not on purchase
        # For arb calculation: assume max profit scenario
        # Effective fee = price * 0.10 (profit fee) + price * 0.05 (withdrawal)
        # But only if profitable. For conservative estimate:
        return price * 0.05  # withdrawal fee always applies
    return 0.0


def compute_predictit_total_fee(buy_price: float, profit: float) -> float:
    """
    PredictIt's actual fee on a winning contract:
    - 10% of profit (sell_price - buy_price)
    - 5% of total withdrawal amount
    """
    if profit <= 0:
        return buy_price * 0.05  # just withdrawal fee on principal
    return profit * 0.10 + (buy_price + profit) * 0.05


class ArbitrageScanner:
    """
    Continuously scans for cross-platform and within-platform arbitrage.
    Produces ArbitrageOpportunity objects when edges exceed threshold.
    """

    def __init__(self, config: ScannerConfig, price_store: PriceStore):
        self.config = config
        self.store = price_store
        self._running = False
        self._opportunities: List[ArbitrageOpportunity] = []
        self._subscribers: List[asyncio.Queue] = []
        self._scan_count = 0
        self._last_scan_time = 0.0
        self._opportunity_history: List[ArbitrageOpportunity] = []

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to new arbitrage opportunities."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    async def scan_once(self) -> List[ArbitrageOpportunity]:
        """
        Run one full arbitrage scan across all markets and platform pairs.
        Returns list of profitable opportunities.
        """
        opportunities = []
        self._scan_count += 1
        scan_start = time.time()

        for canonical_id, mapping in MARKET_MAP.items():
            # Get all available prices for this market
            prices = await self.store.get_all_for_market(canonical_id)

            if len(prices) < 2:
                continue  # need at least 2 platforms

            platforms = list(prices.keys())
            description = mapping.get("description", canonical_id)

            # ─── Cross-platform pairs ──────────────────────────────
            for i in range(len(platforms)):
                for j in range(len(platforms)):
                    if i == j:
                        continue

                    pa, pb = platforms[i], platforms[j]
                    price_a = prices[pa]  # buy YES here
                    price_b = prices[pb]  # buy NO here

                    yes_price = price_a.yes_price
                    no_price = price_b.no_price

                    if yes_price <= 0 or no_price <= 0:
                        continue

                    # Gross edge before fees
                    gross = 1.0 - yes_price - no_price

                    if gross <= 0:
                        continue  # no edge

                    # Compute platform-specific fees
                    fee_a = compute_fee(pa, yes_price)
                    fee_b = compute_fee(pb, no_price)

                    # For PredictIt, recalculate with actual profit fee
                    if pa == "predictit":
                        # Winning YES pays $1, cost was yes_price
                        pi_profit = 1.0 - yes_price
                        fee_a = compute_predictit_total_fee(yes_price, pi_profit)
                    if pb == "predictit":
                        pi_profit = 1.0 - no_price
                        fee_b = compute_predictit_total_fee(no_price, pi_profit)

                    total_fees = fee_a + fee_b
                    net_edge = gross - total_fees
                    net_cents = net_edge * 100

                    if net_cents < self.config.min_edge_cents:
                        continue

                    # Position sizing
                    max_qty = self._compute_position_size(
                        pa, pb, yes_price, no_price
                    )

                    # Confidence based on volume
                    conf = self._compute_confidence(price_a, price_b)

                    opp = ArbitrageOpportunity(
                        canonical_id=canonical_id,
                        description=description,
                        yes_platform=pa,
                        yes_price=yes_price,
                        yes_fee=fee_a,
                        yes_market_id=price_a.raw_market_id,
                        no_platform=pb,
                        no_price=no_price,
                        no_fee=fee_b,
                        no_market_id=price_b.raw_market_id,
                        gross_edge=gross,
                        total_fees=total_fees,
                        net_edge=net_edge,
                        net_edge_cents=net_cents,
                        suggested_qty=max_qty,
                        max_profit_usd=net_edge * max_qty,
                        timestamp=time.time(),
                        confidence=conf,
                        arb_type="cross_platform",
                    )
                    opportunities.append(opp)

            # ─── Within-platform check ─────────────────────────────
            for platform, price in prices.items():
                if price.yes_price > 0 and price.no_price > 0:
                    within_gross = 1.0 - price.yes_price - price.no_price
                    if within_gross > 0.02:  # 2 cents minimum
                        fee = compute_fee(platform, price.yes_price) + compute_fee(platform, price.no_price)
                        net = within_gross - fee
                        if net * 100 >= self.config.min_edge_cents:
                            opp = ArbitrageOpportunity(
                                canonical_id=canonical_id,
                                description=f"{description} (within-platform)",
                                yes_platform=platform,
                                yes_price=price.yes_price,
                                yes_fee=compute_fee(platform, price.yes_price),
                                yes_market_id=price.raw_market_id,
                                no_platform=platform,
                                no_price=price.no_price,
                                no_fee=compute_fee(platform, price.no_price),
                                no_market_id=price.raw_market_id,
                                gross_edge=within_gross,
                                total_fees=fee,
                                net_edge=net,
                                net_edge_cents=net * 100,
                                suggested_qty=int(self.config.max_position_usd / max(price.yes_price + price.no_price, 0.01)),
                                max_profit_usd=net * int(self.config.max_position_usd / max(price.yes_price + price.no_price, 0.01)),
                                timestamp=time.time(),
                                confidence=0.5,
                                arb_type="within_platform",
                            )
                            opportunities.append(opp)

        # Sort by net edge (best first)
        opportunities.sort(key=lambda o: o.net_edge_cents, reverse=True)
        self._opportunities = opportunities
        self._last_scan_time = time.time() - scan_start

        # Notify subscribers
        for opp in opportunities:
            for q in self._subscribers:
                try:
                    q.put_nowait(opp)
                except asyncio.QueueFull:
                    pass

        # Log summary
        if opportunities:
            best = opportunities[0]
            logger.info(
                f"Scan #{self._scan_count} ({self._last_scan_time*1000:.0f}ms): "
                f"{len(opportunities)} opportunities found. "
                f"Best: {best.canonical_id} {best.yes_platform}↔{best.no_platform} "
                f"net={best.net_edge_cents:.1f}¢ profit=${best.max_profit_usd:.2f}"
            )
        else:
            if self._scan_count % 60 == 0:  # log every ~minute
                logger.info(f"Scan #{self._scan_count}: no opportunities above {self.config.min_edge_cents}¢ threshold")

        return opportunities

    def _compute_position_size(self, platform_a: str, platform_b: str,
                                yes_price: float, no_price: float) -> int:
        """Compute max position size accounting for platform limits."""
        cost_per_pair = yes_price + no_price
        if cost_per_pair <= 0:
            return 0

        max_by_capital = int(self.config.max_position_usd / cost_per_pair)

        # PredictIt $850 cap
        if platform_a == "predictit":
            pi_max = int(self.config.predictit_cap / yes_price) if yes_price > 0 else 0
            max_by_capital = min(max_by_capital, pi_max)
        if platform_b == "predictit":
            pi_max = int(self.config.predictit_cap / no_price) if no_price > 0 else 0
            max_by_capital = min(max_by_capital, pi_max)

        return max(1, max_by_capital)

    def _compute_confidence(self, price_a: PricePoint, price_b: PricePoint) -> float:
        """
        Confidence score based on volume and freshness.
        Higher volume + fresher prices = higher confidence.
        """
        now = time.time()
        # Freshness: exponential decay, 10s half-life
        age_a = now - price_a.timestamp
        age_b = now - price_b.timestamp
        freshness = min(1.0, 2 ** (-max(age_a, age_b) / 10.0))

        # Volume: log scale, cap at 1.0
        vol = min(price_a.yes_volume, price_b.no_volume)
        volume_score = min(1.0, (vol + 1) / 1000.0)

        return freshness * 0.7 + volume_score * 0.3

    @property
    def current_opportunities(self) -> List[ArbitrageOpportunity]:
        return self._opportunities

    @property
    def stats(self) -> dict:
        return {
            "scan_count": self._scan_count,
            "last_scan_ms": round(self._last_scan_time * 1000, 1),
            "active_opportunities": len(self._opportunities),
            "best_edge_cents": self._opportunities[0].net_edge_cents if self._opportunities else 0,
        }

    async def run(self):
        """Continuous scanning loop."""
        self._running = True
        logger.info(
            f"Arbitrage scanner started (interval: {self.config.scan_interval}s, "
            f"min_edge: {self.config.min_edge_cents}¢, "
            f"dry_run: {self.config.dry_run})"
        )

        while self._running:
            try:
                await self.scan_once()
                await asyncio.sleep(self.config.scan_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scanner error: {e}")
                await asyncio.sleep(2)

        logger.info("Arbitrage scanner stopped")

    async def stop(self):
        self._running = False
