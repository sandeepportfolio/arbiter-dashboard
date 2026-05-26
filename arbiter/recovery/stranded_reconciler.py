"""StrandedPositionReconciler — periodic venue-truth audit + cleanup.

Background service that closes the operator-visible gap between what
the engine THINKS exists and what the venues actually hold. Without
this the system can carry indefinite naked positions left over from
crashed sessions, race-condition recoveries that dropped state, or
adapter exceptions during unwind — exactly the 25-lot stranded
inventory the FILL-01 audit surfaced on 2026-05-24.

Design (per the audit + arbitrage best-practice research):

  1. Every ``interval_s`` seconds, fetch the live venue position list
     from each enabled adapter (Kalshi, Polymarket US, ForecastEx).
  2. For each non-zero position, classify it:
       - tracked: matches a runtime exposure the engine is actively
         managing (an open arb leg or in-flight unwind)
       - stranded: present on the venue but unrecognised by the
         runtime — the bug signal we care about
  3. Emit a ``stranded_position`` incident the first time each
     stranded lot is observed (deduped by canonical/ticker so a 5-min
     loop does not spam the operator).
  4. If ``auto_close`` is True AND the position passes the safety
     gate (small notional, market is reasonably liquid, spread within
     tolerance), submit an unwind via the adapter's ``place_unwind_
     sell``. Otherwise leave it for manual review with the structured
     incident already raised.
  5. Maintain a ``last_snapshot`` so /api/system + the ops console
     can render the table without re-querying the venues.

Conservative defaults: ``auto_close`` is OFF by default, requires
explicit ``STRANDED_AUTO_CLOSE=true`` env var. We bias toward visible
incidents (operator review) over silent close attempts, since a
mis-classified hedge leg auto-closed at panic price would destroy
real cross-platform coverage.

References: arbitrage best-practice research 2026-05-24
  - InsiderSignal.ai: centralized naked-leg tracker; halt-if-naked
    propagation across engines.
  - DEV Community Polymarket binary hedging: separate naked-leg
    reconciliation loop from main scanner so a hung venue doesn't
    block opportunity flow.
  - clawarbs.com: hit-rate gap (10-20% naive vs 70-85% execution-
    aware) is mostly driven by missed reconciliation, not detection.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Sequence

import aiohttp
import structlog

logger = structlog.get_logger("arbiter.recovery.stranded_reconciler")


@dataclass
class StrandedPosition:
    """A single non-zero venue position the reconciler observed."""

    platform: str
    market_id: str  # ticker (kalshi) or slug (polymarket)
    side: str  # YES or NO
    qty: float
    cost_basis_usd: float
    mtm_usd: float
    unrealized_usd: float
    best_bid: float
    best_ask: float
    title: str
    first_seen_ts: float
    last_seen_ts: float
    # Market expiry as a unix timestamp. None when the venue did not
    # publish a settlement time on the position record — we then fall
    # back to "treat as far from expiry" so the let-settle path doesn't
    # fire on unknown-dated lots.
    expiry_ts: Optional[float] = None
    auto_close_attempted: bool = False
    auto_close_result: Optional[str] = None
    # Operator-facing recommendation produced by ``classify_action``.
    # One of: ``let_settle`` | ``close_market`` | ``manual_review``
    # | ``closed`` | ``no_adapter`` | ``illiquid_no_bbo``
    # | ``spread_too_wide`` | ``large_notional`` | ``unknown``.
    # Persists across cycles so the UI can render the table without
    # waiting for a fresh classify pass.
    mitigation_action: str = "unknown"
    # Full decision dict emitted by the MitigationEngine — carries the
    # profitability snapshot, the structured action (COMPLETE_ARB /
    # HOLD_TO_SETTLE / CLOSE_NOW / HEDGE / MANUAL_REVIEW), the
    # rationale, and (when applicable) the missing-leg venue + price
    # for an arb completion. Plumbed straight through to the API +
    # ops.html so the operator sees the full reasoning chain.
    mitigation_decision: Optional[Dict[str, Any]] = None

    @property
    def cost_per_share(self) -> float:
        q = abs(float(self.qty or 0.0))
        if q <= 0:
            return 0.0
        return abs(float(self.cost_basis_usd or 0.0)) / q

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "market_id": self.market_id,
            "side": self.side,
            "qty": self.qty,
            "cost_basis_usd": round(self.cost_basis_usd, 4),
            "cost_per_share": round(self.cost_per_share, 4),
            "mtm_usd": round(self.mtm_usd, 4),
            "unrealized_usd": round(self.unrealized_usd, 4),
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "title": self.title,
            "first_seen_ts": self.first_seen_ts,
            "last_seen_ts": self.last_seen_ts,
            "expiry_ts": self.expiry_ts,
            "seconds_to_expiry": (
                round(self.expiry_ts - time.time(), 1)
                if self.expiry_ts is not None else None
            ),
            "age_seconds": round(time.time() - self.first_seen_ts, 1),
            "auto_close_attempted": self.auto_close_attempted,
            "auto_close_result": self.auto_close_result,
            "mitigation_action": self.mitigation_action,
            "mitigation_decision": self.mitigation_decision,
        }


@dataclass
class ReconcilerSnapshot:
    """One reconciler-cycle result, exposed via /api/system."""

    timestamp: float
    cycle_count: int
    stranded_count: int
    stranded: List[Dict[str, Any]]
    duration_ms: float
    errors: List[str]
    auto_close_enabled: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cycle_count": self.cycle_count,
            "stranded_count": self.stranded_count,
            "stranded": self.stranded,
            "duration_ms": round(self.duration_ms, 1),
            "errors": self.errors,
            "auto_close_enabled": self.auto_close_enabled,
        }


class StrandedPositionReconciler:
    """Periodic background task that audits venue truth vs engine state."""

    def __init__(
        self,
        *,
        config,
        adapters: Optional[Dict[str, Any]] = None,
        engine: Optional[Any] = None,
        forecastex_client: Optional[Any] = None,
        price_store: Optional[Any] = None,
        market_map_provider: Optional[Any] = None,
        interval_s: float = 300.0,
        auto_close: bool = False,
        max_auto_close_notional_usd: float = 30.0,
        max_auto_close_spread_bps: float = 1000.0,  # 10¢ wide max
        penny_cost_threshold_usd: float = 0.05,
        let_settle_window_seconds: float = 7 * 24 * 3600.0,
        mitigation_engine: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._adapters = adapters or {}
        self._engine = engine
        self._forecastex_client = forecastex_client
        self._price_store = price_store
        self._market_map_provider = market_map_provider
        self._interval_s = max(30.0, float(interval_s))
        self._auto_close = bool(auto_close)
        self._max_auto_close_notional_usd = float(max_auto_close_notional_usd)
        self._max_auto_close_spread_bps = float(max_auto_close_spread_bps)
        # Lazy-construct the mitigation engine if the caller didn't
        # supply one; gives test paths a quick override hook while
        # production wiring just passes price_store + market_map.
        if mitigation_engine is None and price_store is not None:
            from arbiter.recovery.mitigation_engine import (
                MitigationConfig as _MitConfig, MitigationEngine as _MitEng,
            )
            self._mitigation_engine = _MitEng(
                price_store=price_store,
                market_map_provider=(
                    market_map_provider
                    or (lambda: __import__(
                        "arbiter.config.settings", fromlist=["MARKET_MAP"]
                    ).MARKET_MAP)
                ),
                adapters=self._adapters,
                config=_MitConfig(
                    penny_cost_threshold_usd=float(penny_cost_threshold_usd),
                    max_autonomous_notional_usd=float(max_auto_close_notional_usd),
                    max_autonomous_spread_bps=float(max_auto_close_spread_bps),
                ),
            )
        else:
            self._mitigation_engine = mitigation_engine
        # Penny-position protection: lots whose per-share cost is at or
        # below this threshold get the "let_settle" recommendation
        # instead of an auto-close attempt. Closing a 23-share lot of
        # 2¢ Hart-trophy NHL longshots on Kalshi costs ~$1 in fees and
        # captures ~$0.46 of value — strictly negative-EV vs holding
        # through settlement (binary loses, we recoup $0; binary wins,
        # we get $1 per share which the close would have foregone). The
        # 5¢ floor catches every Hart/Vezina/etc. longshot in the
        # current strand corpus and excludes everything ≥6¢ where
        # closing IS rational.
        self._penny_cost_threshold_usd = max(0.0, float(penny_cost_threshold_usd))
        # Within this window before expiry, penny positions are
        # auto-classified as "let_settle" regardless of liquidity. The
        # outer ``let_settle`` recommendation also fires for unknown-
        # expiry penny positions (we don't have a date to compare to).
        self._let_settle_window_seconds = max(0.0, float(let_settle_window_seconds))
        self._cycle_count: int = 0
        # Persistent map keyed by (platform, market_id) so dedup survives
        # across cycles — the first_seen_ts only resets when the position
        # disappears off the venue.
        self._tracked: Dict[tuple, StrandedPosition] = {}
        self._last_snapshot: Optional[ReconcilerSnapshot] = None
        self._history: Deque[ReconcilerSnapshot] = deque(maxlen=20)
        self._stopped: bool = False
        self._task: Optional[asyncio.Task] = None

    @property
    def last_snapshot(self) -> Optional[ReconcilerSnapshot]:
        return self._last_snapshot

    @property
    def tracked(self) -> Dict[tuple, StrandedPosition]:
        return self._tracked

    # ─── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the periodic reconciler loop as a background task.

        The loop survives single-cycle errors (logged + swallowed) so a
        venue outage cannot kill reconciliation permanently. Cancel via
        ``stop()``.
        """
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run_loop(self) -> None:
        # Initial sleep so we don't compete with collector cold-start
        # for HTTP slots.
        await asyncio.sleep(30.0)
        while not self._stopped:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("stranded_reconciler.cycle_failed", err=str(exc))
            await asyncio.sleep(self._interval_s)

    # ─── single-cycle entry point (also used by tests) ────────────────────

    async def run_once(self) -> ReconcilerSnapshot:
        t0 = time.monotonic()
        self._cycle_count += 1
        errors: List[str] = []
        observed: Dict[tuple, StrandedPosition] = {}
        # Platforms whose fetch raised this cycle. We MUST NOT prune
        # previously-tracked positions for these platforms — a venue
        # outage would otherwise wipe the tracker, and the next
        # successful cycle would re-classify every still-stranded lot
        # as "new" and re-emit an incident + Telegram alert per lot.
        # This is the dedup invariant the operator relies on.
        failed_platforms: set = set()

        # Each venue contributes its observed positions. Failures on one
        # venue must not block the others — best-effort scan.
        for platform, fetch in (
            ("kalshi", self._fetch_kalshi_positions),
            ("polymarket", self._fetch_polymarket_us_positions),
            ("forecastex", self._fetch_forecastex_positions),
        ):
            try:
                for pos in await fetch():
                    key = (pos.platform, pos.market_id)
                    observed[key] = pos
            except Exception as exc:
                err = f"{fetch.__name__}: {exc}"
                logger.warning("stranded_reconciler.fetch_failed", err=err)
                errors.append(err)
                failed_platforms.add(platform)

        now = time.time()
        # Merge with persistent tracker so first_seen_ts stays stable.
        new_keys: List[tuple] = []
        for key, pos in observed.items():
            existing = self._tracked.get(key)
            if existing is not None:
                # Keep original first_seen_ts; refresh mtm and bid/ask.
                pos.first_seen_ts = existing.first_seen_ts
                pos.auto_close_attempted = existing.auto_close_attempted
                pos.auto_close_result = existing.auto_close_result
            else:
                new_keys.append(key)
            pos.last_seen_ts = now
            self._tracked[key] = pos

        # Prune positions that vanished from the venue (closed / settled).
        # Skip pruning for platforms whose fetch failed this cycle — those
        # positions are not "vanished", we just don't have fresh truth.
        for key in list(self._tracked.keys()):
            platform = key[0]
            if platform in failed_platforms:
                continue
            if key not in observed:
                del self._tracked[key]

        # Refresh classification on EVERY position EVERY cycle so the
        # ops UI shows the current recommendation even when auto-close
        # is OFF (operator observation mode). Idempotent — pure read
        # plus dict assignment.
        for pos in list(self._tracked.values()):
            try:
                pos.mitigation_action = self.classify_action(pos)
            except Exception as exc:
                logger.debug(
                    "stranded_reconciler.classify_failed",
                    platform=pos.platform, market_id=pos.market_id, err=str(exc),
                )

        # Run the sophisticated MitigationEngine on every position so
        # the ops UI sees the full cost-benefit chain regardless of
        # whether auto-close is enabled. Engine output sets the
        # mitigation_action label too (overriding the legacy classify
        # output) so the action badge matches the engine's decision.
        if self._mitigation_engine is not None:
            for pos in list(self._tracked.values()):
                try:
                    decision = await self._mitigation_engine.decide(pos)
                    pos.mitigation_decision = decision.to_dict()
                    # Map engine action → legacy action label so the
                    # existing ops UI badge keeps working.
                    pos.mitigation_action = decision.action.lower()
                except Exception as exc:
                    logger.warning(
                        "stranded_reconciler.mitigation_engine_failed",
                        platform=pos.platform, market_id=pos.market_id,
                        err=str(exc),
                    )

        # Emit incident only for new lots that need operator attention.
        # Autonomous-OK decisions (HOLD_TO_SETTLE penny lots,
        # CLOSE_NOW within auto-cap, COMPLETE_ARB within auto-cap)
        # mitigate silently — the operator already gets the action
        # outcome via the ops dashboard and Telegram only fires on
        # decisions that genuinely need their eyes (MANUAL_REVIEW,
        # HEDGE, oversized CLOSE_NOW). Reduces the 25-position
        # Telegram burst that the 2026-05-26 cycle generated from
        # 25 alerts → ~2 alerts.
        for key in new_keys:
            pos = self._tracked[key]
            dec = pos.mitigation_decision or {}
            action_upper = str(dec.get("action") or "").upper()
            autonomous_ok = bool(dec.get("autonomous_ok"))
            # Always alert on unclassified positions (engine wasn't
            # wired) — defensive against silent loss of visibility.
            if not action_upper:
                await self._emit_stranded_incident(pos)
                continue
            # Silent mitigation when the engine has a confident
            # autonomous recommendation. Operator still sees it on
            # the dashboard; no Telegram burst.
            if autonomous_ok and action_upper in (
                "HOLD_TO_SETTLE", "CLOSE_NOW", "COMPLETE_ARB",
            ):
                logger.info(
                    "stranded_reconciler.silent_mitigation",
                    platform=pos.platform,
                    market_id=pos.market_id,
                    action=action_upper,
                    rationale=str(dec.get("rationale") or "")[:160],
                )
                continue
            await self._emit_stranded_incident(pos)

        # Auto-mitigation pass — uses the engine's decision when
        # auto_close is enabled. Only autonomous_ok decisions execute
        # silently; the rest leave an incident for operator review.
        if self._auto_close:
            for pos in list(self._tracked.values()):
                if pos.auto_close_attempted:
                    continue
                if self._mitigation_engine is not None and pos.mitigation_decision:
                    await self._execute_decision(pos)
                else:
                    # Fallback to the legacy single-shot close gate
                    # (kept for tests + dev environments that don't
                    # wire the engine).
                    await self._maybe_auto_close(pos)

        duration_ms = (time.monotonic() - t0) * 1000.0
        snapshot = ReconcilerSnapshot(
            timestamp=now,
            cycle_count=self._cycle_count,
            stranded_count=len(self._tracked),
            stranded=[p.to_dict() for p in self._tracked.values()],
            duration_ms=duration_ms,
            errors=errors,
            auto_close_enabled=self._auto_close,
        )
        self._last_snapshot = snapshot
        self._history.append(snapshot)
        logger.info(
            "stranded_reconciler.cycle_complete",
            cycle=self._cycle_count,
            stranded=len(self._tracked),
            new=len(new_keys),
            duration_ms=round(duration_ms, 1),
        )
        return snapshot

    # ─── venue fetchers ───────────────────────────────────────────────────

    async def _fetch_kalshi_positions(self) -> List[StrandedPosition]:
        """Read Kalshi /portfolio/positions and join market BBO."""
        from arbiter.collectors.kalshi import KalshiAuth

        cfg = self._config
        kcfg = cfg.kalshi
        if not getattr(kcfg, "api_key_id", "") or not getattr(kcfg, "private_key_path", ""):
            return []
        auth = KalshiAuth(kcfg.api_key_id, kcfg.private_key_path)
        base = kcfg.base_url.rstrip("/")
        out: List[StrandedPosition] = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20.0)) as session:
            try:
                h = auth.get_headers("GET", "/trade-api/v2/portfolio/positions")
                async with session.get(
                    base + "/portfolio/positions?limit=500", headers=h
                ) as r:
                    text = await r.text()
                    data = json.loads(text)
            except Exception as exc:
                raise RuntimeError(f"kalshi.portfolio fetch: {exc}") from exc
            for p in data.get("market_positions", []) or []:
                qty = float(p.get("position_fp", 0) or 0)
                if qty == 0:
                    continue
                ticker = p["ticker"]
                yes_bid = yes_ask = 0.0
                title = ""
                expiry_ts: Optional[float] = None
                try:
                    mh = auth.get_headers("GET", f"/trade-api/v2/markets/{ticker}")
                    async with session.get(
                        base + f"/markets/{ticker}", headers=mh
                    ) as mr:
                        mkt = json.loads(await mr.text()).get("market", {}) or {}
                        yes_bid = float(mkt.get("yes_bid", 0) or 0) / 100.0
                        yes_ask = float(mkt.get("yes_ask", 0) or 0) / 100.0
                        title = (mkt.get("title") or "")[:80]
                        # Kalshi exposes ``close_time`` (ISO-8601) on the
                        # market detail response. Convert to unix ts so
                        # the classifier can decide "near expiry" without
                        # re-parsing dates downstream.
                        close_iso = (
                            mkt.get("close_time")
                            or mkt.get("expiration_time")
                            or ""
                        )
                        if close_iso:
                            from datetime import datetime
                            try:
                                expiry_ts = datetime.fromisoformat(
                                    close_iso.replace("Z", "+00:00")
                                ).timestamp()
                            except (TypeError, ValueError):
                                expiry_ts = None
                except Exception:
                    # BBO lookup failure is non-fatal; we still report
                    # the position with empty quote fields so the
                    # operator sees it.
                    pass
                cost = float(p.get("total_traded_dollars", 0) or 0)
                mtm = qty * yes_bid
                out.append(
                    StrandedPosition(
                        platform="kalshi",
                        market_id=ticker,
                        side="YES" if qty > 0 else "NO",
                        qty=qty,
                        cost_basis_usd=cost,
                        mtm_usd=mtm,
                        unrealized_usd=mtm - cost,
                        best_bid=yes_bid,
                        best_ask=yes_ask,
                        title=title,
                        first_seen_ts=time.time(),
                        last_seen_ts=time.time(),
                        expiry_ts=expiry_ts,
                    )
                )
        return out

    async def _fetch_polymarket_us_positions(self) -> List[StrandedPosition]:
        """Read Polymarket US /portfolio/positions and join BBO."""
        from arbiter.auth.ed25519_signer import Ed25519Signer
        from arbiter.collectors.polymarket_us import (
            PolymarketUSClient,
            _amount_value,
        )
        from arbiter.config.settings import PolymarketUSConfig

        cfg = self._config
        pu = cfg.polymarket
        if not isinstance(pu, PolymarketUSConfig):
            return []
        if not (pu.api_key_id and pu.api_secret):
            return []
        signer = Ed25519Signer(key_id=pu.api_key_id, secret_b64=pu.api_secret)
        client = PolymarketUSClient(
            base_url=pu.api_url,
            public_base_url=pu.gateway_url,
            signer=signer,
        )
        out: List[StrandedPosition] = []
        try:
            resp = await client._signed("GET", "/portfolio/positions")
            positions = (resp or {}).get("positions", {}) or {}
            for slug, p in positions.items():
                net = int(p.get("netPosition", 0) or 0)
                if net == 0:
                    continue
                cost = float((p.get("cost") or {}).get("value", 0) or 0)
                cv = float((p.get("cashValue") or {}).get("value", 0) or 0)
                md = p.get("marketMetadata", {}) or {}
                title = (md.get("title") or "")[:80]
                # Polymarket exposes ``endDate`` as ISO-8601 on the
                # marketMetadata block. Parse it for the classifier.
                expiry_ts: Optional[float] = None
                end_iso = md.get("endDate") or md.get("endDateIso") or ""
                if end_iso:
                    try:
                        from datetime import datetime
                        expiry_ts = datetime.fromisoformat(
                            str(end_iso).replace("Z", "+00:00")
                        ).timestamp()
                    except (TypeError, ValueError):
                        expiry_ts = None
                yes_bid = yes_ask = 0.0
                try:
                    book = await client.get_market_book(slug)
                    book_md = book.get("marketData", book) if isinstance(book, dict) else {}
                    bids = book_md.get("bids") or []
                    offers = book_md.get("offers") or []
                    yes_bid = _amount_value((bids[0] or {}).get("px")) if bids else 0.0
                    yes_ask = _amount_value((offers[0] or {}).get("px")) if offers else 0.0
                except Exception:
                    pass
                out.append(
                    StrandedPosition(
                        platform="polymarket",
                        market_id=slug,
                        side="NO" if net < 0 else "YES",
                        qty=float(net),
                        cost_basis_usd=cost,
                        mtm_usd=cv,
                        unrealized_usd=cv - cost,
                        best_bid=yes_bid,
                        best_ask=yes_ask,
                        title=title,
                        first_seen_ts=time.time(),
                        last_seen_ts=time.time(),
                        expiry_ts=expiry_ts,
                    )
                )
        finally:
            await client.close()
        return out

    async def _fetch_forecastex_positions(self) -> List[StrandedPosition]:
        """Read IBKR portfolio positions and surface FORECASTX lots.

        The IBKR portfolio endpoint mixes ForecastEx contracts with
        non-FORECASTX inventory (the user may also hold equities), so
        we filter by ``listingExchange`` / ``contractDesc`` containing
        the FORECASTX marker. The conid → BBO snapshot reuses the
        same ``market_snapshot`` path the collector uses.
        """
        client = self._forecastex_client
        if client is None or not getattr(client, "account_id", ""):
            return []
        try:
            positions = await client.positions()
        except Exception as exc:
            raise RuntimeError(f"forecastex.positions fetch: {exc}") from exc
        if not positions:
            return []
        out: List[StrandedPosition] = []
        for p in positions:
            if not isinstance(p, dict):
                continue
            # Filter to FORECASTX inventory only — the user may hold
            # other IBKR products in the same account.
            exchange = str(p.get("listingExchange") or p.get("exchange") or "").upper()
            desc = str(p.get("contractDesc") or p.get("name") or "").upper()
            if "FORECASTX" not in exchange and "FORECASTX" not in desc:
                continue
            try:
                net = float(p.get("position") or 0)
            except (TypeError, ValueError):
                net = 0.0
            if net == 0:
                continue
            conid = str(p.get("conid") or "").strip()
            if not conid:
                continue
            # Cost basis: IBKR reports ``avgCost`` per contract (in
            # dollars). FORECASTX child contracts settle to $1 so a
            # 0.55 avgCost on 10 shares means $5.50 cost basis.
            try:
                avg_cost = float(p.get("avgCost") or 0)
            except (TypeError, ValueError):
                avg_cost = 0.0
            qty_abs = abs(net)
            cost = avg_cost * qty_abs
            mtm_per_share = float(p.get("mktPrice") or 0) or avg_cost
            mtm = mtm_per_share * qty_abs
            best_bid = best_ask = 0.0
            try:
                snap = await client.market_snapshot(conid)
                # 84 = bid, 86 = ask in IBKR's field-code dialect.
                bid_raw = snap.get("84") if isinstance(snap, dict) else None
                ask_raw = snap.get("86") if isinstance(snap, dict) else None
                if bid_raw is not None:
                    try:
                        best_bid = float(str(bid_raw).lstrip("C").strip())
                    except (TypeError, ValueError):
                        best_bid = 0.0
                if ask_raw is not None:
                    try:
                        best_ask = float(str(ask_raw).lstrip("C").strip())
                    except (TypeError, ValueError):
                        best_ask = 0.0
            except Exception:
                pass
            out.append(
                StrandedPosition(
                    platform="forecastex",
                    market_id=conid,
                    side="YES" if net > 0 else "NO",
                    qty=float(net),
                    cost_basis_usd=cost,
                    mtm_usd=mtm,
                    unrealized_usd=mtm - cost,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    title=desc[:80],
                    first_seen_ts=time.time(),
                    last_seen_ts=time.time(),
                )
            )
        return out

    # ─── incident + auto-close ────────────────────────────────────────────

    @staticmethod
    def _strand_arb_id(platform: str, market_id: str) -> str:
        """Build a synthetic arb_id for the stranded-position incident that
        fits the ExecutionStore.execution_incidents.arb_id ``varchar(40)``
        column. ``STRAND-{venue}-{12-hex-of-market-id}`` is unique,
        deterministic across cycles (so dedup keeps working), and ≤ 32
        characters even on the longest venue prefix.

        The market_id is hashed (truncated SHA-1) instead of truncated
        verbatim because two slugs sharing the first 20 chars (common for
        sport-of-the-day markets like aec-mlb-PIT-TOR-2026-05-24 vs
        aec-mlb-PIT-TOR-2026-05-25) would otherwise collide on dedup.
        SHA-1 is fine here — we're not signing anything, just bucketing.
        """
        import hashlib
        venue = (platform or "UNK").upper()[:8]
        digest = hashlib.sha1(market_id.encode("utf-8")).hexdigest()[:12]
        return f"STRAND-{venue}-{digest}"

    async def _emit_stranded_incident(self, pos: StrandedPosition) -> None:
        """Surface a new stranded lot as a warning incident the engine
        already knows how to route to Telegram + the ops UI."""
        engine = self._engine
        if engine is None or not hasattr(engine, "_record_incident"):
            return
        try:
            # _record_incident wants an opportunity; we don't have one
            # for a stranded position, so we synthesise the minimum:
            # canonical_id = the market id, description = title.
            class _StubOpp:
                canonical_id = pos.market_id
                description = pos.title or pos.market_id

                def to_dict(self):
                    return {
                        "canonical_id": pos.market_id,
                        "description": pos.title or pos.market_id,
                    }

            await engine._record_incident(
                self._strand_arb_id(pos.platform, pos.market_id),
                _StubOpp(),
                "warning",
                f"Stranded position detected on {pos.platform}: "
                f"{abs(int(pos.qty))} {pos.side} @ avg ${pos.cost_basis_usd / abs(pos.qty):.4f} per contract"
                if pos.qty else "Stranded zero-qty marker",
                metadata={
                    "event_type": "stranded_position",
                    "platform": pos.platform,
                    "market_id": pos.market_id,
                    "side": pos.side,
                    "qty": pos.qty,
                    "cost_basis_usd": pos.cost_basis_usd,
                    "mtm_usd": pos.mtm_usd,
                    "unrealized_usd": pos.unrealized_usd,
                    "best_bid": pos.best_bid,
                    "best_ask": pos.best_ask,
                    "title": pos.title,
                },
            )
        except Exception as exc:
            logger.warning(
                "stranded_reconciler.incident_emit_failed",
                platform=pos.platform,
                market_id=pos.market_id,
                err=str(exc),
            )

    async def _execute_decision(self, pos: StrandedPosition) -> None:
        """Execute a MitigationEngine decision against the live venues.

        Decision actions map to concrete venue calls:
          - COMPLETE_ARB    → place_fok on the missing-leg adapter
                              (BUY for the canonical opposite side).
                              If it fills, the original stranded leg
                              becomes part of a real arb and we record
                              that fact on the position.
          - HOLD_TO_SETTLE  → no-op. We mark attempted so the cycle
                              skip-list doesn't reclassify forever;
                              the engine will still REFRESH the
                              decision every cycle so the operator
                              sees fresh EV.
          - CLOSE_NOW       → place_unwind_sell on this position's
                              venue at the bid (same path the legacy
                              auto-close gate used).
          - HEDGE           → place_fok on hedge venue (not yet
                              implemented — falls through to manual).
          - MANUAL_REVIEW   → mark attempted, leave the incident open
                              for operator action.
        """
        from arbiter.recovery.mitigation_engine import (
            CLOSE_NOW as _CLOSE_NOW,
            COMPLETE_ARB as _COMPLETE_ARB,
            HEDGE as _HEDGE,
            HOLD_TO_SETTLE as _HOLD,
            MANUAL_REVIEW as _MANUAL,
        )
        decision = pos.mitigation_decision or {}
        action = str(decision.get("action") or "").upper()
        autonomous = bool(decision.get("autonomous_ok"))

        if action == _HOLD:
            pos.auto_close_attempted = True
            pos.auto_close_result = (
                f"HOLD_TO_SETTLE: {decision.get('rationale', '')[:200]}"
            )
            return
        if action == _MANUAL:
            pos.auto_close_attempted = True
            pos.auto_close_result = (
                f"MANUAL_REVIEW: {decision.get('rationale', '')[:200]}"
            )
            return
        if not autonomous:
            pos.auto_close_attempted = True
            pos.auto_close_result = (
                f"{action}: needs operator approval — {decision.get('rationale', '')[:160]}"
            )
            return
        if action == _COMPLETE_ARB:
            await self._execute_complete_arb(pos, decision)
            return
        if action == _CLOSE_NOW:
            await self._execute_close_now(pos, decision)
            return
        if action == _HEDGE:
            # Hedge execution intentionally deferred — surfaces as
            # operator-review until we have a tested hedge path.
            pos.auto_close_attempted = True
            pos.auto_close_result = (
                f"HEDGE: deferred to operator — {decision.get('rationale', '')[:160]}"
            )
            return
        # Unknown action — defensive default.
        pos.auto_close_attempted = True
        pos.auto_close_result = f"unknown action {action!r}"

    async def _execute_complete_arb(
        self, pos: StrandedPosition, decision: Dict[str, Any],
    ) -> None:
        """Place the missing arb leg on the recommended venue.

        Uses the venue adapter's ``place_fok`` (the same path the live
        executor uses to enter new arbs) so the order goes through
        every safety gate the engine relies on for normal trades.
        """
        info = decision.get("complete_arb") or {}
        venue = info.get("venue")
        market_id = info.get("market_id")
        side = str(info.get("side") or "").lower()
        price = float(info.get("price") or 0.0)
        qty = int(info.get("qty") or 0)
        canonical_id = info.get("canonical_id") or pos.market_id
        adapter = self._adapters.get(venue) if venue else None
        if adapter is None or not hasattr(adapter, "place_fok") or qty <= 0:
            pos.auto_close_attempted = True
            pos.auto_close_result = (
                f"COMPLETE_ARB blocked: venue={venue!r} has no place_fok"
            )
            return
        try:
            arb_id = self._strand_arb_id(pos.platform, pos.market_id)
            order = await adapter.place_fok(
                arb_id=arb_id,
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=price,
                qty=qty,
            )
            pos.auto_close_attempted = True
            fill = float(getattr(order, "fill_qty", 0) or 0)
            status = getattr(getattr(order, "status", None), "value", str(getattr(order, "status", "")))
            pos.auto_close_result = (
                f"COMPLETE_ARB: placed BUY {qty} {side.upper()} on {venue} "
                f"@ ${price:.4f} → status={status} fill_qty={fill}"
            )
            logger.info(
                "stranded_reconciler.complete_arb_attempt",
                pos_platform=pos.platform,
                pos_market_id=pos.market_id,
                arb_venue=venue, arb_market_id=market_id, arb_side=side,
                price=price, qty=qty, status=status, fill_qty=fill,
            )
        except Exception as exc:
            pos.auto_close_attempted = True
            pos.auto_close_result = f"COMPLETE_ARB exception: {exc}"
            logger.warning(
                "stranded_reconciler.complete_arb_failed",
                pos_platform=pos.platform, pos_market_id=pos.market_id,
                venue=venue, err=str(exc),
            )

    async def _execute_close_now(
        self, pos: StrandedPosition, decision: Dict[str, Any],
    ) -> None:
        """Place a market sell (IOC) on the position's own venue."""
        adapter = self._adapters.get(pos.platform)
        if adapter is None or not hasattr(adapter, "place_unwind_sell"):
            pos.auto_close_attempted = True
            pos.auto_close_result = "CLOSE_NOW blocked: no place_unwind_sell"
            return
        try:
            order = await adapter.place_unwind_sell(
                arb_id=self._strand_arb_id(pos.platform, pos.market_id),
                market_id=pos.market_id,
                canonical_id=pos.market_id,
                side=pos.side.lower(),
                qty=int(abs(pos.qty)),
            )
            pos.auto_close_attempted = True
            fill = float(getattr(order, "fill_qty", 0) or 0)
            status = getattr(getattr(order, "status", None), "value", str(getattr(order, "status", "")))
            pos.mitigation_action = "closed" if fill > 0 else "close_now"
            pos.auto_close_result = (
                f"CLOSE_NOW: status={status} fill_qty={fill}"
            )
            logger.info(
                "stranded_reconciler.close_now_done",
                platform=pos.platform, market_id=pos.market_id,
                qty=pos.qty, status=status, fill_qty=fill,
            )
        except Exception as exc:
            pos.auto_close_attempted = True
            pos.auto_close_result = f"CLOSE_NOW exception: {exc}"
            logger.warning(
                "stranded_reconciler.close_now_failed",
                platform=pos.platform, market_id=pos.market_id, err=str(exc),
            )

    def classify_action(self, pos: StrandedPosition) -> str:
        """Return the recommended mitigation action for ``pos``.

        Decision tree (safety > speed):

          1. ``no_adapter`` — venue adapter missing or lacks the
             ``place_unwind_sell`` hook. Operator must intervene.
          2. ``let_settle`` — penny position (cost_per_share ≤ penny
             threshold). Closing fees exceed expected recoverable
             value, regardless of expiry distance:
               - Kalshi flat per-contract fee floor swallows the
                 entire 2¢ NHL Hart-trophy lots when closing 23 of
                 them; let them expire worthless instead of paying
                 to confirm worthless.
               - If the binary resolves YES, holding through expiry
                 gets us $1/share vs the few cents of MTM we'd lock
                 in by closing.
          3. ``large_notional`` — cost basis exceeds the auto-close
             ceiling. Operator review only.
          4. ``illiquid_no_bbo`` — no published bid/ask. Cannot price
             an unwind without painting the market.
          5. ``spread_too_wide`` — spread > configured bps cap.
             Auto-closing here locks in the spread as a loss.
          6. ``close_market`` — has real value AND venue is liquid
             AND notional fits. This is where the 2 polymarket
             midterms (~$27 each, fully liquid binaries that won't
             resolve for months) get auto-mitigated: every day we
             hold them stranded is a day of opportunity cost the
             reconciler can recover by selling at the public bid.
        """
        adapter = self._adapters.get(pos.platform)
        if adapter is None or not hasattr(adapter, "place_unwind_sell"):
            return "no_adapter"
        cps = pos.cost_per_share
        if cps > 0 and cps <= self._penny_cost_threshold_usd:
            return "let_settle"
        notional = abs(float(pos.cost_basis_usd or 0.0))
        if notional > self._max_auto_close_notional_usd:
            return "large_notional"
        if pos.best_bid <= 0 or pos.best_ask <= 0:
            return "illiquid_no_bbo"
        spread_bps = abs(pos.best_ask - pos.best_bid) * 10_000.0
        if spread_bps > self._max_auto_close_spread_bps:
            return "spread_too_wide"
        return "close_market"

    async def _maybe_auto_close(self, pos: StrandedPosition) -> None:
        """Classify the position, surface the recommendation, and
        execute the close when (and only when) the classification says
        ``close_market``. Every other branch marks the position
        ``auto_close_attempted`` so the next cycle doesn't loop.

        Bias: safety > speed. The ``let_settle`` branch deliberately
        does NOT touch the venue; we record the recommendation and let
        the binary resolve naturally so we never pay closing fees that
        exceed the lot's recoverable value.
        """
        action = self.classify_action(pos)
        pos.mitigation_action = action

        if action == "no_adapter":
            pos.auto_close_attempted = True
            pos.auto_close_result = "no adapter / no place_unwind_sell"
            return
        if action == "let_settle":
            # Soft-attempt: keep the recommendation alive across cycles
            # without ever calling the venue. Mark attempted so the
            # next cycle doesn't reclassify endlessly (cheap, but
            # noisy). Operator can override via the manual close hook
            # if they disagree.
            pos.auto_close_attempted = True
            pos.auto_close_result = (
                f"let_settle: cost/share ${pos.cost_per_share:.4f} ≤ "
                f"${self._penny_cost_threshold_usd:.2f} threshold — "
                f"closing fees would exceed recoverable value"
            )
            return
        if action == "large_notional":
            pos.auto_close_attempted = True
            pos.auto_close_result = (
                f"notional ${abs(pos.cost_basis_usd):.2f} > auto-close cap "
                f"${self._max_auto_close_notional_usd:.2f} — manual review"
            )
            return
        if action == "illiquid_no_bbo":
            pos.auto_close_attempted = True
            pos.auto_close_result = "illiquid (no bbo) — manual review"
            return
        if action == "spread_too_wide":
            spread_bps = abs(pos.best_ask - pos.best_bid) * 10_000.0
            pos.auto_close_attempted = True
            pos.auto_close_result = (
                f"spread {spread_bps:.0f}bps > cap "
                f"{self._max_auto_close_spread_bps:.0f}bps — manual review"
            )
            return

        # action == "close_market" → execute the unwind. arb_id reuses
        # the same hash-based id as the incident so DB joins (and
        # operator-side dedup) line up.
        adapter = self._adapters[pos.platform]
        try:
            order = await adapter.place_unwind_sell(
                arb_id=self._strand_arb_id(pos.platform, pos.market_id),
                market_id=pos.market_id,
                canonical_id=pos.market_id,
                side=pos.side.lower(),
                qty=int(abs(pos.qty)),
            )
            pos.auto_close_attempted = True
            fill = float(getattr(order, "fill_qty", 0) or 0)
            pos.mitigation_action = "closed" if fill > 0 else "close_market"
            pos.auto_close_result = (
                f"status={order.status.value} fill_qty={fill}"
            )
            logger.info(
                "stranded_reconciler.auto_close_done",
                platform=pos.platform,
                market_id=pos.market_id,
                qty=pos.qty,
                status=order.status.value,
                fill_qty=fill,
            )
        except Exception as exc:
            pos.auto_close_attempted = True
            pos.auto_close_result = f"exception: {exc}"
            logger.warning(
                "stranded_reconciler.auto_close_failed",
                platform=pos.platform,
                market_id=pos.market_id,
                err=str(exc),
            )


def reconciler_from_env(
    *, config, adapters, engine,
    forecastex_client=None, price_store=None, market_map_provider=None,
) -> StrandedPositionReconciler:
    """Build a reconciler with config sourced from env vars.

    Env vars:
      - STRANDED_RECONCILE_INTERVAL_S      (default 300)
      - STRANDED_AUTO_CLOSE                (default false — operator review)
      - STRANDED_AUTO_CLOSE_MAX_USD        (default 30 — covers ~$27 poly midterms)
      - STRANDED_AUTO_CLOSE_MAX_SPREAD_BPS (default 1000 = 10c wide)
      - STRANDED_PENNY_COST_USD            (default 0.05 — below this, let_settle)
      - STRANDED_LET_SETTLE_WINDOW_S       (default 604800 = 7d to expiry; informational)
    """
    try:
        interval = float(os.getenv("STRANDED_RECONCILE_INTERVAL_S", "300") or "300")
    except (TypeError, ValueError):
        interval = 300.0
    auto_close = str(os.getenv("STRANDED_AUTO_CLOSE", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )
    try:
        max_usd = float(os.getenv("STRANDED_AUTO_CLOSE_MAX_USD", "30") or "30")
    except (TypeError, ValueError):
        max_usd = 30.0
    try:
        max_bps = float(os.getenv("STRANDED_AUTO_CLOSE_MAX_SPREAD_BPS", "1000") or "1000")
    except (TypeError, ValueError):
        max_bps = 1000.0
    try:
        penny = float(os.getenv("STRANDED_PENNY_COST_USD", "0.05") or "0.05")
    except (TypeError, ValueError):
        penny = 0.05
    try:
        let_settle_window = float(
            os.getenv("STRANDED_LET_SETTLE_WINDOW_S", "604800") or "604800"
        )
    except (TypeError, ValueError):
        let_settle_window = 7 * 24 * 3600.0
    return StrandedPositionReconciler(
        config=config,
        adapters=adapters,
        engine=engine,
        forecastex_client=forecastex_client,
        price_store=price_store,
        market_map_provider=market_map_provider,
        interval_s=interval,
        auto_close=auto_close,
        max_auto_close_notional_usd=max_usd,
        max_auto_close_spread_bps=max_bps,
        penny_cost_threshold_usd=penny,
        let_settle_window_seconds=let_settle_window,
    )
