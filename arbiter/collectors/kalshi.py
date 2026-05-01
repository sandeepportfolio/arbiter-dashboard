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
import re
import time
from datetime import date, datetime, timedelta, timezone
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

    @staticmethod
    def _is_expired_mapping(mapping: dict, grace_days: int = 1) -> bool:
        """Check if a mapping's Polymarket slug contains an expired date."""
        slug = str(mapping.get("polymarket", "") or "")
        if not slug:
            return False
        _date_re = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
        match = _date_re.search(slug)
        if not match:
            return False
        try:
            slug_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return slug_date < date.today() - timedelta(days=grace_days)
        except ValueError:
            return False

    def refresh_tracked_markets(self) -> None:
        """Reload the reverse ticker map from the current runtime MARKET_MAP.

        Skips mappings whose Polymarket slug has an expired date to avoid
        wasting Kalshi API quota on markets that can no longer be arbitraged.
        """
        ticker_map: Dict[str, List[str]] = {}
        skipped = 0
        for canonical_id, mapping in MARKET_MAP.items():
            event_ticker = str(mapping.get("kalshi", "") or "")
            if not event_ticker:
                continue
            if self._is_expired_mapping(mapping):
                skipped += 1
                continue
            ticker_map.setdefault(event_ticker, []).append(canonical_id)
        if skipped:
            logger.info(
                "Kalshi: tracking %d tickers, skipped %d expired mappings",
                len(ticker_map),
                skipped,
            )
        self._ticker_map = ticker_map

    async def _get_with_429_retry(
        self,
        url: str,
        *,
        params: dict,
        path: str,
        max_retries: int = 5,
        fallback_delay: float = 5.0,
    ) -> dict:
        """GET helper that honors Retry-After on 429 and re-attempts.

        Discovery pulls thousands of markets/events with cursor pagination.
        Without 429 handling, a single 429 aborts the entire pass. Honoring
        Retry-After (or falling back to ``fallback_delay``) lets us pace the
        request and resume.
        """
        session = await self._get_session()
        for attempt in range(max_retries):
            await self.rate_limiter.acquire()
            headers = {"Accept": "application/json"}
            if self.auth.is_authenticated:
                headers.update(self.auth.get_headers("GET", path))
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 429:
                    delay = self.rate_limiter.apply_retry_after(
                        resp.headers.get("Retry-After"),
                        fallback_delay=fallback_delay * (attempt + 1),
                        reason="kalshi_429_discovery",
                    )
                    logger.warning(
                        "Kalshi 429 on %s (attempt %d/%d), backing off %.1fs",
                        path, attempt + 1, max_retries, delay,
                    )
                    continue
                resp.raise_for_status()
                return await resp.json()
        raise RuntimeError(
            f"Kalshi 429 retry budget exhausted for {path} after {max_retries} attempts"
        )

    async def list_all_markets(
        self,
        status: Optional[str] = None,
        page_size: int = 1000,
        max_pages: int = 80,
    ) -> list[dict]:
        """List Kalshi markets with cursor pagination for discovery."""
        cursor: Optional[str] = None
        all_markets: list[dict] = []

        for _ in range(max_pages):
            params = {"limit": str(page_size)}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor

            data = await self._get_with_429_retry(
                f"{self.config.base_url}/markets",
                params=params,
                path="/trade-api/v2/markets",
            )

            markets = list(data.get("markets") or [])
            all_markets.extend(markets)
            cursor = data.get("cursor") or None
            if not cursor:
                break

        return all_markets

    async def list_all_events(
        self,
        page_size: int = 200,
        max_pages: int = 100,
        status: Optional[str] = None,
    ) -> list[dict]:
        """List Kalshi events for coarse-grained discovery matching.

        Events are dramatically less noisy than the raw global market feed and
        expose long-dated contracts that can be buried deep in market paging.
        """
        cursor: Optional[str] = None
        all_events: list[dict] = []

        for _ in range(max_pages):
            params = {"limit": str(page_size)}
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor

            data = await self._get_with_429_retry(
                f"{self.config.base_url}/events",
                params=params,
                path="/trade-api/v2/events",
            )

            events = list(data.get("events") or [])
            all_events.extend(events)
            cursor = data.get("cursor") or None
            if not cursor:
                break

        return all_events

    async def list_markets_for_event(self, event_ticker: str, limit: int = 50) -> list[dict]:
        """Fetch all submarkets for a specific Kalshi event ticker."""
        data = await self._get_with_429_retry(
            f"{self.config.base_url}/markets",
            params={"event_ticker": event_ticker, "limit": str(limit)},
            path="/trade-api/v2/markets",
        )
        return list(data.get("markets") or [])

    async def get_orderbook(self, market_id: str, depth: int = 100) -> dict:
        """Fetch a raw Kalshi market orderbook for mapping and execution checks."""
        return await self._get_with_429_retry(
            f"{self.config.base_url}/markets/{market_id}/orderbook",
            params={"depth": str(depth)},
            path=f"/trade-api/v2/markets/{market_id}/orderbook",
            max_retries=3,
            fallback_delay=2.0,
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    _BATCH_SIZE = 100  # tickers per bulk request; Kalshi allows up to ~1000 but 100 is safe

    async def fetch_markets(self) -> list:
        """Fetch all tracked markets from Kalshi using bulk ticker batching.

        Instead of 1 request per ticker (1000+ calls/cycle), we send batches of
        _BATCH_SIZE tickers via the ?tickers= param, reducing API calls ~100x.
        """
        self.refresh_tracked_markets()

        # Build flat map: kalshi_market_ticker -> [canonical_id, ...]
        ticker_to_canonicals: Dict[str, List[str]] = {}
        for canonical_id, mapping in MARKET_MAP.items():
            kalshi_ticker = str(mapping.get("kalshi", "") or "").strip()
            if kalshi_ticker:
                ticker_to_canonicals.setdefault(kalshi_ticker, []).append(canonical_id)

        if not ticker_to_canonicals:
            return []

        all_tickers = list(ticker_to_canonicals.keys())
        ticker_to_market: Dict[str, dict] = {}
        session = await self._get_session()

        num_batches = (len(all_tickers) + self._BATCH_SIZE - 1) // self._BATCH_SIZE
        logger.info("Kalshi bulk fetch: %d tickers in %d batches", len(all_tickers), num_batches)

        for batch_idx in range(0, len(all_tickers), self._BATCH_SIZE):
            batch = all_tickers[batch_idx : batch_idx + self._BATCH_SIZE]
            await self.rate_limiter.acquire()

            headers = {"Accept": "application/json"}
            if self.auth.is_authenticated:
                headers.update(self.auth.get_headers("GET", "/trade-api/v2/markets"))

            try:
                async with session.get(
                    f"{self.config.base_url}/markets",
                    params={"tickers": ",".join(batch), "limit": str(self._BATCH_SIZE)},
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for market in data.get("markets", []):
                            t = str(market.get("ticker", "") or "")
                            if t:
                                ticker_to_market[t] = market
                    elif resp.status == 429:
                        delay = self.rate_limiter.apply_retry_after(
                            resp.headers.get("Retry-After"),
                            fallback_delay=max(self.config.poll_interval * 4, 10.0),
                            reason="kalshi_429_bulk",
                        )
                        logger.warning(
                            "Kalshi rate limited on batch %d/%d, backing off %.1fs",
                            batch_idx // self._BATCH_SIZE + 1,
                            num_batches,
                            delay,
                        )
                    else:
                        text = await resp.text()
                        logger.warning(
                            "Kalshi bulk batch %d/%d HTTP %s: %s",
                            batch_idx // self._BATCH_SIZE + 1,
                            num_batches,
                            resp.status,
                            text[:200],
                        )
            except Exception as e:
                logger.error(
                    "Kalshi bulk batch %d/%d error: %s",
                    batch_idx // self._BATCH_SIZE + 1,
                    num_batches,
                    e,
                )

        # Fallback: fetch unresolved tickers individually via /markets/{ticker}
        # The bulk ?tickers= query doesn't resolve some market tickers (e.g.
        # political markets like CONTROLH-2026-D) even though the single-market
        # endpoint /markets/{ticker} returns them fine.
        unresolved = [t for t in all_tickers if t not in ticker_to_market]
        if unresolved:
            logger.info(
                "Kalshi: %d/%d tickers unresolved after bulk fetch, trying individual fallback",
                len(unresolved),
                len(all_tickers),
            )
            for ticker in unresolved:
                await self.rate_limiter.acquire()
                try:
                    path = f"/trade-api/v2/markets/{ticker}"
                    headers = {"Accept": "application/json"}
                    if self.auth.is_authenticated:
                        headers.update(self.auth.get_headers("GET", path))
                    async with session.get(
                        f"{self.config.base_url}/markets/{ticker}",
                        headers=headers,
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            market = data.get("market", data)
                            t = str(market.get("ticker", "") or "")
                            if t:
                                ticker_to_market[t] = market
                                logger.info("Kalshi individual fetch OK: %s", t)
                        else:
                            logger.debug(
                                "Kalshi individual fetch %s: HTTP %s",
                                ticker,
                                resp.status,
                            )
                except Exception as e:
                    logger.debug("Kalshi individual fetch %s error: %s", ticker, e)

        # Map fetched markets back to canonical IDs and publish to price store
        results = []
        for kalshi_ticker, canonical_ids in ticker_to_canonicals.items():
            market = ticker_to_market.get(kalshi_ticker)
            if not market:
                continue
            for canonical_id in canonical_ids:
                price = self._build_price_point(canonical_id, kalshi_ticker, market)
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

        logger.info(
            "Kalshi bulk fetch complete: %d/%d markets resolved",
            len(results),
            len(all_tickers),
        )
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
        yes_ask = self._normalize_price(self._market_float(market, "yes_ask", "ask", "yes_ask_dollars"))
        no_bid = self._normalize_price(self._market_float(market, "no_bid", "no_bid_dollars"))
        no_ask = self._normalize_price(self._market_float(market, "no_ask", "no_ask_dollars"))
        yes_price = yes_ask or yes_bid
        no_price = no_ask or no_bid or 0.0

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
            yes_ask=float(yes_ask or 0),
            no_bid=float(no_bid or 0),
            no_ask=float(no_ask or 0),
            fee_rate=KALSHI_TAKER_FEE_RATE,
            mapping_status=str(mapping.get("status", "candidate")),
            mapping_score=float(mapping.get("mapping_score", 0.0)),
            metadata={
                "event_ticker": event_ticker,
                "market_title": market.get("title", ""),
                "yes_sub_title": market.get("yes_sub_title", ""),
                "no_sub_title": market.get("no_sub_title", ""),
                "response_price_units": market.get("response_price_units", ""),
                "market_type": market.get("market_type", ""),
                "result": market.get("result", ""),
                "can_close_early": market.get("can_close_early", False),
                "cap_strike": market.get("cap_strike"),
                "floor_strike": market.get("floor_strike"),
                "category": market.get("category", ""),
                "sub_title": market.get("sub_title", ""),
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
        logger.info(f"Tracking {len(self._ticker_map)} market tickers (bulk fetch, batch size={self._BATCH_SIZE})")
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
