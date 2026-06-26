"""
ForecastEx TWS (socket) client — drop-in replacement for ``ForecastExClient``.

Background
----------
The REST client in ``forecastex.py`` talks to the IBKR *Client Portal Web
API* gateway (``https://localhost:5000/v1/api``). That gateway owns an SSO
session that expires every few hours, which kills the ForecastEx price feed
and trade execution and trips the ``forecastex-rest`` circuit breaker until
an operator re-authenticates by hand.

This module replaces that with a *persistent socket* connection to **IB
Gateway** (the headless TWS API endpoint, default port 4001) driven by
``ib_async``. IB Gateway + IBC keeps itself logged in (with a nightly
``AUTO_RESTART_TIME`` re-login) so the socket session does not silently
expire mid-session the way the Client Portal SSO does.

Contract: identical public surface
----------------------------------
``ForecastExTWSClient`` exposes the **exact same async method signatures** as
``ForecastExClient`` so it is a true drop-in for ``ForecastExCollector``,
``ForecastExAdapter``, the child-conid resolver, the discovery pass, and the
cross-platform matcher. Crucially, every method returns data in the SAME
shape those consumers already parse:

  * ``market_snapshot()`` returns a dict keyed by IBKR *field codes* —
    ``{"31": last, "84": bid, "86": ask, "7295": bid_size, "7296": ask_size}``
    — so ``ForecastExClient.is_tradeable_snapshot`` and
    ``ForecastExCollector._build_price_point`` work unchanged.
  * ``place_order()`` / ``get_order()`` / ``list_live_orders()`` return
    REST-style dicts (``order_id`` / ``order_status`` / ``filled_quantity`` /
    ``avg_price`` / ``conid`` / ``side``) that ``ForecastExAdapter`` already
    walks.
  * ``resolve_event_children()`` returns ``{conid, right, strike,
    maturity_date, desc, source}`` dicts identical to the REST path.

ForecastEx contract model (per IBKR docs + REST implementation)
---------------------------------------------------------------
ForecastEx event contracts are modelled as **OPTIONS**:
``secType="OPT"``, ``exchange="FORECASTX"``. The YES side is the **Call**
(``right="C"``) and the NO side is the **Put** (``right="P"``) at the same
strike under a shared event parent.

Price units
-----------
ForecastEx contracts are quoted in **dollars in [0, 1]** (probability;
each contract settles to $1). This matches what the REST gateway sent for
order prices and what the collector's ``_to_dollars`` normaliser expects
(values already in [0, 1] pass through; values > 1 are treated as cents and
divided by 100). We therefore pass limit prices straight through and store
raw bid/ask numbers without rescaling — the downstream normaliser owns unit
handling, exactly as it does for the REST path.

Circuit breaker
---------------
Instead of HTTP status codes, the breaker is driven by **connection state**
(``ib.isConnected()``): a failed connect / a dropped socket records a
failure; a successful operation records a success. The breaker keeps the
same name/threshold semantics so the collector's readiness wiring is
unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from arbiter.utils.retry import CircuitBreaker, RateLimiter

logger = logging.getLogger("arbiter.collector.forecastex_tws")

# Guarded import — the module must import cleanly even where ``ib_async`` is
# not installed (CI / dev boxes that never touch the TWS path). The hard
# failure is deferred to first *use* with an actionable message.
try:  # pragma: no cover - import guard
    from ib_async import IB, Contract, Order  # type: ignore
    _IB_IMPORT_ERROR: Optional[BaseException] = None
except Exception as _exc:  # noqa: BLE001 - any import failure defers to use
    IB = None  # type: ignore
    Contract = None  # type: ignore
    Order = None  # type: ignore
    _IB_IMPORT_ERROR = _exc


# Terminal IBKR order states (ib_async ``OrderStatus.status`` strings).
_DONE_STATES = frozenset(
    {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
)
# States that mean "the broker accepted the order and it is live/resting".
_LIVE_STATES = frozenset(
    {"PreSubmitted", "Submitted", "PendingSubmit"}
)


def _num(value: Any) -> float:
    """Coerce an ib_async numeric (which uses NaN for 'unset') to a float.

    Returns 0.0 for None / NaN / non-numeric so the resulting field-code
    dict matches the REST snapshot convention (empty fields read as 0).
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f):
        return 0.0
    return f


def _normalize_status(status: str) -> str:
    """Map ib_async order statuses onto the wire strings ForecastExAdapter
    already recognises in ``_map_status``.

    ib_async emits ``ApiCancelled`` / ``PendingCancel`` which the adapter's
    normaliser does not match (and would silently downgrade to FAILED). We
    fold them onto ``Cancelled`` so an IOC/FOK that returned unfilled is
    correctly treated as a cancel, not a hard failure.
    """
    s = (status or "").strip()
    if s in ("ApiCancelled", "PendingCancel"):
        return "Cancelled"
    if s == "PendingSubmit":
        return "Submitted"
    return s


@dataclass
class ForecastExTWSClient:
    """Async ForecastEx client backed by IB Gateway over the TWS socket.

    Parameters mirror what ``build_forecastex_collector`` can supply from
    ``IBGatewayConfig``. ``account_id`` / ``circuit`` / ``live_rate_limiter``
    are exposed as attributes because ``ForecastExCollector``,
    ``ForecastExAdapter`` and ``stranded_reconciler`` read them off the
    client directly.
    """

    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 11
    account_id: str = ""
    paper_trading: bool = True
    connect_timeout: float = 30.0

    # Public attributes other modules read off the client (parity with the
    # REST client). ``gateway_url`` is a human-readable descriptor only.
    gateway_url: str = field(default="", repr=False)

    circuit: CircuitBreaker = field(init=False, repr=False)
    live_rate_limiter: RateLimiter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.gateway_url:
            self.gateway_url = f"tws://{self.host}:{self.port}"
        # Same env-tunable thresholds as the REST client so operators can
        # reuse the FORECASTEX_CB_* knobs they already understand. Driven by
        # connection state rather than HTTP status (see module docstring).
        cb_threshold = max(
            1, int(os.getenv("FORECASTEX_CB_FAILURE_THRESHOLD", "10"))
        )
        cb_timeout = max(
            1, int(os.getenv("FORECASTEX_CB_RECOVERY_TIMEOUT_S", "120"))
        )
        self.circuit = CircuitBreaker(
            "forecastex-tws",
            failure_threshold=cb_threshold,
            recovery_timeout=cb_timeout,
        )
        # IBKR's socket API pacing limit is ~50 msg/s; 10rps keeps us well
        # clear and matches the REST client's steady-state budget. Kept for
        # attribute parity (collector/adapter read ``live_rate_limiter``).
        self.live_rate_limiter = RateLimiter(
            "forecastex-tws-live", max_requests=10, window_seconds=1.0,
        )
        self._ib: Any = None
        self._connect_lock = asyncio.Lock()
        # conid -> qualified ib_async Contract (avoid re-qualifying each poll).
        self._contract_cache: dict[str, Any] = {}
        # conid -> live ib_async Ticker (persistent market-data subscription).
        self._tickers: dict[str, Any] = {}

    # ── Connection management ──────────────────────────────────────

    async def _ensure_connected(self) -> Any:
        """Return a live, connected ``IB`` instance, (re)connecting if needed.

        Gated by the circuit breaker: when OPEN we refuse rather than hammer
        a dead gateway. A failed connect records a breaker failure; a
        successful connect records a success.
        """
        if _IB_IMPORT_ERROR is not None:
            raise RuntimeError(
                "ib_async is not installed — cannot use the ForecastEx TWS "
                "client. Add `ib_async` to the environment (it is pinned in "
                f"requirements.txt). Original import error: {_IB_IMPORT_ERROR}"
            )
        if self._ib is not None and self._ib.isConnected():
            return self._ib

        async with self._connect_lock:
            # Re-check under the lock — another coroutine may have connected.
            if self._ib is not None and self._ib.isConnected():
                return self._ib
            if not self.circuit.can_execute():
                raise RuntimeError(
                    f"Circuit [{self.circuit.name}] is OPEN — TWS connect rejected"
                )
            ib = self._ib if self._ib is not None else IB()
            # A stale half-open socket must be torn down before reconnecting.
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
            try:
                await ib.connectAsync(
                    self.host,
                    self.port,
                    clientId=self.client_id,
                    timeout=self.connect_timeout,
                    readonly=False,
                    account=self.account_id or "",
                )
            except Exception as exc:
                self.circuit.record_failure()
                raise RuntimeError(
                    f"forecastex TWS connect failed "
                    f"({self.host}:{self.port} clientId={self.client_id}): {exc}"
                ) from exc
            self._ib = ib
            # A fresh connection invalidates any cached subscriptions.
            self._tickers.clear()
            self.circuit.record_success()
            logger.info(
                "forecastex TWS connected (%s:%s clientId=%s paper=%s)",
                self.host, self.port, self.client_id, self.paper_trading,
            )
            return ib

    def _on_op_exception(self, name: str, exc: Exception) -> None:
        """Record a breaker failure when an op failed due to a dropped socket."""
        ib = self._ib
        if ib is None or not ib.isConnected():
            self.circuit.record_failure()
        logger.error("forecastex TWS %s failed: %s", name, exc)

    # ── Contract resolution ────────────────────────────────────────

    async def _contract_for_conid(self, conid: str) -> Optional[Any]:
        """Return a qualified ib_async ``Contract`` for ``conid`` (cached).

        ForecastEx market data needs a fully-qualified contract (conId +
        exchange). We qualify by conId; if the exchange comes back empty we
        pin it to FORECASTX.
        """
        key = str(conid)
        cached = self._contract_cache.get(key)
        if cached is not None:
            return cached
        ib = await self._ensure_connected()
        try:
            iconid = int(conid)
        except (TypeError, ValueError):
            logger.warning("forecastex TWS: non-numeric conid %r", conid)
            return None
        contract = Contract(conId=iconid, exchange="FORECASTX")
        qualified = await ib.qualifyContractsAsync(contract)
        result = qualified[0] if qualified else None
        if result is None:
            # Retry without forcing the exchange — let IBKR resolve it.
            contract = Contract(conId=iconid)
            qualified = await ib.qualifyContractsAsync(contract)
            result = qualified[0] if qualified else None
        if result is not None:
            if not getattr(result, "exchange", ""):
                result.exchange = "FORECASTX"
            self._contract_cache[key] = result
        return result

    # ── Account / portfolio ────────────────────────────────────────

    async def accounts(self) -> dict:
        """Parity stub — REST exposed ``/portfolio/accounts``. The TWS client
        does not need a separate refresh call, so we return the managed
        account list in the same ``{"items": [...]}`` shape callers tolerate.
        """
        ib = await self._ensure_connected()
        try:
            managed = list(ib.managedAccounts() or [])
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("accounts", exc)
            raise
        return {"items": [{"id": acct} for acct in managed]}

    async def account_balance(self) -> float:
        """Return cash balance (USD) for the configured account.

        Prefers ``AvailableFunds`` (what the engine can actually spend
        without breaching margin), falling back to total cash / cash balance
        — identical preference order to the REST client.
        """
        if not self.account_id:
            raise RuntimeError("IBKR_ACCOUNT_ID is not configured")
        ib = await self._ensure_connected()
        try:
            summary = await ib.accountSummaryAsync(self.account_id)
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("account_balance", exc)
            raise
        by_tag: dict[str, float] = {}
        for row in summary or []:
            # AccountValue: account, tag, value, currency
            if self.account_id and getattr(row, "account", "") not in (
                "", self.account_id
            ):
                continue
            currency = getattr(row, "currency", "") or ""
            if currency not in ("", "USD", "BASE"):
                continue
            tag = getattr(row, "tag", "")
            try:
                by_tag[tag] = float(getattr(row, "value", 0.0))
            except (TypeError, ValueError):
                continue
        for key in ("AvailableFunds", "TotalCashValue", "CashBalance"):
            amount = by_tag.get(key)
            if amount:
                return amount
        return 0.0

    async def positions(self) -> list[dict]:
        """Return open positions as REST-shaped dicts.

        ``stranded_reconciler`` filters by ``listingExchange`` / ``exchange``
        and ``contractDesc`` / ``name`` containing the FORECASTX marker and
        reads ``position`` / ``conid`` / ``avgCost`` / ``mktPrice``.
        """
        if not self.account_id:
            return []
        ib = await self._ensure_connected()
        try:
            positions = await ib.reqPositionsAsync()
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("positions", exc)
            raise
        out: list[dict] = []
        for p in positions or []:
            if self.account_id and getattr(p, "account", "") not in (
                "", self.account_id
            ):
                continue
            c = getattr(p, "contract", None)
            if c is None:
                continue
            desc = (
                getattr(c, "localSymbol", "")
                or getattr(c, "description", "")
                or getattr(c, "tradingClass", "")
                or getattr(c, "symbol", "")
            )
            out.append(
                {
                    "conid": str(getattr(c, "conId", "") or ""),
                    "position": getattr(p, "position", 0.0),
                    "avgCost": getattr(p, "avgCost", 0.0),
                    "exchange": getattr(c, "exchange", "")
                    or getattr(c, "primaryExchange", ""),
                    "listingExchange": getattr(c, "primaryExchange", "")
                    or getattr(c, "exchange", ""),
                    "contractDesc": desc,
                    "name": desc,
                    "right": getattr(c, "right", ""),
                    "strike": getattr(c, "strike", 0.0),
                }
            )
        return out

    # ── Market data ────────────────────────────────────────────────

    async def market_snapshot(self, conid: str) -> dict:
        """Single-conid snapshot keyed by IBKR field codes.

        Returns ``{"31": last, "84": bid, "86": ask, "7295": bid_size,
        "7296": ask_size}`` (only the fields IBKR has populated). Returns
        ``{}`` while the field cache is still warming, matching the REST
        client's empty-on-warmup behaviour — the collector's
        ``_snapshot_with_warmup`` retry covers the cold-cache case.

        Uses a *persistent* market-data subscription per conid (the whole
        point of the socket migration) and reads the live ticker on each
        call rather than re-subscribing.
        """
        await self.live_rate_limiter.acquire()
        ib = await self._ensure_connected()
        key = str(conid)
        try:
            ticker = self._tickers.get(key)
            if ticker is None:
                contract = await self._contract_for_conid(conid)
                if contract is None:
                    return {}
                # Streaming subscription (snapshot=False) kept live for the
                # client's lifetime; first read after subscribe is often
                # empty until IBKR pushes the first tick.
                ticker = ib.reqMktData(contract, "", False, False)
                self._tickers[key] = ticker
                # Give IBKR a brief moment to push the first tick so the
                # very first poll has a chance of being populated.
                await asyncio.sleep(0.25)
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("market_snapshot", exc)
            raise

        snapshot: dict[str, Any] = {}
        last = _num(getattr(ticker, "last", None))
        if last <= 0:
            # Fall back to the last close if no live trade has printed.
            last = _num(getattr(ticker, "close", None))
        bid = _num(getattr(ticker, "bid", None))
        ask = _num(getattr(ticker, "ask", None))
        bid_size = _num(getattr(ticker, "bidSize", None))
        ask_size = _num(getattr(ticker, "askSize", None))
        if last > 0:
            snapshot["31"] = last
        if bid > 0:
            snapshot["84"] = bid
        if ask > 0:
            snapshot["86"] = ask
        if bid_size > 0:
            snapshot["7295"] = bid_size
        if ask_size > 0:
            snapshot["7296"] = ask_size
        return snapshot

    @staticmethod
    def is_tradeable_snapshot(snapshot: dict) -> bool:
        """A FORECASTX snapshot is tradeable when it carries a positive bid
        (84), ask (86), or last (31). Identical semantics to
        ``ForecastExClient.is_tradeable_snapshot`` so callers can use either
        client interchangeably.
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

    async def search_contracts(self, symbol: str) -> list[dict]:
        """Fuzzy symbol/name search (REST ``/iserver/secdef/search`` analog).

        Backed by ``reqMatchingSymbolsAsync``; returns items in the shape
        discovery/matcher expect (``conid`` / ``symbol`` / ``companyHeader``).
        """
        payload = await self._request(
            "POST", "/iserver/secdef/search",
            json_body={"symbol": symbol, "name": True},
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    async def get_contract_info(self, conid: str) -> dict:
        """Return contract metadata used by NO-sibling discovery.

        The collector reads ``underlying_con_id`` (event parent),
        ``strike`` and ``right`` (expects ``C``/``CALL`` for the YES leg).
        ``reqContractDetailsAsync`` exposes the parent via ``underConId``.
        """
        ib = await self._ensure_connected()
        try:
            details = await ib.reqContractDetailsAsync(
                Contract(conId=int(conid))
            )
            self.circuit.record_success()
        except Exception as exc:
            # Parity with REST: best-effort, swallow and return {}.
            logger.debug("forecastex TWS get_contract_info(%s) failed: %s", conid, exc)
            return {}
        if not details:
            return {}
        d = details[0]
        c = getattr(d, "contract", None)
        return {
            "conid": str(getattr(c, "conId", conid) if c else conid),
            "underlying_con_id": getattr(d, "underConId", None),
            "strike": getattr(c, "strike", None) if c else None,
            "right": getattr(c, "right", "") if c else "",
            "symbol": getattr(c, "symbol", "") if c else "",
            "maturity_date": getattr(c, "lastTradeDateOrContractMonth", "") if c else "",
        }

    async def get_trsrv_secdef(self, conid: str) -> dict:
        """Minimal parity stub. REST used ``/trsrv/secdef`` for ``name`` /
        ``assetClass`` in its NO-sibling fallback; the TWS
        ``resolve_event_children`` does not need it, so we return enough
        for callers that only read ``name``.
        """
        info = await self.get_contract_info(conid)
        return {
            "name": info.get("symbol", ""),
            "assetClass": "OPT",
            "conid": str(conid),
        }

    async def resolve_event_children(
        self, parent_conid: str, *, months: tuple[str, ...] = (),
    ) -> list[dict]:
        """Enumerate YES/NO child contracts under a FORECASTX event parent.

        TWS flow (analogous to the REST secdef/strikes + secdef/info dance):

          1. Qualify ``parent_conid`` to learn its symbol / secType.
          2. ``reqSecDefOptParamsAsync`` returns the option chain
             (expirations + strikes) on the FORECASTX exchange.
          3. For every (expiration, strike) build the Call (YES) and Put
             (NO) OPT contracts and qualify them in one batch to obtain the
             real child conids.

        Returns ``{conid, right, strike, maturity_date, desc, source}`` dicts
        — identical to the REST path — so the collector's NO-sibling matcher
        and the discovery/matcher consumers are unaffected.

        ``months`` is accepted for signature parity; the chain enumeration
        already covers all live expirations, so it is not used to gate the
        TWS lookup.
        """
        children: list[dict] = []
        parent = await self._contract_for_conid(parent_conid)
        if parent is None:
            return children
        ib = self._ib
        symbol = getattr(parent, "symbol", "") or getattr(parent, "tradingClass", "")
        sec_type = getattr(parent, "secType", "") or "IND"
        try:
            chains = await ib.reqSecDefOptParamsAsync(
                symbol, "FORECASTX", sec_type, int(parent_conid),
            )
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("resolve_event_children", exc)
            raise

        # Collect FORECASTX chains (fall back to all if exchange unlabelled).
        fx_chains = [
            ch for ch in (chains or [])
            if str(getattr(ch, "exchange", "")).upper() == "FORECASTX"
        ] or list(chains or [])
        if not fx_chains:
            return children

        # Build the full (expiration, strike, right) candidate set, then
        # qualify in a single batch to resolve child conids efficiently.
        candidates: list[Any] = []
        meta: list[tuple[str, float, str]] = []  # (expiration, strike, right)
        seen: set[tuple[str, float, str]] = set()
        for ch in fx_chains:
            expirations = sorted(getattr(ch, "expirations", []) or [])
            strikes = sorted(getattr(ch, "strikes", []) or [])
            trading_class = getattr(ch, "tradingClass", "") or symbol
            for expiration in expirations:
                for strike in strikes:
                    for right in ("C", "P"):
                        try:
                            strike_f = float(strike)
                        except (TypeError, ValueError):
                            continue
                        sig = (expiration, strike_f, right)
                        if sig in seen:
                            continue
                        seen.add(sig)
                        candidates.append(
                            Contract(
                                secType="OPT",
                                symbol=symbol,
                                exchange="FORECASTX",
                                currency="USD",
                                lastTradeDateOrContractMonth=expiration,
                                strike=strike_f,
                                right=right,
                                tradingClass=trading_class,
                            )
                        )
                        meta.append(sig)
        if not candidates:
            return children
        try:
            qualified = await ib.qualifyContractsAsync(*candidates)
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("resolve_event_children.qualify", exc)
            raise
        for contract, (expiration, strike_f, right) in zip(qualified, meta):
            if contract is None:
                continue
            conid = str(getattr(contract, "conId", "") or "").strip()
            if not conid:
                continue
            children.append(
                {
                    "conid": conid,
                    "right": (getattr(contract, "right", right) or right).upper(),
                    "strike": getattr(contract, "strike", strike_f),
                    "maturity_date": getattr(
                        contract, "lastTradeDateOrContractMonth", expiration
                    ),
                    "desc": getattr(contract, "localSymbol", "")
                    or f"{symbol} {strike_f} {right}",
                    "source": f"tws:secdefopt:{expiration}:strike={strike_f}",
                }
            )
        return children

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
        """LMT order placement (BUY or SELL) over the TWS socket.

        Returns a REST-shaped ack dict that ``ForecastExAdapter`` already
        parses: ``order_id`` / ``order_status`` / ``filled_quantity`` /
        ``avg_price`` / ``conid`` / ``side``.

        For IOC/FOK we wait briefly for the order to reach a terminal state
        so the ack reflects the actual fill; for GTC we return as soon as
        the broker acknowledges the resting order.
        """
        if not self.account_id:
            raise RuntimeError("IBKR_ACCOUNT_ID is not configured")
        normalized_side = str(side).upper()
        if normalized_side not in ("BUY", "SELL"):
            raise ValueError(
                f"forecastex.place_order rejects side={side!r}: must be BUY or SELL"
            )
        await self.live_rate_limiter.acquire()
        ib = await self._ensure_connected()
        contract = await self._contract_for_conid(conid)
        if contract is None:
            raise RuntimeError(
                f"forecastex TWS: could not qualify contract for conid {conid!r}"
            )
        order = Order(
            orderType=order_type,
            action=normalized_side,
            totalQuantity=int(quantity),
            lmtPrice=float(price),
            tif=tif,
            account=self.account_id,
            outsideRth=True,
        )
        try:
            trade = ib.placeOrder(contract, order)
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("place_order", exc)
            raise

        # Wait for a terminal (IOC/FOK) or live-resting (GTC) state. ib_async
        # updates the Trade in place as order-status events arrive.
        deadline = self.connect_timeout  # reuse as an upper bound (seconds)
        waited = 0.0
        step = 0.1
        terminal = _DONE_STATES if str(tif).upper() in ("IOC", "FOK") else (
            _DONE_STATES | _LIVE_STATES
        )
        while waited < min(deadline, 5.0):
            status = getattr(trade.orderStatus, "status", "") or ""
            if status in terminal:
                break
            await asyncio.sleep(step)
            waited += step
        return self._trade_to_dict(trade, contract, normalized_side)

    @staticmethod
    def _trade_to_dict(trade: Any, contract: Any, side: str) -> dict:
        """Translate an ib_async ``Trade`` into a REST-style ack dict."""
        os_ = trade.orderStatus
        order_id = (
            getattr(trade.order, "orderId", 0)
            or getattr(os_, "orderId", 0)
            or getattr(trade.order, "permId", 0)
        )
        return {
            "order_id": str(order_id) if order_id else "",
            "order_status": _normalize_status(getattr(os_, "status", "")),
            "filled_quantity": _num(getattr(os_, "filled", 0.0)),
            "avg_price": _num(getattr(os_, "avgFillPrice", 0.0)),
            "conid": str(getattr(contract, "conId", "") or ""),
            "side": side,
        }

    async def get_order(self, order_id: str) -> dict:
        """Return current state for ``order_id`` as a REST-shaped dict.

        Returns ``{}`` when the order is not known to this session (the
        adapter treats an empty payload as 'no fresh signal', not a failure).
        """
        ib = await self._ensure_connected()
        target = str(order_id)
        try:
            trades = list(ib.trades())
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("get_order", exc)
            raise
        for trade in trades:
            tid = str(getattr(trade.order, "orderId", "") or "")
            pid = str(getattr(trade.order, "permId", "") or "")
            if target in (tid, pid):
                contract = getattr(trade, "contract", None)
                side = getattr(trade.order, "action", "")
                return self._trade_to_dict(trade, contract, side)
        return {}

    async def cancel_order(self, order_id: str) -> dict:
        if not self.account_id:
            raise RuntimeError("IBKR_ACCOUNT_ID is not configured")
        ib = await self._ensure_connected()
        target = str(order_id)
        try:
            for trade in list(ib.trades()):
                tid = str(getattr(trade.order, "orderId", "") or "")
                pid = str(getattr(trade.order, "permId", "") or "")
                if target in (tid, pid):
                    ib.cancelOrder(trade.order)
                    break
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("cancel_order", exc)
            raise
        return {}

    async def list_live_orders(self) -> list[dict]:
        """Return open orders as REST-shaped dicts.

        The adapter's broker-id recovery path matches on ``conid`` /
        ``conidEx`` / ``side`` / ``totalSize`` / ``size`` / ``quantity`` and
        ``orderId`` / ``order_id``, optionally ``lastExecutionTime_r``.
        """
        ib = await self._ensure_connected()
        try:
            open_trades = list(ib.openTrades())
            self.circuit.record_success()
        except Exception as exc:
            self._on_op_exception("list_live_orders", exc)
            raise
        out: list[dict] = []
        for trade in open_trades:
            c = getattr(trade, "contract", None)
            o = getattr(trade, "order", None)
            os_ = getattr(trade, "orderStatus", None)
            out.append(
                {
                    "conid": str(getattr(c, "conId", "") or "") if c else "",
                    "side": getattr(o, "action", "") if o else "",
                    "totalSize": getattr(o, "totalQuantity", 0) if o else 0,
                    "quantity": getattr(o, "totalQuantity", 0) if o else 0,
                    "orderId": str(getattr(o, "orderId", "") or "") if o else "",
                    "order_id": str(getattr(o, "orderId", "") or "") if o else "",
                    "status": _normalize_status(
                        getattr(os_, "status", "") if os_ else ""
                    ),
                    "filled_quantity": _num(
                        getattr(os_, "filled", 0.0) if os_ else 0.0
                    ),
                }
            )
        return out

    async def cancel_all_open_orders(self) -> list[str]:
        cancelled: list[str] = []
        ib = await self._ensure_connected()
        try:
            open_trades = list(ib.openTrades())
        except Exception as exc:
            self._on_op_exception("cancel_all_open_orders", exc)
            raise
        for trade in open_trades:
            o = getattr(trade, "order", None)
            if o is None:
                continue
            order_id = str(getattr(o, "orderId", "") or "")
            if not order_id:
                continue
            try:
                ib.cancelOrder(o)
                cancelled.append(order_id)
            except Exception as exc:  # noqa: BLE001 - mirror REST best-effort
                logger.warning(
                    "forecastex TWS cancel_all failed for %s: %s", order_id, exc
                )
        return cancelled

    # ── Compatibility shim for direct REST callers ─────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        use_circuit: bool = True,
    ) -> dict:
        """Best-effort compatibility for the two modules that call the REST
        client's private ``_request`` directly: ``forecastex_discovery`` and
        ``fx_cross_platform_matcher`` (both for ``/iserver/secdef/search``).

        Only the secdef/search path is supported, mapped to
        ``reqMatchingSymbolsAsync``. Any other path raises ``NotImplementedError``
        — both call sites wrap this in a try/except that degrades to "no
        results", so an unsupported path is non-fatal.
        """
        norm_path = (path or "").rstrip("/")
        if norm_path.endswith("/iserver/secdef/search"):
            symbol = ""
            if isinstance(json_body, dict):
                symbol = str(json_body.get("symbol") or "")
            if not symbol:
                return {"items": []}
            ib = await self._ensure_connected()
            try:
                descriptions = await ib.reqMatchingSymbolsAsync(symbol)
                self.circuit.record_success()
            except Exception as exc:
                self._on_op_exception("_request.secdef_search", exc)
                raise
            items: list[dict] = []
            for desc in descriptions or []:
                c = getattr(desc, "contract", None)
                if c is None:
                    continue
                exch = (
                    getattr(c, "primaryExchange", "")
                    or getattr(c, "exchange", "")
                )
                name = getattr(c, "description", "") or getattr(c, "symbol", "")
                # Synthesize a companyHeader so the upstream FORECASTX marker
                # filter behaves the same way it does against the REST payload.
                header = f"{name} ({exch})" if exch else name
                items.append(
                    {
                        "conid": str(getattr(c, "conId", "") or ""),
                        "symbol": getattr(c, "symbol", ""),
                        "companyHeader": header,
                        "secType": getattr(c, "secType", ""),
                    }
                )
            return {"items": items}
        raise NotImplementedError(
            f"ForecastExTWSClient._request does not support {method} {path} "
            "(only /iserver/secdef/search is bridged to the TWS socket)"
        )

    # ── Lifecycle ──────────────────────────────────────────────────

    async def close(self) -> None:
        ib = self._ib
        if ib is not None:
            try:
                if ib.isConnected():
                    # Cancel live market-data subscriptions before parting.
                    for ticker in list(self._tickers.values()):
                        contract = getattr(ticker, "contract", None)
                        if contract is not None:
                            try:
                                ib.cancelMktData(contract)
                            except Exception:  # noqa: BLE001
                                pass
                    ib.disconnect()
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.warning("forecastex TWS close error: %s", exc)
        self._tickers.clear()
        self._ib = None
