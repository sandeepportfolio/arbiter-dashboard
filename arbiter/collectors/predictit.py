"""
PredictIt Price Collector
- HTTP polling only (no WebSocket, no trade API)
- Public API, no auth required
- Updates ~every 60 seconds server-side
- $850 per-contract position cap
"""
import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import aiohttp

from ..config.settings import PredictItConfig, MARKET_MAP
from ..utils.price_store import PricePoint, PriceStore
from ..utils.retry import CircuitBreaker, RateLimiter, retry_with_backoff

logger = logging.getLogger("arbiter.collector.predictit")


class PredictItCollector:
    """
    Collects prices from PredictIt public API.
    Maps PredictIt market IDs to canonical market IDs.
    PredictIt is the slowest platform (~60s refresh) but often has the most mispricing.
    """

    def __init__(self, config: PredictItConfig, price_store: PriceStore):
        self.config = config
        self.store = price_store
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        # Resilience
        self.circuit = CircuitBreaker("predictit", failure_threshold=5, recovery_timeout=60)
        self.rate_limiter = RateLimiter("predictit", max_requests=2, window_seconds=1.0)  # conservative
        self.consecutive_errors = 0
        self.total_fetches = 0
        # Build reverse map: predictit_market_id -> list of canonical_ids
        # (multiple canonical IDs can map to same PI market with different contracts)
        self._market_map: Dict[str, List[str]] = {}
        self._canonical_to_pi: Dict[str, str] = {}
        for canonical_id, mapping in MARKET_MAP.items():
            if "predictit" in mapping:
                pi_id = str(mapping["predictit"])
                if pi_id not in self._market_map:
                    self._market_map[pi_id] = []
                self._market_map[pi_id].append(canonical_id)
                self._canonical_to_pi[canonical_id] = pi_id

        # Cache the full market data for matching
        self._all_markets_cache: Dict[str, dict] = {}
        self._last_full_fetch: float = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Accept": "application/json"}
            )
        return self._session

    async def fetch_all_markets(self) -> Dict[str, dict]:
        """
        Fetch ALL PredictIt markets in one call.
        The API returns everything in a single endpoint.
        """
        session = await self._get_session()

        try:
            async with session.get(self.config.base_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    markets = data.get("markets", [])
                    logger.debug(f"PredictIt fetched {len(markets)} total markets")

                    # Index by market ID
                    indexed = {}
                    for m in markets:
                        mid = str(m.get("id", ""))
                        indexed[mid] = m
                    self._all_markets_cache = indexed
                    self._last_full_fetch = time.time()
                    return indexed
                elif resp.status == 429:
                    delay = self.rate_limiter.apply_retry_after(
                        resp.headers.get("Retry-After"),
                        fallback_delay=max(self.config.poll_interval * 1.5, self.config.min_poll_interval),
                        reason="predictit_429",
                    )
                    logger.warning("PredictIt rate limited, backing off %.1fs", delay)
                    self.config.poll_interval = min(max(delay, self.config.min_poll_interval), self.config.max_poll_interval)
                    return self._all_markets_cache
                else:
                    logger.warning(f"PredictIt API returned {resp.status}")
                    return self._all_markets_cache

        except Exception as e:
            logger.error(f"PredictIt fetch error: {e}")
            return self._all_markets_cache

    async def extract_prices(self, all_markets: Dict[str, dict]) -> list:
        """
        Extract prices for tracked markets and push to price store.
        Handles PredictIt's contract structure (each market has multiple contracts).
        """
        results = []

        for pi_id, canonical_ids in self._market_map.items():
            market = all_markets.get(pi_id)
            if not market:
                continue

            contracts = market.get("contracts", [])
            market_name = market.get("name", "")

            for canonical_id in canonical_ids:
                # Find the matching contract within the market
                contract = self._match_contract(canonical_id, contracts, market_name)
                if not contract:
                    logger.debug(f"PredictIt: no matching contract for {canonical_id} in market {pi_id}")
                    continue

                yes_price = contract.get("lastTradePrice") or contract.get("bestBuyYesCost") or 0.0
                no_price = contract.get("bestBuyNoCost") or (1.0 - yes_price) if yes_price else 0.0

                # PredictIt prices are already in 0-1 range
                mapping = MARKET_MAP.get(canonical_id, {})
                price = PricePoint(
                    platform="predictit",
                    canonical_id=canonical_id,
                    yes_price=yes_price,
                    no_price=no_price,
                    yes_volume=float(contract.get("totalSharesTraded", 0)),
                    no_volume=float(contract.get("totalSharesTraded", 0)),
                    timestamp=time.time(),
                    raw_market_id=f"{pi_id}:{contract.get('id', '')}",
                    yes_market_id=f"{pi_id}:{contract.get('id', '')}",
                    no_market_id=f"{pi_id}:{contract.get('id', '')}",
                    yes_bid=float(contract.get("bestBuyYesCost", yes_price) or yes_price),
                    yes_ask=float(contract.get("bestBuyYesCost", yes_price) or yes_price),
                    no_bid=float(contract.get("bestBuyNoCost", no_price) or no_price),
                    no_ask=float(contract.get("bestBuyNoCost", no_price) or no_price),
                    fee_rate=0.15,
                    mapping_status=str(mapping.get("status", "candidate")),
                    mapping_score=float(mapping.get("mapping_score", 0.0)),
                    metadata={
                        "market_name": market_name,
                        "contract_name": contract.get("name", ""),
                    },
                )
                results.append(price)
                await self.store.put(price)
                logger.debug(
                    f"PredictIt {canonical_id}: YES={yes_price:.2f} NO={no_price:.2f} "
                    f"({contract.get('name', '')[:40]})"
                )

        return results

    def _match_contract(self, canonical_id: str, contracts: list, market_name: str) -> Optional[dict]:
        """
        Match a canonical market ID to a specific PredictIt contract.
        Uses keyword matching since PredictIt contract names vary.
        """
        mapping = MARKET_MAP.get(canonical_id, {})
        keywords = list(mapping.get("predictit_contract_keywords", ()))

        if not keywords:
            # Default: return first contract if only one
            return contracts[0] if len(contracts) == 1 else None

        for contract in contracts:
            name = (contract.get("name", "") + " " + contract.get("shortName", "")).lower()
            if any(kw.lower() in name for kw in keywords):
                return contract

        # Fallback: if market has single contract (binary yes/no)
        if len(contracts) == 1:
            return contracts[0]

        return None

    async def fetch_balance(self) -> Optional[float]:
        """
        PredictIt has no programmatic balance API.
        Balance must be scraped from the website or entered manually.
        Returns None — user must configure balance manually or via scraping.
        """
        logger.debug("PredictIt has no balance API — manual tracking required")
        return None

    async def run(self):
        """Main polling loop with circuit breaker and retry."""
        self._running = True
        logger.info(f"PredictIt collector started (poll interval: {self.config.poll_interval}s)")
        logger.info(f"Tracking {len(self._market_map)} markets with {len(self._canonical_to_pi)} canonical mappings")
        logger.info(f"Circuit breaker: threshold={self.circuit.failure_threshold}, recovery={self.circuit.recovery_timeout}s")

        while self._running:
            try:
                if not self.circuit.can_execute():
                    logger.warning(f"PredictIt circuit OPEN, waiting")
                    await asyncio.sleep(self.circuit.recovery_timeout / 2)
                    continue

                await self.rate_limiter.acquire()
                self.total_fetches += 1

                async def _do_fetch():
                    mkts = await self.fetch_all_markets()
                    if mkts:
                        return await self.extract_prices(mkts)
                    return []

                prices = await retry_with_backoff(
                    _do_fetch,
                    retries=2,
                    base_delay=2.0,
                    circuit=self.circuit,
                )
                if prices:
                    logger.info(f"PredictIt: updated {len(prices)} prices")
                self.consecutive_errors = 0

                self.config.poll_interval = max(self.config.min_poll_interval, self.config.poll_interval * 0.98)
                await asyncio.sleep(self.config.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.consecutive_errors += 1
                self.config.poll_interval = min(self.config.poll_interval * 1.25, self.config.max_poll_interval)
                backoff = min(self.config.poll_interval * (2 ** min(self.consecutive_errors, 4)), self.config.max_poll_interval)
                logger.error(f"PredictIt error (#{self.consecutive_errors}), backoff {backoff:.0f}s: {e}")
                await asyncio.sleep(backoff)

        logger.info(f"PredictIt collector stopped (fetches={self.total_fetches})")

    async def stop(self):
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
