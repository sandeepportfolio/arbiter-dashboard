"""PolymarketAdapter — extracted from arbiter/execution/engine.py:974-1048 + 732-745.

Implements PlatformAdapter Protocol. Three critical functional changes vs the
extracted code:

1. Two-phase FOK: `create_order(args)` then `post_order(signed, OrderType.FOK)`
   instead of the legacy one-shot combined call. Required for EXEC-01.
2. Reconcile-before-retry: Polymarket has no idempotency key, so blind retry
   on a network timeout can submit the same trade twice (Pitfall 2). Before
   each retry, query `get_orders(market=token_id)` and treat a matching open
   order as a successful previous attempt.
3. Stale-book guard: `get_order_book` is known to return cached/stale data
   (py-clob-client issue #180); cross-check against `get_price` and refuse
   the trade if they diverge by >1¢ (Pitfall 1).

D-13 invariant: the adapter does NOT instantiate or hold its own ClobClient.
A `clob_client_factory()` callable returns the engine's cached client so the
heartbeat task (engine.polymarket_heartbeat_loop) and the adapter share the
same client. The heartbeat lifecycle is exclusively owned by the engine
(heartbeat calls are NOT made from this adapter).
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable, Optional

import structlog

from ..engine import Order, OrderStatus

log = structlog.get_logger("arbiter.adapters.polymarket")


class PolymarketAdapter:
    """Per-platform execution adapter for Polymarket (EXEC-04)."""

    platform = "polymarket"

    def __init__(
        self,
        config,
        clob_client_factory: Callable[[], Optional[Any]],
        rate_limiter,
        circuit,
    ):
        """
        config: ArbiterConfig — uses config.polymarket.private_key for auth check
        clob_client_factory: callable returning the engine's cached ClobClient (or None)
        rate_limiter: arbiter.utils.retry.RateLimiter
        circuit: arbiter.utils.retry.CircuitBreaker
        """
        self.config = config
        self._get_client = clob_client_factory
        self.rate_limiter = rate_limiter
        self.circuit = circuit
        self._warned_no_client_order_id = False

    # --- place_fok -------------------------------------------------------

    async def place_fok(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
    ) -> Order:
        now = time.time()

        if not getattr(self.config.polymarket, "private_key", None):
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty,
                now, "Polymarket wallet not configured",
            )

        if not self.circuit.can_execute():
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty,
                now, "polymarket circuit open",
            )

        client = self._get_client()
        if client is None:
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty,
                now, "Unable to initialize Polymarket client",
            )

        # Phase 4 blast-radius hard-lock (D-02): adapter-layer belt above RiskManager + test-wallet
        # hardware cap. When PHASE4_MAX_ORDER_USD is unset, this is a no-op (production behavior
        # unchanged). When set, rejects any order whose notional (qty * price) exceeds the cap.
        # Notional is the correct risk measure (Pitfall 8); raw qty is meaningless on prediction markets.
        max_order_usd_raw = os.getenv("PHASE4_MAX_ORDER_USD")
        if max_order_usd_raw:
            try:
                max_order_usd = float(max_order_usd_raw)
            except (TypeError, ValueError):
                max_order_usd = 0.0  # Unparseable -> maximally restrictive (safe default).
            notional_usd = float(qty) * float(price)
            if notional_usd > max_order_usd:
                log.warning(
                    "polymarket.phase4_hardlock.rejected",
                    arb_id=arb_id,
                    notional=notional_usd,
                    max=max_order_usd,
                    qty=qty,
                    price=price,
                )
                return self._failed_order(
                    arb_id, market_id, canonical_id, side, price, qty, now,
                    f"PHASE4_MAX_ORDER_USD hard-lock: notional ${notional_usd:.2f} > ${max_order_usd:.2f}",
                )

        # Phase 5 blast-radius hard-lock (Plan 05-01): identical semantics to
        # PHASE4 but a separate env var so Phase 4 sandbox + Phase 5 live can
        # co-exist without cross-contamination. When both are set, the Phase 4
        # block runs first (above); the Phase 5 block runs here. This means
        # the stricter cap effectively wins because each belt is enforced in
        # sequence with no short-circuit. Unset env -> no-op. Unparseable ->
        # 0.0 cap (maximally restrictive, T-5-01-01 parity).
        max_order_usd_raw = os.getenv("PHASE5_MAX_ORDER_USD")
        if max_order_usd_raw:
            try:
                max_order_usd = float(max_order_usd_raw)
            except (TypeError, ValueError):
                max_order_usd = 0.0
            notional_usd = float(qty) * float(price)
            if notional_usd > max_order_usd:
                log.warning(
                    "polymarket.phase5_hardlock.rejected",
                    arb_id=arb_id,
                    notional=notional_usd,
                    max=max_order_usd,
                    qty=qty,
                    price=price,
                )
                return self._failed_order(
                    arb_id, market_id, canonical_id, side, price, qty, now,
                    f"PHASE5_MAX_ORDER_USD hard-lock: notional ${notional_usd:.2f} > ${max_order_usd:.2f}",
                )

        return await self._place_fok_reconciling(
            client, arb_id, market_id, canonical_id, side, price, qty,
            max_attempts=3,
        )

    async def _place_fok_reconciling(
        self, client, arb_id, market_id, canonical_id, side, price, qty,
        max_attempts: int = 3,
    ) -> Order:
        """Reconcile-before-retry loop. Pitfall 2 mitigation.

        Before each submission attempt, query `client.get_orders(market=market_id)`
        to see whether a previous attempt already placed the order. If a matching
        open order exists, return it without re-submitting — Polymarket has no
        idempotency key, so blind retries can create duplicate orders.
        """
        loop = asyncio.get_event_loop()
        last_exc: Optional[Exception] = None

        for attempt in range(max_attempts):
            # --- Pre-check: did a previous attempt succeed? --------------
            try:
                existing = await loop.run_in_executor(
                    None, lambda: client.get_orders(market=market_id)
                )
            except Exception as exc:
                log.warning(
                    "polymarket.reconcile.list_failed",
                    arb_id=arb_id, market_id=market_id,
                    err=str(exc), attempt=attempt,
                )
                existing = []

            matching = self._match_existing(existing, side, price, qty)
            if matching is not None:
                log.info(
                    "polymarket.order.reconciled",
                    arb_id=arb_id, attempt=attempt,
                    order_id=str(matching.get("id", matching.get("orderID", "?"))),
                )
                return self._order_from_record(
                    matching, arb_id, market_id, canonical_id, side, price, qty,
                )

            # --- Submit (two-phase FOK) ---------------------------------
            try:
                await self.rate_limiter.acquire()
                from py_clob_client.clob_types import OrderArgs, OrderType
                order_args = OrderArgs(
                    token_id=market_id,
                    price=round(price, 2),
                    size=float(qty),
                    side=self._poly_side(side),
                )
                signed = await loop.run_in_executor(
                    None, lambda: client.create_order(order_args)
                )
                response = await loop.run_in_executor(
                    None, lambda: client.post_order(signed, OrderType.FOK)
                )
                self.circuit.record_success()
                return self._order_from_response(
                    response, arb_id, market_id, canonical_id, side, price, qty,
                )

            except (TimeoutError, asyncio.TimeoutError) as exc:
                last_exc = exc
                log.warning(
                    "polymarket.order.timeout",
                    arb_id=arb_id, attempt=attempt, err=str(exc),
                )
                # Backoff; next iteration's pre-check finds the order if it went through
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue

            except Exception as exc:
                # SAFE-04: py-clob-client surfaces HTTP 429 as a plain
                # exception whose message contains "429" or "rate limit".
                # On 429 we apply Retry-After backoff, record a circuit
                # failure, and return FAILED — NEVER retry a FOK order.
                if self._is_rate_limit_error(exc):
                    delay = self.rate_limiter.apply_retry_after(
                        "1", fallback_delay=2.0, reason="polymarket_429",
                    )
                    delay = min(float(delay or 0.0), 60.0)
                    log.warning(
                        "polymarket.rate_limited",
                        arb_id=arb_id, attempt=attempt, penalty_seconds=delay,
                    )
                    self.circuit.record_failure()
                    return self._failed_order(
                        arb_id, market_id, canonical_id, side, price, qty,
                        time.time(),
                        f"rate_limited ({delay:.1f}s)",
                    )
                self.circuit.record_failure()
                log.error(
                    "polymarket.order.error",
                    arb_id=arb_id, attempt=attempt, err=str(exc),
                )
                return self._failed_order(
                    arb_id, market_id, canonical_id, side, price, qty,
                    time.time(),
                    f"Polymarket order exception: {exc}",
                )

        # Max attempts exhausted
        self.circuit.record_failure()
        return self._failed_order(
            arb_id, market_id, canonical_id, side, price, qty,
            time.time(),
            f"Polymarket max attempts exhausted (last_exc={last_exc})",
        )

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Detect whether a py-clob-client exception represents an HTTP 429.

        The SDK surfaces rate-limiting as a plain exception; inspect the message
        for telltale markers. Case-insensitive substring match covers the usual
        phrasings ('429', 'rate limit', 'too many requests').
        """
        msg = str(exc).lower()
        return (
            "429" in msg
            or "rate limit" in msg
            or "rate_limit" in msg
            or "too many requests" in msg
        )

    @staticmethod
    def _poly_side(side: str) -> str:
        """Map engine's side to Polymarket's CLOB side.

        Engine represents an arb leg by which token it is buying ("yes" | "no");
        the specific token is already encoded in `token_id`, so the CLOB side
        for a buy leg is always "BUY". A "SELL" is only used for closing out
        an existing position — pass-through if explicitly requested.
        (Matches the hardcoded `side="BUY"` in engine.py:1007 before extraction.)
        """
        s = str(side).upper()
        if s in ("BUY", "SELL"):
            return s
        if s in ("YES", "NO"):
            return "BUY"
        # Unknown — default to BUY (matches pre-extraction behavior)
        return "BUY"

    def _match_existing(
        self, existing: list, side: str, price: float, qty: int,
    ) -> Optional[dict]:
        """Find an open order matching price (tolerance <0.01), size (exact),
        and side. Returns the order dict or None."""
        if not existing:
            return None
        side_normalized = self._poly_side(side)
        for o in existing:
            o_dict = o if isinstance(o, dict) else getattr(o, "__dict__", {})
            try:
                o_price = float(o_dict.get("price", 0) or 0)
                o_size = float(o_dict.get("size", 0) or 0)
                o_side = str(o_dict.get("side", "")).upper()
            except (TypeError, ValueError):
                continue
            if (
                abs(o_price - price) < 0.01
                and o_size == float(qty)
                and o_side == side_normalized
            ):
                return o_dict
        return None

    def _order_from_response(
        self, response, arb_id, market_id, canonical_id, side, price, qty,
    ) -> Order:
        """Build Order from post_order response. FOK returns a terminal state.

        NOTE: The status mapping below treats "matched"/"filled"/"executed" as
        FILLED, which is the documented FOK outcome per agentbets.ai reference.
        Phase 4 sandbox testing should validate the exact response shape — if
        Polymarket returns a different status string, extend `status_map`.
        """
        now = time.time()
        if isinstance(response, dict) and not response.get("success", True):
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty,
                now, str(response.get("errorMsg", "Order rejected")),
            )
        if isinstance(response, dict):
            order_id = str(
                response.get("orderID", response.get("id", f"{arb_id}-{side.upper()}-POLY"))
            )
        else:
            order_id = f"{arb_id}-{side.upper()}-POLY"

        api_status = (
            response.get("status", "matched") if isinstance(response, dict) else "matched"
        )
        api_status = str(api_status).lower()
        status_map = {
            "matched":   OrderStatus.FILLED,
            "filled":    OrderStatus.FILLED,
            "executed":  OrderStatus.FILLED,
            "canceled":  OrderStatus.CANCELLED,
            "cancelled": OrderStatus.CANCELLED,
            "rejected":  OrderStatus.FAILED,
        }
        mapped = status_map.get(api_status, OrderStatus.SUBMITTED)

        # Fill qty: FOK fills fully or not at all; use size_matched if provided
        try:
            fill_qty = float(
                response.get("size_matched", qty) if isinstance(response, dict) else qty
            )
        except (TypeError, ValueError):
            fill_qty = float(qty)

        return Order(
            order_id=order_id,
            platform="polymarket",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=qty,
            status=mapped,
            fill_price=price,
            fill_qty=fill_qty,
            timestamp=now,
            # CR-02 parity: Polymarket has no client_order_id concept.
            external_client_order_id=None,
        )

    def _order_from_record(
        self, record: dict, arb_id, market_id, canonical_id, side, price, qty,
    ) -> Order:
        """Build Order from an existing-order record returned by get_orders().

        Reconcile path: the adapter found a matching order on the platform from
        a previous attempt. Return it with SUBMITTED status; the engine can
        refresh via get_order() if it needs terminal state.
        """
        now = time.time()
        order_id = str(
            record.get("id", record.get("orderID", f"{arb_id}-{side.upper()}-POLY"))
        )
        try:
            fill_price = float(record.get("price", price))
        except (TypeError, ValueError):
            fill_price = price
        try:
            fill_qty = float(
                record.get("size_matched", record.get("filled", 0)) or 0
            )
        except (TypeError, ValueError):
            fill_qty = 0.0
        return Order(
            order_id=order_id,
            platform="polymarket",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=qty,
            status=OrderStatus.SUBMITTED,
            fill_price=fill_price,
            fill_qty=fill_qty,
            timestamp=now,
            # CR-02 parity: Polymarket has no client_order_id concept.
            external_client_order_id=None,
        )

    # --- cancel_order (mirrors engine.py:732-745) ------------------------

    async def cancel_order(self, order: Order) -> bool:
        client = self._get_client()
        if client is None:
            return False
        # SAFE-04: acquire a rate-limit token before any SDK call.
        await self.rate_limiter.acquire()
        loop = asyncio.get_event_loop()
        try:
            for attr in ("cancel", "cancel_order"):
                if hasattr(client, attr):
                    method = getattr(client, attr)
                    await loop.run_in_executor(None, lambda: method(order.order_id))
                    return True
        except Exception as exc:
            # SAFE-04: distinguish 429 from other SDK errors.
            if self._is_rate_limit_error(exc):
                delay = self.rate_limiter.apply_retry_after(
                    "1", fallback_delay=2.0, reason="polymarket_429",
                )
                delay = min(float(delay or 0.0), 60.0)
                log.warning(
                    "polymarket.rate_limited",
                    op="cancel", order_id=order.order_id, penalty_seconds=delay,
                )
                self.circuit.record_failure()
                return False
            log.error(
                "polymarket.cancel.failed",
                order_id=order.order_id, err=str(exc),
            )
        return False

    # --- cancel_all (SAFE-05: SDK-backed implementation) -----------------

    async def cancel_all(self) -> list[str]:
        """Cancel every open Polymarket order via the CLOB SDK.

        Invokes ``client.cancel_all()`` on the shared ClobClient after
        acquiring a rate-limit token. The SDK method is synchronous, so the
        call is dispatched through ``run_in_executor`` to avoid blocking the
        event loop (matches the adapter's other SDK-call pattern).

        Returns the ``canceled`` list from the SDK response (SDK returns
        ``{"canceled": [...], "not_canceled": [...]}``). Empty list is
        returned when the client is missing, the SDK raises, or the response
        shape is unexpected — ``cancel_all`` never raises across this
        boundary (used by graceful shutdown under SIGINT/SIGTERM pressure).
        """
        client = self._get_client()
        if client is None:
            return []

        # SAFE-04: one token per SDK call (acquire-before-I/O invariant).
        await self.rate_limiter.acquire()

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, lambda: client.cancel_all())
        except Exception as exc:
            # SAFE-04: distinguish 429 so operators see why shutdown couldn't
            # finish. Still return [] so run_shutdown_sequence can proceed.
            if self._is_rate_limit_error(exc):
                delay = self.rate_limiter.apply_retry_after(
                    "1", fallback_delay=2.0, reason="polymarket_429",
                )
                delay = min(float(delay or 0.0), 60.0)
                log.warning(
                    "polymarket.rate_limited",
                    op="cancel_all", penalty_seconds=delay,
                )
                self.circuit.record_failure()
                return []
            log.error("polymarket.cancel_all.failed", err=str(exc))
            return []

        # SDK returns {"canceled": [...], "not_canceled": [...]}.
        if isinstance(result, dict):
            canceled = result.get("canceled") or []
            if isinstance(canceled, (list, tuple)):
                return [str(x) for x in canceled]
            log.warning(
                "polymarket.cancel_all.unexpected_canceled_type",
                got=type(canceled).__name__,
            )
            return []

        # Legacy/non-dict shapes: treat as empty.
        log.warning(
            "polymarket.cancel_all.unexpected_response_type",
            got=type(result).__name__,
        )
        return []

    # --- check_depth with stale-book guard (Pitfall 1) -------------------

    async def check_depth(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        """Pre-trade liquidity check with Pitfall 1 stale-book guard.

        Cross-checks `get_order_book` against `get_price`. If `get_price`
        falls outside the book's [best_bid, best_ask] range by more than 1¢,
        refuses the trade because the cached book is likely stale
        (py-clob-client issue #180).
        """
        client = self._get_client()
        if client is None:
            return (False, 0.0)
        loop = asyncio.get_event_loop()
        try:
            book_future = loop.run_in_executor(
                None, lambda: client.get_order_book(market_id)
            )
            price_future = loop.run_in_executor(
                None, lambda: client.get_price(market_id, side.upper())
            )
            book = await book_future
            tick_price = await price_future
        except Exception as exc:
            log.warning(
                "polymarket.depth.failed", market_id=market_id, err=str(exc),
            )
            return (False, 0.0)

        asks = self._extract_levels(book, "asks")
        bids = self._extract_levels(book, "bids")
        if not asks:
            log.info("polymarket.depth.no_asks", market_id=market_id)
            return (False, 0.0)

        sorted_asks = sorted(asks, key=lambda lvl: lvl[0])  # ascending price
        best_ask = sorted_asks[0][0]
        best_bid = max((lvl[0] for lvl in bids), default=0.0)

        # Stale-book guard (Pitfall 1): tick must be within [best_bid-0.01, best_ask+0.01]
        if tick_price is not None:
            try:
                tick = float(tick_price)
                if tick > best_ask + 0.01 or tick < best_bid - 0.01:
                    log.warning(
                        "polymarket.depth.stale_book",
                        market_id=market_id, tick=tick,
                        best_ask=best_ask, best_bid=best_bid,
                    )
                    return (False, 0.0)
            except (TypeError, ValueError):
                pass  # unparseable tick — fall through to depth-only check

        cumulative = 0.0
        for level in sorted_asks:
            cumulative += float(level[1])
            if cumulative >= float(required_qty):
                return (True, best_ask)
        return (False, best_ask)

    @staticmethod
    def _extract_levels(book: Any, key: str) -> list:
        """Normalize book levels into a list of (price, size) tuples.

        Accepts dict (`{"asks": [...], "bids": [...]}`) or object with .asks/.bids
        attributes. Each level may be a dict {price,size}, an object with
        price/size attributes, or a [price, size] sequence.
        """
        if book is None:
            return []
        if isinstance(book, dict):
            levels = book.get(key, [])
        else:
            levels = getattr(book, key, None) or []
        out: list = []
        for lvl in levels:
            try:
                if isinstance(lvl, dict):
                    out.append((float(lvl.get("price", 0)), float(lvl.get("size", 0))))
                elif hasattr(lvl, "price") and hasattr(lvl, "size"):
                    out.append((float(lvl.price), float(lvl.size)))
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    out.append((float(lvl[0]), float(lvl[1])))
            except (TypeError, ValueError):
                continue
        return out

    # --- get_order -------------------------------------------------------

    async def get_order(self, order: Order) -> Order:
        client = self._get_client()
        if client is None:
            order.status = OrderStatus.FAILED
            order.error = "Polymarket client unavailable for get_order"
            return order
        # SAFE-04: acquire a rate-limit token before any SDK call.
        await self.rate_limiter.acquire()
        loop = asyncio.get_event_loop()
        try:
            record = await loop.run_in_executor(
                None,
                lambda: (
                    client.get_order(order.order_id)
                    if hasattr(client, "get_order")
                    else None
                ),
            )
        except Exception as exc:
            if self._is_rate_limit_error(exc):
                delay = self.rate_limiter.apply_retry_after(
                    "1", fallback_delay=2.0, reason="polymarket_429",
                )
                delay = min(float(delay or 0.0), 60.0)
                log.warning(
                    "polymarket.rate_limited",
                    op="get_order", order_id=order.order_id, penalty_seconds=delay,
                )
                self.circuit.record_failure()
                order.status = OrderStatus.FAILED
                order.error = f"rate_limited ({delay:.1f}s)"
                return order
            order.status = OrderStatus.FAILED
            order.error = f"Polymarket get_order exception: {exc}"
            return order

        if record is None:
            order.status = OrderStatus.FAILED
            order.error = "not found on platform"
            return order

        rec = record if isinstance(record, dict) else getattr(record, "__dict__", {})
        api_status = str(rec.get("status", "")).lower()
        status_map = {
            "matched":   OrderStatus.FILLED,
            "filled":    OrderStatus.FILLED,
            "executed":  OrderStatus.FILLED,
            "canceled":  OrderStatus.CANCELLED,
            "cancelled": OrderStatus.CANCELLED,
            "live":      OrderStatus.SUBMITTED,
            "open":      OrderStatus.SUBMITTED,
        }
        order.status = status_map.get(api_status, order.status)
        try:
            order.fill_qty = float(
                rec.get("size_matched", rec.get("filled", order.fill_qty))
                or order.fill_qty
            )
        except (TypeError, ValueError):
            pass
        return order

    # --- list_open_orders_by_client_id (Polymarket has no client_order_id)

    async def list_open_orders_by_client_id(
        self, client_order_id_prefix: str,
    ) -> list[Order]:
        """Polymarket has no client_order_id concept — returns [] and logs a
        warning on first call so operators know recovery relies on the DB-side
        path (engine.recovery matches DB order IDs against platform IDs via
        `get_order(order)` per non-terminal row).
        """
        if not self._warned_no_client_order_id:
            log.warning(
                "polymarket.list_open_orders_by_client_id.unsupported",
                prefix=client_order_id_prefix,
                note="Polymarket has no client_order_id; returning []",
            )
            self._warned_no_client_order_id = True
        return []

    # --- helpers ---------------------------------------------------------

    def _failed_order(
        self, arb_id, market_id, canonical_id, side, price, qty, ts, error: str,
    ) -> Order:
        return Order(
            order_id=f"{arb_id}-{side.upper()}-POLY",
            platform="polymarket",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=qty,
            status=OrderStatus.FAILED,
            timestamp=ts,
            error=error,
            # CR-02 parity: Polymarket has no client_order_id concept.
            external_client_order_id=None,
        )
