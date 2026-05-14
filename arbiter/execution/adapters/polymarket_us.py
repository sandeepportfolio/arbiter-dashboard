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
import math
import time
from typing import Any, Optional

import structlog

from arbiter.collectors.polymarket_us import _amount_value

from ..engine import Order, OrderStatus
from .exceptions import OrderRejected

logger = structlog.get_logger("arbiter.adapters.polymarket_us")

# Polymarket US default tick is 1¢. If the market metadata exposes a smaller
# tick (e.g. 0.001) we use that instead, but never below ``_MIN_TICK``.
_DEFAULT_TICK = 0.01
_MIN_TICK = 1e-4


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

    # Keep the platform key stable for the rest of the runtime. The venue is
    # still "polymarket" from the scanner/engine/readiness perspective even
    # though the transport is the US retail API.
    platform = "polymarket"

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

    # ─── place_fok ────────────────────────────────────────────────────────

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
        return await self._sign_and_send(
            arb_id, market_id, canonical_id, side, price, qty,
            max_affordable=max_affordable,
        )

    async def _sign_and_send(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        tif: str = "FILL_OR_KILL",
        max_affordable: Optional[float] = None,
    ) -> Order:
        """Build the order body, call the client, and map the response to Order.

        ``market_id`` is used as the Polymarket US market slug.

        ``tif`` defaults to ``FILL_OR_KILL`` for backwards compatibility with
        ``place_fok``.  ``place_ioc`` overrides to ``IMMEDIATE_OR_CANCEL``,
        which is what cross-venue arb actually wants for the secondary leg —
        FOK rejects the entire order if even one contract can't fill at the
        limit, while IOC fills what's available and the engine then unwinds
        the unfilled excess on the primary venue.

        ``max_affordable`` is the highest price (in YES-leg units) we can
        pay before the trade goes negative.  When passed, the marketability
        clamp aborts (raises ``OrderRejected``) rather than crossing it.
        """
        intent, request_price = self._us_order_params(side, price)

        # Marketability + tick-size guard (P0).  Polymarket US instantly
        # rejects IOC/FOK orders whose limit is non-marketable on receipt
        # (e.g. selling YES above the best bid, buying YES below the best
        # ask).  We fetch live BBO + tick metadata, clamp to the touch,
        # and snap to the market's tick before signing.
        request_price = await self._marketability_clamp_and_snap(
            arb_id=arb_id,
            slug=market_id,
            intent=intent,
            side=side,
            original_price=price,
            request_price=request_price,
            max_affordable=max_affordable,
        )

        response = await self._client.place_order(
            slug=market_id,
            intent=intent,
            price=request_price,
            qty=qty,
            tif=tif,
        )
        order = self._order_from_response(
            response, arb_id, market_id, canonical_id, side, price, qty
        )
        # Make the wire-level response inspectable from the engine's logs.
        # Without this, a Polymarket TIME_IN_FORCE_KILLED reply is invisible
        # because the response dict lives only in the local closure.
        if isinstance(response, dict):
            api_status = str(
                response.get(
                    "state",
                    response.get(
                        "status",
                        (response.get("executions") or [{}])[0].get("order", {}).get(
                            "state",
                            (response.get("executions") or [{}])[0].get("order", {}).get("status", ""),
                        )
                        if response.get("executions") else "",
                    ),
                )
            )
            # Capture rejection diagnostics. The execution adapter previously
            # logged only ``api_status`` so REJECTED responses left no trail
            # of WHY (min-size? geographic restriction? market suspended?).
            # Pull common Polymarket error fields from the top-level response
            # AND the first execution's order so we can diagnose patterns
            # like the DEM_HOUSE_2026 cascade where one slug always rejects.
            execution_order = (
                (response.get("executions") or [{}])[0].get("order") or {}
                if response.get("executions") else {}
            )
            rejection_reason = (
                response.get("rejectionReason")
                or response.get("errorMessage")
                or response.get("errorMsg")
                or response.get("error")
                or response.get("message")
                or execution_order.get("rejectionReason")
                or execution_order.get("statusReason")
                or execution_order.get("errorMessage")
            )
            log_payload = dict(
                arb_id=arb_id,
                slug=market_id,
                side=side,
                tif=tif,
                qty=qty,
                limit_price=price,
                api_status=api_status,
                order_status=order.status.value,
                fill_qty=order.fill_qty,
                fill_price=order.fill_price,
            )
            if rejection_reason:
                log_payload["rejection_reason"] = str(rejection_reason)[:300]
            if "REJECTED" in api_status.upper() or "KILLED" in api_status.upper():
                # Echo the full response (capped) so we can mine it later for
                # whatever field the API actually used.
                import json as _json
                try:
                    raw = _json.dumps(response, default=str)[:5000]
                except Exception:
                    raw = str(response)[:5000]
                log_payload["raw_response"] = raw
                logger.warning("polymarket_us.order.rejected", **log_payload)
                # Surface the rejection on the Order so retry classification
                # and the dashboard see a non-empty reason.  Previously the
                # Order returned with ``error=None`` and downstream classifiers
                # bucketed every rejection as "Platform error".
                if "REJECTED" in api_status.upper() and not order.error:
                    suffix = f": {str(rejection_reason)[:200]}" if rejection_reason else ""
                    order.error = f"polymarket_us {api_status}{suffix}"
            else:
                logger.info("polymarket_us.order.placed", **log_payload)
        return order

    # ─── place_ioc ────────────────────────────────────────────────────────

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
        """Submit an immediate-or-cancel order.

        Same hard-lock gates as ``place_fok`` (PHASE4, PHASE5, supervisor).
        Differs only in TIF: IOC fills what is available at the limit (or
        better) and cancels the rest, rather than killing the whole order if
        even one contract can't fill.  Used by the engine on the SECONDARY
        leg so a stale book doesn't strand the PRIMARY in a naked position.
        """
        notional = float(price) * float(qty)

        if self._phase4_max_usd is not None and notional > self._phase4_max_usd:
            logger.warning(
                "polymarket_us.phase4_hardlock.rejected",
                arb_id=arb_id, notional=notional, max=self._phase4_max_usd, op="place_ioc",
            )
            raise OrderRejected(
                f"PHASE4 hard-lock: notional ${notional:.2f} > ${self._phase4_max_usd:.2f}"
            )
        if self._phase5_max_usd is not None and notional > self._phase5_max_usd:
            logger.warning(
                "polymarket_us.phase5_hardlock.rejected",
                arb_id=arb_id, notional=notional, max=self._phase5_max_usd, op="place_ioc",
            )
            raise OrderRejected(
                f"PHASE5 hard-lock: notional ${notional:.2f} > ${self._phase5_max_usd:.2f}"
            )
        if self._supervisor is not None and self._supervisor.is_armed:
            logger.warning(
                "polymarket_us.supervisor_armed.rejected", arb_id=arb_id, op="place_ioc",
            )
            raise OrderRejected("supervisor armed")

        return await self._sign_and_send(
            arb_id, market_id, canonical_id, side, price, qty,
            tif="IMMEDIATE_OR_CANCEL",
            max_affordable=max_affordable,
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
                payload = resp.get("order", resp) if isinstance(resp, dict) else {}
                api_status = str(
                    payload.get("state", payload.get("status", ""))
                ).upper()
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
        try:
            if hasattr(self._client, "cancel_all_open_orders"):
                resp = await self._client.cancel_all_open_orders()
                order_ids = resp.get("canceledOrderIds") or resp.get("cancelledOrderIds") or []
                return [str(order_id) for order_id in order_ids]
        except Exception as exc:
            logger.warning(
                "polymarket_us.cancel_all.failed",
                err=str(exc),
            )
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

        market_data = book.get("marketData", book) if isinstance(book, dict) else {}

        # For BUY YES we consume long-side offers. For BUY NO we consume the
        # inverse of long-side bids.
        side_is_yes = str(side).lower() in ("buy", "yes")
        levels_key = "offers" if side_is_yes else "bids"
        levels = list(market_data.get(levels_key, []))
        if not levels:
            return (False, 0.0)

        cumulative = 0.0
        best_price = 0.0
        sort_reverse = not side_is_yes
        for lvl in sorted(
            levels,
            key=lambda x: _amount_value(x.get("px", x.get("price"))),
            reverse=sort_reverse,
        ):
            raw_price = _amount_value(lvl.get("px", lvl.get("price")))
            level_price = raw_price if side_is_yes else max(1.0 - raw_price, 0.0)
            if best_price == 0.0:
                best_price = level_price
            try:
                cumulative += float(lvl.get("qty", lvl.get("size", 0)))
            except (TypeError, ValueError):
                continue
            if cumulative >= float(required_qty):
                return (True, best_price)
        return (False, best_price)

    # ─── best_executable_price ────────────────────────────────────────────

    async def best_executable_price(
        self, market_id: str, side: str, required_qty: int
    ) -> tuple[bool, float]:
        """Walk the order book to find the worst price needed to fill
        ``required_qty`` for a buy. Used as the FOK limit price."""
        try:
            book = await self._client.get_orderbook(market_id, depth=10)
        except Exception as exc:
            logger.warning(
                "polymarket_us.executable_price.failed",
                market_id=market_id,
                err=str(exc),
            )
            return (False, 0.0)

        market_data = book.get("marketData", book) if isinstance(book, dict) else {}
        side_is_yes = str(side).lower() in ("buy", "yes")
        levels_key = "offers" if side_is_yes else "bids"
        levels = list(market_data.get(levels_key, []))
        if not levels:
            return (False, 0.0)

        cumulative = 0.0
        worst_price = 0.0
        sort_reverse = not side_is_yes
        for lvl in sorted(
            levels,
            key=lambda x: _amount_value(x.get("px", x.get("price"))),
            reverse=sort_reverse,
        ):
            raw_price = _amount_value(lvl.get("px", lvl.get("price")))
            level_price = raw_price if side_is_yes else max(1.0 - raw_price, 0.0)
            try:
                cumulative += float(lvl.get("qty", lvl.get("size", 0)))
            except (TypeError, ValueError):
                continue
            worst_price = level_price
            if cumulative >= float(required_qty):
                return (True, level_price)
        return (False, worst_price)

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

    async def _fetch_market_meta(self, slug: str) -> dict:
        """Return market metadata dict (or empty dict on failure).

        Reads tick size from the same ``get_market_by_slug`` response that
        scanner code already calls, so a cached/memoized client cuts the
        cost to zero on the hot path.
        """
        if not hasattr(self._client, "get_market_by_slug"):
            return {}
        try:
            payload = await self._client.get_market_by_slug(slug)
        except Exception as exc:
            logger.warning(
                "polymarket_us.market_meta.failed",
                slug=slug,
                err=str(exc),
            )
            return {}
        if not isinstance(payload, dict):
            return {}
        # API returns the market either at top-level or under "market" /
        # "marketData".  Probe each location.
        for key in ("market", "marketData"):
            sub = payload.get(key)
            if isinstance(sub, dict):
                return sub
        return payload

    @staticmethod
    def _tick_from_meta(meta: dict) -> float:
        """Extract the minimum price tick from market metadata.

        Polymarket US returns ``orderPriceMinTickSize`` (sometimes nested
        as ``{"value": "0.01"}``).  Default to ``_DEFAULT_TICK`` (1¢) when
        absent.  Never returns below ``_MIN_TICK`` to guard against zero
        / negative values that would break the snap math.
        """
        if not meta:
            return _DEFAULT_TICK
        raw = (
            meta.get("orderPriceMinTickSize")
            or meta.get("minTickSize")
            or meta.get("priceIncrement")
        )
        tick = _amount_value(raw) if raw is not None else 0.0
        if tick <= 0.0:
            return _DEFAULT_TICK
        return max(tick, _MIN_TICK)

    @staticmethod
    def _snap_to_tick(price: float, tick: float) -> float:
        """Round ``price`` to the nearest ``tick`` increment, clipped to [0,1].

        Polymarket rejects prices that don't sit on the tick grid (the API
        replies REJECTED with no fill).  We round-to-nearest rather than
        floor/ceil because rounding away from the touch would push the IOC
        non-marketable again — exactly the bug we are fixing.
        """
        if tick <= 0.0:
            return max(0.0, min(price, 1.0))
        snapped = round(price / tick) * tick
        snapped = round(snapped, 6)
        return max(0.0, min(snapped, 1.0))

    async def _get_bbo(self, slug: str) -> tuple[float, float]:
        """Return (best_bid_yes, best_ask_yes) from the live book.

        Both values are on the YES probability scale (0..1).  Returns
        (0.0, 0.0) when the book is empty or the fetch fails — callers
        treat that as "no BBO available, skip the clamp" rather than
        aborting the trade entirely (a missing BBO is not by itself a
        reason to reject the order).
        """
        try:
            book = await self._client.get_orderbook(slug, depth=1)
        except Exception as exc:
            logger.warning(
                "polymarket_us.bbo.fetch_failed",
                slug=slug,
                err=str(exc),
            )
            return (0.0, 0.0)
        market_data = book.get("marketData", book) if isinstance(book, dict) else {}
        bids = list(market_data.get("bids") or [])
        offers = list(market_data.get("offers") or [])
        best_bid = _amount_value((bids[0] or {}).get("px")) if bids else 0.0
        best_ask = _amount_value((offers[0] or {}).get("px")) if offers else 0.0
        return (best_bid, best_ask)

    async def _marketability_clamp_and_snap(
        self,
        *,
        arb_id: str,
        slug: str,
        intent: str,
        side: str,
        original_price: float,
        request_price: float,
        max_affordable: Optional[float],
    ) -> float:
        """Clamp ``request_price`` to the live BBO and snap it to the tick grid.

        ``request_price`` is already on the YES-scale (``_us_order_params``
        flipped NO-leg prices via ``1 - p``).  ``intent`` tells us which
        side of the book we'll cross:

          - BUY_LONG / BUY_SHORT  → we are TAKING liquidity (buying YES
            with BUY_LONG, buying NO via BUY_SHORT which sells YES short).
            For BUY_LONG we need ``price >= best_ask`` to be marketable;
            we therefore lift the wire price up to ``best_ask`` if it is
            below.  For BUY_SHORT we need ``(1 - no_price) <= best_bid``
            which means ``request_price <= best_bid``; clamp DOWN.

        Aborts with ``OrderRejected`` when the clamp would cross
        ``max_affordable`` (a guaranteed-loss trade).
        """
        meta = await self._fetch_market_meta(slug)
        tick = self._tick_from_meta(meta)
        best_bid, best_ask = await self._get_bbo(slug)

        clamped = request_price
        clamp_direction = "none"

        # BUY_LONG: limit must be >= best_ask to cross the book.
        if intent in ("BUY_LONG", "ORDER_INTENT_BUY_LONG"):
            if best_ask > 0.0 and clamped < best_ask:
                clamped = best_ask
                clamp_direction = "raised_to_ask"
        # BUY_SHORT: we're selling YES; limit must be <= best_bid.
        elif intent in ("BUY_SHORT", "ORDER_INTENT_BUY_SHORT"):
            if best_bid > 0.0 and clamped > best_bid:
                clamped = best_bid
                clamp_direction = "lowered_to_bid"
        # SELL_LONG / SELL_SHORT — keep the limit as-is; we're not currently
        # using those paths from the engine but the tick-snap still applies.

        snapped = self._snap_to_tick(clamped, tick)

        # Convert wire-side ``snapped`` back to the original side's view
        # (NO-leg = 1 - yes_price) so the max_affordable check is on the
        # same axis the engine passed in.
        if intent in ("BUY_SHORT", "ORDER_INTENT_BUY_SHORT", "SELL_SHORT", "ORDER_INTENT_SELL_SHORT"):
            user_side_price = max(1.0 - snapped, 0.0)
        else:
            user_side_price = snapped

        if (
            max_affordable is not None
            and user_side_price > max_affordable + 1e-9
        ):
            logger.warning(
                "polymarket_us.marketability.aborted_unprofitable",
                arb_id=arb_id,
                slug=slug,
                intent=intent,
                side=side,
                original_price=original_price,
                clamped_user_price=user_side_price,
                max_affordable=max_affordable,
                best_bid=best_bid,
                best_ask=best_ask,
                tick=tick,
            )
            raise OrderRejected(
                f"polymarket_us marketability clamp pushed price to "
                f"{user_side_price:.4f} > max_affordable {max_affordable:.4f}"
            )

        if clamp_direction != "none" or abs(snapped - request_price) > 1e-9:
            logger.info(
                "polymarket_us.price_clamped",
                arb_id=arb_id,
                slug=slug,
                intent=intent,
                side=side,
                old=request_price,
                new=snapped,
                bbo_bid=best_bid,
                bbo_ask=best_ask,
                tick=tick,
                clamp=clamp_direction,
            )
        return snapped

    @staticmethod
    def _us_order_params(side: str, price: float) -> tuple[str, float]:
        """Map the engine's YES/NO leg semantics into Polymarket US order fields.

        The engine passes NO-leg prices as NO probabilities. Polymarket US
        expects every request price on the long/YES scale, so NO-leg buys use
        ``ORDER_INTENT_BUY_SHORT`` with ``price.value = 1 - no_price``.
        """
        s = str(side).strip().upper()
        clipped_price = min(max(float(price), 0.0), 1.0)
        if s == "NO":
            return ("BUY_SHORT", max(1.0 - clipped_price, 0.0))
        if s in ("SELL_YES", "SELL_LONG"):
            return ("SELL_LONG", clipped_price)
        if s in ("SELL_NO", "SELL_SHORT"):
            return ("SELL_SHORT", max(1.0 - clipped_price, 0.0))
        return ("BUY_LONG", clipped_price)

    @staticmethod
    def _map_status(api_status: str, default: OrderStatus) -> OrderStatus:
        """Map Polymarket US wire-status strings to internal OrderStatus.

        Critical: FOK/IOC ``KILLED`` and ``EXPIRED`` were previously falling
        through to the SUBMITTED default, which made the engine treat a
        kill-on-no-fill response as a resting order — and trigger the soft-
        naked recovery path even though the order had already terminated.
        Now they map to CANCELLED, and unknown statuses default to FAILED so
        a future Polymarket schema change cannot silently re-introduce the
        same bug.
        """
        normalized = api_status.upper()
        if "PARTIALLY_FILLED" in normalized or "PARTIAL" in normalized:
            return OrderStatus.PARTIAL
        if normalized in {"FILLED", "MATCHED", "EXECUTED", "ORDER_STATE_FILLED"}:
            return OrderStatus.FILLED
        if normalized in {
            "CANCELED", "CANCELLED",
            "ORDER_STATE_CANCELED", "ORDER_STATE_CANCELLED",
            "KILLED", "ORDER_STATE_KILLED",
            "EXPIRED", "ORDER_STATE_EXPIRED",
            "ORDER_STATE_UNFILLED",  # FOK/IOC reply when nothing matched
            # ORDER_STATE_NEW arrives synchronously when Polymarket's matching
            # engine has accepted the IOC but not yet completed processing
            # within ``maxBlockTime``.  Verified empirically (order
            # 9RPY2RKG00YX, 2026-05-02): a synchronous NEW reply with
            # fill_qty=0 transitions to ORDER_STATE_EXPIRED moments later
            # because the IOC didn't match the live book.  Treating it as
            # CANCELLED here matches the eventual terminal state and routes
            # the trade through soft-naked recovery so the primary leg gets
            # unwound promptly instead of sitting exposed waiting for a
            # delayed terminal callback that may never fire.
            "NEW", "ORDER_STATE_NEW",
        }:
            return OrderStatus.CANCELLED
        if normalized in {"REJECTED", "ORDER_STATE_REJECTED"}:
            return OrderStatus.FAILED
        if normalized in {"LIVE", "OPEN", "ORDER_STATE_OPEN", "ORDER_STATE_LIVE"}:
            return OrderStatus.SUBMITTED
        # Unknown status from a wire format we have not seen before.  Fail
        # loud rather than fall through to ``default`` (which used to be
        # SUBMITTED) — the engine treats SUBMITTED as a still-open order
        # holding live exposure, which is the wrong call for an unknown
        # state from a FOK/IOC reply.
        return OrderStatus.FAILED if default == OrderStatus.SUBMITTED else default

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
                platform="polymarket",
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

        execution_order = {}
        executions = response.get("executions") or []
        if executions and isinstance(executions[0], dict):
            execution_order = executions[0].get("order") or {}

        # Current docs return top-level "id"; keep legacy aliases as fallbacks.
        order_id = str(
            response.get(
                "id",
                response.get(
                    "orderId",
                    response.get(
                        "orderID",
                        execution_order.get("id", f"{arb_id}-{side.upper()}-POLYUS"),
                    ),
                ),
            )
        )
        api_status = str(
            response.get(
                "state",
                response.get(
                    "status",
                    execution_order.get("state", execution_order.get("status", "OPEN")),
                ),
            )
        ).upper()
        mapped_status = self._map_status(api_status, OrderStatus.SUBMITTED)

        try:
            fill_qty = float(
                execution_order.get(
                    "cumQuantity",
                    response.get("filledQty", response.get("size_matched", 0)),
                )
            )
        except (TypeError, ValueError):
            fill_qty = 0.0

        avg_px_payload = execution_order.get("avgPx") or {}
        fill_price = _amount_value(avg_px_payload) or price
        if mapped_status == OrderStatus.FILLED and fill_qty == 0.0:
            fill_qty = float(qty)

        return Order(
            order_id=order_id,
            platform="polymarket",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=qty,
            status=mapped_status,
            fill_price=fill_price,
            fill_qty=fill_qty,
            timestamp=now,
            external_client_order_id=None,
        )
