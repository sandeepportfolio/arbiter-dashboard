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

BUY and SELL orders are exposed (`place_order(side='BUY'|'SELL')`); the
adapter sells the same conid we bought to close exposure directly when the
engine smart-unwinds a naked leg.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

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
        use_circuit: bool = True,
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
            if use_circuit and not self.circuit.can_execute():
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
                        # Honor Retry-After when present; otherwise fall back
                        # to exponential backoff. The discovery endpoint
                        # /iserver/secdef/strikes ALWAYS uses the exponential
                        # ladder — IBKR's rate limit on it is tight enough
                        # that a flat 1s/1s/1s would flood straight back into
                        # a 429 (verified 2026-05-25). Bumped from 1/3/9s to
                        # 5/15/45s on 2026-05-26 after observing every one of
                        # 41 resolver candidates 429'd in a single cycle —
                        # the prior ladder retried too aggressively to let
                        # the rate-limit bucket refill.
                        header_retry = resp.headers.get("Retry-After")
                        is_strikes = path.startswith("/iserver/secdef/strikes")
                        exp_backoff = (5.0, 15.0, 45.0)[min(attempt, 2)] if is_strikes else (1.0, 3.0, 9.0)[min(attempt, 2)]
                        if is_strikes:
                            retry_after = exp_backoff
                        elif header_retry:
                            try:
                                retry_after = float(header_retry)
                            except (TypeError, ValueError):
                                retry_after = exp_backoff
                        else:
                            retry_after = exp_backoff
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
                    if use_circuit:
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
                if use_circuit and exc.status >= 500:
                    self.circuit.record_failure()
                raise
            except RuntimeError:
                raise
            except Exception as exc:
                if use_circuit:
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
            use_circuit=False,
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
                use_circuit=False,
            )
        except Exception:
            return {}

    async def get_trsrv_secdef(self, conid: str) -> dict:
        """Return /trsrv/secdef payload — includes hasOptions, assetClass, type."""
        try:
            payload = await self._request(
                "GET", "/trsrv/secdef", params={"conids": str(conid)},
                use_circuit=False,
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
        """Enumerate the YES/NO child contracts under a FORECASTX event
        parent using the documented IBKR Web API discovery flow.

        Per docs.interactivebrokers.com/campus/ibkr-api-page/event-
        contracts/ (verified live 2026-05-25 against the IBKR Client
        Portal gateway), ForecastEx products are modeled as OPTIONS:

          1. /iserver/secdef/strikes?conid=<parent>&exchange=FORECASTX
             &sectype=OPT&month=<MMMYY>
             → returns {call: [...strikes], put: [...strikes]}
          2. /iserver/secdef/info?...&strike=<X>
             → returns [{conid, right=C|P, strike, desc2, maturityDate}]

        ``right=C`` is the YES (Call) child; ``right=P`` is the NO (Put).

        Returns a list of ``{conid, right, strike, source, maturityDate,
        desc}`` dicts for ALL strike/right combinations found. Empty
        list when no strikes exist in any probed month (caller decides
        whether to mark the parent ``forecastex_not_available``).

        ``months`` may be empty (uses a built-in 12-month + 12-month
        candidate list) or a tuple of MMMYY strings. We stop at the
        FIRST month that returns non-empty strikes so a single-event
        contract like HORC (NOV26 only) doesn't waste rate-limit budget
        probing 24 other months.

        Previous implementation used ``sectype=EC`` which returned
        empty results AND triggered 503s on the EC endpoint — known
        to be flaky / unsupported per IBKR's own docs. The OPT path
        is the documented one and works on weekdays AND weekends.
        """
        children: list[dict] = []
        if months:
            month_candidates = tuple(months)
        else:
            # Span 2026 + 2027 so per-event maturity months are covered.
            month_candidates = tuple(
                f"{m}{y}"
                for y in ("26", "27")
                for m in (
                    "JAN","FEB","MAR","APR","MAY","JUN",
                    "JUL","AUG","SEP","OCT","NOV","DEC",
                )
            )

        # Step 1: enumerate strikes per month until one comes back non-empty.
        hit_month: Optional[str] = None
        calls: list[float] = []
        puts: list[float] = []
        for month in month_candidates:
            try:
                payload = await self._request(
                    "GET", "/iserver/secdef/strikes",
                    params={
                        "conid": str(parent_conid),
                        "exchange": "FORECASTX",
                        "sectype": "OPT",
                        "month": month,
                    },
                    use_circuit=False,
                )
            except Exception:
                # 503 / circuit-open / timeout — try next month rather
                # than aborting the whole sequence.
                continue
            if not isinstance(payload, dict):
                continue
            month_calls = list(payload.get("call") or [])
            month_puts = list(payload.get("put") or [])
            if month_calls or month_puts:
                hit_month = month
                calls = month_calls
                puts = month_puts
                break

        if hit_month is None:
            # IBKR's ``/iserver/secdef/strikes`` returns
            # ``{call: [], put: []}`` for some FORECASTX parent conids
            # even when ``/iserver/secdef/info`` does return the strike
            # 1.0 / 2.0 child contracts (verified live 2026-05-26 on
            # SENM 745923952 — US Senate Majority — where strikes was
            # empty on every NOV/OCT/DEC month but info?strike=1.0
            # cleanly returned the DEM and GOP child Call/Put conids).
            # HORC (House Control) does NOT have this quirk, so the
            # primary path above succeeds for it; the fallback is here
            # purely to cover the per-conid IBKR bug.
            #
            # Gate the fallback on the parent name containing a binary
            # political-control marker. Without the gate, the same 24 ×
            # 2 = 48 calls would fire against every wrong-attached
            # multi-strike CPI/GDP/PCE parent in the system, which
            # ``/iserver/secdef/info`` 500s on — burning IBKR's rate
            # budget for nothing. The keyword set matches the binary
            # FORECASTX inventory (HORC, SENM, presidential winner,
            # gubernatorial winners) where strikes 1.0/2.0 are the
            # canonical YES/NO encodings.
            sd = await self.get_trsrv_secdef(parent_conid)
            parent_name = (
                str(sd.get("name") or "").upper() if isinstance(sd, dict) else ""
            )
            if any(kw in parent_name for kw in (
                "HOUSE", "SENATE", "CONTROL", "MAJORITY",
                "PRESIDENT", "ELECTION", "GOVERNOR",
            )):
                # Election-bearing months only — the political
                # FORECASTX inventory matures around Jan after a Nov
                # election, so NOV<YY> is the IBKR ``month`` string for
                # essentially every binary control market. Three months
                # of fallback budget keeps the worst-case per-parent
                # cost at 6 calls when nothing matches.
                for month in ("NOV26", "NOV27", "NOV28"):
                    month_hits = 0
                    for fallback_strike in (1.0, 2.0):
                        try:
                            info_payload = await self._request(
                                "GET", "/iserver/secdef/info",
                                params={
                                    "conid": str(parent_conid),
                                    "exchange": "FORECASTX",
                                    "sectype": "OPT",
                                    "month": month,
                                    "strike": str(fallback_strike),
                                },
                                use_circuit=False,
                            )
                        except Exception:
                            continue
                        items = (
                            info_payload.get("items")
                            if isinstance(info_payload, dict) else info_payload
                        )
                        if not isinstance(items, list):
                            continue
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            conid = str(item.get("conid") or "").strip()
                            if not conid:
                                continue
                            children.append({
                                "conid": conid,
                                "right": str(item.get("right") or "").upper(),
                                "strike": item.get("strike"),
                                "maturity_date": item.get("maturityDate"),
                                "desc": item.get("desc2") or item.get("desc1") or "",
                                "source": f"secdef/info-fallback:OPT:{month}:strike={fallback_strike}",
                            })
                            month_hits += 1
                    if month_hits:
                        # First month that yielded children wins —
                        # don't fan out across other months once we
                        # have a complete YES/NO strike pair.
                        break
            return children

        # Step 2: for each unique strike value (Call ∪ Put), call
        # /iserver/secdef/info to retrieve the contract pair. This is
        # the only endpoint that returns the actual child conid.
        unique_strikes: list[float] = sorted(set(calls) | set(puts))
        for strike in unique_strikes:
            try:
                info_payload = await self._request(
                    "GET", "/iserver/secdef/info",
                    params={
                        "conid": str(parent_conid),
                        "exchange": "FORECASTX",
                        "sectype": "OPT",
                        "month": hit_month,
                        "strike": str(strike),
                    },
                    use_circuit=False,
                )
            except Exception:
                continue
            items = (
                info_payload.get("items")
                if isinstance(info_payload, dict) else info_payload
            )
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                conid = str(item.get("conid") or "").strip()
                if not conid:
                    continue
                children.append({
                    "conid": conid,
                    "right": str(item.get("right") or "").upper(),
                    "strike": item.get("strike"),
                    "maturity_date": item.get("maturityDate"),
                    "desc": item.get("desc2") or item.get("desc1") or "",
                    "source": f"secdef/info:OPT:{hit_month}:strike={strike}",
                })

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
        """LMT order placement (BUY or SELL).

        ForecastEx YES/NO contracts can be bought OR sold against the same
        conid — selling a YES position you hold long closes the exposure
        directly without buying the opposite leg. The adapter calls this
        with ``side="SELL"`` from the smart-unwind path to close naked legs.

        Handles IBKR's confirmation-message reply chain transparently: if
        the gateway returns ``[{"id": "<reply-id>", "message": [...]}]``
        instead of an order ack, we POST ``{"confirmed": true}`` to
        ``/iserver/reply/<reply-id>`` (up to 5 chained prompts) until the
        gateway emits the actual order record.
        """
        if not self.account_id:
            raise RuntimeError("IBKR_ACCOUNT_ID is not configured")
        normalized_side = str(side).upper()
        if normalized_side not in ("BUY", "SELL"):
            raise ValueError(
                f"forecastex.place_order rejects side={side!r}: must be BUY or SELL"
            )
        body = {
            "orders": [
                {
                    "conid": int(conid) if str(conid).isdigit() else conid,
                    "orderType": order_type,
                    "side": normalized_side,
                    "quantity": int(quantity),
                    "price": float(price),
                    "tif": tif,
                }
            ]
        }
        response = await self._request(
            "POST", f"/iserver/account/{self.account_id}/orders", json_body=body,
        )
        return await self._answer_reply_chain(response)

    async def _answer_reply_chain(self, response: dict, *, max_steps: int = 5) -> dict:
        """Walk through any IBKR confirmation prompts attached to ``response``.

        IBKR Client Portal returns one of two shapes on POST /orders:
          1. Order ack: ``[{"order_id": ..., "order_status": ...}]``
          2. Confirmation prompt: ``[{"id": "<uuid>", "message": [...]}]``

        ``_request`` already wraps top-level lists as ``{"items": [...]}``.
        We unwrap, detect the prompt shape, and reply ``{"confirmed": true}``
        to ``/iserver/reply/<id>``. The gateway may chain multiple prompts
        (e.g. price warning then size warning); we follow up to ``max_steps``
        before giving up to avoid an infinite loop on a broken gateway.
        """
        current = response
        for _ in range(max_steps):
            items = current.get("items") if isinstance(current, dict) else None
            entries = items if isinstance(items, list) else (
                [current] if isinstance(current, dict) else []
            )
            if not entries:
                return current
            first = entries[0] if isinstance(entries[0], dict) else {}
            reply_id = first.get("id") or first.get("messageId")
            message = first.get("message")
            # Confirmation prompts carry an ``id`` AND a ``message`` list;
            # order acks carry an ``order_id`` / ``orderId``. Tell them apart
            # by the absence of an order id.
            has_order_id = any(
                isinstance(e, dict) and (e.get("order_id") or e.get("orderId") or e.get("orderStatus") or e.get("order_status"))
                for e in entries
            )
            if has_order_id or not reply_id or not message:
                return current
            logger.info(
                "forecastex.reply.confirm id=%s message=%s",
                reply_id, str(message)[:200],
            )
            current = await self._request(
                "POST", f"/iserver/reply/{reply_id}",
                json_body={"confirmed": True},
            )
        logger.warning(
            "forecastex: confirmation reply chain exceeded %s steps — returning last response",
            max_steps,
        )
        return current

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
        # canonical_id -> (yes_conid, no_conid_or_empty). Empty string for
        # the NO half means the mapping hasn't supplied one explicitly; the
        # collector then attempts runtime sibling discovery via
        # ``_attempt_no_conid_discovery``.
        self._conid_map: dict[str, tuple[str, str]] = {}
        self._inactive_conids: set[str] = set()
        # Track how many times each conid returned a non-tradeable snapshot
        # so we can disable untradeable parent-event conids after a few probes.
        self._parent_probe_counts: dict[str, int] = {}
        # Runtime NO-conid discovery cache: yes_conid -> no_conid. Populated
        # by ``_attempt_no_conid_discovery`` when the mapping carries only
        # a YES conid (legacy rows / mappings predating forecastex_no).
        # Persists for the collector's lifetime so we don't burn IBKR
        # rate-limit budget re-discovering the same sibling every poll.
        self._no_discovery_cache: dict[str, str] = {}
        # Negative cache — YES conids whose NO sibling cannot be resolved
        # (parent IND missing strike children, contract_info empty, etc.).
        # Keeps the discovery path bounded so we don't issue contract_info
        # + secdef/strikes calls every cycle for a parent that will never
        # yield a NO child.
        self._no_discovery_failed: set[str] = set()
        # YES conids attached to multiple canonical markets are ambiguous and
        # unsafe. Skip them entirely until discovery/operator mapping assigns
        # unique ForecastX child conids.
        self._duplicate_conids: dict[str, list[str]] = {}
        # First-cycle discovery throttle: when the container starts cold,
        # every tracked YES conid hits ``_attempt_no_conid_discovery`` on
        # the SAME fetch_markets call before negative caches build up. With
        # ~20 conids × ~3 IBKR calls each (contract_info + a few
        # secdef/strikes months), the fan-out exceeds IBKR's 10 rps budget
        # and trips the circuit breaker (~30s outage) on every restart —
        # see CV-01 diagnosis 2026-05-28. Throttle the first cycle by
        # sleeping 200ms between discovery calls so the burst drops below
        # the rate limit. After the first cycle completes, the negative
        # cache covers all conids that won't yield a sibling, so subsequent
        # cycles don't repeat the storm and the throttle is dropped.
        self._first_discovery_cycle: bool = True
        self._first_cycle_discovery_throttle_s: float = 0.2
        # Optional callback the supervisor wires to the child-conid
        # resolver's ``trigger()``. Invoked the instant we move a conid
        # into ``_inactive_conids`` so the resolver runs against the
        # fresh candidate set within seconds instead of waiting for its
        # next periodic tick (30 min default).
        self._on_conid_disabled: Optional[Callable[[str], None]] = None
        self.refresh_tracked_markets()

    def set_disable_callback(self, cb: Optional[Callable[[str], None]]) -> None:
        """Register a callback fired with the conid whenever the
        collector moves a conid into the inactive set. Pass ``None``
        to clear. The callback runs synchronously inside the fetch
        loop, so it MUST be cheap and non-blocking — typically an
        asyncio.Event.set() forwarder."""
        self._on_conid_disabled = cb

    def _mark_inactive(self, conid: str) -> None:
        """Add ``conid`` to the inactive set and fire the disable
        callback (if any). Centralized so both the probe-budget
        disable and the 404/410 disable paths notify the resolver
        consistently."""
        self._inactive_conids.add(conid)
        cb = self._on_conid_disabled
        if cb is not None:
            try:
                cb(conid)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "ForecastEx disable callback failed for %s: %s",
                    conid, exc,
                )

    def refresh_tracked_markets(self) -> None:
        """Reload tracked ForecastEx contract ids from MARKET_MAP.

        Each mapping carries TWO conids: ``forecastex`` (YES / right=C) and
        ``forecastex_no`` (NO / right=P). The NO half is optional in the
        mapping payload — when missing, the collector will runtime-discover
        the sibling on the first poll via ``_attempt_no_conid_discovery``
        and cache the result for the collector's lifetime.

        Also clears the inactive-conid set and runtime NO-discovery caches
        of any conid that is NO LONGER referenced by any mapping — when the
        auto-resolver (or operator manual override) attaches a different
        child conid, the old parent disappears from MARKET_MAP and the new
        child must get a clean probe budget. Without this, the collector
        carries stale state forever across restarts of the source mapping.
        """
        old_duplicate_conids = dict(self._duplicate_conids)
        candidates: list[tuple[str, str, str]] = []
        owners_by_yes_conid: dict[str, list[str]] = {}
        for canonical_id, mapping in MARKET_MAP.items():
            if bool(mapping.get("forecastex_not_available")):
                continue
            yes_conid = str(mapping.get("forecastex", "") or "").strip()
            if not yes_conid:
                continue
            no_conid = str(mapping.get("forecastex_no", "") or "").strip()
            candidates.append((canonical_id, yes_conid, no_conid))
            owners_by_yes_conid.setdefault(yes_conid, []).append(canonical_id)
        self._duplicate_conids = {
            conid: sorted(owners)
            for conid, owners in owners_by_yes_conid.items()
            if len(owners) > 1
        }
        should_log_duplicates = self._duplicate_conids != old_duplicate_conids
        new_map: dict[str, tuple[str, str]] = {}
        for canonical_id, yes_conid, no_conid in candidates:
            owners = self._duplicate_conids.get(yes_conid)
            if owners:
                if should_log_duplicates:
                    logger.warning(
                        "ForecastEx: skipping duplicate YES conid %s mapped to %s",
                        yes_conid,
                        ",".join(owners),
                    )
                continue
            new_map[canonical_id] = (yes_conid, no_conid)
        if new_map != self._conid_map:
            logger.info(
                "ForecastEx: tracking %d markets (was %d)",
                len(new_map), len(self._conid_map),
            )
        # Drop inactivity / probe-count state for conids that have been
        # replaced or removed by upstream. Conids that are still tracked
        # keep their state (so re-promoted parents don't get re-probed).
        referenced: set[str] = set()
        for yes, no in new_map.values():
            referenced.add(yes)
            if no:
                referenced.add(no)
        stale_inactive = self._inactive_conids - referenced
        for stale in stale_inactive:
            self._inactive_conids.discard(stale)
            self._parent_probe_counts.pop(stale, None)
        # Drop NO-discovery state for YES conids no longer tracked.
        tracked_yes = {yes for yes, _ in new_map.values()}
        for stale_yes in list(self._no_discovery_cache):
            if stale_yes not in tracked_yes:
                self._no_discovery_cache.pop(stale_yes, None)
        for stale_yes in list(self._no_discovery_failed):
            if stale_yes not in tracked_yes:
                self._no_discovery_failed.discard(stale_yes)
        self._conid_map = new_map

    def reactivate_conid(self, conid: str) -> None:
        """Remove a specific conid from the inactive set so the next
        fetch cycle re-probes it. Called by the auto-resolver after a
        child-conid attach succeeds, AND by the manual-override API
        endpoint. Idempotent.

        Also drops the conid from the NO-discovery negative cache so a
        previously-failed sibling lookup gets re-tried (in case IBKR's
        secdef endpoints were temporarily down during the prior attempt).
        """
        conid_str = str(conid)
        self._inactive_conids.discard(conid_str)
        self._parent_probe_counts.pop(conid_str, None)
        self._no_discovery_failed.discard(conid_str)

    async def _attempt_no_conid_discovery(self, yes_conid: str) -> Optional[str]:
        """Runtime sibling lookup: given a YES conid, find the NO conid
        under the same parent at the same strike.

        Path: ``/iserver/contract/{yes_conid}/info`` returns the YES child's
        strike and the parent (``underlying_con_id``); ``resolve_event_children``
        on the parent enumerates ALL siblings; we pick the one with
        right=P and matching strike. Cached for the collector's lifetime
        (positive: ``_no_discovery_cache``; negative: ``_no_discovery_failed``).

        Returns the NO conid string on success, ``None`` when the sibling
        cannot be resolved. Callers MUST treat ``None`` as "NO-side
        orders disabled for this market" — never fall back to the YES
        conid (the root cause of the ARB-000695/699 phantom trades).
        """
        if yes_conid in self._no_discovery_cache:
            return self._no_discovery_cache[yes_conid]
        if yes_conid in self._no_discovery_failed:
            return None
        try:
            info = await self.client.get_contract_info(yes_conid)
        except Exception as exc:
            logger.debug(
                "forecastex: no-conid discovery contract_info(%s) failed: %s",
                yes_conid, exc,
            )
            return None
        if not isinstance(info, dict) or not info:
            self._no_discovery_failed.add(yes_conid)
            return None
        # IBKR Client Portal field-name variants observed across gateway
        # versions: underlying_con_id (snake), underlyingConid (camel),
        # undConid (legacy).
        parent = (
            info.get("underlying_con_id")
            or info.get("underlyingConid")
            or info.get("undConid")
            or info.get("underconid")
        )
        strike = info.get("strike")
        right = str(info.get("right") or "").upper()
        if not parent or strike is None or right not in ("C", "CALL"):
            # YES conid metadata missing the data we need to find the
            # sibling — either it's a parent IND itself (no right field)
            # or the IBKR field naming changed. Fail-closed so we don't
            # paper over a malformed contract by inventing a NO conid.
            self._no_discovery_failed.add(yes_conid)
            logger.warning(
                "forecastex: NO-conid discovery skipped for YES %s — "
                "contract_info lacks parent/strike/right=C "
                "(parent=%s strike=%s right=%s)",
                yes_conid, parent, strike, right,
            )
            return None
        try:
            children = await self.client.resolve_event_children(str(parent))
        except Exception as exc:
            logger.debug(
                "forecastex: no-conid discovery resolve_event_children(%s) "
                "failed for YES %s: %s",
                parent, yes_conid, exc,
            )
            # Transient IBKR endpoint failure — do NOT mark failed so the
            # next cycle can retry.
            return None
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            strike_f = None
        chosen_no: Optional[str] = None
        for child in children:
            if not isinstance(child, dict):
                continue
            if str(child.get("right", "")).upper() not in ("P", "PUT"):
                continue
            if strike_f is not None:
                try:
                    child_strike = float(child.get("strike") or 0.0)
                except (TypeError, ValueError):
                    continue
                if abs(child_strike - strike_f) > 1e-6:
                    continue
            no_candidate = str(child.get("conid") or "").strip()
            if no_candidate and no_candidate != yes_conid:
                chosen_no = no_candidate
                break
        if chosen_no:
            self._no_discovery_cache[yes_conid] = chosen_no
            logger.info(
                "forecastex: discovered NO conid %s for YES %s "
                "(parent=%s strike=%s)",
                chosen_no, yes_conid, parent, strike,
            )
            return chosen_no
        self._no_discovery_failed.add(yes_conid)
        logger.warning(
            "forecastex: NO sibling not found for YES %s under parent %s "
            "at strike %s — cross-side NO orders disabled for this conid",
            yes_conid, parent, strike,
        )
        return None

    @staticmethod
    def _to_dollars(value: float) -> float:
        """Normalize an IBKR-quoted price to the [0, 1] dollar range.

        ForecastEx prices come over the wire in cents (0–100). Values
        already in dollars (0–1) pass through unchanged.
        """
        if value <= 0:
            return 0.0
        if value > 1.0:
            return max(0.0, min(value / 100.0, 1.0))
        return max(0.0, min(value, 1.0))

    def _build_price_point(
        self,
        canonical_id: str,
        yes_conid: str,
        yes_snapshot: dict,
        no_conid: Optional[str],
        no_snapshot: Optional[dict],
    ) -> Optional[PricePoint]:
        """Build a PricePoint from a YES/NO snapshot pair.

        ForecastEx binary contracts have SEPARATE IBKR conids for YES
        (right=C) and NO (right=P) — buying NO requires placing a BUY
        on the NO conid at its own ask, NOT on the YES conid at
        ``1 - yes_bid``. The historical implementation synthesized the
        NO side from ``1 - yes_bid`` AND set ``no_market_id=yes_conid``,
        which (a) fabricated an edge that didn't exist on the real NO
        order book and (b) caused the executor to silently buy YES when
        the engine asked for NO. Postmortem: ARB-000695/699 phantom
        trades on 2026-05-27.

        Returns ``None`` when YES has no executable ask/bid. When NO has
        no snapshot data (sibling discovery failed, conid is dead),
        sets ``no_market_id=""`` and ``no_ask=no_price=0`` so the
        scanner's dead-ask gate skips any cross-side opportunity. We
        NEVER fall back to the YES conid for the NO market id — that
        is exactly the bug being fixed.
        """
        yes_bid_raw = _amount_value(yes_snapshot.get("84"))
        yes_ask_raw = _amount_value(yes_snapshot.get("86"))
        yes_bid_size = _amount_value(yes_snapshot.get("7295"))
        yes_ask_size = _amount_value(yes_snapshot.get("7296"))

        yes_bid = self._to_dollars(yes_bid_raw)
        yes_ask = self._to_dollars(yes_ask_raw)
        # SIGNAL-INTEGRITY (2026-05-27): never let a last-trade print
        # synthesize an executable yes_price. ForecastEx binaries can
        # report a stale `31` (last) on a market whose live book has
        # gone empty. Using last-trade as a mark fed a phantom $0.44
        # into ARB-000695/699 against an otherwise-dead order book.
        yes_price = yes_ask or yes_bid
        if yes_price <= 0:
            return None

        # ── REAL NO snapshot (not synthetic) ─────────────────────────
        # Only populate NO bid/ask when we have a SEPARATE NO snapshot
        # from the NO conid. If no_snapshot is empty or missing, leave
        # NO side at zero so downstream code treats this as a YES-only
        # quote and refuses cross-side opportunities.
        no_bid = 0.0
        no_ask = 0.0
        no_bid_size = 0.0
        no_ask_size = 0.0
        effective_no_conid = ""
        if no_conid and no_snapshot:
            no_bid = self._to_dollars(_amount_value(no_snapshot.get("84")))
            no_ask = self._to_dollars(_amount_value(no_snapshot.get("86")))
            no_bid_size = _amount_value(no_snapshot.get("7295"))
            no_ask_size = _amount_value(no_snapshot.get("7296"))
            # Only commit the NO market id if the snapshot actually
            # produced a real bid OR ask. An empty NO snapshot leaves
            # market_id="" so the scanner skips the cross-side opp
            # rather than letting the executor route to a dead book.
            if no_bid > 0 or no_ask > 0:
                effective_no_conid = str(no_conid)
        no_price = no_ask or no_bid  # executable NO price; 0 when no data

        mapping = MARKET_MAP.get(canonical_id, {})
        return PricePoint(
            platform="forecastex",
            canonical_id=canonical_id,
            yes_price=yes_price,
            no_price=no_price,
            yes_volume=yes_ask_size or yes_bid_size,
            # NO volume is from the NO snapshot when present, otherwise 0.
            no_volume=no_ask_size or no_bid_size,
            timestamp=time.time(),
            # raw_market_id stays as the YES conid for backward compat
            # with downstream consumers (audit, dashboards) that expect
            # a single "primary" id per quote.
            raw_market_id=yes_conid,
            yes_market_id=yes_conid,
            no_market_id=effective_no_conid,
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
                "yes_conid": yes_conid,
                "no_conid": effective_no_conid,
                "yes_snapshot": {
                    k: v for k, v in yes_snapshot.items()
                    if k in ("31", "84", "86", "7295", "7296")
                },
                "no_snapshot": (
                    {
                        k: v for k, v in (no_snapshot or {}).items()
                        if k in ("31", "84", "86", "7295", "7296")
                    }
                    if no_snapshot else {}
                ),
            },
        )

    # Number of consecutive non-tradeable probes before disabling a conid.
    # IBKR's /iserver/marketdata/snapshot endpoint warms its field cache
    # lazily — the first 1–3 calls for a freshly-subscribed child conid
    # often return only metadata keys (conid/conidEx/_updated) before
    # bid/ask field codes appear. The original threshold of 3 was tight
    # enough to disable real tradeable children whenever the collector
    # happened to first probe them during a cold IBKR session (verified
    # 2026-05-25: 762089343 DEM_HOUSE and 773659700 GOP_HOUSE both
    # disabled despite IBKR returning live 84/86 fields seconds later).
    # 8 probes at the default 1.5s poll cadence gives ~12s of warmup
    # budget per conid before disabling — still small enough to drop
    # genuine parent (IND) conids within 15s of startup but wide enough
    # to absorb IBKR's cold-cache latency.
    _PROBE_DISABLE_THRESHOLD = 8

    async def _snapshot_with_warmup(self, conid: str) -> dict:
        """Single snapshot with a one-shot warmup retry.

        IBKR returns metadata-only on the FIRST call to a conid whose
        field cache hasn't been populated yet (no 31/84/86 keys). A
        short sleep + second call usually returns the bid/ask. We do
        this defensively rather than counting the first empty response
        toward the probe-disable budget, which was disabling real
        tradeable children during cold IBKR sessions.
        """
        snapshot = await self.client.market_snapshot(conid)
        if snapshot and not ForecastExClient.is_tradeable_snapshot(snapshot):
            # Only retry once, only when first attempt produced an empty
            # data-field set — avoids doubling rate-limit cost on the
            # steady-state path where most conids are warm.
            await asyncio.sleep(1.0)
            snapshot = await self.client.market_snapshot(conid)
        return snapshot

    async def _fetch_side_snapshot(
        self, yes_conid: str, no_conid: str,
    ) -> Optional[dict]:
        """Fetch a NO-side snapshot, handling 404/410 by purging caches.

        Returns the snapshot dict on success, ``None`` on any failure or
        when the NO conid is in the inactive set. NO-side errors are
        soft: we strip the NO snapshot rather than fail the whole
        ``fetch_markets`` cycle, so an isolated dead NO conid degrades
        gracefully to YES-only quotes.
        """
        if not no_conid or no_conid in self._inactive_conids:
            return None
        try:
            no_snapshot = await self._snapshot_with_warmup(no_conid)
        except aiohttp.ClientResponseError as exc:
            if exc.status in (404, 410):
                self._mark_inactive(no_conid)
                self._no_discovery_failed.add(yes_conid)
                self._no_discovery_cache.pop(yes_conid, None)
                logger.warning(
                    "ForecastEx NO conid %s for YES %s returned %s, disabling",
                    no_conid, yes_conid, exc.status,
                )
                return None
            logger.warning(
                "ForecastEx NO conid %s fetch failed (YES %s): %s",
                no_conid, yes_conid, exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "ForecastEx NO conid %s fetch error (YES %s): %s",
                no_conid, yes_conid, exc,
            )
            return None
        if no_snapshot and not ForecastExClient.is_tradeable_snapshot(no_snapshot):
            # NO snapshot returned only metadata / empty fields. Don't
            # fail the cycle — emit a YES-only quote. The scanner's
            # dead-ask gate will skip any cross-side opportunity, and
            # the executor will never be handed an unfillable NO conid.
            return None
        return no_snapshot

    async def fetch_markets(self) -> list[PricePoint]:
        self.refresh_tracked_markets()
        results: list[PricePoint] = []

        for canonical_id, (yes_conid, mapped_no_conid) in self._conid_map.items():
            if yes_conid in self._inactive_conids:
                continue
            self.total_fetches += 1
            try:
                yes_snapshot = await self._snapshot_with_warmup(yes_conid)
                # A successful HTTP response means the IBKR bridge is healthy
                # — reset the consecutive_errors counter now, BEFORE the
                # tradeable check. An empty bid/ask snapshot is a market-data
                # issue (parent-event conid, illiquid contract), not a
                # connectivity issue, and shouldn't keep the readiness gate
                # tripped after the auth outage clears.
                self.consecutive_errors = 0
                # FORECASTX event-parent (IND) conids return only conidEx +
                # symbol + empty bid/ask. Polling them every cycle wastes
                # rate-limit budget and pollutes "price_point is None" logs
                # forever. Mark them inactive and let the operator (or the
                # YES/NO resolver, when IBKR's FORECASTX endpoints are
                # available) attach the child conid instead.
                if yes_snapshot and not ForecastExClient.is_tradeable_snapshot(yes_snapshot):
                    self._parent_probe_counts[yes_conid] = (
                        self._parent_probe_counts.get(yes_conid, 0) + 1
                    )
                    if self._parent_probe_counts[yes_conid] >= self._PROBE_DISABLE_THRESHOLD:
                        self._mark_inactive(yes_conid)
                        logger.warning(
                            "ForecastEx YES conid %s returns no bid/ask after %d "
                            "probes — looks like an untradeable parent event "
                            "conid. Disabling. Attach a child YES/NO conid via "
                            "ops UI to enable.",
                            yes_conid, self._parent_probe_counts[yes_conid],
                        )
                    continue
                # Tradeable snapshot — clear any prior warmup probe count so
                # a future blip doesn't accumulate toward the disable budget.
                self._parent_probe_counts.pop(yes_conid, None)

                # Resolve the NO conid: explicit mapping → runtime cache
                # → on-demand sibling discovery. Falls back to None when
                # discovery can't find a Put sibling — _build_price_point
                # then emits a YES-only quote.
                no_conid: Optional[str] = mapped_no_conid or None
                if not no_conid:
                    # First-cycle throttle (see __post_init__ comment).
                    # Only sleep when we're actually about to do
                    # network-bound discovery (cache miss + not in
                    # negative cache); cached lookups are free.
                    if (
                        self._first_discovery_cycle
                        and yes_conid not in self._no_discovery_cache
                        and yes_conid not in self._no_discovery_failed
                    ):
                        await asyncio.sleep(
                            self._first_cycle_discovery_throttle_s
                        )
                    no_conid = await self._attempt_no_conid_discovery(yes_conid)

                no_snapshot = (
                    await self._fetch_side_snapshot(yes_conid, no_conid)
                    if no_conid else None
                )

                price = self._build_price_point(
                    canonical_id,
                    yes_conid,
                    yes_snapshot or {},
                    no_conid if no_snapshot else None,
                    no_snapshot,
                )
                if price is None:
                    continue
                results.append(price)
                await self.store.put(price)
            except aiohttp.ClientResponseError as exc:
                if exc.status in (404, 410, 500):
                    self._mark_inactive(yes_conid)
                    logger.warning(
                        "ForecastEx YES conid %s returned %s, disabling",
                        yes_conid, exc.status,
                    )
                    continue
                self.total_errors += 1
                self.consecutive_errors += 1
                logger.error("ForecastEx fetch failed for %s: %s", yes_conid, exc)
            except Exception as exc:
                self.total_errors += 1
                self.consecutive_errors += 1
                logger.error("ForecastEx fetch failed for %s: %s", yes_conid, exc)

        # First cycle complete: every YES conid has been probed for a NO
        # sibling exactly once (cached either positive or negative), so
        # subsequent cycles will hit the cache and won't generate IBKR
        # discovery traffic. Drop the throttle.
        if self._first_discovery_cycle:
            self._first_discovery_cycle = False
            logger.info(
                "ForecastEx first-cycle discovery complete "
                "(cached=%d, failed=%d) — throttle dropped",
                len(self._no_discovery_cache),
                len(self._no_discovery_failed),
            )

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
