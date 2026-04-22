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

from ..config.settings import KalshiConfig, MARKET_MAP, KALSHI_TAKER_FEE_RATE, similarity_score
from ..utils.price_store import PricePoint, PriceStore
from ..utils.retry import CircuitBreaker, RateLimiter, SessionManager, retry_with_backoff

logger = logging.getLogger("arbiter.collector.kalshi")


def _safe_float(value) -> float:
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
                salt_length=padding.PSS.DIGEST_LENGTH,
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
        self._ticker_map: Dict[str, List[str]] = {}
        self.refresh_tracked_markets()

    def refresh_tracked_markets(self) -> None:
        """Reload the reverse ticker map from the current runtime MARKET_MAP."""
        ticker_map: Dict[str, List[str]] = {}
        for canonical_id, mapping in MARKET_MAP.items():
            event_ticker = str(mapping.get("kalshi", "") or "")
            if not event_ticker:
                continue
            ticker_map.setdefault(event_ticker, []).append(canonical_id)
        self._ticker_map = ticker_map

    async def list_all_markets(
        self,
        status: Optional[str] = None,
        page_size: int = 1000,
        max_pages: int = 20,
    ) -> list[dict]:
        """List Kalshi markets with cursor pagination for discovery."""
        session = await self._get_session()
        cursor: Optional[str] = None
        all_markets: list[dict] = []

        for _ in range(max_pages):
            await self.rate_limiter.acquire()
            headers = {"Accept": "application/json"}
            if self.auth.is_authenticated:
                headers.update(self.auth.get_headers("GET", "/trade-api/v2/markets"))
            params = {"limit": str(page_size)}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor

            async with session.get(
                f"{self.config.base_url}/markets",
                params=params,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            markets = list(data.get("markets") or [])
            all_markets.extend(markets)
            cursor = data.get("cursor") or None
            if not cursor:
                break

        return all_markets

    async def list_all_events(self, page_size: int = 100, max_pages: int = 20) -> list[dict]:
        """List Kalshi events for coarse-grained discovery matching.

        Events are dramatically less noisy than the raw global market feed and
        expose long-dated contracts that can be buried deep in market paging.
        """
        session = await self._get_session()
        cursor: Optional[str] = None
        all_events: list[dict] = []

        for _ in range(max_pages):
            await self.rate_limiter.acquire()
            headers = {"Accept": "application/json"}
            if self.auth.is_authenticated:
                headers.update(self.auth.get_headers("GET", "/trade-api/v2/events"))
            params = {"limit": str(page_size)}
            if cursor:
                params["cursor"] = cursor

            async with session.get(
                f"{self.config.base_url}/events",
                params=params,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            events = list(data.get("events") or [])
            all_events.extend(events)
            cursor = data.get("cursor") or None
            if not cursor:
                break

        return all_events

    async def list_markets_for_event(self, event_ticker: str, limit: int = 50) -> list[dict]:
        """Fetch all submarkets for a specific Kalshi event ticker."""
        session = await self._get_session()
        await self.rate_limiter.acquire()
        headers = {"Accept": "application/json"}
        if self.auth.is_authenticated:
            headers.update(self.auth.get_headers("GET", "/trade-api/v2/markets"))

        async with session.get(
            f"{self.config.base_url}/markets",
            params={"event_ticker": event_ticker, "limit": str(limit)},
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return list(data.get("markets") or [])

    async def get_orderbook(self, market_id: str, depth: int = 100) -> dict:
        """Fetch a raw Kalshi market orderbook for mapping and execution checks."""
        session = await self._get_session()
        await self.rate_limiter.acquire()
        headers = {"Accept": "application/json"}
        async with session.get(
            f"{self.config.base_url}/markets/{market_id}/orderbook",
            params={"depth": str(depth)},
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def fetch_markets(self) -> list:
        """Fetch all tracked markets from Kalshi REST API using event_ticker."""
        self.refresh_tracked_markets()
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
                    path = "/trade-api/v2/markets"
                    headers.update(self.auth.get_headers("GET", path))

                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        markets = data.get("markets", [])
                        for canonical_id in canonical_ids:
                            market = self._select_market_for_canonical(canonical_id, event_ticker, markets)
                            if not market:
                                continue
                            price = self._build_price_point(canonical_id, event_ticker, market)
                            if price is None:
                                continue
                            results.append(price)
                            await self.store.put(price)
                            logger.debug(
                                "Kalshi %s: YES=%.2f NO=%.2f (%s)",
                                canonical_id,
                                price.yes_price,
                                price.no_price,
                                price.raw_market_id,
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

    @staticmethod
    def _normalize_price(value: float) -> float:
        return value / 100.0 if value and value > 1 else value

    @staticmethod
    def _market_float(market: dict, *keys: str) -> float:
        for key in keys:
            value = _safe_float(market.get(key))
            if value:
                return value
        return 0.0

    def _select_market_for_canonical(self, canonical_id: str, event_ticker: str, markets: List[dict]) -> Optional[dict]:
        if not markets:
            return None

        mapping = MARKET_MAP.get(canonical_id, {})
        target = str(mapping.get("kalshi", "") or "").strip()
        for market in markets:
            if str(market.get("ticker", "") or "").strip() == target:
                return market

        if len(markets) == 1:
            return markets[0]

        candidate_text = " ".join(
            part
            for part in (
                str(mapping.get("description", "") or ""),
                " ".join(str(alias) for alias in mapping.get("aliases", ()) or ()),
            )
            if part
        )
        scored = [
            (
                similarity_score(
                    candidate_text,
                    " ".join(
                        str(market.get(field, "") or "")
                        for field in ("title", "yes_sub_title", "no_sub_title", "ticker")
                    ),
                ),
                market,
            )
            for market in markets
        ]
        best_score, best_market = max(scored, key=lambda item: item[0])
        if best_score >= 0.2:
            return best_market

        logger.debug(
            "Kalshi skipped ambiguous event %s for %s; best match score %.3f was too low",
            event_ticker,
            canonical_id,
            best_score,
        )
        return None

    def _build_price_point(self, canonical_id: str, event_ticker: str, market: dict) -> Optional[PricePoint]:
        yes_bid = self._normalize_price(self._market_float(market, "yes_bid", "bid", "yes_bid_dollars"))
        yes_ask = self._normalize_price(self._market_float(market, "yes_ask", "ask", "yes_ask_dollars", "last_price", "last_price_dollars"))
        no_bid = self._normalize_price(self._market_float(market, "no_bid", "no_bid_dollars"))
        no_ask = self._normalize_price(self._market_float(market, "no_ask", "no_ask_dollars"))
        yes_price = self._normalize_price(self._market_float(market, "last_price", "last_price_dollars")) or yes_ask or yes_bid
        no_price = no_ask or no_bid or (1.0 - yes_price if yes_price else 0.0)

        if yes_price == 0.0 and no_price == 0.0:
            return None

        yes_depth = self._market_float(market, "yes_ask_size_fp", "yes_bid_size_fp", "volume_fp", "volume")
        no_depth = self._market_float(market, "no_ask_size_fp", "no_bid_size_fp", "volume_fp", "volume")
        mapping = MARKET_MAP.get(canonical_id, {})
        return PricePoint(
            platform="kalshi",
            canonical_id=canonical_id,
            yes_price=yes_price,
            no_price=no_price,
            yes_volume=yes_depth,
            no_volume=no_depth,
            timestamp=time.time(),
            raw_market_id=market.get("ticker", event_ticker),
            yes_market_id=market.get("ticker", event_ticker),
            no_market_id=market.get("ticker", event_ticker),
            yes_bid=float(yes_bid or 0),
            yes_ask=float(yes_ask or yes_price or 0),
            no_bid=float(no_bid or 0),
            no_ask=float(no_ask or no_price or 0),
            fee_rate=KALSHI_TAKER_FEE_RATE,
            mapping_status=str(mapping.get("status", "candidate")),
            mapping_score=float(mapping.get("mapping_score", 0.0)),
            metadata={
                "event_ticker": event_ticker,
                "market_title": market.get("title", ""),
                "yes_sub_title": market.get("yes_sub_title", ""),
                "no_sub_title": market.get("no_sub_title", ""),
                "response_price_units": market.get("response_price_units", ""),
            },
        )

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
