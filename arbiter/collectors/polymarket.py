"""
Polymarket collector using Gamma discovery, CLOB books, and market WebSockets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, Optional

import aiohttp

from ..config.settings import MARKET_MAP, POLYMAKET_DEFAULT_TAKER_FEE_RATE, PolymarketConfig
from ..utils.price_store import PricePoint, PriceStore
from ..utils.retry import CircuitBreaker, RateLimiter, retry_with_backoff

logger = logging.getLogger("arbiter.collector.polymarket")


def _coerce_json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class PolymarketCollector:
    def __init__(self, config: PolymarketConfig, price_store: PriceStore):
        self.config = config
        self.store = price_store
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self.circuit_gamma = CircuitBreaker("poly-gamma", failure_threshold=5, recovery_timeout=30)
        self.circuit_clob = CircuitBreaker("poly-clob", failure_threshold=5, recovery_timeout=20)
        self.circuit_ws = CircuitBreaker("poly-ws", failure_threshold=3, recovery_timeout=60)
        self.rate_limiter = RateLimiter("polymarket", max_requests=10, window_seconds=1.0)
        self.consecutive_errors = 0
        self.total_fetches = 0
        self.total_errors = 0
        self.ws_reconnect_count = 0
        self._slug_map: Dict[str, list[tuple[str, str]]] = {}
        self._token_registry: Dict[str, dict] = {}
        self._token_to_market: Dict[str, tuple[str, str]] = {}
        self._clob_client = None  # Set externally when ClobClient is available for fee lookup

        for canonical_id, mapping in MARKET_MAP.items():
            slug = str(mapping.get("polymarket", "") or "")
            question_match = str(mapping.get("polymarket_question", "") or "")
            if slug:
                self._slug_map.setdefault(slug, []).append((canonical_id, question_match))

    def set_clob_client(self, client):
        """Inject ClobClient for dynamic fee rate lookups (per D-09)."""
        self._clob_client = client
        logger.info("PolymarketCollector: ClobClient injected for dynamic fee rate lookups")

    def _get_market_category(self, market: dict) -> str:
        """Extract market category from Gamma API market data."""
        category = market.get("category", market.get("groupItemTitle", "")).lower()
        if not category:
            tags = market.get("tags", [])
            category = tags[0].lower() if tags else "default"
        return category

    def _fetch_dynamic_fee_rate(self, token_id: str, category: str = "default") -> float:
        """
        Fetch per-token fee rate from Polymarket via ClobClient.get_fee_rate_bps().
        Falls back to hardcoded category rates with a warning log (per D-10).

        Args:
            token_id: The CLOB token ID for the market outcome
            category: Market category for fallback rate lookup

        Returns:
            Fee rate as a decimal (e.g., 0.04 for 4%)
        """
        FALLBACK_RATES = {
            "crypto": 0.072,
            "sports": 0.03,
            "finance": 0.04,
            "politics": 0.04,
            "economics": 0.05,
            "culture": 0.05,
            "weather": 0.05,
            "tech": 0.04,
            "mentions": 0.04,
            "geopolitics": 0.0,
            "default": 0.05,
        }
        if not token_id or self._clob_client is None:
            return FALLBACK_RATES.get(category, FALLBACK_RATES["default"])

        try:
            bps = self._clob_client.get_fee_rate_bps(token_id)
            # T-01-11: Validate returned bps is non-negative and within reasonable range
            if not isinstance(bps, (int, float)) or bps < 0 or bps > 10000:
                fallback = FALLBACK_RATES.get(category, FALLBACK_RATES["default"])
                logger.warning(
                    "Fee rate bps out of range for token %s: %s -- using fallback rate %.4f",
                    token_id[:12], bps, fallback,
                )
                return fallback
            rate = bps / 10000.0
            logger.debug("Dynamic fee rate for token %s: %.4f (from %d bps)", token_id[:12], rate, bps)
            return rate
        except Exception as exc:
            fallback = FALLBACK_RATES.get(category, FALLBACK_RATES["default"])
            logger.warning(
                "Fee rate fetch failed for token %s (category=%s): %s -- using fallback rate %.4f",
                token_id[:12], category, exc, fallback,
            )
            return fallback

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def discover_markets(self) -> Dict[str, dict]:
        session = await self._get_session()
        discovered: Dict[str, dict] = {}

        for slug, canonical_entries in self._slug_map.items():
            try:
                await self.rate_limiter.acquire()
                async with session.get(f"{self.config.gamma_url}/events", params={"slug": slug}) as response:
                    if response.status == 429:
                        delay = self.rate_limiter.apply_retry_after(
                            response.headers.get("Retry-After"),
                            fallback_delay=max(self.config.poll_interval * 3, 3.0),
                            reason="polymarket_gamma_429",
                        )
                        logger.warning("Polymarket gamma rate limited for slug=%s, backing off %.1fs", slug, delay)
                        continue
                    if response.status != 200:
                        logger.warning("Polymarket gamma %s for slug=%s", response.status, slug)
                        continue
                    events = await response.json()

                if not events:
                    continue
                event = events[0]
                markets = event.get("markets", []) or []
                for canonical_id, question_match in canonical_entries:
                    market = self._match_market(markets, question_match)
                    if not market:
                        continue

                    token_ids = _coerce_json_list(market.get("clobTokenIds"))
                    outcomes = _coerce_json_list(market.get("outcomes"))
                    prices = _coerce_json_list(market.get("outcomePrices"))

                    yes_token_id = str(token_ids[0]) if len(token_ids) > 0 else ""
                    no_token_id = str(token_ids[1]) if len(token_ids) > 1 else ""
                    yes_price = _safe_float(prices[0]) if len(prices) > 0 else 0.0
                    no_price = _safe_float(prices[1]) if len(prices) > 1 else max(1.0 - yes_price, 0.0)
                    # Per D-09: Try dynamic fee fetch first, fall back to category-based rates (D-10)
                    fee_rate = self._fetch_dynamic_fee_rate(
                        token_id=yes_token_id,
                        category=self._get_market_category(market),
                    )
                    mapping = MARKET_MAP.get(canonical_id, {})

                    discovered[canonical_id] = {
                        "slug": slug,
                        "question": market.get("question", ""),
                        "yes_token_id": yes_token_id,
                        "no_token_id": no_token_id,
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "volume": _safe_float(market.get("volume")),
                        "liquidity": _safe_float(market.get("liquidity")),
                        "fee_rate": fee_rate,
                        "mapping_status": str(mapping.get("status", "candidate")),
                        "mapping_score": float(mapping.get("mapping_score", 0.0)),
                    }
                    self._token_registry[canonical_id] = discovered[canonical_id]
                    if yes_token_id:
                        self._token_to_market[yes_token_id] = (canonical_id, "yes")
                    if no_token_id:
                        self._token_to_market[no_token_id] = (canonical_id, "no")
                    logger.debug(
                        "Polymarket %s -> yes=%s no=%s question=%s",
                        canonical_id,
                        yes_token_id[:12],
                        no_token_id[:12],
                        market.get("question", "")[:60],
                    )
            except Exception as exc:
                self.total_errors += 1
                logger.error("Polymarket discover error for %s: %s", slug, exc)

        return discovered

    async def fetch_clob_prices(self) -> list[PricePoint]:
        session = await self._get_session()
        results: list[PricePoint] = []

        for canonical_id, token_data in self._token_registry.items():
            yes_token_id = token_data.get("yes_token_id", "")
            no_token_id = token_data.get("no_token_id", "")
            yes_book = await self._fetch_book(session, yes_token_id) if yes_token_id else {}
            no_book = await self._fetch_book(session, no_token_id) if no_token_id else {}

            yes_bid = self._best_price(yes_book.get("bids"))
            yes_ask = self._best_price(yes_book.get("asks"))
            no_bid = self._best_price(no_book.get("bids"))
            no_ask = self._best_price(no_book.get("asks"))
            yes_price = yes_ask or yes_bid or token_data.get("yes_price", 0.0)
            no_price = no_ask or no_bid or token_data.get("no_price", max(1.0 - yes_price, 0.0))

            mapping = MARKET_MAP.get(canonical_id, {})
            price = PricePoint(
                platform="polymarket",
                canonical_id=canonical_id,
                yes_price=yes_price,
                no_price=no_price,
                yes_volume=self._depth_volume(yes_book.get("asks") or yes_book.get("bids")),
                no_volume=self._depth_volume(no_book.get("asks") or no_book.get("bids")),
                timestamp=time.time(),
                raw_market_id=yes_token_id or no_token_id,
                yes_market_id=yes_token_id,
                no_market_id=no_token_id,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                fee_rate=float(token_data.get("fee_rate", POLYMAKET_DEFAULT_TAKER_FEE_RATE)),
                mapping_status=str(mapping.get("status", "candidate")),
                mapping_score=float(mapping.get("mapping_score", 0.0)),
                metadata={
                    "slug": token_data.get("slug", ""),
                    "question": token_data.get("question", ""),
                },
            )
            results.append(price)
            await self.store.put(price)
        return results

    async def fetch_gamma_prices(self) -> list[PricePoint]:
        discovered = await self.discover_markets()
        results: list[PricePoint] = []
        for canonical_id, token_data in discovered.items():
            price = PricePoint(
                platform="polymarket",
                canonical_id=canonical_id,
                yes_price=float(token_data.get("yes_price", 0.0)),
                no_price=float(token_data.get("no_price", 0.0)),
                yes_volume=float(token_data.get("liquidity", 0.0)),
                no_volume=float(token_data.get("liquidity", 0.0)),
                timestamp=time.time(),
                raw_market_id=str(token_data.get("yes_token_id") or token_data.get("no_token_id") or ""),
                yes_market_id=str(token_data.get("yes_token_id", "")),
                no_market_id=str(token_data.get("no_token_id", "")),
                fee_rate=float(token_data.get("fee_rate", POLYMAKET_DEFAULT_TAKER_FEE_RATE)),
                mapping_status=str(token_data.get("mapping_status", "candidate")),
                mapping_score=float(token_data.get("mapping_score", 0.0)),
                metadata={
                    "slug": token_data.get("slug", ""),
                    "question": token_data.get("question", ""),
                },
            )
            results.append(price)
            await self.store.put(price)
        return results

    async def connect_websocket(self):
        if not self._token_to_market:
            await asyncio.sleep(5)
            return

        session = await self._get_session()
        tracked_tokens = list(self._token_to_market.keys())
        self._ws = await session.ws_connect(self.config.ws_url)
        logger.info("Polymarket WebSocket connected, subscribing to %s tokens", len(tracked_tokens))

        for token_id in tracked_tokens:
            await self._ws.send_json({"type": "subscribe", "channel": "market", "market": token_id})

        async for message in self._ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    await self._handle_ws_message(json.loads(message.data))
                except json.JSONDecodeError:
                    continue
            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                logger.warning("Polymarket WebSocket closed/error")
                break

    async def _handle_ws_message(self, data: dict):
        token_id = str(data.get("market", "") or data.get("token_id", "") or "")
        if not token_id or token_id not in self._token_to_market:
            return

        canonical_id, side = self._token_to_market[token_id]
        existing = await self.store.get("polymarket", canonical_id)
        yes_price = existing.yes_price if existing else 0.0
        no_price = existing.no_price if existing else 0.0

        price_value = _safe_float(data.get("price") or data.get("best_ask") or 0.0)
        if side == "yes":
            yes_price = price_value or yes_price
        else:
            no_price = price_value or no_price

        token_data = self._token_registry.get(canonical_id, {})
        mapping = MARKET_MAP.get(canonical_id, {})
        await self.store.put(
            PricePoint(
                platform="polymarket",
                canonical_id=canonical_id,
                yes_price=yes_price,
                no_price=no_price or max(1.0 - yes_price, 0.0),
                yes_volume=existing.yes_volume if existing else 0.0,
                no_volume=existing.no_volume if existing else 0.0,
                timestamp=time.time(),
                raw_market_id=token_id,
                yes_market_id=str(token_data.get("yes_token_id", "")),
                no_market_id=str(token_data.get("no_token_id", "")),
                yes_bid=existing.yes_bid if existing else 0.0,
                yes_ask=existing.yes_ask if existing else 0.0,
                no_bid=existing.no_bid if existing else 0.0,
                no_ask=existing.no_ask if existing else 0.0,
                fee_rate=float(token_data.get("fee_rate", POLYMAKET_DEFAULT_TAKER_FEE_RATE)),
                mapping_status=str(mapping.get("status", "candidate")),
                mapping_score=float(mapping.get("mapping_score", 0.0)),
                metadata={"source": "websocket", "question": token_data.get("question", "")},
            )
        )

    async def _fetch_book(self, session: aiohttp.ClientSession, token_id: str) -> dict:
        if not token_id:
            return {}
        await self.rate_limiter.acquire()
        try:
            async with session.get(f"{self.config.clob_url}/book", params={"token_id": token_id}) as response:
                if response.status == 200:
                    return await response.json()
                if response.status == 429:
                    delay = self.rate_limiter.apply_retry_after(
                        response.headers.get("Retry-After"),
                        fallback_delay=max(self.config.poll_interval * 2, 2.0),
                        reason="polymarket_clob_429",
                    )
                    logger.warning("Polymarket CLOB rate limited for token %s, backing off %.1fs", token_id[:12], delay)
                return {}
        except Exception as exc:
            logger.error("Polymarket CLOB error for token %s: %s", token_id[:12], exc)
            return {}

    def _match_market(self, markets: list, question_match: str) -> Optional[dict]:
        if question_match:
            for market in markets:
                question = str(market.get("question", "") or "")
                if question_match.lower() in question.lower():
                    return market
        return markets[0] if markets else None

    def _extract_fee_rate(self, market: dict) -> float:
        for key in ("feeRate", "fee_rate", "takerFeeRate"):
            if key in market and market.get(key) is not None:
                return _safe_float(market.get(key), POLYMAKET_DEFAULT_TAKER_FEE_RATE)
        for key in ("feeRateBps", "takerFeeBps", "takerBaseFee"):
            if key in market and market.get(key) is not None:
                return _safe_float(market.get(key), POLYMAKET_DEFAULT_TAKER_FEE_RATE * 10000) / 10000.0
        fee_schedule = market.get("feeSchedule") or {}
        if isinstance(fee_schedule, dict):
            for key in ("takerFeeRate", "takerFee", "taker"):
                if key in fee_schedule and fee_schedule.get(key) is not None:
                    value = _safe_float(fee_schedule.get(key), 0.0)
                    return value / 10000.0 if value > 1 else value
        return POLYMAKET_DEFAULT_TAKER_FEE_RATE

    @staticmethod
    def _best_price(levels) -> float:
        if not levels:
            return 0.0
        try:
            return float(levels[0].get("price", 0.0))
        except (AttributeError, IndexError, TypeError, ValueError):
            return 0.0

    @staticmethod
    def _depth_volume(levels) -> float:
        if not levels:
            return 0.0
        total = 0.0
        for level in levels[:5]:
            total += _safe_float(level.get("size", 0.0))
        return total

    async def fetch_balance(self) -> Optional[float]:
        if not self.config.private_key:
            logger.debug("Polymarket wallet key not configured, skipping balance")
            return None

        try:
            from eth_account import Account
            from web3 import Web3

            rpc_candidates = ("https://polygon-rpc.com", "https://rpc.ankr.com/polygon", "https://polygon.llamarpc.com")
            w3 = None
            for rpc in rpc_candidates:
                candidate = Web3(Web3.HTTPProvider(rpc))
                if candidate.is_connected():
                    w3 = candidate
                    break
            if w3 is None:
                logger.warning("Polymarket balance: cannot connect to Polygon RPC")
                return None

            wallet_address = Account.from_key(self.config.private_key).address
            usdc_addresses = (
                Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
                Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"),
            )
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function",
                },
                {
                    "constant": True,
                    "inputs": [],
                    "name": "decimals",
                    "outputs": [{"name": "", "type": "uint8"}],
                    "type": "function",
                },
            ]

            total_balance = 0.0
            for address in usdc_addresses:
                try:
                    contract = w3.eth.contract(address=address, abi=erc20_abi)
                    decimals = contract.functions.decimals().call()
                    raw_balance = contract.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call()
                    total_balance += raw_balance / (10 ** decimals)
                except Exception as exc:
                    logger.debug("Polymarket balance check failed for %s: %s", address, exc)
            return total_balance
        except ImportError:
            logger.warning("Polymarket balance: web3 not installed")
            return None
        except Exception as exc:
            logger.error("Polymarket balance error: %s", exc)
            return None

    async def run(self):
        self._running = True
        logger.info("Polymarket collector started (poll interval: %ss)", self.config.poll_interval)
        await self.discover_markets()

        poll_task = asyncio.create_task(self._poll_loop())
        ws_task = asyncio.create_task(self._ws_loop())
        try:
            await asyncio.gather(poll_task, ws_task)
        except asyncio.CancelledError:
            poll_task.cancel()
            ws_task.cancel()

    async def _poll_loop(self):
        while self._running:
            try:
                self.total_fetches += 1
                if self._token_registry and self.circuit_clob.can_execute():
                    try:
                        await retry_with_backoff(self.fetch_clob_prices, retries=2, base_delay=0.5, circuit=self.circuit_clob)
                        self.consecutive_errors = 0
                    except Exception:
                        if self.circuit_gamma.can_execute():
                            await retry_with_backoff(self.fetch_gamma_prices, retries=2, base_delay=1.0, circuit=self.circuit_gamma)
                elif self.circuit_gamma.can_execute():
                    await retry_with_backoff(self.fetch_gamma_prices, retries=2, base_delay=1.0, circuit=self.circuit_gamma)
                else:
                    logger.warning("Polymarket: both REST circuits open, waiting")
                    await asyncio.sleep(15)
                    continue

                await asyncio.sleep(self.config.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.total_errors += 1
                self.consecutive_errors += 1
                delay = min(2 ** min(self.consecutive_errors, 5), 30)
                logger.error("Polymarket poll error (#%s), backoff %ss: %s", self.consecutive_errors, delay, exc)
                await asyncio.sleep(delay)

    async def _ws_loop(self):
        while self._running:
            if not self.config.ws_enabled or not self.circuit_ws.can_execute():
                await asyncio.sleep(self.circuit_ws.recovery_timeout / 2)
                continue
            try:
                await self.connect_websocket()
                self.circuit_ws.record_success()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.total_errors += 1
                self.circuit_ws.record_failure()
                self.ws_reconnect_count += 1
                delay = min(3 * (1.5 ** min(self.ws_reconnect_count, 8)), 60)
                logger.error("Polymarket WS reconnect #%s in %ss: %s", self.ws_reconnect_count, int(delay), exc)
                await asyncio.sleep(delay)

    async def stop(self):
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
