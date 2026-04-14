"""
ARBITER — Execution Engine + Risk Manager
Places orders on Kalshi and Polymarket when profitable arbs detected.
PredictIt has no trade API — logs instructions for manual execution.

SAFETY: Starts in dry_run mode. All trades are simulated until explicitly enabled.
"""
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

import aiohttp

from ..config.settings import ArbiterConfig, ScannerConfig
from ..scanner.arbitrage import ArbitrageOpportunity
from ..monitor.balance import BalanceMonitor

logger = logging.getLogger("arbiter.execution")


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SIMULATED = "simulated"


@dataclass
class Order:
    """Represents a single order leg."""
    order_id: str
    platform: str
    market_id: str
    canonical_id: str
    side: str          # "yes" or "no"
    price: float
    quantity: int
    status: OrderStatus
    fill_price: float = 0.0
    fill_qty: int = 0
    timestamp: float = 0.0
    error: str = ""


@dataclass
class ArbExecution:
    """Tracks a full arbitrage execution (2 legs)."""
    arb_id: str
    opportunity: ArbitrageOpportunity
    leg_yes: Order
    leg_no: Order
    status: str = "pending"  # pending, partial, complete, failed
    realized_pnl: float = 0.0
    timestamp: float = 0.0


class RiskManager:
    """
    Pre-trade risk checks.
    Prevents excessive exposure and enforces position limits.
    """

    def __init__(self, config: ScannerConfig):
        self.config = config
        self._open_positions: Dict[str, float] = {}  # canonical_id -> USD exposure
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._max_daily_trades: int = 100
        self._max_daily_loss: float = -50.0  # stop at $50 loss
        self._max_total_exposure: float = 500.0

    def check_trade(self, opp: ArbitrageOpportunity) -> Tuple[bool, str]:
        """
        Run pre-trade risk checks. Returns (approved, reason).
        """
        # Check confidence threshold
        if opp.confidence < self.config.confidence_threshold:
            return False, f"Low confidence: {opp.confidence:.2f} < {self.config.confidence_threshold}"

        # Check minimum edge
        if opp.net_edge_cents < self.config.min_edge_cents:
            return False, f"Edge too thin: {opp.net_edge_cents:.1f}¢ < {self.config.min_edge_cents}¢"

        # Check daily trade limit
        if self._daily_trades >= self._max_daily_trades:
            return False, f"Daily trade limit reached: {self._daily_trades}"

        # Check daily PnL stop
        if self._daily_pnl <= self._max_daily_loss:
            return False, f"Daily loss limit: ${self._daily_pnl:.2f} <= ${self._max_daily_loss:.2f}"

        # Check position limits
        existing = self._open_positions.get(opp.canonical_id, 0.0)
        new_exposure = opp.suggested_qty * (opp.yes_price + opp.no_price)
        if existing + new_exposure > self.config.max_position_usd:
            return False, f"Position limit: ${existing + new_exposure:.2f} > ${self.config.max_position_usd:.2f}"

        # Check total exposure
        total = sum(self._open_positions.values()) + new_exposure
        if total > self._max_total_exposure:
            return False, f"Total exposure limit: ${total:.2f} > ${self._max_total_exposure:.2f}"

        # PredictIt $850 cap
        if opp.yes_platform == "predictit":
            pi_exposure = opp.suggested_qty * opp.yes_price
            if pi_exposure > 850:
                return False, f"PredictIt cap: ${pi_exposure:.2f} > $850"
        if opp.no_platform == "predictit":
            pi_exposure = opp.suggested_qty * opp.no_price
            if pi_exposure > 850:
                return False, f"PredictIt cap: ${pi_exposure:.2f} > $850"

        return True, "approved"

    def record_trade(self, canonical_id: str, exposure: float, pnl: float = 0.0):
        """Record a completed trade for risk tracking."""
        self._open_positions[canonical_id] = self._open_positions.get(canonical_id, 0.0) + exposure
        self._daily_pnl += pnl
        self._daily_trades += 1

    def reset_daily(self):
        """Reset daily counters (call at midnight)."""
        self._daily_pnl = 0.0
        self._daily_trades = 0
        logger.info("Risk manager daily counters reset")


class ExecutionEngine:
    """
    Executes arbitrage trades across platforms.
    Supports dry-run simulation and live trading.
    """

    def __init__(self, config: ArbiterConfig, balance_monitor: BalanceMonitor,
                 collectors: Optional[Dict[str, Any]] = None):
        self.config = config
        self.scanner_config = config.scanner
        self.balance_monitor = balance_monitor
        self.risk = RiskManager(config.scanner)
        self._running = False
        self._executions: List[ArbExecution] = []
        self._execution_count = 0
        # Collectors for reusing auth + sessions
        self._collectors = collectors or {}
        self._own_session: Optional[aiohttp.ClientSession] = None
        # Polymarket CLOB client (lazy init)
        self._poly_clob_client = None

    async def execute_opportunity(self, opp: ArbitrageOpportunity) -> Optional[ArbExecution]:
        """
        Execute an arbitrage opportunity.
        In dry_run mode, simulates the trade.
        """
        # Risk check
        approved, reason = self.risk.check_trade(opp)
        if not approved:
            logger.debug(f"Trade rejected by risk manager: {reason}")
            return None

        self._execution_count += 1
        arb_id = f"ARB-{self._execution_count:06d}"

        if self.scanner_config.dry_run:
            return await self._simulate_execution(arb_id, opp)
        else:
            return await self._live_execution(arb_id, opp)

    async def _simulate_execution(self, arb_id: str, opp: ArbitrageOpportunity) -> ArbExecution:
        """Simulate trade execution for testing."""
        now = time.time()

        leg_yes = Order(
            order_id=f"{arb_id}-YES",
            platform=opp.yes_platform,
            market_id=opp.yes_market_id,
            canonical_id=opp.canonical_id,
            side="yes",
            price=opp.yes_price,
            quantity=opp.suggested_qty,
            status=OrderStatus.SIMULATED,
            fill_price=opp.yes_price,
            fill_qty=opp.suggested_qty,
            timestamp=now,
        )

        leg_no = Order(
            order_id=f"{arb_id}-NO",
            platform=opp.no_platform,
            market_id=opp.no_market_id,
            canonical_id=opp.canonical_id,
            side="no",
            price=opp.no_price,
            quantity=opp.suggested_qty,
            status=OrderStatus.SIMULATED,
            fill_price=opp.no_price,
            fill_qty=opp.suggested_qty,
            timestamp=now,
        )

        execution = ArbExecution(
            arb_id=arb_id,
            opportunity=opp,
            leg_yes=leg_yes,
            leg_no=leg_no,
            status="simulated",
            realized_pnl=opp.net_edge * opp.suggested_qty,
            timestamp=now,
        )

        self._executions.append(execution)
        self.risk.record_trade(
            opp.canonical_id,
            opp.suggested_qty * (opp.yes_price + opp.no_price),
        )

        logger.info(
            f"[DRY RUN] {arb_id}: {opp.canonical_id} "
            f"YES@{opp.yes_platform}={opp.yes_price:.2f} × {opp.suggested_qty} + "
            f"NO@{opp.no_platform}={opp.no_price:.2f} × {opp.suggested_qty} → "
            f"simulated PnL=${execution.realized_pnl:.2f}"
        )

        return execution

    async def _live_execution(self, arb_id: str, opp: ArbitrageOpportunity) -> Optional[ArbExecution]:
        """
        Live trade execution.
        Places orders on Kalshi and/or Polymarket.
        Logs manual instructions for PredictIt.
        """
        now = time.time()
        legs = []

        for side, platform, price, market_id in [
            ("yes", opp.yes_platform, opp.yes_price, opp.yes_market_id),
            ("no", opp.no_platform, opp.no_price, opp.no_market_id),
        ]:
            if platform == "kalshi":
                order = await self._place_kalshi_order(
                    arb_id, market_id, opp.canonical_id, side, price, opp.suggested_qty
                )
            elif platform == "polymarket":
                order = await self._place_polymarket_order(
                    arb_id, market_id, opp.canonical_id, side, price, opp.suggested_qty
                )
            elif platform == "predictit":
                order = self._log_predictit_instruction(
                    arb_id, market_id, opp.canonical_id, side, price, opp.suggested_qty
                )
            else:
                order = Order(
                    order_id=f"{arb_id}-{side.upper()}-UNKNOWN",
                    platform=platform, market_id=market_id,
                    canonical_id=opp.canonical_id, side=side,
                    price=price, quantity=opp.suggested_qty,
                    status=OrderStatus.FAILED, error=f"Unknown platform: {platform}",
                    timestamp=now,
                )
            legs.append(order)

        # Determine execution status from leg outcomes
        yes_ok = legs[0].status not in (OrderStatus.FAILED, OrderStatus.CANCELLED)
        no_ok = legs[1].status not in (OrderStatus.FAILED, OrderStatus.CANCELLED)

        if yes_ok and no_ok:
            exec_status = "submitted"
        elif yes_ok or no_ok:
            exec_status = "partial"
            logger.warning(
                f"[EXECUTION] {arb_id}: PARTIAL — "
                f"YES={legs[0].status.value} NO={legs[1].status.value}"
            )
        else:
            exec_status = "failed"
            logger.error(
                f"[EXECUTION] {arb_id}: FAILED — "
                f"YES: {legs[0].error} | NO: {legs[1].error}"
            )

        execution = ArbExecution(
            arb_id=arb_id,
            opportunity=opp,
            leg_yes=legs[0],
            leg_no=legs[1],
            status=exec_status,
            timestamp=now,
        )
        self._executions.append(execution)

        if exec_status == "submitted":
            self.risk.record_trade(
                opp.canonical_id,
                opp.suggested_qty * (opp.yes_price + opp.no_price),
            )
            logger.info(
                f"[EXECUTION] {arb_id}: {opp.canonical_id} LIVE — "
                f"YES@{opp.yes_platform} + NO@{opp.no_platform}"
            )

        return execution

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session for API calls."""
        if self._own_session is None or self._own_session.closed:
            self._own_session = aiohttp.ClientSession()
        return self._own_session

    async def _place_kalshi_order(self, arb_id: str, market_id: str,
                                    canonical_id: str, side: str,
                                    price: float, qty: int) -> Order:
        """
        Place order on Kalshi via REST API.
        POST /trade-api/v2/portfolio/orders
        Uses RSA-PSS signature authentication from KalshiCollector.
        """
        now = time.time()
        kalshi_col = self._collectors.get("kalshi")

        if not kalshi_col or not kalshi_col.auth.is_authenticated:
            logger.error(f"[KALSHI] Cannot place order — auth not configured")
            return Order(
                order_id=f"{arb_id}-{side.upper()}-KALSHI",
                platform="kalshi", market_id=market_id,
                canonical_id=canonical_id, side=side,
                price=price, quantity=qty,
                status=OrderStatus.FAILED,
                error="Kalshi auth not configured (missing API key or private key)",
                timestamp=now,
            )

        session = await self._get_session()

        # Kalshi order body
        # price is in cents (integer 1-99), side is "yes"/"no"
        price_cents = int(round(price * 100))
        price_cents = max(1, min(99, price_cents))

        order_body = {
            "ticker": market_id,
            "client_order_id": f"{arb_id}-{side.upper()}-{uuid.uuid4().hex[:8]}",
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": qty,
        }
        # Kalshi API: set yes_price or no_price based on side
        if side == "yes":
            order_body["yes_price"] = price_cents
        else:
            order_body["no_price"] = price_cents

        path = "/trade-api/v2/portfolio/orders"
        url = f"{self.config.kalshi.base_url}/portfolio/orders"
        headers = kalshi_col.auth.get_headers("POST", path)

        logger.info(
            f"[KALSHI LIVE] Placing {side.upper()} order: {market_id} "
            f"@ {price_cents}¢ × {qty} contracts"
        )

        try:
            async with session.post(url, json=order_body, headers=headers) as resp:
                resp_text = await resp.text()

                if resp.status in (200, 201):
                    data = json.loads(resp_text)
                    order_data = data.get("order", data)
                    kalshi_order_id = order_data.get("order_id", f"{arb_id}-{side.upper()}")
                    status_str = order_data.get("status", "resting")

                    # Map Kalshi status to our OrderStatus
                    status_map = {
                        "resting": OrderStatus.SUBMITTED,
                        "pending": OrderStatus.PENDING,
                        "executed": OrderStatus.FILLED,
                        "canceled": OrderStatus.CANCELLED,
                    }
                    order_status = status_map.get(status_str, OrderStatus.SUBMITTED)

                    fill_price = order_data.get("avg_price", price_cents) / 100.0
                    fill_qty = order_data.get("count_filled", 0)

                    logger.info(
                        f"[KALSHI LIVE] Order placed: {kalshi_order_id} "
                        f"status={status_str} filled={fill_qty}/{qty}"
                    )

                    return Order(
                        order_id=kalshi_order_id,
                        platform="kalshi", market_id=market_id,
                        canonical_id=canonical_id, side=side,
                        price=price, quantity=qty,
                        status=order_status,
                        fill_price=fill_price,
                        fill_qty=fill_qty,
                        timestamp=now,
                    )
                else:
                    error_msg = f"Kalshi API {resp.status}: {resp_text[:300]}"
                    logger.error(f"[KALSHI] Order failed — {error_msg}")
                    return Order(
                        order_id=f"{arb_id}-{side.upper()}-KALSHI",
                        platform="kalshi", market_id=market_id,
                        canonical_id=canonical_id, side=side,
                        price=price, quantity=qty,
                        status=OrderStatus.FAILED,
                        error=error_msg,
                        timestamp=now,
                    )
        except Exception as e:
            error_msg = f"Kalshi request exception: {str(e)}"
            logger.error(f"[KALSHI] {error_msg}")
            return Order(
                order_id=f"{arb_id}-{side.upper()}-KALSHI",
                platform="kalshi", market_id=market_id,
                canonical_id=canonical_id, side=side,
                price=price, quantity=qty,
                status=OrderStatus.FAILED,
                error=error_msg,
                timestamp=now,
            )

    def _get_poly_clob_client(self):
        """Lazy-init Polymarket CLOB client with wallet signing."""
        if self._poly_clob_client is not None:
            return self._poly_clob_client

        private_key = self.config.polymarket.private_key
        if not private_key:
            return None

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType

            self._poly_clob_client = ClobClient(
                host=self.config.polymarket.clob_url,
                key=private_key,
                chain_id=self.config.polymarket.chain_id,
            )
            # Derive API credentials (creates/fetches API key from Polymarket)
            self._poly_clob_client.set_api_creds(self._poly_clob_client.create_or_derive_api_creds())
            logger.info("[POLYMARKET] CLOB client initialized with wallet signing")
            return self._poly_clob_client
        except Exception as e:
            logger.error(f"[POLYMARKET] Failed to init CLOB client: {e}")
            return None

    async def _place_polymarket_order(self, arb_id: str, market_id: str,
                                        canonical_id: str, side: str,
                                        price: float, qty: int) -> Order:
        """
        Place order on Polymarket via CLOB API.
        Uses py-clob-client for EIP-712 wallet signing.
        market_id here is the condition_id (token_id).
        """
        now = time.time()

        if not self.config.polymarket.private_key:
            logger.error("[POLYMARKET] Cannot place order — POLY_PRIVATE_KEY not set in .env")
            return Order(
                order_id=f"{arb_id}-{side.upper()}-POLY",
                platform="polymarket", market_id=market_id,
                canonical_id=canonical_id, side=side,
                price=price, quantity=qty,
                status=OrderStatus.FAILED,
                error="Polymarket wallet key not configured (set POLY_PRIVATE_KEY in .env)",
                timestamp=now,
            )

        try:
            # Run the blocking CLOB client call in a thread executor
            clob = self._get_poly_clob_client()
            if clob is None:
                raise RuntimeError("CLOB client initialization failed")

            from py_clob_client.clob_types import OrderArgs, OrderType

            # Polymarket: BUY side maps to token_id
            # "yes" = buy YES token, "no" = buy NO token
            # The token_id (market_id) is the condition_id for the YES outcome
            poly_side = "BUY"  # We always buy the side we want

            order_args = OrderArgs(
                price=round(price, 2),
                size=float(qty),
                side=poly_side,
                token_id=market_id,
            )

            logger.info(
                f"[POLYMARKET LIVE] Placing {side.upper()} order: "
                f"token={market_id[:16]}... @ {price:.2f} × {qty}"
            )

            # Create and sign the order (blocking — run in executor)
            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(
                None, lambda: clob.create_and_post_order(order_args)
            )

            # Parse response
            if signed_order and isinstance(signed_order, dict):
                poly_order_id = signed_order.get("orderID", signed_order.get("id", f"{arb_id}-{side.upper()}"))
                status_str = signed_order.get("status", "live")
                is_success = signed_order.get("success", True)

                if not is_success:
                    error_msg = signed_order.get("errorMsg", "Order rejected by Polymarket")
                    logger.error(f"[POLYMARKET] Order rejected: {error_msg}")
                    return Order(
                        order_id=f"{arb_id}-{side.upper()}-POLY",
                        platform="polymarket", market_id=market_id,
                        canonical_id=canonical_id, side=side,
                        price=price, quantity=qty,
                        status=OrderStatus.FAILED,
                        error=error_msg,
                        timestamp=now,
                    )

                logger.info(
                    f"[POLYMARKET LIVE] Order placed: {poly_order_id} "
                    f"status={status_str}"
                )

                return Order(
                    order_id=str(poly_order_id),
                    platform="polymarket", market_id=market_id,
                    canonical_id=canonical_id, side=side,
                    price=price, quantity=qty,
                    status=OrderStatus.SUBMITTED,
                    timestamp=now,
                )
            else:
                logger.warning(f"[POLYMARKET] Unexpected response: {signed_order}")
                return Order(
                    order_id=f"{arb_id}-{side.upper()}-POLY",
                    platform="polymarket", market_id=market_id,
                    canonical_id=canonical_id, side=side,
                    price=price, quantity=qty,
                    status=OrderStatus.SUBMITTED,
                    timestamp=now,
                )

        except Exception as e:
            error_msg = f"Polymarket order exception: {str(e)}"
            logger.error(f"[POLYMARKET] {error_msg}")
            return Order(
                order_id=f"{arb_id}-{side.upper()}-POLY",
                platform="polymarket", market_id=market_id,
                canonical_id=canonical_id, side=side,
                price=price, quantity=qty,
                status=OrderStatus.FAILED,
                error=error_msg,
                timestamp=now,
            )

    def _log_predictit_instruction(self, arb_id: str, market_id: str,
                                    canonical_id: str, side: str,
                                    price: float, qty: int) -> Order:
        """Log manual execution instructions for PredictIt (no trade API)."""
        logger.warning(
            f"[PREDICTIT MANUAL] {arb_id}: "
            f"Go to https://www.predictit.org/markets/detail/{market_id.split(':')[0]} → "
            f"Buy {side.upper()} @ ${price:.2f} × {qty} contracts"
        )
        return Order(
            order_id=f"{arb_id}-{side.upper()}-PI-MANUAL",
            platform="predictit", market_id=market_id,
            canonical_id=canonical_id, side=side,
            price=price, quantity=qty,
            status=OrderStatus.PENDING,
            timestamp=time.time(),
            error="Manual execution required — PredictIt has no trade API",
        )

    @property
    def execution_history(self) -> List[ArbExecution]:
        return self._executions

    @property
    def stats(self) -> dict:
        simulated = sum(1 for e in self._executions if e.status == "simulated")
        live = sum(1 for e in self._executions if e.status != "simulated")
        total_pnl = sum(e.realized_pnl for e in self._executions)
        return {
            "total_executions": len(self._executions),
            "simulated": simulated,
            "live": live,
            "total_pnl": round(total_pnl, 2),
            "dry_run": self.scanner_config.dry_run,
        }

    async def run(self, arb_queue: asyncio.Queue):
        """Process arbitrage opportunities from the scanner."""
        self._running = True
        logger.info(f"Execution engine started (dry_run={self.scanner_config.dry_run})")

        while self._running:
            try:
                opp = await asyncio.wait_for(arb_queue.get(), timeout=5.0)
                result = await self.execute_opportunity(opp)
                if result:
                    # Notify balance monitor for alerts
                    await self.balance_monitor.alert_opportunity(opp)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Execution error: {e}")

        logger.info("Execution engine stopped")

    async def stop(self):
        self._running = False
        if self._own_session and not self._own_session.closed:
            await self._own_session.close()
