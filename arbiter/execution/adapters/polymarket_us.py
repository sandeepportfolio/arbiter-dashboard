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
from collections import deque
from typing import Any, Deque, Dict, Optional

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
        # Diagnostic ring buffer: every time get_order observes a SUBMITTED
        # order flip to a non-FILL terminal state we append the raw venue
        # api_status + a BBO snapshot here. Exposed via the engine's
        # ``terminal_diagnostics`` property and surfaced in /api/system so
        # the ops console can show WHY each FOK kill happened (KILLED vs
        # UNFILLED vs REJECTED vs …). Pure observability — no behavior
        # change. maxlen=50 caps memory; the dashboard only renders the
        # last 10.
        self._terminal_diagnostics: Deque[Dict[str, Any]] = deque(maxlen=50)
        # Tick metadata cache (FILL-01): per-slug (tick, expiry_ts). The
        # previous _marketability_clamp_and_snap path fetched market_meta
        # AND BBO on EVERY order, adding two HTTP round-trips (~200-600ms)
        # of latency between the engine's book_walk and the venue arrival.
        # That latency was the primary cause of the 2026-05-14+ FOK-kill
        # streak: by the time our order arrived the touch had moved up
        # one tick and the FOK could not fill at the stale limit. We now
        # cache tick info for 5 minutes (it never changes for a market)
        # and skip the BBO fetch entirely; the engine's best_executable
        # _price already walked the book so a redundant BBO read adds
        # latency without adding safety.
        self._market_meta_cache: Dict[str, tuple[float, float]] = {}
        self._market_meta_ttl_s: float = 300.0

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

        # FILL-01 (2026-05-24): the clamp no longer hits the wire — it
        # tick-snaps against a cached meta lookup, so the only HTTP
        # round-trip between the engine's book_walk and the venue is the
        # /orders POST below. Engine adds the slippage buffer up-front.
        t_enter_ns = time.monotonic_ns()
        request_price = await self._marketability_clamp_and_snap(
            arb_id=arb_id,
            slug=market_id,
            intent=intent,
            side=side,
            original_price=price,
            request_price=request_price,
            max_affordable=max_affordable,
        )
        t_after_clamp_ns = time.monotonic_ns()

        response = await self._client.place_order(
            slug=market_id,
            intent=intent,
            price=request_price,
            qty=qty,
            tif=tif,
        )
        t_after_place_ns = time.monotonic_ns()
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
                # FILL-01: surface the actual wire price (post-tick-snap)
                # alongside the original user-side limit_price. Was a
                # diagnostic blind spot — when the clamp moved the price
                # the log only showed the original, hiding what we
                # actually sent. Also surface per-segment timing so we
                # can verify the latency budget post-cache.
                wire_price=request_price,
                clamp_ms=round((t_after_clamp_ns - t_enter_ns) / 1e6, 1),
                place_ms=round((t_after_place_ns - t_after_clamp_ns) / 1e6, 1),
                api_status=api_status,
                order_status=order.status.value,
                fill_qty=order.fill_qty,
                fill_price=order.fill_price,
            )
            if rejection_reason:
                log_payload["rejection_reason"] = str(rejection_reason)[:300]
            # Surface the wire-level response on any non-success terminal
            # state (FAILED, CANCELLED, ABORTED).  Previously only the literal
            # "REJECTED" branch populated ``order.error`` — KILLED, EXPIRED,
            # TIME_IN_FORCE_KILLED, and unknown states all fell through to
            # ``order.error = None`` and persisted as empty strings in the DB,
            # making post-hoc diagnosis impossible.  Now every non-fill
            # terminal echoes api_status (and rejection_reason when present)
            # so the operator can tell ``no liquidity`` apart from ``geo
            # block`` apart from ``min-size violation``.
            non_success_terminal = order.status in (
                OrderStatus.FAILED,
                OrderStatus.CANCELLED,
                OrderStatus.ABORTED,
            )
            if non_success_terminal:
                import json as _json
                try:
                    raw = _json.dumps(response, default=str)[:5000]
                except Exception:
                    raw = str(response)[:5000]
                log_payload["raw_response"] = raw
                logger.warning("polymarket_us.order.rejected", **log_payload)
                if not order.error:
                    suffix = f": {str(rejection_reason)[:200]}" if rejection_reason else ""
                    order.error = f"polymarket_us {api_status or 'unknown'}{suffix}"
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

    # ─── place_unwind_sell ────────────────────────────────────────────────

    async def place_unwind_sell(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        qty: int,
        panic_price: float = 0.01,
    ) -> Order:
        """SELL IOC at a panic price to close a naked Polymarket US position.

        Mirrors ``KalshiAdapter.place_unwind_sell``: used by
        ``ExecutionEngine._smart_unwind`` Phase 2 (and the fallback
        path when no resting sell is supported) when one leg of an arb
        is confirmed FILLED but the hedge leg never filled. We sell
        whatever the book absorbs at >= ``panic_price`` (default 1¢)
        and the IOC TIF cancels the rest, leaving any residual as a
        manual-review incident upstream.

        Before this method existed PolymarketUSAdapter had no SELL
        surface at all, so the engine's ``hasattr(adapter,
        "place_unwind_sell")`` guard in _recover_one_leg_risk skipped
        the unwind branch and the naked PU position became permanently
        stranded — observable in the two legacy 2026-11-03 midterm NO
        lots currently parked in venue inventory (cost basis ~$26.83,
        $70 of buying-power margin locked).

        ``side`` is the side originally bought:
          - "yes" → we hold YES long, SELL_LONG closes
          - "no"  → we hold NO long, SELL_SHORT closes
        ``panic_price`` is in user-side units (the price WE would
        receive per contract); _us_order_params handles the wire
        flip for SELL_SHORT.

        Skips PHASE4/PHASE5 hard-locks and the supervisor armed gate
        deliberately: those gates exist to STOP new exposure; this
        path is CLOSING existing exposure that the supervisor itself
        flagged. Blocking it would lock the venue into a permanent
        naked state.

        Always returns an Order in a terminal state; never raises
        across this boundary.
        """
        # Translate engine's bought-side semantics into the adapter's
        # SELL_LONG / SELL_SHORT intent slugs that _us_order_params
        # understands.
        s = str(side).strip().lower()
        if s == "yes":
            sell_side = "SELL_YES"
        elif s == "no":
            sell_side = "SELL_NO"
        else:
            sell_side = side  # already in SELL_* form; pass through
        return await self._sign_and_send(
            arb_id, market_id, canonical_id, sell_side, panic_price, qty,
            tif="IMMEDIATE_OR_CANCEL",
            max_affordable=None,
        )

    # ─── place_resting_sell ───────────────────────────────────────────────

    async def place_resting_sell(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
    ) -> Order:
        """GTC SELL limit at ``price`` (break-even by convention).

        Phase 1 of ``ExecutionEngine._smart_unwind``: rest at break-even
        for ~30s so passive buyers can hit our cost basis and close the
        naked leg flat, before the panic-IOC fallback chews through the
        spread. If a buyer doesn't materialise within the timeout, the
        engine cancels and falls back to place_unwind_sell.

        Mirrors ``KalshiAdapter.place_resting_sell``. Same SELL_LONG /
        SELL_SHORT side translation as place_unwind_sell.

        Same gate-skip rationale as place_unwind_sell — closing exposure
        must not be blocked by the gates that stop OPENING exposure.

        Returns the Order with status SUBMITTED on success; caller must
        poll get_order or cancel_order to drive the lifecycle.
        """
        s = str(side).strip().lower()
        if s == "yes":
            sell_side = "SELL_YES"
        elif s == "no":
            sell_side = "SELL_NO"
        else:
            sell_side = side
        return await self._sign_and_send(
            arb_id, market_id, canonical_id, sell_side, price, qty,
            # Polymarket US OpenAPI enum: TIME_IN_FORCE_GOOD_TILL_CANCEL
            # (no "ED" suffix). Verified against the create-order schema
            # at docs.polymarket.us 2026-05-24.
            tif="GOOD_TILL_CANCEL",
            max_affordable=None,
        )

    # ─── get_order_status ─────────────────────────────────────────────────

    async def get_order_status(self, order: Order) -> Order:
        """Alias for get_order (protocol compatibility)."""
        return await self.get_order(order)

    # ─── get_order ────────────────────────────────────────────────────────

    async def get_order(self, order: Order) -> Order:
        """Query the platform for current order state.

        Maps the response's ``status`` field to ``OrderStatus``. Transient
        failures (no client method, non-dict response, network exception)
        leave the order unchanged and flag ``idempotency_ambiguous=True`` so
        downstream callers can distinguish "we have no fresh signal" from
        "we confirmed terminal state". Only a definitive venue response
        mutates ``order.status``.

        Previously a missing ``client.get_order`` silently returned the
        order unchanged with no signal, and ``_poll_submitted_to_terminal``
        would loop until timeout before deciding to cancel — even if the
        order had already filled. Now the ambiguity is explicit.
        """
        try:
            if not hasattr(self._client, "get_order"):
                logger.warning(
                    "polymarket_us.get_order.unsupported",
                    order_id=order.order_id,
                    note="client lacks get_order — cannot verify terminal state",
                )
                order.idempotency_ambiguous = True
                return order
            resp = await self._client.get_order(order.order_id)
            if not isinstance(resp, dict):
                logger.warning(
                    "polymarket_us.get_order.non_dict_response",
                    order_id=order.order_id,
                    resp_type=type(resp).__name__,
                )
                order.idempotency_ambiguous = True
                return order
            payload = resp.get("order", resp)
            api_status = str(
                payload.get("state", payload.get("status", ""))
            ).upper()
            if not api_status:
                logger.warning(
                    "polymarket_us.get_order.empty_status",
                    order_id=order.order_id,
                )
                order.idempotency_ambiguous = True
                return order
            prev_status = order.status
            order.status = self._map_status(api_status, order.status)
            # Diagnostic ring-buffer capture for the SUBMITTED → terminal-
            # non-fill transition. This is the exact event the dashboard
            # currently has zero visibility into: post-submit poll flips
            # a NEW order to CANCELLED but no log captures the raw venue
            # string or the BBO at that moment. We need it to tell apart
            # KILLED (no liquidity at our limit), ORDER_STATE_UNFILLED
            # (FOK swept nothing), REJECTED (post-accept validation), etc.
            terminal_non_fill = {OrderStatus.CANCELLED, OrderStatus.FAILED}
            if (
                prev_status == OrderStatus.SUBMITTED
                and order.status in terminal_non_fill
            ):
                # BBO snapshot is best-effort — never block the poll path
                # on a flaky book fetch.
                try:
                    best_bid, best_ask = await self._get_bbo(order.market_id)
                except Exception:
                    best_bid, best_ask = 0.0, 0.0
                # Pull any rejection-reason fields the venue surfaces.
                rejection_reason = (
                    payload.get("rejectionReason")
                    or payload.get("statusReason")
                    or payload.get("errorMessage")
                    or payload.get("message")
                    or ""
                )
                diag = {
                    "ts": time.time(),
                    "order_id": order.order_id,
                    "market_id": order.market_id,
                    "side": order.side,
                    "limit_price": float(order.price),
                    "qty": int(order.quantity),
                    "api_status": api_status,
                    "mapped_status": order.status.value,
                    "fill_qty": float(order.fill_qty or 0.0),
                    "best_bid_yes": float(best_bid),
                    "best_ask_yes": float(best_ask),
                    "rejection_reason": str(rejection_reason)[:200],
                }
                self._terminal_diagnostics.append(diag)
                logger.warning(
                    "polymarket_us.terminal_via_poll",
                    **diag,
                )
        except Exception as exc:
            logger.warning(
                "polymarket_us.get_order.failed",
                order_id=order.order_id,
                err=str(exc),
            )
            # Transient venue/network error — leave order.status unchanged
            # so the caller does not collapse a possibly-still-live order
            # to FAILED on a single network blip. Flag ambiguity instead.
            order.idempotency_ambiguous = True
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

    async def _cached_tick(self, slug: str) -> float:
        """Return the per-slug tick size, using a 5-minute in-memory cache.

        Polymarket US never changes a market's tick after listing, so the
        meta payload is functionally immutable. The previous code fetched
        it on every order; that HTTP call (plus the BBO fetch in the
        clamp path) added 200-600ms of wire latency that caused the FOK
        kill cascade described in ``__init__``.
        """
        now = time.time()
        cached = self._market_meta_cache.get(slug)
        if cached is not None and cached[1] > now:
            return cached[0]
        meta = await self._fetch_market_meta(slug)
        tick = self._tick_from_meta(meta)
        self._market_meta_cache[slug] = (tick, now + self._market_meta_ttl_s)
        return tick

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
        """Snap ``request_price`` to the tick grid and apply ``max_affordable``.

        FILL-01 (2026-05-24): the previous implementation fetched live BBO
        and ALSO market metadata on every order — two extra HTTP round-
        trips that added ~200-600ms of latency between the engine's
        ``best_executable_price`` book walk and the venue arrival. That
        latency window let the touch move up by one tick on
        fast-settling sports markets, after which the FOK limit was no
        longer marketable and the venue killed the order. Result: 214
        consecutive zero-fill trades since 2026-05-14.

        The engine now adds a ``FOK_SLIPPAGE_TICKS`` price buffer (with
        net-edge re-validation) BEFORE calling place_fok, which absorbs
        the same adverse book movement the BBO clamp was trying to
        defend against — but does so on cached/in-process data with
        zero added wire latency. The adapter therefore drops the BBO
        fetch entirely and only does tick-snapping + max_affordable
        enforcement.

        ``request_price`` is already on the YES-scale (``_us_order_params``
        flipped NO-leg prices via ``1 - p``). ``max_affordable`` is
        evaluated on the original user-side axis so the engine's
        slippage budget is honored regardless of intent flip.
        """
        tick = await self._cached_tick(slug)
        snapped = self._snap_to_tick(request_price, tick)

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
                tick=tick,
            )
            raise OrderRejected(
                f"polymarket_us tick-snap pushed price to "
                f"{user_side_price:.4f} > max_affordable {max_affordable:.4f}"
            )

        if abs(snapped - request_price) > 1e-9:
            logger.info(
                "polymarket_us.price_snapped",
                arb_id=arb_id,
                slug=slug,
                intent=intent,
                side=side,
                old=request_price,
                new=snapped,
                tick=tick,
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
        }:
            return OrderStatus.CANCELLED
        if normalized in {"REJECTED", "ORDER_STATE_REJECTED"}:
            return OrderStatus.FAILED
        if normalized in {
            "LIVE", "OPEN", "ORDER_STATE_OPEN", "ORDER_STATE_LIVE",
            # NEW / ORDER_STATE_NEW: Polymarket's matching engine has accepted
            # the order but not yet returned a terminal verdict. Map to
            # SUBMITTED so the engine's post-submit poll
            # (_poll_submitted_to_terminal) drives it to a real terminal state
            # via get_order. Previously this mapped to CANCELLED based on a
            # single empirical observation; that race-condition shortcut could
            # trigger smart-unwind on the primary while the venue still
            # filled the secondary, producing double-direction exposure.
            "NEW", "ORDER_STATE_NEW",
        }:
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
