"""
In-memory price store with optional Redis backing and light chart history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("arbiter.price_store")


@dataclass
class PricePoint:
    platform: str
    canonical_id: str
    yes_price: float
    no_price: float
    yes_volume: float
    no_volume: float
    timestamp: float
    raw_market_id: str
    yes_market_id: str = ""
    no_market_id: str = ""
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    fee_rate: float = 0.0
    mapping_status: str = "candidate"
    mapping_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "PricePoint":
        return cls(**payload)

    @property
    def age_seconds(self) -> float:
        return max(time.time() - self.timestamp, 0.0)


class PriceStore:
    """
    Thread-safe price store keyed by platform and canonical market.
    """

    def __init__(self, redis_client=None, ttl: int = 10, history_limit: int = 240):
        self._mem: Dict[str, PricePoint] = {}
        self._redis = redis_client
        self._ttl = ttl
        self._history_limit = history_limit
        self._lock = asyncio.Lock()
        self._subscribers: List[asyncio.Queue] = []
        self._history: Dict[str, Deque[dict]] = defaultdict(lambda: deque(maxlen=self._history_limit))

    async def put(self, price: PricePoint) -> None:
        key = self._key(price.platform, price.canonical_id)
        async with self._lock:
            self._mem[key] = price
            self._history[price.canonical_id].append(
                {
                    "timestamp": price.timestamp,
                    "platform": price.platform,
                    "yes_price": price.yes_price,
                    "no_price": price.no_price,
                    "mid_price": round((price.yes_price + (1.0 - price.no_price)) / 2.0, 4),
                    "mapping_status": price.mapping_status,
                }
            )

        if self._redis:
            try:
                await self._redis.setex(key, self._ttl, json.dumps(price.to_dict()))
            except Exception as exc:
                logger.warning("Redis write failed: %s", exc)

        for subscriber in list(self._subscribers):
            try:
                subscriber.put_nowait(price)
            except asyncio.QueueFull:
                logger.debug("Skipping slow price subscriber")

    async def get(self, platform: str, canonical_id: str) -> Optional[PricePoint]:
        key = self._key(platform, canonical_id)
        async with self._lock:
            cached = self._mem.get(key)
            if cached and cached.age_seconds < self._ttl:
                return cached

        if self._redis:
            try:
                raw = await self._redis.get(key)
                if raw:
                    return PricePoint.from_dict(json.loads(raw))
            except Exception:
                pass
        return None

    async def get_all_for_market(self, canonical_id: str) -> Dict[str, PricePoint]:
        result = {}
        for platform in ("kalshi", "polymarket", "predictit"):
            price = await self.get(platform, canonical_id)
            if price:
                result[platform] = price
        return result

    async def get_all_prices(self) -> Dict[str, PricePoint]:
        async with self._lock:
            now = time.time()
            return {
                key: value
                for key, value in self._mem.items()
                if (now - value.timestamp) < self._ttl * 6
            }

    async def get_market_history(self, canonical_id: str, limit: int = 180) -> List[dict]:
        async with self._lock:
            return list(self._history.get(canonical_id, []))[-limit:]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers = [subscriber for subscriber in self._subscribers if subscriber is not queue]

    async def get_cross_platform_pairs(self, canonical_id: str) -> List[Tuple[PricePoint, PricePoint]]:
        prices = await self.get_all_for_market(canonical_id)
        pairs = []
        platforms = list(prices.keys())
        for index, left in enumerate(platforms):
            for right in platforms[index + 1:]:
                pairs.append((prices[left], prices[right]))
        return pairs

    @staticmethod
    def _key(platform: str, canonical_id: str) -> str:
        return f"price:{platform}:{canonical_id}"
