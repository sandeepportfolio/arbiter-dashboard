"""
Polymarket US REST client + polling collector.

Polymarket US splits its API surface in two:
  - Authenticated trading/account API: ``https://api.polymarket.us``
  - Public market-data API: ``https://gateway.polymarket.us``

This module keeps those surfaces separate so the runtime talks to the same
endpoints the current docs describe.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional
from urllib.parse import urlencode

import aiohttp

from arbiter.auth.ed25519_signer import Ed25519Signer
from arbiter.config.settings import (
    MARKET_MAP,
    POLYMARKET_DEFAULT_TAKER_FEE_RATE,
    PolymarketUSConfig,
)
from arbiter.utils.price_store import PricePoint, PriceStore
from arbiter.utils.retry import CircuitBreaker, RateLimiter

logger = logging.getLogger("arbiter.collector.polymarket_us")

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Matches YYYY-MM-DD anywhere in a slug (e.g. "asc-mlb-mil-mia-2026-04-17-neg-3pt5")
_SLUG_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _slug_is_expired(slug: str, grace_days: int = 1) -> bool:
    """Return True if the slug contains a date that is in the past (with grace period).

    Sports-event slugs embed their game date (e.g. 2026-04-17). Once the date has
    passed, the market is settled/expired and fetching its book is pointless.
    Slugs without a date (e.g. midterm politics) are never considered expired.
    """
    match = _SLUG_DATE_RE.search(slug)
    if not match:
        return False
    try:
        slug_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return slug_date < date.today() - timedelta(days=grace_days)
    except ValueError:
        return False


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    return path if path.startswith("/") else f"/{path}"


def _request_path_for_base(base_url: str, path: str) -> str:
    normalized = _normalize_path(path)
    if base_url.rstrip("/").endswith("/v1"):
        if normalized.startswith("/v1/"):
            return normalized[3:] or "/"
        return normalized
    if normalized.startswith("/v1/"):
        return normalized
    return f"/v1{normalized}"


def _signature_path(path: str) -> str:
    normalized = _normalize_path(path)
    if normalized.startswith("/v1/"):
        return normalized
    return f"/v1{normalized}"


def _amount_value(value) -> float:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _level_qty(levels: list[dict], limit: int = 5) -> float:
    total = 0.0
    for level in (levels or [])[:limit]:
        try:
            total += float(level.get("qty", 0.0))
        except (TypeError, ValueError):
            continue
    return total


def extract_current_balance(payload: dict) -> float:
    """Return the USD currentBalance from a Polymarket US balances payload."""
    if not isinstance(payload, dict):
        raise ValueError("balance payload must be an object")

    balances = payload.get("balances")
    if isinstance(balances, list):
        preferred: Optional[dict] = None
        for entry in balances:
            if not isinstance(entry, dict):
                continue
            if preferred is None:
                preferred = entry
            if str(entry.get("currency", "")).upper() == "USD":
                preferred = entry
                break
        if preferred is not None:
            return float(preferred.get("currentBalance", 0.0))

    # Backwards-compat fallback for any older flat payload shape.
    return float(payload.get("currentBalance", 0.0))


@dataclass
class PolymarketUSClient:
    """Async REST client for the current Polymarket US retail API."""

    base_url: str
    signer: Optional[Ed25519Signer]
    public_base_url: str = "https://gateway.polymarket.us"
    session: Optional[aiohttp.ClientSession] = field(default=None, repr=False)

    circuit: CircuitBreaker = field(init=False, repr=False)
    live_rate_limiter: RateLimiter = field(init=False, repr=False)
    discovery_rate_limiter: RateLimiter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.circuit = CircuitBreaker(
            "polymarket-us-rest",
            failure_threshold=5,
            recovery_timeout=30,
        )
        self.live_rate_limiter = RateLimiter(
            "polymarket-us-live", max_requests=18, window_seconds=1.0
        )
        self.discovery_rate_limiter = RateLimiter(
            "polymarket-us-discovery", max_requests=2, window_seconds=1.0
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=_DEFAULT_TIMEOUT)
        return self.session

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json_body: Optional[dict] = None,
        purpose: str = "live",
    ) -> dict:
        rate_limiter = (
            self.discovery_rate_limiter if purpose == "discovery" else self.live_rate_limiter
        )
        session = await self._ensure_session()

        for attempt in range(3):
            if not self.circuit.can_execute():
                raise RuntimeError(
                    f"Circuit [{self.circuit.name}] is OPEN — request rejected"
                )

            await rate_limiter.acquire()

            try:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                ) as resp:
                    logger.debug("polymarket-us %s %s -> %s", method, url, resp.status)

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", "0") or "0")
                        logger.warning(
                            "polymarket-us 429 on %s, retry after %.1fs (attempt %s/3)",
                            url,
                            retry_after,
                            attempt + 1,
                        )
                        if retry_after > 0:
                            await asyncio.sleep(retry_after)
                        continue

                    resp.raise_for_status()
                    self.circuit.record_success()
                    if resp.status == 204:
                        return {}
                    text = await resp.text()
                    return await resp.json() if text else {}
            except aiohttp.ClientResponseError as exc:
                if exc.status >= 500:
                    self.circuit.record_failure()
                raise
            except Exception as exc:
                self.circuit.record_failure()
                raise RuntimeError(
                    f"polymarket-us request failed ({method} {url}): {exc}"
                ) from exc

        raise RuntimeError(
            f"polymarket-us rate-limit retry exhausted after 3 attempts ({method} {url})"
        )

    async def _signed(
        self,
        method: str,
        path: str,
        json_body: Optional[dict] = None,
        *,
        purpose: str = "live",
    ) -> dict:
        if self.signer is None:
            raise RuntimeError("Polymarket US signer not configured")

        request_path = _request_path_for_base(self.base_url, path)
        url = f"{self.base_url.rstrip('/')}{request_path}"
        headers = self.signer.headers(method, _signature_path(path))
        headers.setdefault("Content-Type", "application/json")
        return await self._request(
            method,
            url,
            headers=headers,
            json_body=json_body,
            purpose=purpose,
        )

    async def _public(
        self,
        method: str,
        path: str,
        *,
        purpose: str = "live",
    ) -> dict:
        request_path = _request_path_for_base(self.public_base_url, path)
        url = f"{self.public_base_url.rstrip('/')}{request_path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "arbiter-polymarket-us/1.0",
        }
        return await self._request(method, url, headers=headers, purpose=purpose)

    async def list_markets(
        self,
        page_size: int = 100,
        max_pages: Optional[int] = None,
        purpose: str = "live",
        active: Optional[bool] = None,
        closed: Optional[bool] = None,
        archived: Optional[bool] = None,
    ) -> AsyncIterator[dict]:
        offset = 0
        pages = 0
        while True:
            if max_pages is not None and pages >= max_pages:
                logger.warning(
                    "Polymarket US list_markets reached max_pages=%d at offset=%d",
                    max_pages,
                    offset,
                )
                break

            params = {"limit": page_size, "offset": offset}
            if active is not None:
                params["active"] = str(active).lower()
            if closed is not None:
                params["closed"] = str(closed).lower()
            if archived is not None:
                params["archived"] = str(archived).lower()

            data = await self._public(
                "GET",
                f"/markets?{urlencode(params)}",
                purpose=purpose,
            )
            pages += 1
            markets = data.get("markets", [])
            if not markets:
                break

            for market in markets:
                yield market

            has_more = data.get("hasMore")
            if has_more is False:
                break

            offset += len(markets)

    async def get_market_by_slug(self, slug: str) -> dict:
        return await self._public("GET", f"/market/slug/{slug}")

    async def get_market_book(self, slug: str) -> dict:
        return await self._public("GET", f"/markets/{slug}/book")

    async def get_market_bbo(self, slug: str) -> dict:
        return await self._public("GET", f"/markets/{slug}/bbo")

    async def get_orderbook(self, symbol: str, depth: int = 10) -> dict:
        """Compatibility alias for the old client API.

        The current public docs expose market books at ``/v1/markets/{slug}/book``.
        The REST API does not support a ``depth`` query param on this endpoint.
        """
        return await self.get_market_book(symbol)

    async def place_order(
        self,
        slug: str,
        intent: str,
        price: float,
        qty: int,
        tif: str = "FILL_OR_KILL",
        *,
        manual_order_indicator: str = "AUTOMATIC",
        synchronous_execution: bool = True,
        max_block_time: int = 10,
    ) -> dict:
        resolved_intent = intent if intent.startswith("ORDER_INTENT_") else f"ORDER_INTENT_{intent}"
        resolved_tif = tif if tif.startswith("TIME_IN_FORCE_") else f"TIME_IN_FORCE_{tif}"
        resolved_manual = (
            manual_order_indicator
            if manual_order_indicator.startswith("MANUAL_ORDER_INDICATOR_")
            else f"MANUAL_ORDER_INDICATOR_{manual_order_indicator}"
        )
        body = {
            "marketSlug": slug,
            "intent": resolved_intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": str(price), "currency": "USD"},
            "quantity": qty,
            "tif": resolved_tif,
            "manualOrderIndicator": resolved_manual,
            "synchronousExecution": synchronous_execution,
            "maxBlockTime": str(max_block_time),
        }
        return await self._signed("POST", "/orders", json_body=body)

    async def get_order(self, order_id: str) -> dict:
        return await self._signed("GET", f"/order/{order_id}")

    async def cancel_order(self, order_id: str, slug: str) -> dict:
        return await self._signed(
            "POST",
            f"/order/{order_id}/cancel",
            json_body={"marketSlug": slug},
        )

    async def cancel_all_open_orders(self, market_slug: Optional[str] = None) -> dict:
        body = {"marketSlug": market_slug} if market_slug else {}
        return await self._signed("POST", "/orders/open/cancel", json_body=body)

    async def balance(self) -> dict:
        return await self._signed("GET", "/account/balances")

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None


@dataclass
class PolymarketUSCollector:
    """Polling collector for Polymarket US public market data + signed balances."""

    config: PolymarketUSConfig
    store: PriceStore
    client: PolymarketUSClient

    total_fetches: int = 0
    total_errors: int = 0
    consecutive_errors: int = 0
    _running: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.circuit = self.client.circuit
        self.rate_limiter = self.client.live_rate_limiter
        self._slug_map: dict[str, str] = {}
        self.refresh_tracked_markets()
        self._inactive_slugs: set[str] = set()

    def refresh_tracked_markets(self) -> None:
        """Reload tracked slugs so newly discovered mappings are picked up.

        Automatically skips slugs with embedded dates that are in the past
        (expired sports events, etc.) to avoid hammering the API with 404s.
        """
        slug_map: dict[str, str] = {}
        skipped = 0
        for canonical_id, mapping in MARKET_MAP.items():
            slug = str(mapping.get("polymarket", "") or "").strip()
            if not slug:
                continue
            if _slug_is_expired(slug):
                skipped += 1
                continue
            slug_map[canonical_id] = slug
        if skipped:
            logger.info(
                "Polymarket US: tracking %d markets, skipped %d expired slugs",
                len(slug_map),
                skipped,
            )
        self._slug_map = slug_map

    def _build_price_point(
        self,
        canonical_id: str,
        slug: str,
        market_book: dict,
    ) -> Optional[PricePoint]:
        market_data = market_book.get("marketData") if isinstance(market_book, dict) else None
        if not isinstance(market_data, dict):
            return None

        bids = list(market_data.get("bids") or [])
        offers = list(market_data.get("offers") or [])
        stats = market_data.get("stats") or {}
        state = str(market_data.get("state", "") or "").strip().lower()

        if state in {"closed", "resolved", "settled", "suspended", "halted", "expired", "finalized"}:
            return None
        if not bids and not offers:
            return None

        yes_bid = _amount_value((bids[0] or {}).get("px")) if bids else 0.0
        yes_ask = _amount_value((offers[0] or {}).get("px")) if offers else 0.0
        current_px = _amount_value(stats.get("currentPx") or stats.get("lastTradePx"))

        yes_price = yes_ask or current_px or yes_bid
        no_ask = (1.0 - yes_bid) if yes_bid else max(1.0 - current_px, 0.0)
        no_bid = (1.0 - yes_ask) if yes_ask else 0.0
        no_price = no_ask or max(1.0 - yes_price, 0.0)

        if yes_price <= 0.0 and no_price <= 0.0:
            return None

        mapping = MARKET_MAP.get(canonical_id, {})
        return PricePoint(
            platform="polymarket",
            canonical_id=canonical_id,
            yes_price=yes_price,
            no_price=no_price,
            yes_volume=_level_qty(offers) or _level_qty(bids),
            no_volume=_level_qty(bids) or _level_qty(offers),
            timestamp=time.time(),
            raw_market_id=slug,
            yes_market_id=slug,
            no_market_id=slug,
            yes_bid=yes_bid,
            yes_ask=yes_ask or current_px,
            no_bid=no_bid,
            no_ask=no_ask,
            fee_rate=POLYMARKET_DEFAULT_TAKER_FEE_RATE,
            mapping_status=str(mapping.get("status", "candidate")),
            mapping_score=float(mapping.get("mapping_score", 0.0)),
            metadata={
                "slug": slug,
                "market_state": market_data.get("state"),
            },
        )

    async def fetch_markets(self) -> list[PricePoint]:
        self.refresh_tracked_markets()
        results: list[PricePoint] = []

        for canonical_id, slug in self._slug_map.items():
            if slug in self._inactive_slugs:
                continue

            self.total_fetches += 1
            try:
                book = await self.client.get_market_book(slug)
                price = self._build_price_point(canonical_id, slug, book)
                if price is None:
                    continue
                results.append(price)
                await self.store.put(price)
                self.consecutive_errors = 0
            except aiohttp.ClientResponseError as exc:
                if exc.status in {404, 410}:
                    self._inactive_slugs.add(slug)
                    logger.warning(
                        "Polymarket US market slug is unavailable on the public gateway, disabling it: %s",
                        slug,
                    )
                    continue
                self.total_errors += 1
                self.consecutive_errors += 1
                logger.error("Polymarket US market fetch failed for %s: %s", slug, exc)
            except Exception as exc:
                self.total_errors += 1
                self.consecutive_errors += 1
                logger.error("Polymarket US market fetch failed for %s: %s", slug, exc)

        return results

    async def fetch_balance(self) -> Optional[float]:
        if not (self.config.api_key_id and self.config.api_secret):
            logger.debug("Polymarket US API credentials not configured, skipping balance")
            return None

        try:
            body = await self.client.balance()
            balance = extract_current_balance(body)
            logger.info("Polymarket US balance: $%.2f", balance)
            return balance
        except Exception as exc:
            logger.error("Polymarket US balance error: %s", exc)
            return None

    async def run(self) -> None:
        self._running = True
        logger.info(
            "Polymarket US collector started (poll interval: %ss)",
            self.config.poll_interval,
        )
        while self._running:
            try:
                await self.fetch_markets()
                await asyncio.sleep(self.config.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.total_errors += 1
                self.consecutive_errors += 1
                delay = min(2 ** min(self.consecutive_errors, 5), 30)
                logger.error(
                    "Polymarket US collector error (#%s), backoff %ss: %s",
                    self.consecutive_errors,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._running = False
        await self.client.close()
