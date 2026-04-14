"""
Kalshi Price Collector
- REST API polling for market data
- RSA-PSS signature auth for authenticated endpoints (balance, orders)
- WebSocket for real-time orderbook updates (when available)
"""
import asyncio
import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from typing import Dict, List

from ..config.settings import KalshiConfig, MARKET_MAP, KALSHI_TAKER_FEE_RATE
from ..utils.price_store import PricePoint, PriceStore
from ..utils.retry import CircuitBreaker, RateLimiter, SessionManager, retry_with_backoff

logger = logging.getLogger("arbiter.collector.kalshi")


class KalshiAuth:
    """RSA-PSS signature generator for Kalshi API v2."""

    def __init__(self, api_key_id: str, private_key_path: str):
        self.api_key_id = api_key_id
        self._private_key = None
        if private_key_path:
            try:
                with open(private_key_path, "rb") as f:
                    self._private_key = serialization.load_pem_private_key(f.read(), password=None)
                logger.info("Kalshi RSA private key loaded successfully")
            except Exception as e:
                logger.warning(f"Could not load Kalshi private key: {e}")

    def sign_request(self, method: str, path: str, timestamp_ms: int) -> str:
        """
        Generate RSA-PSS signature for Kalshi API.
        Signs: timestamp_ms + method + path
        """
        if not self._private_key:
            return ""
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def get_headers(self, method: str, path: str) -> dict:
        """Get authenticated headers for a Kalshi API request."""
        ts = int(time.time() * 1000)
        sig = self.sign_request(method, path, ts)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "Content-Type": "application/json",
        }

    @property
    def is_authenticated(self) -> bool:
        return self._private_key is not None and bool(self.api_key_id)


class KalshiCollector:
    """
    Collects prices from Kalshi via REST polling.
    Maps Kalshi series_ticker/event_ticker to canonical market IDs.
    """

    def __init__(self, config: KalshiConfig, price_store: PriceStore):
        self.config = config
        self.store = price_store
        self.auth = KalshiAuth(config.api_key_id, config.private_key_path)
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        # Resilience
        self.circuit = CircuitBreaker("kalshi", failure_threshold=5, recovery_timeout=30)
        self.rate_limiter = RateLimiter("kalshi", max_requests=8, window_seconds=1.0)
        self.consecutive_errors = 0
        self.total_fetches = 0
        self.total_errors = 0
        # Build reverse map: kalshi_event_ticker -> list of canonical_ids
        self._ticker_map: Dict[str, List[str]] = {}
        for canonical_id, mapping in MARKET_MAP.items():
            event_ticker = str(mapping.get("kalshi", "") or "")
            if event_ticker:
                if event_ticker not in self._ticker_map:
                    self._ticker_map[event_ticker] = []
                self._ticker_map[event_ticker].append(canonical_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def fetch_markets(self) -> list:
        """Fetch all tracked markets from Kalshi REST API using event_ticker."""
        session = await self._get_session()
        results = []

        for event_ticker, canonical_ids in self._ticker_map.items():
            try:
                await self.rate_limiter.acquire()
                url = f"{self.config.base_url}/markets"
                params = {"event_ticker": event_ticker, "limit": 50}
                headers = {"Accept": "application/json"}

                # Use auth if available
                if self.auth.is_authenticated:
                    path = f"/trade-api/v2/markets?event_ticker={event_ticker}&limit=50"
                    headers.update(self.auth.get_headers("GET", path))

                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        markets = data.get("markets", [])
                        for m in markets:
                            yes_bid = m.get("yes_bid") or m.get("bid") or 0.0
                            yes_ask = m.get("yes_ask") or m.get("ask") or m.get("last_price") or 0.0
                            no_bid = m.get("no_bid") or 0.0
                            no_ask = m.get("no_ask") or (1.0 - yes_bid) if yes_bid else 0.0

                            yes_price = yes_ask or yes_bid or m.get("last_price") or 0.0
                            no_price = no_ask or no_bid or (1.0 - yes_price) if yes_price else 0.0

                            # Normalize to 0-1 range (Kalshi uses cents)
                            yes_bid = yes_bid / 100.0 if yes_bid and yes_bid > 1 else yes_bid
                            yes_ask = yes_ask / 100.0 if yes_ask and yes_ask > 1 else yes_ask
                            no_bid = no_bid / 100.0 if no_bid and no_bid > 1 else no_bid
                            no_ask = no_ask / 100.0 if no_ask and no_ask > 1 else no_ask
                            yes_price = yes_price / 100.0 if yes_price and yes_price > 1 else yes_price
                            no_price = no_price / 100.0 if no_price and no_price > 1 else no_price

                            # Skip markets with no price data
                            if yes_price == 0.0 and no_price == 0.0:
                                continue

                            # Map to canonical IDs (use first one; Kalshi
                            # sub-markets would need ticker-level matching)
                            for canonical_id in canonical_ids:
                                mapping = MARKET_MAP.get(canonical_id, {})
                                price = PricePoint(
                                    platform="kalshi",
                                    canonical_id=canonical_id,
                                    yes_price=yes_price,
                                    no_price=no_price,
                                    yes_volume=float(m.get("volume", 0) or 0),
                                    no_volume=float(m.get("volume", 0) or 0),
                                    timestamp=time.time(),
                                    raw_market_id=m.get("ticker", event_ticker),
                                    yes_market_id=m.get("ticker", event_ticker),
                                    no_market_id=m.get("ticker", event_ticker),
                                    yes_bid=float(yes_bid or 0),
                                    yes_ask=float(yes_ask or yes_price or 0),
                                    no_bid=float(no_bid or 0),
                                    no_ask=float(no_ask or no_price or 0),
                                    fee_rate=KALSHI_TAKER_FEE_RATE,
                                    mapping_status=str(mapping.get("status", "candidate")),
                                    mapping_score=float(mapping.get("mapping_score", 0.0)),
                                    metadata={
                                        "event_ticker": event_ticker,
                                        "market_title": m.get("title", ""),
                                    },
                                )
                                results.append(price)
                                await self.store.put(price)
                                logger.debug(
                                    f"Kalshi {canonical_id}: YES={yes_price:.2f} NO={no_price:.2f}"
                                )
                    elif resp.status == 429:
                        delay = self.rate_limiter.apply_retry_after(
                            resp.headers.get("Retry-After"),
                            fallback_delay=max(self.config.poll_interval * 4, 5.0),
                            reason="kalshi_429",
                        )
                        logger.warning("Kalshi rate limited for %s, backing off %.1fs", event_ticker, delay)
                    else:
                        text = await resp.text()
                        logger.warning(f"Kalshi API {resp.status} for {event_ticker}: {text[:200]}")

            except Exception as e:
                logger.error(f"Kalshi fetch error for {event_ticker}: {e}")

        return results

    async def fetch_balance(self) -> Optional[float]:
        """Fetch account balance (requires auth)."""
        if not self.auth.is_authenticated:
            logger.debug("Kalshi auth not configured, skipping balance fetch")
            return None

        session = await self._get_session()
        try:
            path = "/trade-api/v2/portfolio/balance"
            url = f"{self.config.base_url}/portfolio/balance"
            headers = self.auth.get_headers("GET", path)

            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Kalshi returns balance in cents
                    balance = data.get("balance", 0) / 100.0
                    logger.info(f"Kalshi balance: ${balance:.2f}")
                    return balance
                else:
                    logger.warning(f"Kalshi balance fetch failed: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Kalshi balance error: {e}")
            return None

    async def run(self):
        """Main polling loop with circuit breaker and adaptive backoff."""
        self._running = True
        logger.info(f"Kalshi collector started (poll interval: {self.config.poll_interval}s)")
        logger.info(f"Tracking {len(self._ticker_map)} event tickers: {list(self._ticker_map.keys())}")
        logger.info(f"Auth: {'enabled' if self.auth.is_authenticated else 'disabled (public data only)'}")
        logger.info(f"Circuit breaker: threshold={self.circuit.failure_threshold}, recovery={self.circuit.recovery_timeout}s")

        while self._running:
            try:
                if not self.circuit.can_execute():
                    logger.warning(f"Kalshi circuit OPEN, waiting {self.circuit.recovery_timeout}s")
                    await asyncio.sleep(self.circuit.recovery_timeout / 2)
                    continue

                self.total_fetches += 1

                async def _fetch():
                    return await self.fetch_markets()

                await retry_with_backoff(
                    _fetch,
                    retries=2,
                    base_delay=1.0,
                    circuit=self.circuit,
                )
                self.consecutive_errors = 0

                await asyncio.sleep(self.config.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.consecutive_errors += 1
                self.total_errors += 1
                # Adaptive backoff: longer waits on more errors
                backoff = min(self.config.poll_interval * (2 ** min(self.consecutive_errors, 5)), 60)
                logger.error(f"Kalshi collector error (#{self.consecutive_errors}), backoff {backoff:.0f}s: {e}")
                await asyncio.sleep(backoff)

        logger.info(f"Kalshi collector stopped (fetches={self.total_fetches}, errors={self.total_errors})")

    async def stop(self):
        """Stop the collector."""
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
