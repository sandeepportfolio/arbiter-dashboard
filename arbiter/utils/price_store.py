"""
In-memory + Redis price store.
Falls back to in-memory dict when Redis is unavailable.
Each price entry: {platform, market_id, yes_price, no_price, yes_volume, no_volume, timestamp}
"""
import asyncio
import json
import time
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("arbiter.price_store")


@dataclass
class PricePoint:
    platform: str          # "kalshi" | "polymarket" | "predictit"
    canonical_id: str      # from MARKET_MAP keys
    yes_price: float       # 0.00 - 1.00
    no_price: float        # 0.00 - 1.00
    yes_volume: float      # contracts or USD
    no_volume: float
    timestamp: float       # unix epoch
    raw_market_id: str     # platform-specific ID

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PricePoint":
        return cls(**d)


class PriceStore:
    """
    Thread-safe price store with optional Redis backing.
    Key format: "price:{platform}:{canonical_id}"
    """

    def __init__(self, redis_client=None, ttl: int = 10):
        self._mem: Dict[str, PricePoint] = {}
        self._redis = redis_client
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._subscribers: List[asyncio.Queue] = []

    async def put(self, price: PricePoint) -> None:
        """Store a price point and notify subscribers."""
        key = f"price:{price.platform}:{price.canonical_id}"
        async with self._lock:
            self._mem[key] = price

        # Redis backing (fire-and-forget)
        if self._redis:
            try:
                await self._redis.setex(key, self._ttl, json.dumps(price.to_dict()))
            except Exception as e:
                logger.warning(f"Redis write failed: {e}")

        # Notify subscribers
        for q in self._subscribers:
            try:
                q.put_nowait(price)
            except asyncio.QueueFull:
                pass  # subscriber is slow, skip

    async def get(self, platform: str, canonical_id: str) -> Optional[PricePoint]:
        """Get latest price for a specific platform+market."""
        key = f"price:{platform}:{canonical_id}"
        async with self._lock:
            p = self._mem.get(key)
            if p and (time.time() - p.timestamp) < self._ttl:
                return p

        # Try Redis
        if self._redis:
            try:
                raw = await self._redis.get(key)
                if raw:
                    return PricePoint.from_dict(json.loads(raw))
            except Exception:
                pass
        return None

    async def get_all_for_market(self, canonical_id: str) -> Dict[str, PricePoint]:
        """Get latest prices across all platforms for a canonical market."""
        result = {}
        for platform in ("kalshi", "polymarket", "predictit"):
            p = await self.get(platform, canonical_id)
            if p:
                result[platform] = p
        return result

    async def get_all_prices(self) -> Dict[str, PricePoint]:
        """Get all currently stored prices."""
        async with self._lock:
            now = time.time()
            return {
                k: v for k, v in self._mem.items()
                if (now - v.timestamp) < self._ttl * 6  # wider window for snapshot
            }

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to real-time price updates."""
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber."""
        self._subscribers = [s for s in self._subscribers if s is not q]

    async def get_cross_platform_pairs(self, canonical_id: str) -> List[Tuple[PricePoint, PricePoint]]:
        """
        Get all cross-platform price pairs for arbitrage scanning.
        Returns list of (platform_a_price, platform_b_price) tuples.
        """
        prices = await self.get_all_for_market(canonical_id)
        pairs = []
        platforms = list(prices.keys())
        for i in range(len(platforms)):
            for j in range(i + 1, len(platforms)):
                pairs.append((prices[platforms[i]], prices[platforms[j]]))
        return pairs
