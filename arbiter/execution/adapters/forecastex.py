"""ForecastExAdapter — execution adapter for the IBKR-routed ForecastEx venue.

Implements the ``PlatformAdapter`` protocol (see ``base.PlatformAdapter``).
All network calls go through ``ForecastExClient`` (arbiter.collectors.forecastex);
the adapter itself only translates between the engine's order semantics
(YES/NO legs at probability prices) and the IBKR Client Portal API (BUY-only
LMT orders with IOC TIF).

Hard-lock enforcement order (matches ``polymarket_us.py`` spec §5.2):

    Gate 1: PHASE4 hard-lock — if _phase4_max_usd is not None and notional exceeds
    Gate 2: PHASE5 hard-lock — stricter cap
    Gate 3: supervisor armed
    ─────── Only NOW send the order ───────────────────────────────

ForecastEx-specific:
  - BUY and SELL are both supported (SELL closes a position held long against
    the same conid, mirroring Kalshi's place_unwind_sell semantics).
  - Prices are clamped to [0.01, 0.99] dollars and snapped to the cent tick.
  - IOC + LMT for the secondary leg; FOK is emulated as IOC where IBKR doesn't
    expose a native FOK (the engine treats partial fill on IOC as the same
    soft-naked recovery path as a FOK kill).
  - ``place_resting_sell`` (GTC) and ``place_unwind_sell`` (IOC) deliberately
    skip PHASE4/5/supervisor gates — those gates exist to STOP new exposure;
    these paths CLOSE existing exposure that the gates themselves may have
    flagged. Blocking the unwind would lock the venue into a permanent
    naked state.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import structlog

from ..engine import Order, OrderStatus
from .exceptions import OrderRejected

logger = structlog.get_logger("arbiter.adapters.forecastex")

_DEFAULT_TICK = 0.01
_MIN_PRICE = 0.01
_MAX_PRICE = 0.99

# Maximum acceptable drift (cents) between the engine-passed price and a
# freshly-pulled top-of-book ask before we treat the price as stale. The
# scanner emits the opportunity price first, then the engine pulls fresh
# depth via best_executable_price — when the venue moves significantly
# between those two steps, executing at the older price overpays. 5¢ is
# wide enough to absorb normal tick-level chop on a 1¢-tick book and
# tight enough to catch the synthetic-NO regression class (where the
# scanner saw a fake $0.47 and the real ask was $0.55).
_PRICE_DRIFT_CENTS_THRESHOLD = 5.0

# Sentinel marking an Order whose broker_id we couldn't capture from the
# place-order response AND couldn't recover via the live-orders fallback.
# The order is returned as FAILED with this suffix so the engine recovery
# path runs, and so the defensive guard in get_order/cancel_order can
# detect it and skip the wasted HTTP round-trip that would otherwise 400.
_BROKER_ID_LOST_SUFFIX = "-FCST-NOID"


class ForecastExAdapter:
    """Execution adapter for ForecastEx (via IBKR Client Portal Web API).

    Parameters
    ----------
    client:
        ``ForecastExClient`` instance (from arbiter.collectors.forecastex).
    phase4_max_usd / phase5_max_usd:
        Hard-lock caps in USD. ``None`` disables the gate.
    supervisor:
        Optional ``SafetySupervisor``-like object with ``is_armed: bool``.
    """

    platform = "forecastex"

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
        # Cross-side exit bookkeeping (2026-07-06 live probe: the venue
        # CANCELS every SELL order — orders 249901302/303 — so exits must
        # BUY the opposing conid and IBKR nets the position).
        # _sibling_cache: conid -> opposite-side conid (structural, stable
        # for the contract's lifetime, so cached per adapter instance).
        # _inverted_exits: broker order ids whose venue fills are sibling
        # BUY prices and must be translated back to sell-side terms
        # (sell at p == buy sibling at 1-p). NOTE: in-memory only — after a
        # process restart a pre-existing resting exit re-reads untranslated;
        # the recovery reconciler owns that window.
        self._sibling_cache: Dict[str, str] = {}
        self._inverted_exits: set = set()

    # ─── Hard-lock helpers ────────────────────────────────────────────────

    def _check_gates(self, *, arb_id: str, notional: float, op: str) -> None:
        if self._phase4_max_usd is not None and notional > self._phase4_max_usd:
            logger.warning(
                "forecastex.phase4_hardlock.rejected",
                arb_id=arb_id, notional=notional, max=self._phase4_max_usd, op=op,
            )
            raise OrderRejected(
                f"PHASE4 hard-lock: notional ${notional:.2f} > ${self._phase4_max_usd:.2f}"
            )
        if self._phase5_max_usd is not None and notional > self._phase5_max_usd:
            logger.warning(
                "forecastex.phase5_hardlock.rejected",
                arb_id=arb_id, notional=notional, max=self._phase5_max_usd, op=op,
            )
            raise OrderRejected(
                f"PHASE5 hard-lock: notional ${notional:.2f} > ${self._phase5_max_usd:.2f}"
            )
        if self._supervisor is not None and self._supervisor.is_armed:
            logger.warning("forecastex.supervisor_armed.rejected", arb_id=arb_id, op=op)
            raise OrderRejected("supervisor armed")

    # ─── Order placement ──────────────────────────────────────────────────

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
        """ForecastEx has no native FOK — we emit IOC and let the engine route
        partial fills through the same soft-naked recovery path it uses for
        Polymarket US FOK rejections."""
        return await self._submit(
            arb_id, market_id, canonical_id, side, price, qty,
            tif="IOC", op="place_fok", max_affordable=max_affordable,
        )

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
        return await self._submit(
            arb_id, market_id, canonical_id, side, price, qty,
            tif="IOC", op="place_ioc", max_affordable=max_affordable,
        )

    # ─── Sell-side (smart-unwind helpers) ─────────────────────────────────

    async def place_resting_sell(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
    ) -> Order:
        """Resting exit at sell-price ``price`` (typically break-even).

        Phase 1 of ``ExecutionEngine._smart_unwind``: rest for ~30s so the
        market can hit our cost basis. If nothing matches, the engine
        cancels and falls back to ``place_unwind_sell``.

        ForecastEx cancels direct SELL orders (verified live 2026-07-06:
        GTC SELL @0.25 and IOC SELL @0.01 against a 0.19 bid were both
        accepted then venue-cancelled with zero fill). The exit therefore
        BUYS the opposing conid GTC at ``1 - price`` — IBKR nets the two
        positions out; economics are identical to selling at ``price``.

        ``side`` is the side originally bought ("yes" → we hold YES long).
        Deliberately skips PHASE4/5/supervisor gates (closing exposure must
        not be blocked by gates that exist to stop opening it). Always
        returns an Order; never raises across this boundary.
        """
        return await self._submit_sell(
            arb_id, market_id, canonical_id, side, price, qty,
            tif="GTC", op="place_resting_sell",
        )

    async def place_unwind_sell(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        qty: int,
        panic_price: float = 0.01,
    ) -> Order:
        """Immediate exit at sell-price ``panic_price`` to close a naked leg.

        Phase 2 of ``ExecutionEngine._smart_unwind`` (also the no-resting-
        support fallback): exit whatever the book absorbs at >= ``panic_price``
        and the IOC TIF cancels the rest. Residual unfilled qty becomes a
        manual-review incident upstream.

        Routed as an IOC BUY of the opposing conid at ``1 - panic_price``
        (the venue cancels direct SELLs — see ``place_resting_sell``).
        ``side`` is the side originally bought.
        Skips PHASE4/5/supervisor gates by design (see class docstring).
        Always returns an Order in a terminal state; never raises.
        """
        return await self._submit_sell(
            arb_id, market_id, canonical_id, side, panic_price, qty,
            tif="IOC", op="place_unwind_sell",
        )

    async def _submit_sell(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        *,
        tif: str,
        op: str,
    ) -> Order:
        """Common path for resting + unwind exits.

        Deliberately omits ``_check_gates`` — see class-level docstring for
        why the close-exposure path bypasses PHASE4/5/supervisor.

        Cross-side routing: ForecastEx venue-cancels every SELL order, so
        the exit BUYS the sibling conid at ``1 - price``. The returned
        Order keeps sell-side semantics for the engine (``price`` and
        ``fill_price`` are the effective sell prices) while ``market_id``
        records the sibling conid the venue order actually lives on.
        """
        normalized_side = str(side).strip().lower()
        snapped = self._snap_to_tick(price)

        sibling_conid = await self._resolve_sibling_conid(str(market_id).strip())
        if not sibling_conid:
            logger.error(
                "forecastex.sell.sibling_unresolved",
                arb_id=arb_id, market_id=market_id, side=side, op=op,
            )
            return Order(
                order_id=f"{arb_id}-{normalized_side.upper() or 'SELL'}{_BROKER_ID_LOST_SUFFIX}",
                platform="forecastex",
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=snapped,
                quantity=int(qty),
                status=OrderStatus.FAILED,
                timestamp=time.time(),
                error=(
                    f"forecastex {op} refused: sibling conid unresolved for "
                    f"{market_id} — cross-side exit unavailable (the venue "
                    "cancels direct SELL orders)"
                ),
                external_client_order_id=None,
            )
        buy_price = self._snap_to_tick(1.0 - snapped)

        t_before_place_ns = time.monotonic_ns()
        placed_after_wall_ts = time.time()
        try:
            response = await self._client.place_order(
                conid=sibling_conid, side="BUY",
                price=buy_price, quantity=int(qty), tif=tif,
            )
        except Exception as exc:
            place_ms_failed = round((time.monotonic_ns() - t_before_place_ns) / 1e6, 1)
            logger.warning(
                "forecastex.sell.place_order.failed",
                arb_id=arb_id, market_id=market_id, side=side, op=op,
                place_ms=place_ms_failed, err=str(exc),
            )
            return Order(
                order_id=f"{arb_id}-{normalized_side.upper() or 'SELL'}{_BROKER_ID_LOST_SUFFIX}",
                platform="forecastex",
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=snapped,
                quantity=int(qty),
                status=OrderStatus.FAILED,
                timestamp=time.time(),
                error=f"forecastex {op} failed: {exc}",
                external_client_order_id=None,
            )
        place_ms = round((time.monotonic_ns() - t_before_place_ns) / 1e6, 1)

        broker_order_id = self._extract_broker_order_id(response)
        recovery_path = "ack"
        if not broker_order_id:
            logger.warning(
                "forecastex.sell.no_broker_id_in_ack",
                arb_id=arb_id, market_id=market_id, side=side, op=op,
                raw_response=str(response)[:500],
            )
            broker_order_id = await self._lookup_broker_id_via_live_orders(
                market_id=sibling_conid,
                normalized_side="BUY",
                qty=int(qty),
                placed_after_ts=placed_after_wall_ts,
            )
            recovery_path = "live_orders_lookup" if broker_order_id else "lost"

        if not broker_order_id:
            logger.error(
                "forecastex.sell.broker_id_unrecoverable",
                arb_id=arb_id, market_id=market_id, side=side, op=op,
                place_ms=place_ms, raw_response=str(response)[:500],
            )
            return Order(
                order_id=f"{arb_id}-{normalized_side.upper() or 'SELL'}{_BROKER_ID_LOST_SUFFIX}",
                platform="forecastex",
                market_id=sibling_conid,
                canonical_id=canonical_id,
                side=side,
                price=snapped,
                quantity=int(qty),
                status=OrderStatus.FAILED,
                timestamp=time.time(),
                error=(
                    f"forecastex {op} broker_id unrecoverable from ack and "
                    "live-orders lookup; cross-side exit BUY MAY be live at IBKR"
                ),
                external_client_order_id=None,
            )

        order = self._order_from_response(
            response, arb_id, sibling_conid, canonical_id, side, snapped, int(qty),
            broker_order_id=broker_order_id, invert_fill_price=True,
        )
        self._inverted_exits.add(str(broker_order_id))
        logger.info(
            "forecastex.sell.placed",
            arb_id=arb_id, market_id=market_id, sibling_conid=sibling_conid,
            side=side, op=op, tif=tif,
            sell_price=snapped, buy_price=buy_price, qty=qty, place_ms=place_ms,
            order_status=order.status.value, fill_qty=order.fill_qty,
            broker_order_id=broker_order_id, broker_id_path=recovery_path,
        )
        return order

    async def _resolve_sibling_conid(self, conid: str) -> Optional[str]:
        """Opposite-side conid for ``conid`` (YES↔NO at the same strike).

        Path: ``get_contract_info(conid)`` → parent + strike + right;
        ``resolve_event_children(parent)`` → the child with the OPPOSITE
        right at the same strike. Positive and structural-negative results
        are cached for the adapter's lifetime (contract structure is
        immutable); transient lookup failures are NOT cached so the next
        exit attempt retries. Returns None when the sibling cannot be
        proven — callers must fail closed, never fall back to a SELL.
        """
        if not conid or conid in ("0", "None"):
            return None
        if conid in self._sibling_cache:
            return self._sibling_cache[conid] or None
        try:
            info = await self._client.get_contract_info(conid)
        except Exception as exc:
            logger.warning(
                "forecastex.sibling.contract_info_failed",
                conid=conid, err=str(exc),
            )
            return None
        if not isinstance(info, dict) or not info:
            self._sibling_cache[conid] = ""
            return None
        parent = (
            info.get("underlying_con_id")
            or info.get("underlyingConid")
            or info.get("undConid")
            or info.get("underconid")
        )
        strike = info.get("strike")
        right = str(info.get("right") or "").upper()
        if not parent or right not in ("C", "CALL", "P", "PUT"):
            self._sibling_cache[conid] = ""
            logger.warning(
                "forecastex.sibling.unresolvable_contract_info",
                conid=conid, parent=parent, strike=strike, right=right,
            )
            return None
        want_right = ("P", "PUT") if right in ("C", "CALL") else ("C", "CALL")
        try:
            children = await self._client.resolve_event_children(str(parent))
        except Exception as exc:
            logger.warning(
                "forecastex.sibling.children_lookup_failed",
                conid=conid, parent=parent, err=str(exc),
            )
            return None
        try:
            strike_f = float(strike) if strike is not None else None
        except (TypeError, ValueError):
            strike_f = None
        for child in children or []:
            if not isinstance(child, dict):
                continue
            if str(child.get("right", "")).upper() not in want_right:
                continue
            if strike_f is not None:
                try:
                    if abs(float(child.get("strike") or 0.0) - strike_f) > 1e-6:
                        continue
                except (TypeError, ValueError):
                    continue
            candidate = str(child.get("conid") or "").strip()
            if candidate and candidate != conid:
                self._sibling_cache[conid] = candidate
                # The sibling's sibling is us — prime the reverse edge.
                self._sibling_cache.setdefault(candidate, conid)
                return candidate
        self._sibling_cache[conid] = ""
        logger.warning(
            "forecastex.sibling.not_found",
            conid=conid, parent=parent, strike=strike,
        )
        return None

    async def _submit(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        *,
        tif: str,
        op: str,
        max_affordable: Optional[float],
    ) -> Order:
        notional = float(price) * float(qty)
        self._check_gates(arb_id=arb_id, notional=notional, op=op)

        # BUY-only constraint: closing a YES position means buying NO (and
        # vice versa). The engine NEVER asks ForecastEx to SELL.
        normalized_side = str(side).strip().upper()
        if normalized_side in ("SELL_YES", "SELL_NO", "SELL_LONG", "SELL_SHORT", "SELL"):
            raise OrderRejected(
                f"forecastex only supports BUY orders (got side={side!r})"
            )

        # ── CONID INTEGRITY GATE (phantom-trade postmortem 2026-05-28) ──
        # ForecastEx binary contracts have SEPARATE conids for YES (right=C)
        # and NO (right=P). An empty market_id means the collector couldn't
        # discover the NO sibling and the scanner shouldn't have created
        # this opportunity. Refuse to submit rather than letting IBKR
        # interpret a malformed conid (or worse — accept "0" as a contract
        # id and route to an arbitrary listing). The scanner-side gate is
        # the primary defender; this is defense-in-depth.
        normalized_market_id = str(market_id or "").strip()
        if not normalized_market_id or normalized_market_id in ("0", "None"):
            logger.error(
                "forecastex.submit.empty_market_id",
                arb_id=arb_id, canonical_id=canonical_id, side=side,
                op=op, market_id=repr(market_id),
            )
            raise OrderRejected(
                f"forecastex {op} refused: empty market_id "
                f"(side={side!r}); collector likely failed NO-conid "
                f"discovery — investigate before retrying"
            )

        # Explicit side ↔ conid binding log. Operators investigating a
        # phantom-trade incident need to see exactly which contract id was
        # bought for which side, BEFORE the venue ack — the venue ack
        # echoes the conid but not the side semantics.
        logger.info(
            "forecastex.submit.routing",
            arb_id=arb_id, canonical_id=canonical_id, side=side,
            market_id=normalized_market_id, op=op,
            price=price, qty=qty,
        )

        # YES vs NO leg routing. ForecastEx exposes both YES and NO as their
        # own conids; the scanner-supplied market_id IS the conid for the
        # specific contract being bought, so we always pass it through as-is
        # and submit a BUY. The probability we paid is the price the engine
        # passed in (already on the correct side).
        snapped = self._snap_to_tick(price)
        if max_affordable is not None and snapped > max_affordable + 1e-9:
            logger.warning(
                "forecastex.marketability.aborted_unprofitable",
                arb_id=arb_id, market_id=market_id, side=side,
                original_price=price, snapped=snapped, max_affordable=max_affordable,
            )
            raise OrderRejected(
                f"forecastex price {snapped:.4f} > max_affordable {max_affordable:.4f}"
            )

        t_before_place_ns = time.monotonic_ns()
        placed_after_wall_ts = time.time()
        try:
            response = await self._client.place_order(
                conid=market_id, side="BUY",
                price=snapped, quantity=int(qty), tif=tif,
            )
        except OrderRejected:
            raise
        except Exception as exc:
            place_ms_failed = round((time.monotonic_ns() - t_before_place_ns) / 1e6, 1)
            logger.warning(
                "forecastex.place_order.failed",
                arb_id=arb_id, market_id=market_id, side=side,
                place_ms=place_ms_failed, err=str(exc),
            )
            return Order(
                order_id=f"{arb_id}-{normalized_side or 'BUY'}{_BROKER_ID_LOST_SUFFIX}",
                platform="forecastex",
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=price,
                quantity=int(qty),
                status=OrderStatus.FAILED,
                timestamp=time.time(),
                error=f"forecastex place_order failed: {exc}",
                external_client_order_id=None,
            )
        place_ms = round((time.monotonic_ns() - t_before_place_ns) / 1e6, 1)

        broker_order_id = self._extract_broker_order_id(response)
        recovery_path = "ack"
        if not broker_order_id:
            # Ack didn't carry an id. Log the raw shape (truncated) so the
            # next round of diagnosis has the ground truth from production,
            # then try the live-orders lookup before giving up.
            logger.warning(
                "forecastex.submit.no_broker_id_in_ack",
                arb_id=arb_id, market_id=market_id, side=side, op=op,
                raw_response=str(response)[:500],
            )
            broker_order_id = await self._lookup_broker_id_via_live_orders(
                market_id=market_id,
                normalized_side="BUY",
                qty=int(qty),
                placed_after_ts=placed_after_wall_ts,
            )
            recovery_path = "live_orders_lookup" if broker_order_id else "lost"

        if not broker_order_id:
            # We have no handle on the order. It MAY still be live at
            # IBKR; the engine returning FAILED here prevents the secondary
            # leg from firing (no naked exposure on our side). Log loudly
            # so operators can manually intervene if the order does fill.
            logger.error(
                "forecastex.submit.broker_id_unrecoverable",
                arb_id=arb_id, market_id=market_id, side=side, op=op,
                place_ms=place_ms, raw_response=str(response)[:500],
            )
            return Order(
                order_id=f"{arb_id}-{normalized_side or 'BUY'}{_BROKER_ID_LOST_SUFFIX}",
                platform="forecastex",
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=snapped,
                quantity=int(qty),
                status=OrderStatus.FAILED,
                timestamp=time.time(),
                error=(
                    "forecastex broker_id unrecoverable from ack and live-orders "
                    "lookup; order MAY be live at IBKR — operator review required"
                ),
                external_client_order_id=None,
            )

        order = self._order_from_response(
            response, arb_id, market_id, canonical_id, side, snapped, int(qty),
            broker_order_id=broker_order_id,
        )
        # V19 wire-path telemetry — per-leg place latency so operators can
        # tell venue slowness apart from network slowness in live-fire.
        logger.info(
            "forecastex.order.placed",
            arb_id=arb_id, market_id=market_id, side=side, tif=tif,
            price=snapped, qty=qty, place_ms=place_ms,
            order_status=order.status.value, fill_qty=order.fill_qty,
            broker_order_id=broker_order_id, broker_id_path=recovery_path,
        )
        return order

    # ─── Status queries ───────────────────────────────────────────────────

    @staticmethod
    def _looks_like_synthetic_id(order_id: str) -> bool:
        """Return True when ``order_id`` is one of the internal sentinels
        the adapter falls back to when no real broker id is available
        (e.g. ``-FCST-NOID`` after the place-order ack came back empty).

        IBKR-assigned order ids are short numeric strings; the synthetic
        sentinels embed our ARB- prefix and a recognizable suffix. The
        belt-and-suspenders check here avoids the 400-storm observed on
        ARB-000768/769 even if some future regression reintroduces a
        synthetic-id path the extraction logic missed.
        """
        if not order_id:
            return True
        return (
            order_id.startswith("ARB-")
            and (
                order_id.endswith(_BROKER_ID_LOST_SUFFIX)
                or order_id.endswith("-FCST")
            )
        )

    async def get_order(self, order: Order) -> Order:
        """Query IBKR for the current order state.

        Only definitive venue responses mutate ``order.status``. Transient
        errors (network blip, empty payload, malformed shape) leave the
        order unchanged and set ``idempotency_ambiguous=True`` so callers
        can distinguish "no fresh signal" from "venue confirmed FAILED".

        Previously every exception terminally marked the order FAILED,
        which let a single network blip during ``_poll_submitted_to_terminal``
        flip a resting Forecastex order to FAILED and trigger an
        unwarranted naked-leg unwind on the paired leg.
        """
        # ARB-000768/769 backstop: never round-trip a synthetic id back
        # to IBKR — it always 400s, burns rate budget, and floods the log
        # with one warning per poll. Mark ambiguous and let the engine's
        # recovery path own the resolution.
        if self._looks_like_synthetic_id(order.order_id):
            logger.warning(
                "forecastex.get_order.synthetic_id_skip",
                order_id=order.order_id, status=order.status.value,
            )
            order.idempotency_ambiguous = True
            return order
        try:
            payload = await self._client.get_order(order.order_id)
        except Exception as exc:
            logger.warning(
                "forecastex.get_order.failed",
                order_id=order.order_id, err=str(exc),
            )
            order.idempotency_ambiguous = True
            order.error = f"get_order failed: {exc}"
            return order

        # IBKR returns either a single dict or a list of status dicts;
        # take the most recent entry.
        if isinstance(payload, list) and payload:
            payload = payload[-1]
        if not isinstance(payload, dict) or not payload:
            logger.warning(
                "forecastex.get_order.empty_payload",
                order_id=order.order_id,
            )
            order.idempotency_ambiguous = True
            return order

        api_status = str(
            payload.get("status")
            or payload.get("order_status")
            or payload.get("orderStatus")
            or ""
        ).upper()
        if not api_status:
            logger.warning(
                "forecastex.get_order.empty_status",
                order_id=order.order_id,
            )
            order.idempotency_ambiguous = True
            return order

        order.status = self._map_status(api_status, order.status)
        try:
            fill_qty = float(
                payload.get("filled_quantity")
                or payload.get("filledQuantity")
                or payload.get("cumQty")
                # /iserver/account/order/status/{id} reports fills as
                # cum_fill (verified live 2026-07-06, order 249901299) —
                # missing it made FILLED orders read as fill_qty=0, which
                # the engine's FILL-02 guard treats as "no exposure".
                or payload.get("cum_fill")
                or 0.0
            )
            if fill_qty:
                order.fill_qty = int(fill_qty)
        except (TypeError, ValueError):
            pass
        try:
            avg_px = float(
                payload.get("avg_price")
                or payload.get("avgPrice")
                or payload.get("avgFillPrice")
                # same endpoint reports the fill price as average_price
                or payload.get("average_price")
                or 0.0
            )
            if avg_px:
                px = avg_px if avg_px <= 1.0 else avg_px / 100.0
                # Cross-side exits fill on the SIBLING conid; translate the
                # sibling BUY price back to sell-side terms (sell at p ==
                # buy sibling at 1-p).
                if str(order.order_id) in self._inverted_exits:
                    px = round(1.0 - px, 4)
                order.fill_price = px
        except (TypeError, ValueError):
            pass
        return order

    async def get_order_status(self, order: Order) -> Order:
        return await self.get_order(order)

    async def cancel_order(self, order: Order) -> bool:
        # See note in ``get_order`` — synthetic ids can never cancel.
        if self._looks_like_synthetic_id(order.order_id):
            logger.warning(
                "forecastex.cancel_order.synthetic_id_skip",
                order_id=order.order_id, status=order.status.value,
            )
            return False
        try:
            await self._client.cancel_order(order.order_id)
            return True
        except Exception as exc:
            logger.warning(
                "forecastex.cancel_order.failed",
                order_id=order.order_id, err=str(exc),
            )
            return False

    async def cancel_all(self) -> list[str]:
        try:
            return await self._client.cancel_all_open_orders()
        except Exception as exc:
            logger.warning("forecastex.cancel_all.failed", err=str(exc))
            return []

    async def list_open_orders_by_client_id(
        self, client_order_id_prefix: str,
    ) -> list[Order]:
        # IBKR has a ``cOID`` field for client-side IDs but the gateway does
        # not expose a query-by-prefix endpoint. Return an empty list to
        # match the Polymarket US adapter behaviour; the engine's startup
        # reconciliation will resync on next status poll.
        logger.warning(
            "forecastex.list_open_orders_by_client_id.unsupported",
            prefix=client_order_id_prefix,
        )
        return []

    # ─── Depth / executable-price helpers ─────────────────────────────────

    async def check_depth(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        """Best-effort depth check using the snapshot bid/ask sizes.

        IBKR exposes a separate ``iserver/marketdata/history`` endpoint for
        full-depth books, but the runtime cost (multiple round-trips per
        market per scan) outweighs the benefit on top-of-book ForecastEx
        liquidity. Use the snapshot's bid/ask size and treat any size at the
        touch as sufficient for the current ask.

        ForecastEx YES and NO have SEPARATE conids — the caller MUST pass
        the conid for the side it intends to BUY (NO conid for NO leg,
        YES conid for YES leg). The function fetches that conid's own
        ask, which is the executable price; we no longer subtract from
        1.0 to synthesize a NO price from the YES bid (root cause of the
        ARB-000695/699 phantom edge).
        """
        # CONID INTEGRITY GATE (phantom-trade postmortem 2026-05-28).
        normalized_market_id = str(market_id or "").strip()
        if not normalized_market_id or normalized_market_id in ("0", "None"):
            logger.warning(
                "forecastex.check_depth.empty_market_id",
                market_id=repr(market_id), side=side,
                required_qty=required_qty,
            )
            return (False, 0.0)
        try:
            snap = await self._client.market_snapshot(normalized_market_id)
        except Exception as exc:
            logger.warning(
                "forecastex.check_depth.failed",
                market_id=normalized_market_id, err=str(exc),
            )
            return (False, 0.0)
        if not isinstance(snap, dict):
            return (False, 0.0)
        bid = self._snap_dollar(snap.get("84"))
        ask = self._snap_dollar(snap.get("86"))
        # We are always BUYing on this venue, so size we care about is the
        # ASK side regardless of whether the leg is YES or NO (the NO leg
        # buys the NO conid, but the conid's own ASK is what fills it).
        try:
            size = float(snap.get("7296") or 0.0)
        except (TypeError, ValueError):
            size = 0.0
        best = ask if ask > 0 else bid
        if best <= 0:
            return (False, 0.0)
        # IBKR Client Portal does NOT broadcast 7295/7296 (bid/ask size) for
        # ForecastEx binary political contracts — field returns 0 even when
        # the book has real depth (see settings.py min_liquidity_by_pair
        # comment + 2026-05-27 audit). Scanner-side workaround set
        # MIN_LIQUIDITY_FORECASTEX_*=0; executor-side depth check needed the
        # symmetric treatment or every K×FX / P×FX opp gets skipped
        # depth_low forever. Trust the touch when size is unbroadcast and
        # let IOC primary + 50% partial-fill scaling (FILL-02) + MAX_-
        # POSITION_USD cap absorb the depth-uncertainty risk.
        if size <= 0:
            logger.info(
                "forecastex.check_depth.trusted_touch",
                market_id=normalized_market_id,
                side=side,
                required_qty=required_qty,
                ask=best,
            )
            return (True, best)
        return (size >= float(required_qty), best)

    async def best_executable_price(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        """Single-level book: the best executable price IS the ask, and we
        accept the IOC partial-fill semantics for shortfalls."""
        sufficient, price = await self.check_depth(market_id, side, required_qty)
        return (sufficient, price)

    # ─── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _snap_to_tick(price: float, tick: float = _DEFAULT_TICK) -> float:
        clipped = max(_MIN_PRICE, min(float(price), _MAX_PRICE))
        snapped = round(clipped / tick) * tick
        return round(max(_MIN_PRICE, min(snapped, _MAX_PRICE)), 4)

    @staticmethod
    def _snap_dollar(value) -> float:
        """Coerce an IBKR-stringified price into [0, 1] dollar units."""
        if isinstance(value, dict):
            value = value.get("value")
        if isinstance(value, str):
            value = value.lstrip("CHc h").strip()
        try:
            f = float(value)
        except (TypeError, ValueError):
            return 0.0
        if f <= 0:
            return 0.0
        if f > 1.0:
            return max(0.0, min(f / 100.0, 1.0))
        return max(0.0, min(f, 1.0))

    @staticmethod
    def _map_status(api_status: str, default: OrderStatus) -> OrderStatus:
        # Normalize: uppercase AND strip underscores so we accept both the
        # CamelCase wire format IBKR actually emits (``PartiallyFilled``,
        # ``PreSubmitted``) and the snake_case form some docs show
        # (``partially_filled``). Failing to match silently downgrades to
        # FAILED below — a partial fill that the engine treats as a hard
        # failure would skip the soft-naked recovery path.
        normalized = (api_status or "").upper().replace("_", "")
        if normalized in {"FILLED", "EXECUTED"}:
            return OrderStatus.FILLED
        if normalized in {"PARTIALLYFILLED", "PARTIAL"}:
            return OrderStatus.PARTIAL
        if normalized in {
            "CANCELLED", "CANCELED",
            "EXPIRED", "INACTIVE",
            "REPLACED",
        }:
            return OrderStatus.CANCELLED
        if normalized in {"REJECTED"}:
            return OrderStatus.FAILED
        if normalized in {"SUBMITTED", "PRESUBMITTED", "PENDINGSUBMIT", "OPEN", "WORKING"}:
            return OrderStatus.SUBMITTED
        # Unknown wire status: behave like polymarket_us — fail loud if the
        # default we'd fall through to is SUBMITTED, because the engine then
        # treats the order as still-open.
        if default == OrderStatus.SUBMITTED:
            return OrderStatus.FAILED
        return default

    @staticmethod
    def _extract_broker_order_id(response: Any) -> Optional[str]:
        """Walk a ForecastEx/IBKR place-order response and return the
        broker-assigned order id, or None if no broker id is present.

        ARB-000768/ARB-000769 (2026-06-05): the previous extraction logic
        sat inline in ``_order_from_response`` and fell back to a synthetic
        ``ARB-...-FCST`` id whenever the response didn't carry any of the
        three keys we checked (``order_id``/``orderId``/``id``). That
        synthetic id then got threaded into every subsequent
        ``get_order``/``cancel_order`` call, which IBKR rightly rejected
        with HTTP 400 — locking the leg into "submitted, fill_qty=0" for
        the entire poll window and burning the FX trade.

        Shapes we accept here:

          1. Top-level dict ack:  ``{"order_id": "1370093173", ...}``
          2. items-wrapped list:  ``{"items": [{"order_id": "..."}]}``
             (``_request`` wraps top-level lists this way)
          3. ``orders`` wrapper:  ``{"orders": [{"order_id": "..."}]}``
          4. camelCase:           ``{"orderId": "..."}``
          5. Reply-chain ``id``:  ``{"id": "..."}`` — only when no
             ``message`` field is present (a confirmation prompt's ``id``
             is a reply UUID, not an order id, and is handled upstream by
             ``_answer_reply_chain``).
          6. Singleton list:      ``[{"order_id": "..."}]`` (raw, in case
             a caller hands us the unwrapped form).
        """
        if response is None:
            return None
        # Singleton list shape (raw, unwrapped by _request).
        if isinstance(response, list):
            return ForecastExAdapter._extract_broker_order_id(
                response[0] if response else None
            )
        if not isinstance(response, dict):
            return None

        # Direct hit on the standard ack keys.
        for key in ("order_id", "orderId"):
            value = response.get(key)
            if value not in (None, "", 0, "0"):
                return str(value)

        # ``id`` is ambiguous: it doubles as the reply-UUID on
        # confirmation prompts. Only accept it when there's no
        # ``message`` field (which would mark it as a prompt).
        if response.get("id") and not response.get("message"):
            return str(response["id"])

        # Nested under ``items`` (the wrap _request applies to top-level
        # lists) or ``orders`` (some gateway versions).
        for wrapper_key in ("items", "orders"):
            wrapped = response.get(wrapper_key)
            if isinstance(wrapped, list) and wrapped:
                nested = ForecastExAdapter._extract_broker_order_id(wrapped[0])
                if nested:
                    return nested

        return None

    async def _lookup_broker_id_via_live_orders(
        self,
        *,
        market_id: str,
        normalized_side: str,
        qty: int,
        placed_after_ts: float,
    ) -> Optional[str]:
        """Fallback: if the place-order ack didn't carry a broker id, ask
        IBKR for the live-orders list and find the order we just placed
        by conid + side + size, preferring the most recent match.

        Without this, an empty/malformed ack would leave a live order at
        IBKR that we have no handle to query or cancel — the exact failure
        mode that broke ARB-000768. We accept a one-shot extra round-trip
        as the safety belt because the alternative is naked exposure.
        """
        if not hasattr(self._client, "list_live_orders"):
            return None
        try:
            live = await self._client.list_live_orders()
        except Exception as exc:
            logger.warning(
                "forecastex.broker_id_lookup.list_live_orders_failed",
                err=str(exc),
            )
            return None
        if not isinstance(live, list):
            return None

        target_conid = str(market_id).strip()
        best_id: Optional[str] = None
        best_ts: float = -1.0
        for entry in live:
            if not isinstance(entry, dict):
                continue
            entry_conid = str(
                entry.get("conid") or entry.get("conidEx") or ""
            ).split("@", 1)[0].strip()
            if entry_conid != target_conid:
                continue
            entry_side = str(entry.get("side") or "").upper()
            if entry_side and entry_side != normalized_side:
                continue
            # IBKR may report total size as ``totalSize``, ``size``, or
            # ``quantity`` depending on the gateway version. Match on the
            # union — qty mismatch ≠ definitively a different order.
            entry_qty = (
                entry.get("totalSize")
                or entry.get("size")
                or entry.get("quantity")
            )
            try:
                if entry_qty is not None and int(float(entry_qty)) != int(qty):
                    continue
            except (TypeError, ValueError):
                pass
            # Prefer the most recent placement: ``lastExecutionTime_r`` is
            # epoch-millis on IBKR; ``lastExecutionTime`` is the same in a
            # different unit. Accept whichever shows up.
            try:
                entry_ts = float(
                    entry.get("lastExecutionTime_r")
                    or entry.get("lastExecutionTime")
                    or 0.0
                )
            except (TypeError, ValueError):
                entry_ts = 0.0
            # IBKR ms-epoch fields will be ≫ our `placed_after_ts` seconds
            # value; normalize the comparison so both are seconds.
            if entry_ts > 1e12:
                entry_ts = entry_ts / 1000.0
            if placed_after_ts and entry_ts and entry_ts + 5.0 < placed_after_ts:
                # This live order predates our placement — not ours.
                continue
            entry_order_id = str(
                entry.get("orderId") or entry.get("order_id") or ""
            ).strip()
            if not entry_order_id:
                continue
            if entry_ts >= best_ts:
                best_ts = entry_ts
                best_id = entry_order_id
        if best_id:
            logger.info(
                "forecastex.broker_id_lookup.recovered_via_live_orders",
                market_id=market_id, side=normalized_side, qty=qty,
                broker_order_id=best_id,
            )
        return best_id

    def _order_from_response(
        self,
        response: dict,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        broker_order_id: str,
        invert_fill_price: bool = False,
    ) -> Order:
        """Build an ``Order`` from the venue ack using the broker id that
        the caller already extracted (or recovered via the live-orders
        fallback). The caller is responsible for ensuring
        ``broker_order_id`` is the IBKR-assigned id — never an internal
        synthetic id — because every subsequent ``get_order``/``cancel_order``
        round-trip threads it back to IBKR verbatim.
        """
        now = time.time()
        if not isinstance(response, dict):
            response = {}

        # Walk the same wrappers as ``_extract_broker_order_id`` so we
        # read order_status / fill fields from the inner ack, not the
        # outer items wrapper.
        items = response.get("items") if isinstance(response, dict) else None
        if isinstance(items, list) and items:
            response = items[0] if isinstance(items[0], dict) else response
        orders_wrap = response.get("orders") if isinstance(response, dict) else None
        if isinstance(orders_wrap, list) and orders_wrap:
            response = orders_wrap[0] if isinstance(orders_wrap[0], dict) else response

        api_status = str(
            response.get("order_status")
            or response.get("orderStatus")
            or response.get("status")
            or "SUBMITTED"
        ).upper()
        mapped = self._map_status(api_status, OrderStatus.SUBMITTED)

        try:
            fill_qty = float(
                response.get("filled_quantity")
                or response.get("filledQuantity")
                or response.get("cumQty")
                or 0.0
            )
        except (TypeError, ValueError):
            fill_qty = 0.0
        try:
            avg_px = float(
                response.get("avg_price")
                or response.get("avgPrice")
                or response.get("avgFillPrice")
                or response.get("average_price")
                or 0.0
            )
        except (TypeError, ValueError):
            avg_px = 0.0
        normalized_avg = avg_px if avg_px <= 1.0 else avg_px / 100.0
        if normalized_avg and invert_fill_price:
            # Cross-side exit: the venue fill is a sibling BUY price;
            # 1 - it is the effective sell price the engine reasons in.
            normalized_avg = round(1.0 - normalized_avg, 4)
        fill_price = normalized_avg or price
        # BUG #1 (same pattern as polymarket_us): if the venue gives us an
        # explicit zero fill quantity but a FILLED status, downgrade to
        # CANCELLED so the engine does not fire a secondary leg on a phantom
        # primary fill. Only triggered on an *explicit* zero key — bare
        # legacy "FILLED" responses with no fill-qty key keep their existing
        # backfill semantics. The key list must cover every spelling the
        # qty parser reads, or an explicit 0 under a parsed-but-unlisted key
        # slips into the backfill and fabricates a full fill.
        explicit_zero_fill = (
            mapped == OrderStatus.FILLED
            and fill_qty == 0.0
            and (
                "filled_qty" in response
                or "filledQty" in response
                or "filled_quantity" in response
                or "filledQuantity" in response
                or "cumQty" in response
                or "cum_fill" in response
            )
        )
        if explicit_zero_fill:
            mapped = OrderStatus.CANCELLED
        elif mapped == OrderStatus.FILLED and fill_qty == 0.0:
            fill_qty = float(qty)

        return Order(
            order_id=broker_order_id,
            platform="forecastex",
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=int(qty),
            status=mapped,
            fill_price=fill_price,
            fill_qty=int(fill_qty),
            timestamp=now,
            external_client_order_id=None,
        )
