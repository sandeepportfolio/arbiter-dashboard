"""ForecastExAdapter — execution adapter for the IBKR-routed ForecastEx venue.

Implements the ``PlatformAdapter`` protocol (see ``base.PlatformAdapter``).
All network calls go through ``ForecastExClient`` (arbiter.collectors.forecastex);
the adapter itself only translates between the engine's order semantics
(YES/NO legs at probability prices) and the IBKR Client Portal API (BUY-only
LMT orders with IOC TIF).

Hard-lock enforcement order (matches ``polymarket_us.py`` spec §5.2):

    Gate 1: PHASE4 hard-lock — if _phase4_max_usd is not None and notional exceeds
    Gate 2: PHASE5 hard-lock — stricter cap
    Gate 3: supervisor armed
    ─────── Only NOW send the order ───────────────────────────────

ForecastEx-specific:
  - BUY-only: SELL legs raise ``OrderRejected`` immediately.
  - Prices are clamped to [0.01, 0.99] dollars and snapped to the cent tick.
  - IOC + LMT for the secondary leg; FOK is emulated as IOC where IBKR doesn't
    expose a native FOK (the engine treats partial fill on IOC as the same
    soft-naked recovery path as a FOK kill).
"""
from __future__ import annotations

import time
from typing import Any, Optional

import structlog

from ..engine import Order, OrderStatus
from .exceptions import OrderRejected

logger = structlog.get_logger("arbiter.adapters.forecastex")

_DEFAULT_TICK = 0.01
_MIN_PRICE = 0.01
_MAX_PRICE = 0.99


class ForecastExAdapter:
    """Execution adapter for ForecastEx (via IBKR Client Portal Web API).

    Parameters
    ----------
    client:
        ``ForecastExClient`` instance (from arbiter.collectors.forecastex).
    phase4_max_usd / phase5_max_usd:
        Hard-lock caps in USD. ``None`` disables the gate.
    supervisor:
        Optional ``SafetySupervisor``-like object with ``is_armed: bool``.
    """

    platform = "forecastex"

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
        self.rate_limiter = getattr(client, "live_rate_limiter", None)
        self.circuit = getattr(client, "circuit", None)

    # ─── Hard-lock helpers ────────────────────────────────────────────────

    def _check_gates(self, *, arb_id: str, notional: float, op: str) -> None:
        if self._phase4_max_usd is not None and notional > self._phase4_max_usd:
            logger.warning(
                "forecastex.phase4_hardlock.rejected",
                arb_id=arb_id, notional=notional, max=self._phase4_max_usd, op=op,
            )
            raise OrderRejected(
                f"PHASE4 hard-lock: notional ${notional:.2f} > ${self._phase4_max_usd:.2f}"
            )
        if self._phase5_max_usd is not None and notional > self._phase5_max_usd:
            logger.warning(
                "forecastex.phase5_hardlock.rejected",
                arb_id=arb_id, notional=notional, max=self._phase5_max_usd, op=op,
            )
            raise OrderRejected(
                f"PHASE5 hard-lock: notional ${notional:.2f} > ${self._phase5_max_usd:.2f}"
            )
        if self._supervisor is not None and self._supervisor.is_armed:
            logger.warning("forecastex.supervisor_armed.rejected", arb_id=arb_id, op=op)
            raise OrderRejected("supervisor armed")

    # ─── Order placement ──────────────────────────────────────────────────

    async def place_fok(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        max_affordable: Optional[float] = None,
    ) -> Order:
        """ForecastEx has no native FOK — we emit IOC and let the engine route
        partial fills through the same soft-naked recovery path it uses for
        Polymarket US FOK rejections."""
        return await self._submit(
            arb_id, market_id, canonical_id, side, price, qty,
            tif="IOC", op="place_fok", max_affordable=max_affordable,
        )

    async def place_ioc(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        max_affordable: Optional[float] = None,
    ) -> Order:
        return await self._submit(
            arb_id, market_id, canonical_id, side, price, qty,
            tif="IOC", op="place_ioc", max_affordable=max_affordable,
        )

    async def _submit(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        *,
        tif: str,
        op: str,
        max_affordable: Optional[float],
    ) -> Order:
        notional = float(price) * float(qty)
        self._check_gates(arb_id=arb_id, notional=notional, op=op)

        # BUY-only constraint: closing a YES position means buying NO (and
        # vice versa). The engine NEVER asks ForecastEx to SELL.
        normalized_side = str(side).strip().upper()
        if normalized_side in ("SELL_YES", "SELL_NO", "SELL_LONG", "SELL_SHORT", "SELL"):
            raise OrderRejected(
                f"forecastex only supports BUY orders (got side={side!r})"
            )

        # YES vs NO leg routing. ForecastEx exposes both YES and NO as their
        # own conids; the scanner-supplied market_id IS the conid for the
        # specific contract being bought, so we always pass it through as-is
        # and submit a BUY. The probability we paid is the price the engine
        # passed in (already on the correct side).
        snapped = self._snap_to_tick(price)
        if max_affordable is not None and snapped > max_affordable + 1e-9:
            logger.warning(
                "forecastex.marketability.aborted_unprofitable",
                arb_id=arb_id, market_id=market_id, side=side,
                original_price=price, snapped=snapped, max_affordable=max_affordable,
            )
            raise OrderRejected(
                f"forecastex price {snapped:.4f} > max_affordable {max_affordable:.4f}"
            )

        try:
            response = await self._client.place_order(
                conid=market_id, side="BUY",
                price=snapped, quantity=int(qty), tif=tif,
            )
        except OrderRejected:
            raise
        except Exception as exc:
            logger.warning(
                "forecastex.place_order.failed",
                arb_id=arb_id, market_id=market_id, side=side, err=str(exc),
            )
            return Order(
                order_id=f"{arb_id}-{normalized_side or 'BUY'}-FCST",
                platform="forecastex",
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=price,
                quantity=int(qty),
                status=OrderStatus.FAILED,
                timestamp=time.time(),
                error=f"forecastex place_order failed: {exc}",
                external_client_order_id=None,
            )

        order = self._order_from_response(
            response, arb_id, market_id, canonical_id, side, snapped, int(qty),
        )
        logger.info(
            "forecastex.order.placed",
            arb_id=arb_id, market_id=market_id, side=side, tif=tif,
            price=snapped, qty=qty,
            order_status=order.status.value, fill_qty=order.fill_qty,
        )
        return order

    # ─── Status queries ───────────────────────────────────────────────────

    async def get_order(self, order: Order) -> Order:
        """Query IBKR for the current order state.

        Only definitive venue responses mutate ``order.status``. Transient
        errors (network blip, empty payload, malformed shape) leave the
        order unchanged and set ``idempotency_ambiguous=True`` so callers
        can distinguish "no fresh signal" from "venue confirmed FAILED".

        Previously every exception terminally marked the order FAILED,
        which let a single network blip during ``_poll_submitted_to_terminal``
        flip a resting Forecastex order to FAILED and trigger an
        unwarranted naked-leg unwind on the paired leg.
        """
        try:
            payload = await self._client.get_order(order.order_id)
        except Exception as exc:
            logger.warning(
                "forecastex.get_order.failed",
                order_id=order.order_id, err=str(exc),
            )
            order.idempotency_ambiguous = True
            order.error = f"get_order failed: {exc}"
            return order

        # IBKR returns either a single dict or a list of status dicts;
        # take the most recent entry.
        if isinstance(payload, list) and payload:
            payload = payload[-1]
        if not isinstance(payload, dict) or not payload:
            logger.warning(
                "forecastex.get_order.empty_payload",
                order_id=order.order_id,
            )
            order.idempotency_ambiguous = True
            return order

        api_status = str(
            payload.get("status")
            or payload.get("order_status")
            or payload.get("orderStatus")
            or ""
        ).upper()
        if not api_status:
            logger.warning(
                "forecastex.get_order.empty_status",
                order_id=order.order_id,
            )
            order.idempotency_ambiguous = True
            return order

        order.status = self._map_status(api_status, order.status)
        try:
            fill_qty = float(
                payload.get("filled_quantity")
                or payload.get("filledQuantity")
                or payload.get("cumQty")
                or 0.0
            )
            if fill_qty:
                order.fill_qty = int(fill_qty)
        except (TypeError, ValueError):
            pass
        try:
            avg_px = float(
                payload.get("avg_price")
                or payload.get("avgPrice")
                or payload.get("avgFillPrice")
                or 0.0
            )
            if avg_px:
                order.fill_price = (
                    avg_px if avg_px <= 1.0 else avg_px / 100.0
                )
        except (TypeError, ValueError):
            pass
        return order

    async def get_order_status(self, order: Order) -> Order:
        return await self.get_order(order)

    async def cancel_order(self, order: Order) -> bool:
        try:
            await self._client.cancel_order(order.order_id)
            return True
        except Exception as exc:
            logger.warning(
                "forecastex.cancel_order.failed",
                order_id=order.order_id, err=str(exc),
            )
            return False

    async def cancel_all(self) -> list[str]:
        try:
            return await self._client.cancel_all_open_orders()
        except Exception as exc:
            logger.warning("forecastex.cancel_all.failed", err=str(exc))
            return []

    async def list_open_orders_by_client_id(
        self, client_order_id_prefix: str,
    ) -> list[Order]:
        # IBKR has a ``cOID`` field for client-side IDs but the gateway does
        # not expose a query-by-prefix endpoint. Return an empty list to
        # match the Polymarket US adapter behaviour; the engine's startup
        # reconciliation will resync on next status poll.
        logger.warning(
            "forecastex.list_open_orders_by_client_id.unsupported",
            prefix=client_order_id_prefix,
        )
        return []

    # ─── Depth / executable-price helpers ─────────────────────────────────

    async def check_depth(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        """Best-effort depth check using the snapshot bid/ask sizes.

        IBKR exposes a separate ``iserver/marketdata/history`` endpoint for
        full-depth books, but the runtime cost (multiple round-trips per
        market per scan) outweighs the benefit on top-of-book ForecastEx
        liquidity. Use the snapshot's bid/ask size and treat any size at the
        touch as sufficient for the current ask.
        """
        try:
            snap = await self._client.market_snapshot(market_id)
        except Exception as exc:
            logger.warning(
                "forecastex.check_depth.failed",
                market_id=market_id, err=str(exc),
            )
            return (False, 0.0)
        if not isinstance(snap, dict):
            return (False, 0.0)
        bid = self._snap_dollar(snap.get("84"))
        ask = self._snap_dollar(snap.get("86"))
        side_is_yes = str(side).lower() in ("buy", "yes", "buy_long")
        # We are always BUYing on this venue, so size we care about is the
        # ASK side regardless of whether the leg is YES or NO (the NO leg
        # buys the NO conid, but the conid's own ASK is what fills it).
        try:
            size = float(snap.get("7296") or 0.0)
        except (TypeError, ValueError):
            size = 0.0
        best = ask if ask > 0 else bid
        if best <= 0:
            return (False, 0.0)
        return (size >= float(required_qty), best)

    async def best_executable_price(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        """Single-level book: the best executable price IS the ask, and we
        accept the IOC partial-fill semantics for shortfalls."""
        sufficient, price = await self.check_depth(market_id, side, required_qty)
        return (sufficient, price)

    # ─── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _snap_to_tick(price: float, tick: float = _DEFAULT_TICK) -> float:
        clipped = max(_MIN_PRICE, min(float(price), _MAX_PRICE))
        snapped = round(clipped / tick) * tick
        return round(max(_MIN_PRICE, min(snapped, _MAX_PRICE)), 4)

    @staticmethod
    def _snap_dollar(value) -> float:
        """Coerce an IBKR-stringified price into [0, 1] dollar units."""
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, str):
            value = value.lstrip("CHc h").strip()
        try:
            f = float(value)
        except (TypeError, ValueError):
            return 0.0
        if f <= 0:
            return 0.0
        if f > 1.0:
            return max(0.0, min(f / 100.0, 1.0))
        return max(0.0, min(f, 1.0))

    @staticmethod
    def _map_status(api_status: str, default: OrderStatus) -> OrderStatus:
        # Normalize: uppercase AND strip underscores so we accept both the
        # CamelCase wire format IBKR actually emits (``PartiallyFilled``,
        # ``PreSubmitted``) and the snake_case form some docs show
        # (``partially_filled``). Failing to match silently downgrades to
        # FAILED below — a partial fill that the engine treats as a hard
        # failure would skip the soft-naked recovery path.
        normalized = (api_status or "").upper().replace("_", "")
        if normalized in {"FILLED", "EXECUTED"}:
            return OrderStatus.FILLED
        if normalized in {"PARTIALLYFILLED", "PARTIAL"}:
            return OrderStatus.PARTIAL
        if normalized in {
            "CANCELLED", "CANCELED",
            "EXPIRED", "INACTIVE",
            "REPLACED",
        }:
            return OrderStatus.CANCELLED
        if normalized in {"REJECTED"}:
            return OrderStatus.FAILED
        if normalized in {"SUBMITTED", "PRESUBMITTED", "PENDINGSUBMIT", "OPEN", "WORKING"}:
            return OrderStatus.SUBMITTED
        # Unknown wire status: behave like polymarket_us — fail loud if the
        # default we'd fall through to is SUBMITTED, because the engine then
        # treats the order as still-open.
        if default == OrderStatus.SUBMITTED:
            return OrderStatus.FAILED
        return default

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
                order_id=f"{arb_id}-{str(side).upper() or 'BUY'}-FCST",
                platform="forecastex",
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=price,
                quantity=int(qty),
                status=OrderStatus.FAILED,
                timestamp=now,
                error="Unexpected non-dict response from ForecastEx",
                external_client_order_id=None,
            )

        # IBKR returns an order placement payload as either {"order_id": ...}
        # or a list under "items". Normalize.
        items = response.get("items") if isinstance(response, dict) else None
        if isinstance(items, list) and items:
            response = items[0] if isinstance(items[0], dict) else response

        order_id = str(
            response.get("order_id")
            or response.get("orderId")
            or response.get("id")
            or f"{arb_id}-{str(side).upper() or 'BUY'}-FCST"
        )
        api_status = str(
            response.get("order_status")
            or response.get("orderStatus")
            or response.get("status")
            or "SUBMITTED"
        ).upper()
        mapped = self._map_status(api_status, OrderStatus.SUBMITTED)

        try:
            fill_qty = float(
                response.get("filled_quantity")
                or response.get("filledQuantity")
                or response.get("cumQty")
                or 0.0
            )
        except (TypeError, ValueError):
            fill_qty = 0.0
        try:
            avg_px = float(
                response.get("avg_price")
                or response.get("avgPrice")
                or response.get("avgFillPrice")
                or 0.0
            )
        except (TypeError, ValueError):
            avg_px = 0.0
        fill_price = (avg_px if avg_px <= 1.0 else avg_px / 100.0) or price
        if mapped == OrderStatus.FILLED and fill_qty == 0.0:
            fill_qty = float(qty)

        return Order(
            order_id=order_id,
            platform="forecastex",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=int(qty),
            status=mapped,
            fill_price=fill_price,
            fill_qty=int(fill_qty),
            timestamp=now,
            external_client_order_id=None,
        )
