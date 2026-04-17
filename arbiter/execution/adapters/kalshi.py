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
        """Submit a Kalshi FOK limit order. Returns ``Order`` in a terminal state.

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

        client_order_id = f"{arb_id}-{side.upper()}-{uuid.uuid4().hex[:8]}"
        order_body: dict[str, Any] = {
            "ticker": market_id,
            "client_order_id": client_order_id,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count_fp": f"{float(qty):.2f}",
            "time_in_force": "fill_or_kill",   # ← EXEC-01: critical FOK directive
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
        fill_price_raw = order_data.get(
            "yes_price_dollars",
            order_data.get(
                "no_price_dollars",
                order_data.get("avg_price", str(price)),
            ),
        )
        try:
            fill_price = float(fill_price_raw) if fill_price_raw is not None else price
        except (TypeError, ValueError):
            fill_price = price

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
        data = json.loads(payload)

        # Kalshi orderbook shape:
        #   {"orderbook": {"yes": [[price_cents, qty], ...], "no": [[...]]}}
        # Buying YES consumes asks on the YES side; same for NO.
        orderbook = data.get("orderbook", {}) or {}
        levels = orderbook.get(side, []) or []
        if not levels:
            return (False, 0.0)

        try:
            sorted_levels = sorted(levels, key=lambda lvl: lvl[0])
            best_price_cents = float(sorted_levels[0][0])
            cumulative = 0.0
            for level in sorted_levels:
                cumulative += float(level[1])
                if cumulative >= float(required_qty):
                    return (True, best_price_cents / 100.0)
            return (False, best_price_cents / 100.0)
        except (IndexError, TypeError, KeyError) as exc:
            log.warning(
                "kalshi.depth.parse_failed", market_id=market_id, err=str(exc),
            )
            return (False, 0.0)

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
        path = f"/trade-api/v2/portfolio/orders?status={status}"
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

    def _order_data_to_order(self, od: dict) -> Order:
        api_status = od.get("status", "resting")
        price_raw = od.get("yes_price_dollars", od.get("no_price_dollars", "0")) or "0"
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            price = 0.0
        try:
            quantity = int(float(od.get("count_fp", "0")) or 0)
        except (TypeError, ValueError):
            quantity = 0
        try:
            fill_price = float(od.get("avg_price", "0") or "0")
        except (TypeError, ValueError):
            fill_price = 0.0
        try:
            fill_qty = float(od.get("fill_count_fp", "0") or "0")
        except (TypeError, ValueError):
            fill_qty = 0.0
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
