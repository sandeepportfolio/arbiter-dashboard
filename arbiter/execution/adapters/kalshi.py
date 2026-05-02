"""KalshiAdapter — extracted from arbiter/execution/engine.py:802-900 + 717-730.

Implements PlatformAdapter Protocol. The single functional change vs the
extracted code is the addition of ``"time_in_force": "fill_or_kill"`` to the
order body (EXEC-01).

Retry safety: Kalshi accepts ``client_order_id`` as an idempotency key, so the
``@transient_retry`` decorator is safe on POST. If a retry resends the same
``client_order_id`` Kalshi returns the existing order record.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import aiohttp  # noqa: F401  (kept for adapter callers importing the module)
import structlog

from ..engine import Order, OrderStatus
from .retry_policy import TRANSIENT_EXCEPTIONS, transient_retry

log = structlog.get_logger("arbiter.adapters.kalshi")

# Kalshi API order-status strings → internal OrderStatus
_FOK_STATUS_MAP: dict[str, OrderStatus] = {
    "executed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,  # tolerate British spelling if ever seen
    "pending": OrderStatus.PENDING,
    "resting": OrderStatus.SUBMITTED,    # unexpected for FOK — emit warning event
}

# place_resting_limit uses the same wire-status → internal mapping. The
# only semantic difference vs place_fok is that "resting" is the EXPECTED
# steady state (not a warning-emitting anomaly), so this variant does not
# log kalshi.fok.unexpected_resting when it sees that status.
_RESTING_STATUS_MAP: dict[str, OrderStatus] = dict(_FOK_STATUS_MAP)


class KalshiAdapter:
    """Per-platform execution adapter for Kalshi (EXEC-04).

    Constructor injection: tests pass mocks for
    session/auth/rate_limiter/circuit; ``arbiter/main.py`` wires real instances
    in Plan 06.
    """

    platform: str = "kalshi"

    def __init__(self, config, session, auth, rate_limiter, circuit):
        """
        Args:
            config: ArbiterConfig — uses ``config.kalshi.base_url``.
            session: ``aiohttp.ClientSession`` — shared HTTP session.
            auth: ``KalshiAuth`` (from ``arbiter.collectors.kalshi.KalshiCollector.auth``).
            rate_limiter: ``arbiter.utils.retry.RateLimiter`` — Kalshi 10 writes/sec (Pitfall 4).
            circuit: ``arbiter.utils.retry.CircuitBreaker`` — sustained-outage gate (D-18).
        """
        self.config = config
        self.session = session
        self.auth = auth
        self.rate_limiter = rate_limiter
        self.circuit = circuit

    # ─── place_fok / place_ioc ────────────────────────────────────────────

    async def place_ioc(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
    ) -> Order:
        """Submit a Kalshi immediate-or-cancel limit order.

        Same semantics as ``place_fok`` (synchronous, terminal-state response,
        never raises across the boundary) but uses ``time_in_force=immediate_or_cancel``
        so a partial fill is accepted instead of killing the whole order.
        Used by the engine for the secondary leg of a cross-venue arb so
        a stale top-of-book on the secondary doesn't strand the primary in
        a naked position — IOC will at least take what's actually there.
        """
        return await self.place_fok(
            arb_id, market_id, canonical_id, side, price, qty,
            time_in_force="immediate_or_cancel",
        )

    async def place_fok(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        *,
        time_in_force: str = "fill_or_kill",
    ) -> Order:
        """Submit a Kalshi limit order with the given time-in-force.

        Defaults to ``fill_or_kill``.  ``immediate_or_cancel`` is also valid
        (see ``place_ioc``).  Returns ``Order`` in a terminal state.

        Always returns an ``Order``; never raises across this boundary.
        """
        now = time.time()

        if not self.auth or not getattr(self.auth, "is_authenticated", False):
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                "Kalshi auth not configured",
            )

        if not (0 < price < 1):
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Invalid price {price}: must be between 0 and 1 exclusive",
            )

        if not self.circuit.can_execute():
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                "kalshi circuit open",
            )

        # Phase 4 blast-radius hard-lock (D-02) — closing the gap documented in
        # Plan 04-02 SUMMARY: Plan 04-02 only added PHASE4 to PolymarketAdapter;
        # Plan 04-02.1 only added it to KalshiAdapter.place_resting_limit.
        # Plan 05-01 adds it to KalshiAdapter.place_fok together with the Phase 5
        # belt below so a PHASE5-only insertion would not leave a regression
        # window where the FOK path has PHASE5 but not PHASE4. Unset env ->
        # no-op. Unparseable -> 0.0 cap (maximally restrictive).
        max_order_usd_raw = os.getenv("PHASE4_MAX_ORDER_USD")
        if max_order_usd_raw:
            try:
                max_order_usd = float(max_order_usd_raw)
            except (TypeError, ValueError):
                max_order_usd = 0.0
            notional_usd = float(qty) * float(price)
            if notional_usd > max_order_usd:
                log.warning(
                    "kalshi.phase4_hardlock.rejected",
                    arb_id=arb_id,
                    notional=notional_usd,
                    max=max_order_usd,
                    qty=qty,
                    price=price,
                    op="place_fok",
                )
                return self._failed_order(
                    arb_id, market_id, canonical_id, side, price, qty, now,
                    f"PHASE4_MAX_ORDER_USD hard-lock: notional ${notional_usd:.2f} > ${max_order_usd:.2f}",
                )

        # Phase 5 blast-radius hard-lock (Plan 05-01): identical semantics to
        # PHASE4 but a separate env var. When both caps are set, PHASE4 runs
        # first (above); PHASE5 runs here. Stricter cap effectively wins.
        max_order_usd_raw = os.getenv("PHASE5_MAX_ORDER_USD")
        if max_order_usd_raw:
            try:
                max_order_usd = float(max_order_usd_raw)
            except (TypeError, ValueError):
                max_order_usd = 0.0
            notional_usd = float(qty) * float(price)
            if notional_usd > max_order_usd:
                log.warning(
                    "kalshi.phase5_hardlock.rejected",
                    arb_id=arb_id,
                    notional=notional_usd,
                    max=max_order_usd,
                    qty=qty,
                    price=price,
                    op="place_fok",
                )
                return self._failed_order(
                    arb_id, market_id, canonical_id, side, price, qty, now,
                    f"PHASE5_MAX_ORDER_USD hard-lock: notional ${notional_usd:.2f} > ${max_order_usd:.2f}",
                )

        client_order_id = f"{arb_id}-{side.upper()}-{uuid.uuid4().hex[:8]}"
        order_body: dict[str, Any] = {
            "ticker": market_id,
            "client_order_id": client_order_id,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count_fp": f"{float(qty):.2f}",
            "time_in_force": time_in_force,
        }
        if side == "yes":
            order_body["yes_price_dollars"] = f"{price:.4f}"
        else:
            order_body["no_price_dollars"] = f"{price:.4f}"

        try:
            response_status, payload, response_headers = await self._post_order(order_body)
        except TRANSIENT_EXCEPTIONS as exc:
            self.circuit.record_failure()
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Kalshi transient (retries exhausted): {exc}",
            )
        except Exception as exc:
            self.circuit.record_failure()
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Kalshi request exception: {exc}",
            )

        # SAFE-04: 429 → apply Retry-After, record circuit failure, return FAILED.
        # FOK semantics mean we NEVER retry a rate-limited POST.
        if response_status == 429:
            retry_after = response_headers.get("Retry-After", "1") if response_headers else "1"
            delay = self.rate_limiter.apply_retry_after(
                retry_after, fallback_delay=2.0, reason="kalshi_429",
            )
            # T-3-04-E: cap forged Retry-After headers at 60 seconds.
            delay = min(float(delay or 0.0), 60.0)
            log.warning(
                "kalshi.rate_limited",
                penalty_seconds=delay,
                client_order_id=client_order_id,
            )
            self.circuit.record_failure()
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"rate_limited ({delay:.1f}s)",
            )

        if response_status not in (200, 201):
            self.circuit.record_failure()
            log.error(
                "kalshi.order.rejected",
                status=response_status,
                body=payload[:200],
                client_order_id=client_order_id,
            )
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Kalshi API {response_status}: {payload[:200]}",
            )

        self.circuit.record_success()
        try:
            data = json.loads(payload)
        except Exception as exc:
            log.error("kalshi.order.parse_failed", body=payload[:200], err=str(exc))
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Kalshi response parse: {exc}",
            )

        order_data = data.get("order", data) if isinstance(data, dict) else {}
        api_status = order_data.get("status", "resting")
        mapped_status = _FOK_STATUS_MAP.get(api_status, OrderStatus.SUBMITTED)
        if api_status == "resting":
            log.warning(
                "kalshi.fok.unexpected_resting",
                client_order_id=client_order_id,
                status=api_status,
            )

        fill_qty = float(
            order_data.get("fill_count_fp", order_data.get("count_filled", "0")) or "0"
        )
        fill_price = self._extract_fill_price(order_data, side, fill_qty, price)

        return Order(
            order_id=str(order_data.get("order_id", client_order_id)),
            platform="kalshi",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=qty,
            status=mapped_status,
            fill_price=fill_price,
            fill_qty=fill_qty,
            timestamp=now,
            # CR-02: preserve the engine-chosen idempotency key so the engine
            # can persist it to ``execution_orders.client_order_id`` instead
            # of Kalshi's server-assigned order_id.
            external_client_order_id=client_order_id,
        )

    @transient_retry()
    async def _post_order(self, body: dict) -> tuple[int, str, dict]:
        """Inner HTTP call wrapped by tenacity.

        Idempotent on Kalshi via ``client_order_id`` — safe to retry on transient
        network errors (OPS-03). Rate-limiter is inside the retry decorator so
        every attempt waits for a token (Pitfall 4).

        Returns ``(status, body_text, response_headers)``. Headers are exposed
        so ``place_fok`` can read ``Retry-After`` on 429 responses (SAFE-04).
        """
        await self.rate_limiter.acquire()
        path = "/trade-api/v2/portfolio/orders"
        url = f"{self.config.kalshi.base_url}/portfolio/orders"
        headers = self.auth.get_headers("POST", path)
        async with self.session.post(url, json=body, headers=headers) as response:
            payload = await response.text()
            # Copy headers into a plain dict so the caller doesn't hold a
            # reference to the aiohttp response after context exit.
            resp_headers = dict(response.headers) if response.headers else {}
            return response.status, payload, resp_headers

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
        """Submit a SELL IOC at a panic price to close out a naked position.

        Used by ``ExecutionEngine._recover_one_leg_risk`` when one leg has
        confirmed FILLED but the hedge leg never filled. We sell whatever
        the orderbook will absorb at >= ``panic_price`` (default 1¢) and
        cancel the unfilled remainder. Partial fills are returned to the
        caller — residual exposure becomes a manual-resolution incident.

        Always returns an Order in a terminal state; never raises across
        this boundary. ``side`` matches the side originally bought
        (selling YES that we hold long, or selling NO that we hold long).
        """
        now = time.time()
        if not self.auth or not getattr(self.auth, "is_authenticated", False):
            return self._failed_order(
                arb_id, market_id, canonical_id, side, panic_price, qty, now,
                "Kalshi auth not configured for unwind",
            )
        if not self.circuit.can_execute():
            return self._failed_order(
                arb_id, market_id, canonical_id, side, panic_price, qty, now,
                "kalshi circuit open",
            )

        client_order_id = f"{arb_id}-{side.upper()}-UNWIND-{uuid.uuid4().hex[:8]}"
        order_body: dict[str, Any] = {
            "ticker": market_id,
            "client_order_id": client_order_id,
            "action": "sell",
            "side": side,
            "type": "limit",
            "count_fp": f"{float(qty):.2f}",
            "time_in_force": "immediate_or_cancel",
        }
        if side == "yes":
            order_body["yes_price_dollars"] = f"{panic_price:.4f}"
        else:
            order_body["no_price_dollars"] = f"{panic_price:.4f}"

        try:
            response_status, payload, _ = await self._post_order(order_body)
        except TRANSIENT_EXCEPTIONS as exc:
            self.circuit.record_failure()
            return self._failed_order(
                arb_id, market_id, canonical_id, side, panic_price, qty, now,
                f"Kalshi unwind transient: {exc}",
            )
        except Exception as exc:
            self.circuit.record_failure()
            return self._failed_order(
                arb_id, market_id, canonical_id, side, panic_price, qty, now,
                f"Kalshi unwind exception: {exc}",
            )

        if response_status not in (200, 201):
            self.circuit.record_failure()
            log.error(
                "kalshi.unwind.rejected",
                status=response_status,
                body=payload[:200],
                client_order_id=client_order_id,
            )
            return self._failed_order(
                arb_id, market_id, canonical_id, side, panic_price, qty, now,
                f"Kalshi unwind {response_status}: {payload[:200]}",
            )

        self.circuit.record_success()
        try:
            data = json.loads(payload)
        except Exception as exc:
            return self._failed_order(
                arb_id, market_id, canonical_id, side, panic_price, qty, now,
                f"Kalshi unwind response parse: {exc}",
            )

        order_data = data.get("order", data) if isinstance(data, dict) else {}
        api_status = order_data.get("status", "executed")
        mapped_status = _FOK_STATUS_MAP.get(api_status, OrderStatus.SUBMITTED)
        fill_qty = float(
            order_data.get("fill_count_fp", order_data.get("count_filled", "0")) or "0"
        )
        fill_price = self._extract_fill_price(order_data, side, fill_qty, panic_price)

        log.info(
            "kalshi.unwind.placed",
            arb_id=arb_id,
            client_order_id=client_order_id,
            status=api_status,
            fill_qty=fill_qty,
            target_qty=qty,
            fill_price=fill_price,
        )

        return Order(
            order_id=str(order_data.get("order_id", client_order_id)),
            platform="kalshi",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=panic_price,
            quantity=qty,
            status=mapped_status,
            fill_price=fill_price,
            fill_qty=fill_qty,
            timestamp=now,
            external_client_order_id=client_order_id,
        )

    # ─── place_resting_limit (Plan 04-02.1 scope expansion) ──────────────

    async def place_resting_limit(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
    ) -> Order:
        """Place a resting limit order (NOT FOK) — order stays on the book
        until filled, cancelled, or the market closes.

        Enables Plan 04-05 SAFE-01 kill-switch live-fire (which needs a
        resting order that survives >=5 seconds so the kill-switch can trip
        and cancel it mid-life). Structure mirrors ``place_fok`` with two
        differences:

        1. ``time_in_force`` is OMITTED from the order body (absence = GTC/resting).
        2. Kalshi's ``status="resting"`` response is the EXPECTED happy-path
           terminal response (SUBMITTED), not an anomaly that needs a warning.

        The PHASE4_MAX_ORDER_USD adapter-layer hard-lock from Plan 04-02
        (D-02) applies identically — notional ``qty * price`` above the cap
        returns FAILED WITHOUT any HTTP call.

        Always returns an Order; never raises across this boundary. On a
        successful resting placement, ``Order.order_id`` is the real Kalshi
        order id (usable directly by ``cancel_order``).
        """
        now = time.time()

        if not self.auth or not getattr(self.auth, "is_authenticated", False):
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                "Kalshi auth not configured",
            )

        if not (0 < price < 1):
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Invalid price {price}: must be between 0 and 1 exclusive",
            )

        # Phase 4 blast-radius hard-lock (D-02), mirroring Plan 04-02's
        # PolymarketAdapter belt. When PHASE4_MAX_ORDER_USD is unset, this
        # is a no-op. When set, rejects any notional (qty * price) > cap
        # BEFORE any HTTP call. Unparseable env → 0.0 cap → maximally
        # restrictive (safe-default failure mode; T-04-02-08 parity).
        max_order_usd_raw = os.getenv("PHASE4_MAX_ORDER_USD")
        if max_order_usd_raw:
            try:
                max_order_usd = float(max_order_usd_raw)
            except (TypeError, ValueError):
                max_order_usd = 0.0
            notional_usd = float(qty) * float(price)
            if notional_usd > max_order_usd:
                log.warning(
                    "kalshi.phase4_hardlock.rejected",
                    arb_id=arb_id,
                    notional=notional_usd,
                    max=max_order_usd,
                    qty=qty,
                    price=price,
                    op="place_resting_limit",
                )
                return self._failed_order(
                    arb_id, market_id, canonical_id, side, price, qty, now,
                    f"PHASE4_MAX_ORDER_USD hard-lock: notional ${notional_usd:.2f} > ${max_order_usd:.2f}",
                )

        # Phase 5 blast-radius hard-lock (Plan 05-01): mirrors PHASE4 block
        # above with PHASE5_MAX_ORDER_USD env var. Both belts enforced in
        # sequence — stricter cap effectively wins. Unset = no-op.
        max_order_usd_raw = os.getenv("PHASE5_MAX_ORDER_USD")
        if max_order_usd_raw:
            try:
                max_order_usd = float(max_order_usd_raw)
            except (TypeError, ValueError):
                max_order_usd = 0.0
            notional_usd = float(qty) * float(price)
            if notional_usd > max_order_usd:
                log.warning(
                    "kalshi.phase5_hardlock.rejected",
                    arb_id=arb_id,
                    notional=notional_usd,
                    max=max_order_usd,
                    qty=qty,
                    price=price,
                    op="place_resting_limit",
                )
                return self._failed_order(
                    arb_id, market_id, canonical_id, side, price, qty, now,
                    f"PHASE5_MAX_ORDER_USD hard-lock: notional ${notional_usd:.2f} > ${max_order_usd:.2f}",
                )

        if not self.circuit.can_execute():
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                "kalshi circuit open",
            )

        client_order_id = f"{arb_id}-{side.upper()}-{uuid.uuid4().hex[:8]}"
        order_body: dict[str, Any] = {
            "ticker": market_id,
            "client_order_id": client_order_id,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count_fp": f"{float(qty):.2f}",
            # NB: NO time_in_force — absence = GTC/resting at Kalshi.
        }
        if side == "yes":
            order_body["yes_price_dollars"] = f"{price:.4f}"
        else:
            order_body["no_price_dollars"] = f"{price:.4f}"

        try:
            response_status, payload, response_headers = await self._post_order(order_body)
        except TRANSIENT_EXCEPTIONS as exc:
            self.circuit.record_failure()
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Kalshi transient (retries exhausted): {exc}",
            )
        except Exception as exc:
            self.circuit.record_failure()
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Kalshi request exception: {exc}",
            )

        # SAFE-04: 429 → apply Retry-After, record circuit failure, return
        # FAILED. NEVER retry — a second POST could place a duplicate
        # resting order (Kalshi's client_order_id dedup helps but we do NOT
        # rely on it for safety across 429s).
        if response_status == 429:
            retry_after = response_headers.get("Retry-After", "1") if response_headers else "1"
            delay = self.rate_limiter.apply_retry_after(
                retry_after, fallback_delay=2.0, reason="kalshi_429",
            )
            delay = min(float(delay or 0.0), 60.0)
            log.warning(
                "kalshi.rate_limited",
                penalty_seconds=delay,
                client_order_id=client_order_id,
                op="place_resting_limit",
            )
            self.circuit.record_failure()
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"rate_limited ({delay:.1f}s)",
            )

        if response_status not in (200, 201):
            self.circuit.record_failure()
            log.error(
                "kalshi.resting_order.rejected",
                status=response_status,
                body=payload[:200],
                client_order_id=client_order_id,
            )
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Kalshi API {response_status}: {payload[:200]}",
            )

        self.circuit.record_success()
        try:
            data = json.loads(payload)
        except Exception as exc:
            log.error(
                "kalshi.resting_order.parse_failed",
                body=payload[:200], err=str(exc),
            )
            return self._failed_order(
                arb_id, market_id, canonical_id, side, price, qty, now,
                f"Kalshi response parse: {exc}",
            )

        order_data = data.get("order", data) if isinstance(data, dict) else {}
        api_status = order_data.get("status", "resting")
        # Resting is the EXPECTED outcome for this variant — no warning
        # emitted when we see it (unlike place_fok, where resting is an
        # anomaly worth logging).
        mapped_status = _RESTING_STATUS_MAP.get(api_status, OrderStatus.SUBMITTED)

        fill_qty = float(
            order_data.get("fill_count_fp", order_data.get("count_filled", "0")) or "0"
        )
        fill_price = self._extract_fill_price(order_data, side, fill_qty, price)

        return Order(
            order_id=str(order_data.get("order_id", client_order_id)),
            platform="kalshi",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=qty,
            status=mapped_status,
            fill_price=fill_price,
            fill_qty=fill_qty,
            timestamp=now,
            # CR-02 parity with place_fok: carry the engine-chosen
            # client_order_id back to the engine so ExecutionStore persists
            # the real idempotency key, not the Kalshi server id.
            external_client_order_id=client_order_id,
        )

    # ─── cancel_order (verbatim from engine.py:717-730 + retry) ────────────

    async def cancel_order(self, order: Order) -> bool:
        if not self.auth or not getattr(self.auth, "is_authenticated", False):
            return False
        # SAFE-04: acquire a rate-limit token before any network I/O.
        await self.rate_limiter.acquire()
        try:
            return await self._delete_order(order.order_id)
        except Exception as exc:
            log.error("kalshi.cancel.failed", order_id=order.order_id, err=str(exc))
            return False

    @transient_retry()
    async def _delete_order(self, order_id: str) -> bool:
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        url = f"{self.config.kalshi.base_url}/portfolio/orders/{order_id}"
        headers = self.auth.get_headers("DELETE", path)
        async with self.session.delete(url, headers=headers) as response:
            # SAFE-04: 429 on DELETE — apply Retry-After + circuit failure.
            if response.status == 429:
                retry_after = (
                    dict(response.headers).get("Retry-After", "1")
                    if response.headers
                    else "1"
                )
                delay = self.rate_limiter.apply_retry_after(
                    retry_after, fallback_delay=2.0, reason="kalshi_429",
                )
                delay = min(float(delay or 0.0), 60.0)
                log.warning("kalshi.rate_limited", penalty_seconds=delay, op="cancel")
                self.circuit.record_failure()
                return False
            return response.status in (200, 204)

    # ─── cancel_all (SAFE-05: full chunked batched implementation) ────────

    # Kalshi's POST/DELETE /portfolio/orders/batched accepts up to 20 orders
    # per call. Shutdown under load will page through open orders in 20-sized
    # chunks and acquire one rate-limit token per chunk (Pitfall 5 budgeted).
    CANCEL_ALL_CHUNK_SIZE = 20

    async def cancel_all(self) -> list[str]:
        """Cancel every open order via batched DELETE. Best-effort, never raises.

        Returns the list of successfully cancelled ``order_id`` strings aggregated
        across chunks. An empty list is returned on any of: no auth, no open
        orders, list-orders error, or every chunk failing.
        """
        if not self.auth or not getattr(self.auth, "is_authenticated", False):
            return []

        try:
            open_orders = await self._list_all_open_orders()
        except Exception as exc:
            log.warning("kalshi.cancel_all.list_failed", err=str(exc))
            return []

        if not open_orders:
            return []

        CHUNK_SIZE = self.CANCEL_ALL_CHUNK_SIZE
        cancelled_ids: list[str] = []

        for i in range(0, len(open_orders), CHUNK_SIZE):
            chunk = open_orders[i : i + CHUNK_SIZE]
            chunk_ids = [
                getattr(o, "order_id", None) or (o.get("order_id") if isinstance(o, dict) else None)
                for o in chunk
            ]
            chunk_ids = [cid for cid in chunk_ids if cid]
            if not chunk_ids:
                continue

            # SAFE-04 invariant: one token per chunk (Pitfall 5 — rate-limiter
            # budget sized to let shutdown finish within the 5s window).
            await self.rate_limiter.acquire()

            path = "/trade-api/v2/portfolio/orders/batched"
            url = f"{self.config.kalshi.base_url}/portfolio/orders/batched"
            try:
                headers = self.auth.get_headers("DELETE", path)
            except Exception as exc:
                log.warning(
                    "kalshi.cancel_all.headers_failed",
                    chunk_index=i // CHUNK_SIZE, err=str(exc),
                )
                continue

            payload = {"ids": list(chunk_ids)}

            try:
                async with self.session.delete(
                    url, json=payload, headers=headers,
                ) as response:
                    status = response.status
                    body_text = await response.text()
                    resp_headers = dict(response.headers) if response.headers else {}

                # 429 on a chunk → apply Retry-After + circuit failure, but
                # keep trying the remaining chunks (partial progress > nothing).
                if status == 429:
                    retry_after = resp_headers.get("Retry-After", "1")
                    delay = self.rate_limiter.apply_retry_after(
                        retry_after, fallback_delay=2.0, reason="kalshi_429",
                    )
                    delay = min(float(delay or 0.0), 60.0)
                    log.warning(
                        "kalshi.rate_limited",
                        penalty_seconds=delay,
                        op="cancel_all",
                        chunk_index=i // CHUNK_SIZE,
                    )
                    self.circuit.record_failure()
                    continue

                if status not in (200, 204):
                    log.warning(
                        "kalshi.cancel_all.chunk_failed",
                        status=status,
                        body=body_text[:200] if body_text else "",
                        chunk_index=i // CHUNK_SIZE,
                    )
                    continue

                # Parse body for per-order results when available. Tolerate
                # minor response-shape variations; on parse failure, assume
                # all ids in the chunk were cancelled (204 / empty body case).
                parsed_ids: list[str] = []
                if body_text:
                    try:
                        data = json.loads(body_text)
                    except Exception:
                        data = None
                    if isinstance(data, dict):
                        # Accept either {"results": [{order_id, error}]} or
                        # {"orders": [{order_id, error}]} or a top-level list.
                        rows = (
                            data.get("results")
                            or data.get("orders")
                            or data.get("cancelled")
                            or []
                        )
                    elif isinstance(data, list):
                        rows = data
                    else:
                        rows = []
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        if row.get("error"):
                            continue
                        rid = row.get("order_id") or row.get("id")
                        if rid:
                            parsed_ids.append(str(rid))

                if parsed_ids:
                    cancelled_ids.extend(parsed_ids)
                else:
                    # No structured response — assume chunk succeeded and
                    # record the ids we submitted. This matches the 204
                    # (no body) pattern common for batched DELETE endpoints.
                    cancelled_ids.extend(str(cid) for cid in chunk_ids)
            except Exception as exc:
                log.error(
                    "kalshi.cancel_all.chunk_exception",
                    err=str(exc),
                    chunk_index=i // CHUNK_SIZE,
                )
                continue

        log.info(
            "kalshi.cancel_all.done",
            total_requested=len(open_orders),
            total_cancelled=len(cancelled_ids),
            chunks=(len(open_orders) + CHUNK_SIZE - 1) // CHUNK_SIZE,
        )
        return cancelled_ids

    async def _list_all_open_orders(self) -> list[Order]:
        """Fetch every resting Kalshi order. Returns [] on error (never raises).

        Uses the same GET /portfolio/orders?status=resting endpoint as
        ``list_open_orders_by_client_id`` but without any client-order-id
        prefix filter — used by SAFE-05 cancel_all to discover shutdown
        candidates.
        """
        if not self.auth or not getattr(self.auth, "is_authenticated", False):
            return []
        try:
            status_code, payload, resp_headers = await self._list_orders("resting")
        except Exception as exc:
            log.warning("kalshi.list_all_open_orders.failed", err=str(exc))
            return []

        if status_code == 429:
            retry_after = resp_headers.get("Retry-After", "1") if resp_headers else "1"
            delay = self.rate_limiter.apply_retry_after(
                retry_after, fallback_delay=2.0, reason="kalshi_429",
            )
            delay = min(float(delay or 0.0), 60.0)
            log.warning(
                "kalshi.rate_limited", penalty_seconds=delay, op="list_all_open",
            )
            self.circuit.record_failure()
            return []

        if status_code not in (200, 201):
            log.warning(
                "kalshi.list_all_open_orders.http_error",
                status=status_code, body=payload[:200] if payload else "",
            )
            return []

        try:
            data = json.loads(payload)
        except Exception as exc:
            log.warning("kalshi.list_all_open_orders.parse_failed", err=str(exc))
            return []

        orders_raw = data.get("orders", []) if isinstance(data, dict) else []
        return [self._order_data_to_order(od) for od in orders_raw if isinstance(od, dict)]

    # ─── check_depth (NEW for EXEC-03) ────────────────────────────────────

    async def check_depth(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        """Sum visible orderbook depth on the side we will buy.

        Returns ``(sufficient, best_ask_price)``. On any error returns
        ``(False, 0.0)`` — does NOT raise; the engine treats False as
        "skip this trade".
        """
        try:
            return await self._fetch_depth(market_id, side, required_qty)
        except Exception as exc:
            log.warning("kalshi.depth.failed", market_id=market_id, err=str(exc))
            return (False, 0.0)

    @staticmethod
    def _extract_buy_levels(payload: dict, side: str) -> list[tuple[float, float]]:
        """Return ``[(buy_price, qty), ...]`` sorted ascending by ``buy_price``.

        Handles two Kalshi orderbook shapes that have been seen in the wild:

        1. Current ``orderbook_fp`` shape (post-2026 dollar fixed-point):
           ``{"orderbook_fp": {"yes_dollars": [["0.80","100"], ...],
                                "no_dollars":  [["0.15","250"], ...]}}``
           Each side is a list of *bids* for that side (highest bid is best),
           prices are dollars as strings, qty is contracts. To BUY ``side`` we
           must walk the OPPOSITE side's bids and convert each level via
           ``buy_price = 1 - opposite_bid_price`` (because selling YES at $X
           is matched against someone bidding NO at $1-X).

        2. Legacy ``orderbook`` shape (cents-based, asks on same side):
           ``{"orderbook": {"yes": [[55, 5], [56, 10]], "no": [[...]]}}``
           Prices in integer cents, qty in contracts. The same side is asks,
           so the cheapest entry is best.

        Returns ``[]`` if the payload is empty or unparseable.
        """
        opposite = "no" if side == "yes" else "yes"

        fp = payload.get("orderbook_fp")
        if isinstance(fp, dict):
            opp_levels = (
                fp.get(f"{opposite}_dollars")
                or fp.get(opposite)
                or []
            )
            if opp_levels:
                converted: list[tuple[float, float]] = []
                for raw in opp_levels:
                    try:
                        opp_bid = float(raw[0])
                        qty = float(raw[1])
                    except (IndexError, TypeError, ValueError):
                        continue
                    if opp_bid <= 0 or qty <= 0:
                        continue
                    if opp_bid > 1.0:
                        # tolerate cents-as-string under the _dollars key
                        opp_bid /= 100.0
                    buy_price = round(1.0 - opp_bid, 4)
                    if buy_price <= 0 or buy_price >= 1.0:
                        continue
                    converted.append((buy_price, qty))
                return sorted(converted, key=lambda lvl: lvl[0])

        legacy = payload.get("orderbook")
        if isinstance(legacy, dict):
            legacy_levels = legacy.get(side, []) or []
            if legacy_levels:
                converted = []
                for raw in legacy_levels:
                    try:
                        price_raw = float(raw[0])
                        qty = float(raw[1])
                    except (IndexError, TypeError, ValueError):
                        continue
                    if price_raw <= 0 or qty <= 0:
                        continue
                    buy_price = price_raw / 100.0 if price_raw > 1.0 else price_raw
                    if buy_price <= 0 or buy_price >= 1.0:
                        continue
                    converted.append((buy_price, qty))
                return sorted(converted, key=lambda lvl: lvl[0])

        return []

    @transient_retry()
    async def _fetch_depth(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        url = f"{self.config.kalshi.base_url}/markets/{market_id}/orderbook?depth=100"
        # Public endpoint — no auth header required
        async with self.session.get(url) as response:
            if response.status != 200:
                return (False, 0.0)
            payload = await response.text()
        try:
            data = json.loads(payload)
        except (ValueError, TypeError) as exc:
            log.warning(
                "kalshi.depth.parse_failed", market_id=market_id, err=str(exc),
            )
            return (False, 0.0)

        levels = self._extract_buy_levels(data, side)
        if not levels:
            return (False, 0.0)
        best_price = levels[0][0]
        cumulative = 0.0
        for _, qty in levels:
            cumulative += qty
            if cumulative >= float(required_qty):
                return (True, best_price)
        return (False, best_price)

    # ─── best_executable_price ────────────────────────────────────────────

    async def best_executable_price(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        """Walk the orderbook to find the worst price needed to fill
        ``required_qty``. Used to set the FOK limit price so Kalshi does NOT
        reject with ``fill_or_kill_insufficient_resting_volume`` when liquidity
        is fragmented across multiple price levels.
        """
        try:
            return await self._fetch_executable_price(market_id, side, required_qty)
        except Exception as exc:
            log.warning(
                "kalshi.executable_price.failed", market_id=market_id, err=str(exc),
            )
            return (False, 0.0)

    @transient_retry()
    async def _fetch_executable_price(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        url = f"{self.config.kalshi.base_url}/markets/{market_id}/orderbook?depth=100"
        async with self.session.get(url) as response:
            if response.status != 200:
                return (False, 0.0)
            payload = await response.text()
        try:
            data = json.loads(payload)
        except (ValueError, TypeError) as exc:
            log.warning(
                "kalshi.executable_price.parse_failed",
                market_id=market_id, err=str(exc),
            )
            return (False, 0.0)

        levels = self._extract_buy_levels(data, side)
        if not levels:
            return (False, 0.0)
        cumulative = 0.0
        for price, qty in levels:
            cumulative += qty
            if cumulative >= float(required_qty):
                return (True, price)
        return (False, levels[-1][0])

    # ─── get_order ────────────────────────────────────────────────────────

    async def get_order(self, order: Order) -> Order:
        """Query platform state for a single order. Used by recovery.py (Plan 06)."""
        if not self.auth or not getattr(self.auth, "is_authenticated", False):
            order.status = OrderStatus.FAILED
            order.error = "Kalshi auth not configured for get_order"
            return order
        # SAFE-04: acquire a rate-limit token before any network I/O.
        await self.rate_limiter.acquire()
        try:
            status_code, payload, resp_headers = await self._fetch_order(order.order_id)
        except Exception as exc:
            order.status = OrderStatus.FAILED
            order.error = f"Kalshi get_order exception: {exc}"
            return order

        if status_code == 429:
            retry_after = resp_headers.get("Retry-After", "1") if resp_headers else "1"
            delay = self.rate_limiter.apply_retry_after(
                retry_after, fallback_delay=2.0, reason="kalshi_429",
            )
            delay = min(float(delay or 0.0), 60.0)
            log.warning("kalshi.rate_limited", penalty_seconds=delay, op="get_order")
            self.circuit.record_failure()
            order.status = OrderStatus.FAILED
            order.error = f"rate_limited ({delay:.1f}s)"
            return order
        if status_code == 404:
            order.status = OrderStatus.FAILED
            order.error = "not found on platform"
            return order
        if status_code not in (200, 201):
            order.status = OrderStatus.FAILED
            order.error = f"Kalshi get_order {status_code}: {payload[:200]}"
            return order
        try:
            data = json.loads(payload)
            order_data = data.get("order", data) if isinstance(data, dict) else {}
            api_status = order_data.get("status", "resting")
            order.status = _FOK_STATUS_MAP.get(api_status, OrderStatus.SUBMITTED)
            order.fill_qty = float(
                order_data.get(
                    "fill_count_fp",
                    order_data.get("count_filled", order.fill_qty),
                )
                or order.fill_qty
            )
            return order
        except Exception as exc:
            order.error = f"Kalshi get_order parse: {exc}"
            return order

    @transient_retry()
    async def _fetch_order(self, order_id: str) -> tuple[int, str, dict]:
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        url = f"{self.config.kalshi.base_url}/portfolio/orders/{order_id}"
        headers = self.auth.get_headers("GET", path)
        async with self.session.get(url, headers=headers) as response:
            resp_headers = dict(response.headers) if response.headers else {}
            return response.status, await response.text(), resp_headers

    # ─── list_open_orders_by_client_id ────────────────────────────────────

    async def list_open_orders_by_client_id(
        self, client_order_id_prefix: str,
    ) -> list[Order]:
        """List resting orders whose ``client_order_id`` starts with the prefix.

        Used by ``arbiter/execution/recovery.py`` on startup to find orphaned
        orders. Kalshi's ``/portfolio/orders`` endpoint supports filtering by
        ``status``; client-side filtering by prefix covers the rest because
        Kalshi does not expose a prefix query.
        """
        if not self.auth or not getattr(self.auth, "is_authenticated", False):
            return []
        # SAFE-04: acquire a rate-limit token before any network I/O.
        await self.rate_limiter.acquire()
        try:
            status_code, payload, resp_headers = await self._list_orders("resting")
            if status_code == 429:
                retry_after = resp_headers.get("Retry-After", "1") if resp_headers else "1"
                delay = self.rate_limiter.apply_retry_after(
                    retry_after, fallback_delay=2.0, reason="kalshi_429",
                )
                delay = min(float(delay or 0.0), 60.0)
                log.warning("kalshi.rate_limited", penalty_seconds=delay, op="list")
                self.circuit.record_failure()
                return []
            if status_code not in (200, 201):
                return []
            data = json.loads(payload)
            orders = data.get("orders", []) or []
            results: list[Order] = []
            for od in orders:
                cid = str(od.get("client_order_id", "") or "")
                if not cid.startswith(client_order_id_prefix):
                    continue
                results.append(self._order_data_to_order(od))
            return results
        except Exception as exc:
            log.warning("kalshi.list_orders.failed", err=str(exc))
            return []

    @transient_retry()
    async def _list_orders(self, status: str) -> tuple[int, str, dict]:
        # G-1 fix (Plan 04-09, 2026-04-20): Kalshi PSS signing requires a
        # querystring-free path in the signed message. The querystring is
        # appended to the REQUEST URL (so Kalshi routes the filter) but is
        # stripped from the SIGNED path. Before this fix, demo Kalshi rejected
        # `/portfolio/orders?status=resting` with HTTP 401
        # INCORRECT_API_KEY_SIGNATURE, which silently DoS'd SAFE-01 kill-switch
        # cancel_all enumeration (no orders listed -> no orders cancelled).
        # See 04-HUMAN-UAT.md Test 6 evidence. _post_order / _fetch_order /
        # _delete_order already sign querystring-free paths; this brings
        # _list_orders into parity.
        path = "/trade-api/v2/portfolio/orders"
        url = f"{self.config.kalshi.base_url}/portfolio/orders?status={status}"
        headers = self.auth.get_headers("GET", path)
        async with self.session.get(url, headers=headers) as response:
            resp_headers = dict(response.headers) if response.headers else {}
            return response.status, await response.text(), resp_headers

    # ─── helpers ──────────────────────────────────────────────────────────

    def _failed_order(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        ts: float,
        error: str,
    ) -> Order:
        return Order(
            order_id=f"{arb_id}-{side.upper()}-KALSHI",
            platform="kalshi",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=qty,
            status=OrderStatus.FAILED,
            timestamp=ts,
            error=error,
        )

    @staticmethod
    def _extract_fill_price(
        order_data: dict, side: str, fill_qty: float, fallback: float,
    ) -> float:
        """Extract the actual average fill price from a Kalshi order response.

        Kalshi returns BOTH ``yes_price_dollars`` and ``no_price_dollars`` on
        every order regardless of which side was traded — they are mirror
        images (yes_price + no_price == 1.0).  For a NO buy at $0.10 the
        response is ``{"no_price_dollars":"0.10","yes_price_dollars":"0.90"}``
        — reading ``yes_price_dollars`` first (as the previous parser did)
        flipped every NO fill onto the YES scale, so a NO order that filled
        for $1 of cash got reported as if we had spent $9.  That bookkeeping
        error then poisoned the engine's max-affordable-secondary calc and
        caused soft-naked recoveries to silently lose money.

        Preferred source: ``taker_fill_cost_dollars / fill_count_fp`` —
        the actual cash that left our account divided by the number of
        contracts that filled.  This is unambiguous regardless of side.

        Fallback: the side-correct ``*_price_dollars`` field (no_price for
        NO orders, yes_price for YES orders).  Last-resort: ``avg_price``
        or the limit price.
        """
        # 1. Most accurate: actual cash / contracts.
        cost_raw = order_data.get("taker_fill_cost_dollars")
        if cost_raw is not None and fill_qty > 0:
            try:
                cost = float(cost_raw)
                if cost > 0:
                    return cost / float(fill_qty)
            except (TypeError, ValueError):
                pass

        # 2. Side-correct price field.
        side_key = "no_price_dollars" if str(side).lower() == "no" else "yes_price_dollars"
        side_price_raw = order_data.get(side_key)
        if side_price_raw is not None:
            try:
                return float(side_price_raw)
            except (TypeError, ValueError):
                pass

        # 3. avg_price (older Kalshi format).
        avg_raw = order_data.get("avg_price")
        if avg_raw is not None:
            try:
                return float(avg_raw)
            except (TypeError, ValueError):
                pass

        # 4. Last resort: caller-supplied fallback (typically the limit).
        return float(fallback)

    def _order_data_to_order(self, od: dict) -> Order:
        api_status = od.get("status", "resting")
        side = str(od.get("side", "")).lower()
        # Side-aware: a NO order's true price field is no_price_dollars
        # (yes_price_dollars on a NO order is the YES-scale equivalent
        # 1 - no_price, NOT the actual fill price).
        price_key = "no_price_dollars" if side == "no" else "yes_price_dollars"
        price_raw = od.get(price_key, od.get("yes_price_dollars", od.get("no_price_dollars", "0"))) or "0"
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            price = 0.0
        try:
            quantity = int(float(od.get("count_fp", "0")) or 0)
        except (TypeError, ValueError):
            quantity = 0
        try:
            fill_qty = float(od.get("fill_count_fp", "0") or "0")
        except (TypeError, ValueError):
            fill_qty = 0.0
        fill_price = self._extract_fill_price(od, side, fill_qty, price)
        # CR-02: surface the engine-chosen client_order_id so callers
        # (e.g. timeout-recovery in engine._place_order_for_leg) can
        # propagate it back into the persistence layer.
        cid = od.get("client_order_id")
        external_cid = str(cid) if cid else None
        return Order(
            order_id=str(od.get("order_id", "")),
            platform="kalshi",
            market_id=str(od.get("ticker", "")),
            canonical_id="",  # not in Kalshi response — caller may rehydrate
            side=str(od.get("side", "")),
            price=price,
            quantity=quantity,
            status=_FOK_STATUS_MAP.get(api_status, OrderStatus.SUBMITTED),
            fill_price=fill_price,
            fill_qty=fill_qty,
            timestamp=time.time(),
            external_client_order_id=external_cid,
        )
