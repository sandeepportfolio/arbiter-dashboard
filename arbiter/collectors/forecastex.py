"""
ForecastEx REST client + polling collector.

ForecastEx is the IBKR-routed prediction market. All HTTP calls go through
the local IBKR Client Portal Web API gateway (defaults to
``https://localhost:5000/v1/api``); the gateway owns the SSO session so this
module never sees the IBKR password or 2FA.

Modelled after ``polymarket_us.py``:
  - dataclass ``ForecastExClient`` with circuit breaker + rate limiter
  - dataclass ``ForecastExCollector`` polling MARKET_MAP entries that carry
    a ``forecastex`` contract id

Only BUY orders are exposed (`place_order(side='BUY')`); ForecastEx contracts
are resolved by closing the matching YES/NO contract rather than selling, so
the engine never asks the adapter to SELL.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from arbiter.config.settings import (
    FORECASTEX_TAKER_FEE_PER_CONTRACT,
    ForecastExConfig,
    MARKET_MAP,
)
from arbiter.utils.price_store import PricePoint, PriceStore
from arbiter.utils.retry import CircuitBreaker, RateLimiter

logger = logging.getLogger("arbiter.collector.forecastex")

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _amount_value(value) -> float:
    """Best-effort numeric coercion. IBKR snapshots stringify everything."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, str):
        # IBKR sometimes prefixes prices with 'C' (close) or 'H' (halted).
        value = value.lstrip("CHc h").strip()
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _build_ssl_context(verify: bool) -> ssl.SSLContext | bool:
    """Match the gateway's self-signed cert when verify_ssl is off."""
    if verify:
        return ssl.create_default_context()
    return False


@dataclass
class ForecastExClient:
    """Async REST client for the IBKR Client Portal Web API.

    The gateway speaks plain JSON over HTTPS once an SSO session is open;
    we treat 401/403 as a signal the operator needs to re-authenticate and
    surface the failure rather than silently retrying.
    """

    gateway_url: str
    account_id: str
    verify_ssl: bool = False
    paper_trading: bool = True
    session: Optional[aiohttp.ClientSession] = field(default=None, repr=False)

    circuit: CircuitBreaker = field(init=False, repr=False)
    live_rate_limiter: RateLimiter = field(init=False, repr=False)
    # IBKR brokerage-bridge state. ``/iserver/*`` endpoints reply
    # ``400 "no bridge"`` until ``/iserver/auth/ssodh/init`` has been called
    # for this gateway session.  We initialize lazily on the first iserver
    # request and re-initialize if a subsequent call ever sees the same
    # "no bridge" response (e.g. the IBKR session timed out and reconnected).
    _bridge_ready: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.circuit = CircuitBreaker(
            "forecastex-rest",
            failure_threshold=5,
            recovery_timeout=30,
        )
        # IBKR documents 10rps as the comfortable steady-state rate.
        self.live_rate_limiter = RateLimiter(
            "forecastex-live", max_requests=10, window_seconds=1.0,
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(ssl=_build_ssl_context(self.verify_ssl))
            self.session = aiohttp.ClientSession(
                timeout=_DEFAULT_TIMEOUT, connector=connector,
            )
        return self.session

    def _url(self, path: str) -> str:
        base = self.gateway_url.rstrip("/")
        suffix = path if path.startswith("/") else f"/{path}"
        return f"{base}{suffix}"

    async def _ensure_iserver_bridge(self) -> None:
        """Initialize the IBKR brokerage bridge required by /iserver/* endpoints.

        The Client Portal gateway maintains TWO sessions: an authenticated
        SSO session (used for /portfolio/*) and a brokerage bridge
        (required for /iserver/*).  The bridge must be explicitly
        initialized via POST /iserver/auth/ssodh/init; otherwise every
        iserver call returns 400 ``{"error":"Bad Request: no bridge"}``.

        Audit 2026-05: forecastex_discovery returned 0 events on every run
        because the bridge had never been initialized — all secdef/search
        calls 400-failed silently in the per-keyword exception handler.
        """
        if self._bridge_ready:
            return
        session = await self._ensure_session()
        # Best-effort — if init fails the next iserver call will surface
        # the underlying error.  Don't block startup on bridge issues.
        try:
            async with session.post(
                self._url("/iserver/auth/ssodh/init"),
                json={"publish": True, "compete": True},
                headers={"Accept": "application/json"},
            ) as resp:
                if resp.status == 200:
                    self._bridge_ready = True
                    logger.info("forecastex: iserver bridge initialized")
                else:
                    body = await resp.text()
                    logger.warning(
                        "forecastex: iserver bridge init returned %s: %s",
                        resp.status, body[:200],
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("forecastex: iserver bridge init raised: %s", exc)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        session = await self._ensure_session()
        url = self._url(path)

        # /iserver/* paths need the brokerage bridge; ensure it up-front so
        # the first FORECASTX discovery call after gateway restart doesn't
        # silently 400.  /portfolio/* and a handful of read-only endpoints
        # work without the bridge, so we don't gate them.
        if path.startswith("/iserver/") and path != "/iserver/auth/ssodh/init":
            await self._ensure_iserver_bridge()

        for attempt in range(3):
            if not self.circuit.can_execute():
                raise RuntimeError(
                    f"Circuit [{self.circuit.name}] is OPEN — request rejected"
                )

            await self.live_rate_limiter.acquire()

            try:
                async with session.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers={"Accept": "application/json"},
                ) as resp:
                    logger.debug("forecastex %s %s -> %s", method, path, resp.status)

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", "1") or "1")
                        logger.warning(
                            "forecastex 429 on %s, retry in %.1fs (attempt %s/3)",
                            path, retry_after, attempt + 1,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if resp.status in (401, 403):
                        text = await resp.text()
                        self.circuit.record_failure()
                        raise RuntimeError(
                            f"forecastex auth error {resp.status}: "
                            f"gateway session expired — re-authenticate via /sso. "
                            f"Body: {text[:200]}"
                        )

                    # ``400 {"error":"Bad Request: no bridge"}`` means the
                    # brokerage bridge needs (re-)initialization.  Re-init
                    # and retry once before surfacing the error so transient
                    # session drops self-heal without operator action.
                    if (
                        resp.status == 400
                        and path.startswith("/iserver/")
                        and path != "/iserver/auth/ssodh/init"
                        and attempt == 0
                    ):
                        body = await resp.text()
                        if "no bridge" in body.lower():
                            logger.warning(
                                "forecastex: '/iserver' 400 'no bridge' on %s "
                                "— re-initializing brokerage bridge",
                                path,
                            )
                            self._bridge_ready = False
                            await self._ensure_iserver_bridge()
                            continue

                    resp.raise_for_status()
                    self.circuit.record_success()
                    if resp.status == 204:
                        return {}
                    text = await resp.text()
                    if not text:
                        return {}
                    # IBKR sometimes returns a JSON array at the top level;
                    # wrap it so callers can always treat the result as dict.
                    parsed = await resp.json()
                    if isinstance(parsed, list):
                        return {"items": parsed}
                    return parsed
            except aiohttp.ClientResponseError as exc:
                if exc.status >= 500:
                    self.circuit.record_failure()
                raise
            except RuntimeError:
                raise
            except Exception as exc:
                self.circuit.record_failure()
                raise RuntimeError(
                    f"forecastex request failed ({method} {path}): {exc}"
                ) from exc

        raise RuntimeError(
            f"forecastex rate-limit retry exhausted after 3 attempts ({method} {path})"
        )

    # ── Account / portfolio ────────────────────────────────────────

    async def accounts(self) -> dict:
        return await self._request("GET", "/portfolio/accounts")

    async def account_balance(self) -> float:
        """Return cash balance (USD) for the configured account.

        The IBKR portfolio endpoint replies with a nested ``cashbalance`` /
        ``totalcashvalue`` payload; we prefer ``availablefunds`` because it
        reflects what the engine can actually spend without breaching margin.
        """
        if not self.account_id:
            raise RuntimeError("IBKR_ACCOUNT_ID is not configured")
        # Force the gateway to re-evaluate cached values so a fresh deposit
        # shows up before the next 60-second internal refresh.
        await self.accounts()
        payload = await self._request(
            "GET", f"/portfolio/{self.account_id}/summary",
        )
        for key in ("availablefunds", "totalcashvalue", "cashbalance"):
            entry = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(entry, dict):
                amount = _amount_value(entry.get("amount"))
                if amount:
                    return amount
            elif entry is not None:
                amount = _amount_value(entry)
                if amount:
                    return amount
        return 0.0

    async def positions(self) -> list[dict]:
        if not self.account_id:
            return []
        payload = await self._request(
            "GET", f"/portfolio/{self.account_id}/positions/0",
        )
        if isinstance(payload, dict):
            return list(payload.get("items") or payload.get("positions") or [])
        if isinstance(payload, list):
            return payload
        return []

    # ── Market data ────────────────────────────────────────────────

    async def market_snapshot(self, conid: str) -> dict:
        """Single-conid snapshot. Returns {} when IBKR is still warming
        the field cache (first call to a conid often comes back empty).
        """
        # IBKR field codes:
        #   31 = last price, 84 = bid, 86 = ask, 7295 = bid size, 7296 = ask size
        params = {"conids": str(conid), "fields": "31,84,86,7295,7296"}
        payload = await self._request(
            "GET", "/iserver/marketdata/snapshot", params=params,
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list) and items:
            return items[0]
        return payload if isinstance(payload, dict) else {}

    async def search_contracts(self, symbol: str) -> list[dict]:
        body = {"symbol": symbol, "name": True, "secType": "OPT"}
        payload = await self._request(
            "POST", "/iserver/secdef/search", json_body=body,
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, list):
            return items
        return []

    async def get_contract_info(self, conid: str) -> dict:
        """Return /iserver/contract/{conid}/info — metadata, no prices."""
        try:
            return await self._request(
                "GET", f"/iserver/contract/{conid}/info",
            )
        except Exception:
            return {}

    async def get_trsrv_secdef(self, conid: str) -> dict:
        """Return /trsrv/secdef payload — includes hasOptions, assetClass, type."""
        try:
            payload = await self._request(
                "GET", "/trsrv/secdef", params={"conids": str(conid)},
            )
            entries = payload.get("secdef") if isinstance(payload, dict) else None
            if isinstance(entries, list) and entries:
                return entries[0]
        except Exception:
            pass
        return {}

    async def resolve_event_children(
        self, parent_conid: str, *, months: tuple[str, ...] = (),
    ) -> list[dict]:
        """Try every known IBKR endpoint to enumerate the YES/NO child
        contracts under a FORECASTX event parent.

        Returns a list of ``{"conid": ..., "right": "Y"|"N"|"C"|"P", ...}`` dicts.
        Empty list if every strategy fails — callers must handle that
        gracefully because IBKR's FORECASTX endpoints return 503 on weekends
        and are inconsistently supported even on weekdays.

        Strategies tried (in order):
          1. POST /iserver/secdef/info with sectype=EC + each month
          2. GET  /iserver/secdef/info  with sectype=EC + each month
          3. POST /iserver/secdef/search with the parent's ticker + secType=EC
          4. /trsrv/secdef hasOptions check + symbol-derived search
        """
        children: list[dict] = []
        month_candidates = months or ("NOV26", "DEC26", "JAN27", "202611", "202612")

        # Strategy 1+2 — secdef/info with sectype=EC
        for method in ("POST", "GET"):
            for month in month_candidates:
                try:
                    if method == "POST":
                        payload = await self._request(
                            "POST", "/iserver/secdef/info",
                            json_body={
                                "conid": str(parent_conid),
                                "sectype": "EC",
                                "month": month,
                            },
                        )
                    else:
                        payload = await self._request(
                            "GET", "/iserver/secdef/info",
                            params={
                                "conid": str(parent_conid),
                                "sectype": "EC",
                                "month": month,
                            },
                        )
                except Exception:
                    continue
                # Successful payloads carry a list of contracts under "items"
                # or directly as a list.
                items = payload.get("items") if isinstance(payload, dict) else payload
                if isinstance(items, list) and items:
                    for item in items:
                        if isinstance(item, dict) and item.get("conid"):
                            children.append({
                                "conid": str(item["conid"]),
                                "right": str(item.get("right") or item.get("putOrCall") or ""),
                                "strike": item.get("strike"),
                                "source": f"secdef/info:{method}:{month}",
                            })
                    if children:
                        return children

        # Strategy 3 — search by parent ticker
        secdef = await self.get_trsrv_secdef(parent_conid)
        ticker = str(secdef.get("ticker") or secdef.get("symbol") or "").strip()
        if ticker:
            try:
                payload = await self._request(
                    "POST", "/iserver/secdef/search",
                    json_body={"symbol": ticker, "secType": "EC"},
                )
                items = payload.get("items") if isinstance(payload, dict) else payload
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        conid = str(item.get("conid") or "").strip()
                        if conid and conid != str(parent_conid):
                            children.append({
                                "conid": conid,
                                "right": "",
                                "strike": None,
                                "source": f"secdef/search:{ticker}",
                            })
            except Exception:
                pass

        return children

    @staticmethod
    def is_tradeable_snapshot(snapshot: dict) -> bool:
        """A FORECASTX snapshot is tradeable when IBKR returns at least one of
        bid (field 84), ask (86), or last (31) with a positive value. Parent
        IND/Event conids return only ``conid``+``conidEx``+symbol+empty fields.
        """
        if not isinstance(snapshot, dict):
            return False
        for field_code in ("31", "84", "86"):
            value = snapshot.get(field_code)
            if value in (None, "", "N/A"):
                continue
            try:
                if float(str(value).lstrip("CHc h").strip()) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    # ── Orders ─────────────────────────────────────────────────────

    async def place_order(
        self,
        conid: str,
        side: str,
        price: float,
        quantity: int,
        *,
        tif: str = "IOC",
        order_type: str = "LMT",
    ) -> dict:
        """BUY-only LMT/IOC order placement.

        ForecastEx contracts are designed so closing a position means buying
        the complementary YES/NO contract — the arbitrage system never asks
        this adapter to SELL.
        """
        if not self.account_id:
            raise RuntimeError("IBKR_ACCOUNT_ID is not configured")
        if str(side).upper() != "BUY":
            raise ValueError(
                f"forecastex.place_order rejects side={side!r}: only BUY is supported"
            )
        body = {
            "orders": [
                {
                    "conid": int(conid) if str(conid).isdigit() else conid,
                    "orderType": order_type,
                    "side": "BUY",
                    "quantity": int(quantity),
                    "price": float(price),
                    "tif": tif,
                }
            ]
        }
        return await self._request(
            "POST", f"/iserver/account/{self.account_id}/orders", json_body=body,
        )

    async def get_order(self, order_id: str) -> dict:
        # IBKR exposes order state through the live orders endpoint; reading
        # by id directly is supported via /iserver/account/order/status/{id}.
        try:
            return await self._request(
                "GET", f"/iserver/account/order/status/{order_id}",
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                return {}
            raise

    async def cancel_order(self, order_id: str) -> dict:
        if not self.account_id:
            raise RuntimeError("IBKR_ACCOUNT_ID is not configured")
        return await self._request(
            "DELETE", f"/iserver/account/{self.account_id}/order/{order_id}",
        )

    async def list_live_orders(self) -> list[dict]:
        payload = await self._request("GET", "/iserver/account/orders")
        if isinstance(payload, dict):
            orders = payload.get("orders") or payload.get("items") or []
            if isinstance(orders, list):
                return orders
        if isinstance(payload, list):
            return payload
        return []

    async def cancel_all_open_orders(self) -> list[str]:
        cancelled: list[str] = []
        for order in await self.list_live_orders():
            order_id = str(order.get("orderId") or order.get("order_id") or "")
            if not order_id:
                continue
            try:
                await self.cancel_order(order_id)
                cancelled.append(order_id)
            except Exception as exc:
                logger.warning("forecastex.cancel_all failed for %s: %s", order_id, exc)
        return cancelled

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None


@dataclass
class ForecastExCollector:
    """Polling collector for ForecastEx market data + IBKR balance."""

    config: ForecastExConfig
    store: PriceStore
    client: ForecastExClient

    total_fetches: int = 0
    total_errors: int = 0
    consecutive_errors: int = 0
    _running: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.circuit = self.client.circuit
        self.rate_limiter = self.client.live_rate_limiter
        self._conid_map: dict[str, str] = {}
        self._inactive_conids: set[str] = set()
        # Track how many times each conid returned a non-tradeable snapshot
        # so we can disable untradeable parent-event conids after a few probes.
        self._parent_probe_counts: dict[str, int] = {}
        self.refresh_tracked_markets()

    def refresh_tracked_markets(self) -> None:
        """Reload tracked ForecastEx contract ids from MARKET_MAP."""
        new_map: dict[str, str] = {}
        for canonical_id, mapping in MARKET_MAP.items():
            conid = str(mapping.get("forecastex", "") or "").strip()
            if not conid:
                continue
            new_map[canonical_id] = conid
        if new_map != self._conid_map:
            logger.info(
                "ForecastEx: tracking %d markets (was %d)",
                len(new_map), len(self._conid_map),
            )
        self._conid_map = new_map

    def _build_price_point(
        self, canonical_id: str, conid: str, snapshot: dict,
    ) -> Optional[PricePoint]:
        bid = _amount_value(snapshot.get("84"))
        ask = _amount_value(snapshot.get("86"))
        last = _amount_value(snapshot.get("31"))
        bid_size = _amount_value(snapshot.get("7295"))
        ask_size = _amount_value(snapshot.get("7296"))

        # ForecastEx prices are quoted in cents (0–100). Normalize to dollars
        # so the scanner can compare apples-to-apples with the other venues.
        def _to_dollars(value: float) -> float:
            if value <= 0:
                return 0.0
            if value > 1.0:
                return max(0.0, min(value / 100.0, 1.0))
            return max(0.0, min(value, 1.0))

        yes_bid = _to_dollars(bid)
        yes_ask = _to_dollars(ask)
        yes_last = _to_dollars(last)
        yes_price = yes_ask or yes_last or yes_bid
        if yes_price <= 0:
            return None

        no_ask = max(1.0 - yes_bid, 0.0) if yes_bid else max(1.0 - yes_price, 0.0)
        no_bid = max(1.0 - yes_ask, 0.0) if yes_ask else 0.0
        no_price = no_ask

        mapping = MARKET_MAP.get(canonical_id, {})
        return PricePoint(
            platform="forecastex",
            canonical_id=canonical_id,
            yes_price=yes_price,
            no_price=no_price,
            yes_volume=ask_size or bid_size,
            no_volume=bid_size or ask_size,
            timestamp=time.time(),
            raw_market_id=conid,
            yes_market_id=conid,
            no_market_id=conid,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            # Flat $0.005/contract — stored so position sizing knows the
            # marginal fee without needing to call the fee function.
            fee_rate=FORECASTEX_TAKER_FEE_PER_CONTRACT,
            mapping_status=str(mapping.get("status", "candidate")),
            mapping_score=float(mapping.get("mapping_score", 0.0)),
            metadata={
                "conid": conid,
                "raw_snapshot": {
                    k: v for k, v in snapshot.items()
                    if k in ("31", "84", "86", "7295", "7296")
                },
            },
        )

    async def fetch_markets(self) -> list[PricePoint]:
        self.refresh_tracked_markets()
        results: list[PricePoint] = []

        for canonical_id, conid in self._conid_map.items():
            if conid in self._inactive_conids:
                continue
            self.total_fetches += 1
            try:
                snapshot = await self.client.market_snapshot(conid)
                # FORECASTX event-parent (IND) conids return only conidEx +
                # symbol + empty bid/ask. Polling them every cycle wastes
                # rate-limit budget and pollutes "price_point is None" logs
                # forever. Mark them inactive and let the operator (or the
                # YES/NO resolver, when IBKR's FORECASTX endpoints are
                # available) attach the child conid instead.
                if snapshot and not ForecastExClient.is_tradeable_snapshot(snapshot):
                    self._parent_probe_counts[conid] = (
                        self._parent_probe_counts.get(conid, 0) + 1
                    )
                    if self._parent_probe_counts[conid] >= 3:
                        self._inactive_conids.add(conid)
                        logger.warning(
                            "ForecastEx conid %s returns no bid/ask after %d "
                            "probes — looks like an untradeable parent event "
                            "conid. Disabling. Attach a child YES/NO conid via "
                            "ops UI to enable.",
                            conid, self._parent_probe_counts[conid],
                        )
                    continue
                price = self._build_price_point(canonical_id, conid, snapshot or {})
                if price is None:
                    continue
                results.append(price)
                await self.store.put(price)
                self.consecutive_errors = 0
            except aiohttp.ClientResponseError as exc:
                if exc.status in (404, 410):
                    self._inactive_conids.add(conid)
                    logger.warning(
                        "ForecastEx conid %s returned %s, disabling",
                        conid, exc.status,
                    )
                    continue
                self.total_errors += 1
                self.consecutive_errors += 1
                logger.error("ForecastEx fetch failed for %s: %s", conid, exc)
            except Exception as exc:
                self.total_errors += 1
                self.consecutive_errors += 1
                logger.error("ForecastEx fetch failed for %s: %s", conid, exc)

        return results

    balance_source: str = field(default="forecastex:ibkr-gateway", init=False)

    async def fetch_balance(self) -> Optional[float]:
        if not self.config.account_id:
            logger.debug("ForecastEx account id missing; skipping balance fetch")
            return None
        try:
            balance = await self.client.account_balance()
            logger.info("ForecastEx balance: $%.2f", balance)
            return balance
        except Exception as exc:
            logger.error("ForecastEx balance error: %s", exc)
            raise

    async def run(self) -> None:
        self._running = True
        logger.info(
            "ForecastEx collector started (poll interval: %ss, paper=%s)",
            self.config.poll_interval, self.config.paper_trading,
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
                    "ForecastEx collector error (#%s), backoff %ss: %s",
                    self.consecutive_errors, delay, exc,
                )
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._running = False
        await self.client.close()
