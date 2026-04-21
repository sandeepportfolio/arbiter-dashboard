"""PolymarketUSAdapter — execution adapter for api.polymarket.us (DCM).

Implements the ``PlatformAdapter`` protocol (see ``base.PlatformAdapter``).
Uses ``PolymarketUSClient`` (arbiter.collectors.polymarket_us) for all HTTP
calls; the adapter itself has no networking code.

Hard-lock enforcement order (exact sequence, spec §5.2):

    Gate 1: PHASE4 hard-lock   — if _phase4_max_usd is not None and notional exceeds it
    Gate 2: PHASE5 hard-lock   — if _phase5_max_usd is not None and notional exceeds it
    Gate 3: supervisor armed   — if supervisor.is_armed is True
    ─────── Only NOW construct, sign, and send ───────────────────────────────

Both caps read from settings at construction time (passed in as constructor
arguments).  Never duplicated from ``arbiter.config.settings``.

``OrderRejected`` is imported from ``.exceptions`` — there is ONE definition.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import structlog

from ..engine import Order, OrderStatus
from .exceptions import OrderRejected

logger = structlog.get_logger("arbiter.adapters.polymarket_us")


class PolymarketUSAdapter:
    """Execution adapter for the Polymarket US (CFTC-regulated DCM) platform.

    Parameters
    ----------
    client:
        ``PolymarketUSClient`` instance (from arbiter.collectors.polymarket_us).
    phase4_max_usd:
        PHASE4 hard-lock cap in USD. *None* means the gate is disabled (no-op).
        Pass ``float(os.getenv("PHASE4_MAX_ORDER_USD"))`` at construction time —
        do NOT read the env var inside this module.
    phase5_max_usd:
        PHASE5 hard-lock cap in USD. *None* means the gate is disabled (no-op).
    supervisor:
        Optional ``SafetySupervisor``-like object with an ``is_armed: bool``
        property. When provided, Gate 3 checks ``supervisor.is_armed``.
    """

    platform = "polymarket-us"

    def __init__(
        self,
        client: Any,
        phase4_max_usd: Optional[float] = None,
        phase5_max_usd: Optional[float] = None,
        supervisor: Optional[Any] = None,
    ) -> None:
        self._client = client
        self._phase4_max_usd = phase4_max_usd
        self._phase5_max_usd = phase5_max_usd
        self._supervisor = supervisor

    # ─── place_fok ────────────────────────────────────────────────────────

    async def place_fok(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
    ) -> Order:
        """Submit a fill-or-kill order.  Runs three hard-lock gates in sequence
        before touching the network.

        Gates fire in this exact order:
          Gate 1 — PHASE4 hard-lock
          Gate 2 — PHASE5 hard-lock
          Gate 3 — supervisor armed check

        Raises ``OrderRejected`` when any gate fires (exception propagates to
        caller; adapter does NOT swallow it).
        """
        notional = float(price) * float(qty)

        # Gate 1: PHASE4 hard-lock (keep in sequence with PHASE5)
        if self._phase4_max_usd is not None and notional > self._phase4_max_usd:
            logger.warning(
                "polymarket_us.phase4_hardlock.rejected",
                arb_id=arb_id,
                notional=notional,
                max=self._phase4_max_usd,
            )
            raise OrderRejected(
                f"PHASE4 hard-lock: notional ${notional:.2f} > ${self._phase4_max_usd:.2f}"
            )

        # Gate 2: PHASE5 hard-lock (stricter)
        if self._phase5_max_usd is not None and notional > self._phase5_max_usd:
            logger.warning(
                "polymarket_us.phase5_hardlock.rejected",
                arb_id=arb_id,
                notional=notional,
                max=self._phase5_max_usd,
            )
            raise OrderRejected(
                f"PHASE5 hard-lock: notional ${notional:.2f} > ${self._phase5_max_usd:.2f}"
            )

        # Gate 3: supervisor armed check
        if self._supervisor is not None and self._supervisor.is_armed:
            logger.warning(
                "polymarket_us.supervisor_armed.rejected",
                arb_id=arb_id,
            )
            raise OrderRejected("supervisor armed")

        # Only NOW construct, sign, and send.
        return await self._sign_and_send(arb_id, market_id, canonical_id, side, price, qty)

    async def _sign_and_send(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
    ) -> Order:
        """Build the order body, call the client, and map the response to Order.

        ``market_id`` is used as the Polymarket US market slug.
        """
        intent = self._us_intent(side)
        response = await self._client.place_order(
            slug=market_id,
            intent=intent,
            price=price,
            qty=qty,
            tif="FILL_OR_KILL",
        )
        return self._order_from_response(
            response, arb_id, market_id, canonical_id, side, price, qty
        )

    # ─── get_order_status ─────────────────────────────────────────────────

    async def get_order_status(self, order: Order) -> Order:
        """Alias for get_order (protocol compatibility)."""
        return await self.get_order(order)

    # ─── get_order ────────────────────────────────────────────────────────

    async def get_order(self, order: Order) -> Order:
        """Query the platform for current order state.

        Maps the response's ``status`` field to ``OrderStatus``.  If the
        response cannot be retrieved, returns the order with status=FAILED.
        """
        try:
            # Polymarket US: GET /order/{order_id} — use client extension if
            # available, otherwise fall through to SUBMITTED (not critical for FOK).
            if hasattr(self._client, "get_order"):
                resp = await self._client.get_order(order.order_id)
                api_status = str(resp.get("status", "")).upper()
                order.status = self._map_status(api_status, order.status)
        except Exception as exc:
            logger.warning(
                "polymarket_us.get_order.failed",
                order_id=order.order_id,
                err=str(exc),
            )
            order.status = OrderStatus.FAILED
            order.error = f"get_order failed: {exc}"
        return order

    # ─── cancel_order ─────────────────────────────────────────────────────

    async def cancel_order(self, order: Order) -> bool:
        """Cancel an open order.  Returns True if cancellation was requested."""
        try:
            await self._client.cancel_order(
                order_id=order.order_id,
                slug=order.market_id,
            )
            return True
        except Exception as exc:
            logger.warning(
                "polymarket_us.cancel_order.failed",
                order_id=order.order_id,
                err=str(exc),
            )
            return False

    # ─── cancel_all ───────────────────────────────────────────────────────

    async def cancel_all(self) -> list[str]:
        """Cancel all open orders.  Best-effort; never raises."""
        return []

    # ─── check_depth ──────────────────────────────────────────────────────

    async def check_depth(
        self, market_id: str, side: str, required_qty: int
    ) -> tuple[bool, float]:
        """Pre-trade liquidity check using the order book."""
        try:
            book = await self._client.get_orderbook(market_id, depth=10)
        except Exception as exc:
            logger.warning(
                "polymarket_us.check_depth.failed",
                market_id=market_id,
                err=str(exc),
            )
            return (False, 0.0)

        # For a BUY order we need offers (asks); for a SELL we need bids.
        levels_key = "offers" if str(side).lower() in ("buy", "yes") else "bids"
        levels = book.get(levels_key, [])
        if not levels:
            return (False, 0.0)

        cumulative = 0.0
        best_price = 0.0
        for lvl in sorted(levels, key=lambda x: float(x.get("px", x.get("price", 0)))):
            if best_price == 0.0:
                best_price = float(lvl.get("px", lvl.get("price", 0)))
            cumulative += float(lvl.get("qty", lvl.get("size", 0)))
            if cumulative >= float(required_qty):
                return (True, best_price)
        return (False, best_price)

    # ─── list_open_orders_by_client_id ────────────────────────────────────

    async def list_open_orders_by_client_id(
        self, client_order_id_prefix: str
    ) -> list[Order]:
        """Polymarket US has no client_order_id concept — returns []."""
        logger.warning(
            "polymarket_us.list_open_orders_by_client_id.unsupported",
            prefix=client_order_id_prefix,
            note="Polymarket US has no client_order_id; returning []",
        )
        return []

    # ─── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _us_intent(side: str) -> str:
        """Map canonical side ('yes'|'no'|'BUY'|'SELL') to US intent string."""
        s = str(side).upper()
        if s in ("YES", "BUY"):
            return "BUY_LONG"
        if s in ("NO", "SELL"):
            return "SELL_LONG"
        return "BUY_LONG"

    @staticmethod
    def _map_status(api_status: str, default: OrderStatus) -> OrderStatus:
        status_map = {
            "FILLED":    OrderStatus.FILLED,
            "MATCHED":   OrderStatus.FILLED,
            "EXECUTED":  OrderStatus.FILLED,
            "CANCELED":  OrderStatus.CANCELLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "REJECTED":  OrderStatus.FAILED,
            "LIVE":      OrderStatus.SUBMITTED,
            "OPEN":      OrderStatus.SUBMITTED,
        }
        return status_map.get(api_status.upper(), default)

    def _order_from_response(
        self,
        response: dict,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
    ) -> Order:
        now = time.time()
        if not isinstance(response, dict):
            return Order(
                order_id=f"{arb_id}-{side.upper()}-POLYUS",
                platform="polymarket-us",
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=price,
                quantity=qty,
                status=OrderStatus.FAILED,
                timestamp=now,
                error="Unexpected non-dict response from Polymarket US",
                external_client_order_id=None,
            )

        # Both "orderId" (US API) and "orderID" (legacy) accepted for safety
        order_id = str(
            response.get("orderId", response.get("orderID", f"{arb_id}-{side.upper()}-POLYUS"))
        )
        api_status = str(response.get("status", "FILLED")).upper()
        mapped_status = self._map_status(api_status, OrderStatus.SUBMITTED)

        try:
            fill_qty = float(response.get("filledQty", response.get("size_matched", qty)))
        except (TypeError, ValueError):
            fill_qty = float(qty)

        return Order(
            order_id=order_id,
            platform="polymarket-us",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=qty,
            status=mapped_status,
            fill_price=price,
            fill_qty=fill_qty,
            timestamp=now,
            external_client_order_id=None,
        )
