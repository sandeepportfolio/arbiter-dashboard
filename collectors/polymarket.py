"""
Polymarket Price Collector
- Gamma API for event metadata and market discovery
- CLOB API for live orderbook prices
- WebSocket for real-time updates
"""
import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import aiohttp

from ..config.settings import PolymarketConfig, MARKET_MAP, polymarket_fee
from ..utils.price_store import PricePoint, PriceStore
from ..utils.retry import CircuitBreaker, RateLimiter, retry_with_backoff

logger = logging.getLogger("arbiter.collector.polymarket")


class PolymarketCollector:
    """
    Collects prices from Polymarket via Gamma API + CLOB.
    Uses slug-based event lookup (query params unreliable).
    """

    def __init__(self, config: PolymarketConfig, price_store: PriceStore):
        self.config = config
        self.store = price_store
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        # Resilience
        self.circuit_gamma = CircuitBreaker("poly-gamma", failure_threshold=5, recovery_timeout=30)
        self.circuit_clob = CircuitBreaker("poly-clob", failure_threshold=5, recovery_timeout=20)
        self.circuit_ws = CircuitBreaker("poly-ws", failure_threshold=3, recovery_timeout=60)
        self.rate_limiter = RateLimiter("polymarket", max_requests=10, window_seconds=1.0)
        self.consecutive_errors = 0
        self.ws_reconnect_count = 0
        # Build reverse map: polymarket_slug -> list of (canonical_id, question_match)
        # Multiple canonical IDs can share a slug (e.g. DEM_SENATE vs GOP_SENATE)
        self._slug_map: Dict[str, List[tuple]] = {}
        # Map canonical_id -> condition_ids for CLOB subscription
        self._condition_ids: Dict[str, List[str]] = {}
        for canonical_id, mapping in MARKET_MAP.items():
            if "polymarket" in mapping:
                slug = mapping["polymarket"]
                question_match = mapping.get("polymarket_question", "")
                if slug not in self._slug_map:
                    self._slug_map[slug] = []
                self._slug_map[slug].append((canonical_id, question_match))

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def discover_markets(self) -> Dict[str, dict]:
        """
        Discover condition_ids by fetching events via slug.
        Returns {canonical_id: {condition_id, outcomes, prices}}.
        Matches specific sub-markets using polymarket_question from MARKET_MAP.
        """
        session = await self._get_session()
        discovered = {}

        for slug, canonical_entries in self._slug_map.items():
            try:
                url = f"{self.config.gamma_url}/events"
                params = {"slug": slug}

                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        events = await resp.json()
                        if events and len(events) > 0:
                            event = events[0]
                            markets = event.get("markets", [])
                            if not markets:
                                continue

                            for canonical_id, question_match in canonical_entries:
                                # Find the matching sub-market by question text
                                market = None
                                if question_match:
                                    for m in markets:
                                        q = m.get("question", "")
                                        if question_match.lower() in q.lower():
                                            market = m
                                            break
                                if market is None:
                                    # Fallback: first market if no question filter
                                    market = markets[0]

                                condition_id = market.get("conditionId", "")
                                outcome_prices = market.get("outcomePrices", "")

                                # Parse outcome prices
                                try:
                                    if isinstance(outcome_prices, str):
                                        prices = json.loads(outcome_prices)
                                    else:
                                        prices = outcome_prices
                                    yes_price = float(prices[0]) if prices else 0.0
                                    no_price = float(prices[1]) if len(prices) > 1 else 1.0 - yes_price
                                except (json.JSONDecodeError, IndexError, TypeError):
                                    yes_price = 0.0
                                    no_price = 0.0

                                discovered[canonical_id] = {
                                    "condition_id": condition_id,
                                    "slug": slug,
                                    "yes_price": yes_price,
                                    "no_price": no_price,
                                    "volume": float(market.get("volume", 0)),
                                    "liquidity": float(market.get("liquidity", 0)),
                                }

                                if condition_id:
                                    self._condition_ids[canonical_id] = [condition_id]

                                logger.debug(
                                    f"Polymarket {canonical_id}: YES={yes_price:.3f} NO={no_price:.3f} "
                                    f"vol=${discovered[canonical_id]['volume']:,.0f} "
                                    f"(matched: {market.get('question', '')[:50]})"
                                )
                    else:
                        logger.warning(f"Polymarket gamma {resp.status} for slug={slug}")

            except Exception as e:
                logger.error(f"Polymarket discover error for {slug}: {e}")

        return discovered

    async def fetch_clob_prices(self) -> list:
        """Fetch live orderbook prices from CLOB API for discovered markets."""
        session = await self._get_session()
        results = []

        for canonical_id, cids in self._condition_ids.items():
            for cid in cids:
                try:
                    url = f"{self.config.clob_url}/book"
                    params = {"token_id": cid}

                    async with session.get(url, params=params) as resp:
                        if resp.status == 200:
                            book = await resp.json()
                            bids = book.get("bids", [])
                            asks = book.get("asks", [])

                            best_bid = float(bids[0]["price"]) if bids else 0.0
                            best_ask = float(asks[0]["price"]) if asks else 0.0

                            price = PricePoint(
                                platform="polymarket",
                                canonical_id=canonical_id,
                                yes_price=best_ask if best_ask > 0 else best_bid,
                                no_price=1.0 - best_bid if best_bid > 0 else 1.0 - best_ask,
                                yes_volume=sum(float(a.get("size", 0)) for a in asks[:5]),
                                no_volume=sum(float(b.get("size", 0)) for b in bids[:5]),
                                timestamp=time.time(),
                                raw_market_id=cid,
                            )
                            results.append(price)
                            await self.store.put(price)
                        elif resp.status == 429:
                            logger.warning("Polymarket CLOB rate limited")
                            await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"Polymarket CLOB error for {canonical_id}: {e}")

        return results

    async def fetch_gamma_prices(self) -> list:
        """Fetch prices from Gamma API (less real-time but more reliable)."""
        discovered = await self.discover_markets()
        results = []

        for canonical_id, data in discovered.items():
            price = PricePoint(
                platform="polymarket",
                canonical_id=canonical_id,
                yes_price=data["yes_price"],
                no_price=data["no_price"],
                yes_volume=data["volume"],
                no_volume=data["volume"],
                timestamp=time.time(),
                raw_market_id=data.get("condition_id", ""),
            )
            results.append(price)
            await self.store.put(price)

        return results

    async def connect_websocket(self):
        """
        Connect to Polymarket WebSocket for real-time orderbook updates.
        Subscribes to all tracked condition_ids.
        """
        if not self._condition_ids:
            await asyncio.sleep(10)  # wait before retrying discovery
            return

        session = await self._get_session()
        all_cids = []
        for cids in self._condition_ids.values():
            all_cids.extend(cids)

        try:
            self._ws = await session.ws_connect(self.config.ws_url)
            logger.info(f"Polymarket WebSocket connected, subscribing to {len(all_cids)} markets")

            # Subscribe to markets
            for cid in all_cids:
                sub_msg = {
                    "type": "subscribe",
                    "channel": "market",
                    "market": cid,
                }
                await self._ws.send_json(sub_msg)

            # Process messages
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_ws_message(data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning("Polymarket WebSocket closed/error")
                    break

        except Exception as e:
            logger.error(f"Polymarket WebSocket error: {e}")
        finally:
            if self._ws and not self._ws.closed:
                await self._ws.close()

    async def _handle_ws_message(self, data: dict):
        """Process a WebSocket message and update price store."""
        msg_type = data.get("type", "")
        if msg_type in ("book", "price_change"):
            market_id = data.get("market", "")
            # Find canonical_id for this condition_id
            canonical_id = None
            for cid, cids in self._condition_ids.items():
                if market_id in cids:
                    canonical_id = cid
                    break
            if not canonical_id:
                return

            yes_price = float(data.get("price", 0))
            price = PricePoint(
                platform="polymarket",
                canonical_id=canonical_id,
                yes_price=yes_price,
                no_price=1.0 - yes_price,
                yes_volume=0,
                no_volume=0,
                timestamp=time.time(),
                raw_market_id=market_id,
            )
            await self.store.put(price)

    async def fetch_balance(self) -> Optional[float]:
        """
        Fetch Polymarket USDC balance on Polygon.
        Derives wallet address from private key, queries on-chain USDC balance.
        """
        if not self.config.private_key:
            logger.debug("Polymarket wallet key not configured, skipping balance")
            return None

        try:
            from web3 import Web3
            from eth_account import Account

            # Polygon mainnet public RPC
            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
            if not w3.is_connected():
                # Fallback RPCs
                for rpc in ["https://rpc.ankr.com/polygon", "https://polygon.llamarpc.com"]:
                    w3 = Web3(Web3.HTTPProvider(rpc))
                    if w3.is_connected():
                        break

            if not w3.is_connected():
                logger.warning("Polymarket balance: cannot connect to Polygon RPC")
                return None

            # Derive wallet address from private key
            acct = Account.from_key(self.config.private_key)
            wallet_address = acct.address

            # USDC on Polygon: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174 (PoS bridged)
            # USDC native on Polygon: 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359
            USDC_POS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
            USDC_NATIVE = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")

            # Minimal ERC20 ABI for balanceOf + decimals
            ERC20_ABI = [
                {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
                 "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}],
                 "type": "function"},
                {"constant": True, "inputs": [],
                 "name": "decimals", "outputs": [{"name": "", "type": "uint8"}],
                 "type": "function"},
            ]

            total_balance = 0.0
            for label, addr in [("USDC.e", USDC_POS), ("USDC", USDC_NATIVE)]:
                try:
                    contract = w3.eth.contract(address=addr, abi=ERC20_ABI)
                    decimals = contract.functions.decimals().call()
                    raw_balance = contract.functions.balanceOf(
                        Web3.to_checksum_address(wallet_address)
                    ).call()
                    balance = raw_balance / (10 ** decimals)
                    total_balance += balance
                    if balance > 0:
                        logger.debug(f"Polymarket {label} balance: ${balance:.2f}")
                except Exception as e:
                    logger.debug(f"Polymarket {label} balance check failed: {e}")

            logger.info(f"Polymarket total USDC balance: ${total_balance:.2f}")
            return total_balance

        except ImportError:
            logger.warning("Polymarket balance: web3 not installed (pip install web3)")
            return None
        except Exception as e:
            logger.error(f"Polymarket balance error: {e}")
            return None

    async def run(self):
        """Main collection loop: discover → poll → optionally WS."""
        self._running = True
        logger.info(f"Polymarket collector started (poll interval: {self.config.poll_interval}s)")
        logger.info(f"Tracking {len(self._slug_map)} markets: {list(self._slug_map.keys())}")

        # Initial discovery
        await self.discover_markets()

        # Run polling and WS in parallel
        poll_task = asyncio.create_task(self._poll_loop())
        ws_task = asyncio.create_task(self._ws_loop())

        try:
            await asyncio.gather(poll_task, ws_task)
        except asyncio.CancelledError:
            poll_task.cancel()
            ws_task.cancel()

    async def _poll_loop(self):
        """REST polling with circuit breaker and retry."""
        while self._running:
            try:
                await self.rate_limiter.acquire()

                if self._condition_ids and self.circuit_clob.can_execute():
                    try:
                        await retry_with_backoff(
                            self.fetch_clob_prices, retries=2, base_delay=0.5,
                            circuit=self.circuit_clob,
                        )
                        self.consecutive_errors = 0
                    except Exception:
                        # Fall back to Gamma
                        if self.circuit_gamma.can_execute():
                            await retry_with_backoff(
                                self.fetch_gamma_prices, retries=2, base_delay=1.0,
                                circuit=self.circuit_gamma,
                            )
                elif self.circuit_gamma.can_execute():
                    await retry_with_backoff(
                        self.fetch_gamma_prices, retries=2, base_delay=1.0,
                        circuit=self.circuit_gamma,
                    )
                else:
                    logger.warning("Polymarket: both circuits open, waiting")
                    await asyncio.sleep(15)
                    continue

                await asyncio.sleep(self.config.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.consecutive_errors += 1
                backoff = min(2 ** min(self.consecutive_errors, 5), 30)
                logger.error(f"Polymarket poll error (#{self.consecutive_errors}), backoff {backoff}s: {e}")
                await asyncio.sleep(backoff)

    async def _ws_loop(self):
        """WebSocket connection with auto-reconnect and circuit breaker."""
        while self._running:
            if not self.circuit_ws.can_execute():
                logger.warning(f"Polymarket WS circuit open, waiting {self.circuit_ws.recovery_timeout}s")
                await asyncio.sleep(self.circuit_ws.recovery_timeout / 2)
                continue
            try:
                await self.connect_websocket()
                self.circuit_ws.record_success()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.circuit_ws.record_failure()
                self.ws_reconnect_count += 1
                delay = min(3 * (1.5 ** min(self.ws_reconnect_count, 8)), 60)
                logger.error(f"Polymarket WS reconnect #{self.ws_reconnect_count} in {delay:.0f}s: {e}")
                await asyncio.sleep(delay)

    async def stop(self):
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
