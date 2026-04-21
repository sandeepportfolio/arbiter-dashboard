"""
Polymarket US REST client — signed, paginated, 429-aware.

Uses Ed25519 auth (arbiter.auth.ed25519_signer) for every request.
Two rate-limiter instances expose distinct budgets:
  - live_rate_limiter  (18 r/s) — default, for live market ops
  - discovery_rate_limiter (2 r/s) — pass purpose="discovery" to _signed()

All requests are wrapped by a CircuitBreaker.
Secrets are never logged; only path + status code appear in log lines.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import aiohttp

from arbiter.auth.ed25519_signer import Ed25519Signer
from arbiter.utils.retry import CircuitBreaker, RateLimiter

logger = logging.getLogger("arbiter.collector.polymarket_us")

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)


@dataclass
class PolymarketUSClient:
    """Async REST client for api.polymarket.us (DCM / CFTC-regulated).

    Parameters
    ----------
    base_url:
        Base URL without trailing slash, e.g. ``"https://api.polymarket.us/v1"``.
    signer:
        Configured :class:`~arbiter.auth.ed25519_signer.Ed25519Signer`.
    session:
        Optional pre-created ``aiohttp.ClientSession``. If *None* (default),
        one is created lazily on the first request.
    """

    base_url: str
    signer: Ed25519Signer
    session: Optional[aiohttp.ClientSession] = field(default=None, repr=False)

    # Resilience helpers — created in __post_init__ so tests can override them
    circuit: CircuitBreaker = field(init=False, repr=False)
    live_rate_limiter: RateLimiter = field(init=False, repr=False)
    discovery_rate_limiter: RateLimiter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.circuit = CircuitBreaker(
            "polymarket-us-rest",
            failure_threshold=5,
            recovery_timeout=30,
        )
        # 18 r/s for live ops, 2 r/s for discovery (budget split per plan)
        self.live_rate_limiter = RateLimiter(
            "polymarket-us-live", max_requests=18, window_seconds=1.0
        )
        self.discovery_rate_limiter = RateLimiter(
            "polymarket-us-discovery", max_requests=2, window_seconds=1.0
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT)
        return self.session

    async def _signed(
        self,
        method: str,
        path: str,
        json_body: Optional[dict] = None,
        purpose: str = "live",
    ) -> dict:
        """Execute a signed HTTP request with 429-retry and circuit-breaker.

        Parameters
        ----------
        method:
            HTTP verb (``"GET"``, ``"POST"``).
        path:
            Path **including** any query string, e.g. ``"/markets?limit=100&offset=0"``.
        json_body:
            Optional JSON body for POST requests.
        purpose:
            ``"discovery"`` → 2 r/s budget; anything else → 18 r/s budget.
        """
        rate_limiter = (
            self.discovery_rate_limiter if purpose == "discovery" else self.live_rate_limiter
        )
        sess = await self._ensure_session()
        url = f"{self.base_url}{path}"

        for attempt in range(3):
            if not self.circuit.can_execute():
                raise RuntimeError(
                    f"Circuit [{self.circuit.name}] is OPEN — request rejected"
                )

            await rate_limiter.acquire()
            headers = self.signer.headers(method, path)

            try:
                async with sess.request(
                    method, url, headers=headers, json=json_body
                ) as resp:
                    # Log path + status only — never the secret or signature value
                    logger.debug("polymarket-us %s %s → %s", method, path, resp.status)

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", "0") or "0")
                        logger.warning(
                            "polymarket-us 429 on %s, retry after %.1fs (attempt %s/3)",
                            path,
                            retry_after,
                            attempt + 1,
                        )
                        if retry_after > 0:
                            await asyncio.sleep(retry_after)
                        continue

                    resp.raise_for_status()
                    self.circuit.record_success()
                    return await resp.json()

            except aiohttp.ClientResponseError:
                self.circuit.record_failure()
                raise
            except Exception as exc:
                self.circuit.record_failure()
                raise RuntimeError(
                    f"polymarket-us request failed ({method} {path}): {exc}"
                ) from exc

        raise RuntimeError(
            f"polymarket-us rate-limit retry exhausted after 3 attempts ({method} {path})"
        )

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    async def list_markets(
        self,
        page_size: int = 100,
        purpose: str = "live",
    ) -> AsyncIterator[dict]:
        """Async generator that pages through ``/markets``.

        Parameters
        ----------
        page_size:
            Markets per page (default 100, max 100 per API contract).
        purpose:
            Rate-limiter budget: ``"discovery"`` (2 r/s) or ``"live"`` (18 r/s).
        """
        offset = 0
        while True:
            path = f"/markets?limit={page_size}&offset={offset}"
            data = await self._signed("GET", path, purpose=purpose)
            for market in data.get("markets", []):
                yield market
            if not data.get("hasMore"):
                break
            offset += page_size

    async def get_orderbook(self, symbol: str, depth: int = 10) -> dict:
        """Fetch the order book for *symbol* with *depth* levels per side."""
        return await self._signed("GET", f"/orderbook/{symbol}?depth={depth}")

    async def place_order(
        self,
        slug: str,
        intent: str,
        price: float,
        qty: int,
        tif: str = "FILL_OR_KILL",
    ) -> dict:
        """Place a limit order.

        Parameters
        ----------
        slug:
            Market slug identifier.
        intent:
            ``"BUY_LONG"`` or ``"SELL_LONG"`` — prefixed with ``ORDER_INTENT_`` automatically.
        price:
            Limit price as a decimal (e.g. ``0.51``).
        qty:
            Quantity (contracts).
        tif:
            Time-in-force: ``"FILL_OR_KILL"`` (default), ``"GTC"``, etc.
            Prefixed with ``TIF_`` automatically.
        """
        body = {
            "marketSlug": slug,
            "intent": f"ORDER_INTENT_{intent}",
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": str(price), "currency": "USD"},
            "quantity": qty,
            "tif": f"TIF_{tif}",
        }
        return await self._signed("POST", "/orders", json_body=body)

    async def cancel_order(self, order_id: str, slug: str) -> dict:
        """Cancel an open order."""
        return await self._signed(
            "POST",
            f"/order/{order_id}/cancel",
            json_body={"marketSlug": slug},
        )

    async def balance(self) -> dict:
        """Fetch current account balance."""
        return await self._signed("GET", "/account/balances")

    async def close(self) -> None:
        """Close the underlying HTTP session idempotently."""
        if self.session is not None and not self.session.closed:
            await self.session.close()
        # Set to None so subsequent calls are safe (idempotent)
        self.session = None
