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
import html
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Sequence

import aiohttp
import structlog

logger = structlog.get_logger("arbiter.recovery.stranded_reconciler")


# ForecastEx event-contract local-symbol signature embedded in the IBKR
# contractDesc, e.g. "SENM   NOV2026 2 C [SENM_1126_Democratic_YES 1]".
# This is the stable identifier: the IBKR positions client returns records
# with exchs=null and no listingExchange (live 2026-07-10), so the exchange
# string alone drops every FX lot. The bracket pattern — TICKER_DIGITS_...
# ending in YES/NO — is present in every ForecastEx event contract and in
# no ordinary equity/option holding.
_FX_LOCAL_SYMBOL_RE = re.compile(r"\[[A-Z0-9]+_\d+_[A-Za-z0-9_]*(?:YES|NO)\b")


def is_forecastex_position(p: dict) -> bool:
    """True when an IBKR position record is a ForecastEx event contract.

    Matches on either an explicit FORECASTX exchange marker OR the
    event-contract local-symbol bracket in the contract description.
    Deliberately narrow so non-ForecastEx IBKR holdings in the same
    account (equities, ordinary options) are excluded.
    """
    if not isinstance(p, dict) or not p:
        return False
    exchange = str(
        p.get("listingExchange") or p.get("exchange") or p.get("exchs") or ""
    ).upper()
    if "FORECASTX" in exchange:
        return True
    desc = str(p.get("contractDesc") or p.get("name") or "")
    return bool(_FX_LOCAL_SYMBOL_RE.search(desc))


def _html(text: object) -> str:
    return html.escape(str(text or ""), quote=False)


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
    # Quantity of this venue lot covered by the opposite leg of a
    # BOTH-legs-filled arb (hedged inventory held to settlement).
    # Computed each cycle from ``ExecutionStore.paired_inventory_by_market``.
    # Only the RESIDUAL (|qty| - paired_qty) is true stranded exposure.
    paired_qty: float = 0.0

    @property
    def residual_qty(self) -> float:
        return max(abs(float(self.qty or 0.0)) - float(self.paired_qty or 0.0), 0.0)

    @property
    def fully_paired(self) -> bool:
        return abs(float(self.qty or 0.0)) > 0 and self.residual_qty <= 1e-9

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
            "paired_qty": round(float(self.paired_qty or 0.0), 4),
            "residual_qty": round(self.residual_qty, 4),
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
    # Lots fully covered by BOTH-legs-filled arb inventory — hedged, not
    # stranded. Tracked for visibility but excluded from stranded_count.
    paired_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cycle_count": self.cycle_count,
            "stranded_count": self.stranded_count,
            "stranded": self.stranded,
            "duration_ms": round(self.duration_ms, 1),
            "errors": self.errors,
            "auto_close_enabled": self.auto_close_enabled,
            "paired_count": self.paired_count,
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
        interval_s: float = 30.0,
        auto_close: bool = False,
        max_auto_close_notional_usd: float = 30.0,
        max_auto_close_spread_bps: float = 1000.0,  # 10¢ wide max
        penny_cost_threshold_usd: float = 0.05,
        let_settle_window_seconds: float = 7 * 24 * 3600.0,
        mitigation_engine: Optional[Any] = None,
        notifier: Optional[Any] = None,
        close_retry_window_seconds: float = 300.0,
        store: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._adapters = adapters or {}
        self._engine = engine
        # ExecutionStore (optional) — source of paired-inventory truth so
        # venue lots hedged by filled arbs are never classified stranded.
        self._store = store
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
            try:
                _unact_cap = float(os.getenv("STRANDED_UNACTIONABLE_MAX_USD", "10") or "10")
            except (TypeError, ValueError):
                _unact_cap = 10.0
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
                    unactionable_max_notional_usd=_unact_cap,
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
        # TelegramNotifier for close-action alerts. Optional — tests omit it.
        self._notifier = notifier
        # Per-position close-attempt rate limit. ``auto_close_attempted``
        # alone is a one-shot flag; this map gives us a sliding-window
        # cooldown so a single transient adapter failure doesn't leave a
        # position permanently un-mitigated. After ``close_retry_window_
        # seconds`` since the last attempt, the gate re-opens.
        self._close_retry_window_s = max(60.0, float(close_retry_window_seconds))
        self._last_close_attempt_ts: Dict[tuple, float] = {}
        # BUG #2: attempt-count tracker for alert suppression. Keyed by
        # (platform, market_id, rounded_price). After
        # ``_alert_suppress_after`` zero-fill attempts at the same price
        # for the same position, _notify_attempt logs only — Telegram
        # falls silent until the price moves (or the position resolves).
        # Stops the GOP_SENATE-style every-cycle spam without losing the
        # initial visibility burst the operator needs to act.
        self._failed_attempt_count: Dict[tuple, int] = {}
        self._alert_suppress_after: int = 3
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
                # Carry decisions forward so a deferred cycle (incomplete
                # venue view) keeps the last trustworthy recommendation.
                pos.mitigation_action = existing.mitigation_action
                pos.mitigation_decision = existing.mitigation_decision
                pos.paired_qty = existing.paired_qty
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

        # ── Paired-inventory netting (2026-07-10) ─────────────────────────
        # Net BOTH-legs-filled arb inventory out of every venue lot before
        # anything classifies or decides. 100 filled arbs left perfectly
        # hedged lots (57 K-YES ↔ 57 FX-NO etc.) that a pairing-blind pass
        # booked as "$75 stranded / -$74 unrealized" and — under an FX
        # outage — tried to COMPLETE_ARB (buying the FX leg AGAIN).
        paired_inventory = await self._load_paired_inventory()
        for pos in self._tracked.values():
            pos.paired_qty = self._covered_qty(pos, paired_inventory)

        # A failed venue fetch means the cross-venue view is incomplete:
        # decisions computed from it are untrustworthy. Defer classify +
        # mitigation this cycle (previous decisions were carried forward in
        # the merge); incident emission below keeps operator visibility.
        decisions_deferred = bool(failed_platforms)
        if decisions_deferred:
            logger.warning(
                "stranded_reconciler.decisions_deferred_incomplete_view",
                failed_platforms=sorted(failed_platforms),
            )

        # Refresh classification on EVERY position EVERY cycle so the
        # ops UI shows the current recommendation even when auto-close
        # is OFF (operator observation mode). Idempotent — pure read
        # plus dict assignment. Fully-paired lots get the terminal
        # ``paired_hold`` label; partially-paired lots are classified on
        # their RESIDUAL view only.
        for pos in list(self._tracked.values()):
            if decisions_deferred:
                break
            if pos.fully_paired:
                pos.mitigation_action = "paired_hold"
                pos.mitigation_decision = {
                    "action": "PAIRED_HOLD",
                    "autonomous_ok": False,
                    "rationale": (
                        f"{pos.paired_qty:g} contracts hedged by the opposite "
                        "leg of filled arb(s); pair pays $1.00 at settlement. "
                        "No action — closing either leg breaks the hedge."
                    ),
                }
                continue
            try:
                pos.mitigation_action = self.classify_action(
                    self._residual_view(pos)
                )
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
        # Fully-paired lots are skipped (decision set above); partial
        # lots are decided on the residual view so the engine can never
        # recommend re-buying the hedged portion.
        if self._mitigation_engine is not None and not decisions_deferred:
            for pos in list(self._tracked.values()):
                if pos.fully_paired:
                    continue
                try:
                    decision = await self._mitigation_engine.decide(
                        self._residual_view(pos)
                    )
                    pos.mitigation_decision = decision.to_dict()
                    if pos.paired_qty > 0:
                        pos.mitigation_decision["residual_qty"] = pos.residual_qty
                        pos.mitigation_decision["paired_qty"] = pos.paired_qty
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
            if pos.fully_paired:
                # Hedged inventory from filled arbs — not a strand, no alert.
                logger.info(
                    "stranded_reconciler.paired_hold",
                    platform=pos.platform, market_id=pos.market_id,
                    paired_qty=pos.paired_qty,
                )
                continue
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
        if self._auto_close and not decisions_deferred:
            for pos in list(self._tracked.values()):
                key = (pos.platform, pos.market_id)
                # Never auto-act on a lot that is (even partially) hedged
                # by filled-arb inventory: execution helpers act on the
                # FULL lot quantity, so acting here would break the hedge
                # or double a leg. Partially-paired lots require operator
                # judgment until residual-quantity execution exists.
                if pos.paired_qty > 0:
                    pos.auto_close_result = "skipped: paired inventory"
                    continue
                # Active-arb safety gate: never touch a position whose
                # venue + market_id matches a leg of an in-flight
                # ArbExecution. Closing it underneath the live executor
                # would crystallise the arb's loss AND leave the engine
                # holding the opposite leg naked.
                if self._is_part_of_active_arb(pos):
                    pos.auto_close_result = (
                        "skipped: part of active arb execution"
                    )
                    pos.mitigation_action = "skip_active_arb"
                    continue
                # Rate-limit close attempts: 1 per (platform, market_id)
                # per ``close_retry_window_seconds``. Replaces the
                # one-shot ``auto_close_attempted`` gate so a position
                # that fails to close (e.g. adapter timeout) can be
                # retried after the cool-down window expires.
                if not self._close_attempt_allowed(key):
                    continue
                if self._mitigation_engine is not None and pos.mitigation_decision:
                    await self._execute_decision(pos)
                else:
                    # Fallback to the legacy single-shot close gate
                    # (kept for tests + dev environments that don't
                    # wire the engine).
                    await self._maybe_auto_close(pos)

        duration_ms = (time.monotonic() - t0) * 1000.0
        paired_count = sum(1 for p in self._tracked.values() if p.fully_paired)
        stranded_count = sum(
            1 for p in self._tracked.values() if not p.fully_paired
        )
        snapshot = ReconcilerSnapshot(
            timestamp=now,
            cycle_count=self._cycle_count,
            stranded_count=stranded_count,
            stranded=[p.to_dict() for p in self._tracked.values()],
            duration_ms=duration_ms,
            errors=errors,
            auto_close_enabled=self._auto_close,
            paired_count=paired_count,
        )
        self._last_snapshot = snapshot
        self._history.append(snapshot)
        logger.info(
            "stranded_reconciler.cycle_complete",
            cycle=self._cycle_count,
            stranded=stranded_count,
            paired=paired_count,
            new=len(new_keys),
            duration_ms=round(duration_ms, 1),
        )
        return snapshot

    # ─── paired-inventory helpers ─────────────────────────────────────────

    async def _load_paired_inventory(self) -> Dict[tuple, float]:
        """Load per-(platform, market_id, side) filled quantities of arbs
        whose status='filled'. Empty dict on any failure — fail SAFE by
        over-reporting strands, never by hiding exposure."""
        store = self._store
        loader = getattr(store, "paired_inventory_by_market", None) if store else None
        if loader is None:
            return {}
        try:
            return dict(await loader() or {})
        except Exception as exc:
            logger.warning(
                "stranded_reconciler.paired_inventory_load_failed", err=str(exc),
            )
            return {}

    @staticmethod
    def _covered_qty(pos: StrandedPosition, inventory: Dict[tuple, float]) -> float:
        """Quantity of this venue lot hedged by filled-arb inventory.

        Kalshi/Polymarket lots are SIGNED net positions on one market_id
        (long-YES positive, long-NO negative), so paired 'yes' fills net
        against positive lots and 'no' fills against negative ones.
        ForecastEx lots are per-conid LONGS (YES and NO are sibling conids;
        the arb-semantic side lives on the order), so every filled buy of
        the conid hedges the long."""
        if not inventory:
            return 0.0
        platform = str(pos.platform or "").lower()
        market_id = str(pos.market_id or "")
        yes_q = float(inventory.get((platform, market_id, "yes"), 0.0) or 0.0)
        no_q = float(inventory.get((platform, market_id, "no"), 0.0) or 0.0)
        qty = float(pos.qty or 0.0)
        if qty == 0.0:
            return 0.0
        if platform == "forecastex":
            expected_long = yes_q + no_q
            return min(abs(qty), expected_long) if qty > 0 else 0.0
        expected_signed = yes_q - no_q
        if expected_signed == 0.0 or (expected_signed > 0) != (qty > 0):
            return 0.0
        return min(abs(qty), abs(expected_signed))

    @staticmethod
    def _residual_view(pos: StrandedPosition) -> StrandedPosition:
        """Shallow copy scaled to the UNHEDGED residual, for classify /
        mitigation. Deciding on the full lot is how COMPLETE_ARB doubles
        already-paired legs."""
        total = abs(float(pos.qty or 0.0))
        residual = pos.residual_qty
        if residual <= 0 or total <= 0 or residual >= total:
            return pos
        import copy as _copy
        frac = residual / total
        view = _copy.copy(pos)
        view.qty = residual if float(pos.qty) > 0 else -residual
        view.cost_basis_usd = float(pos.cost_basis_usd or 0.0) * frac
        view.mtm_usd = float(pos.mtm_usd or 0.0) * frac
        view.unrealized_usd = float(pos.unrealized_usd or 0.0) * frac
        view.paired_qty = 0.0
        return view

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
            # other IBKR products in the same account. Uses the robust
            # multi-signal identifier (the exchange field is null on the
            # live positions payload; the event-contract local-symbol
            # bracket in contractDesc is the stable marker).
            if not is_forecastex_position(p):
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
                    title=str(p.get("contractDesc") or p.get("name") or "")[:80],
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

    def _is_part_of_active_arb(self, pos: StrandedPosition) -> bool:
        """Return True when ``pos`` matches a leg of an in-flight arb.

        The reconciler MUST NOT close legs the executor is still trying
        to use — doing so would crystallise the arb's intended PnL and
        leave the other leg naked. We check ``engine._executions`` for
        any ArbExecution whose YES or NO leg matches our (platform,
        market_id) and is in a non-terminal status.
        """
        engine = self._engine
        if engine is None:
            return False
        executions = getattr(engine, "_executions", None)
        if executions is None or not isinstance(executions, (list, tuple)):
            return False
        # Active = NOT in any terminal/no-op status. Inverting the set
        # keeps us safe against new status values the engine may add.
        terminal_statuses = {"closed", "cancelled", "simulated", "rejected", "failed"}
        for ex in executions:
            status = str(getattr(ex, "status", "") or "").lower()
            if status in terminal_statuses:
                continue
            for leg in (getattr(ex, "leg_yes", None), getattr(ex, "leg_no", None)):
                if leg is None:
                    continue
                leg_platform = str(getattr(leg, "platform", "") or "").lower()
                leg_market = str(getattr(leg, "market_id", "") or "")
                if leg_platform == pos.platform.lower() and leg_market == pos.market_id:
                    return True
        return False

    def _close_attempt_allowed(self, key: tuple) -> bool:
        """Return True when the per-position cool-down has elapsed."""
        last = self._last_close_attempt_ts.get(key)
        if last is None:
            return True
        return (time.time() - last) >= self._close_retry_window_s

    def _mark_close_attempt(self, key: tuple) -> None:
        self._last_close_attempt_ts[key] = time.time()

    async def _notify_close(
        self, pos: StrandedPosition, action: str, fill_qty: float,
        fill_price: float, status: str, pnl_usd: float,
    ) -> None:
        """Send a Telegram alert for a close-action outcome.

        Per-position dedup key prevents the same close (in the rare case
        the reconciler retries mid-window) from spamming the chat. Burst
        guard at the notifier level still backstops a multi-position
        cascade.
        """
        notifier = self._notifier
        if notifier is None or not getattr(notifier, "_enabled", False):
            return
        from ..notifiers.fmt import DIVIDER as _DIV, usd as _usd
        icon = "\U0001f4c8" if pnl_usd >= 0 else "\U0001f4c9"
        verb = "PROFITABLE CLOSE" if pnl_usd >= 0 else "LOSS-CUT CLOSE"
        msg = (
            f"{icon} <b>{_html(verb)}</b>\n"
            f"{_DIV}\n"
            f"\U0001f3e6 Venue: <b>{_html(pos.platform.upper())}</b>\n"
            f"\U0001f4cd Market: <code>{_html(pos.market_id)}</code>\n"
            f"  Side: <code>{_html(pos.side)}</code>  |  Qty: <code>{abs(int(pos.qty))}</code>\n"
            f"  Fill: <code>{fill_qty}</code> @ <code>${fill_price:.2f}</code>  |  Status: <code>{_html(status)}</code>\n"
            f"{_DIV}\n"
            f"\U0001f4b0 Cost basis: <code>{_usd(pos.cost_basis_usd)}</code>\n"
            f"{icon} <b>P&amp;L: <code>{_usd(pnl_usd, signed=True)}</code></b>"
        )
        try:
            await notifier.send(
                msg,
                dedup_key=f"strand_close_{pos.platform}_{pos.market_id}",
            )
        except Exception as exc:
            logger.debug(
                "stranded_reconciler.notify_close_failed",
                platform=pos.platform, market_id=pos.market_id, err=str(exc),
            )

    async def _notify_attempt(
        self,
        pos: StrandedPosition,
        action: str,
        venue: str,
        market_id: str,
        side: str,
        price: float,
        qty: int,
        status: str,
        fill_qty: float,
        net_edge_after_fees_cents: Optional[float] = None,
    ) -> None:
        """Send a Telegram alert for a mitigation attempt.

        HARD BLOCK: when ``net_edge_after_fees_cents`` is provided and < 0
        we suppress the Telegram entirely — by definition the decision
        was not profitable, so notifying the operator about a guaranteed
        loss attempt is pure noise. The internal log line still fires.
        """
        if (
            net_edge_after_fees_cents is not None
            and float(net_edge_after_fees_cents) < 0.0
        ):
            logger.info(
                "stranded_reconciler.alert_blocked_unprofitable",
                pos_platform=pos.platform, pos_market_id=pos.market_id,
                venue=venue, market_id=market_id, side=side,
                price=price, qty=qty, status=status, fill_qty=fill_qty,
                net_edge_after_fees_cents=round(
                    float(net_edge_after_fees_cents), 2,
                ),
            )
            return
        notifier = self._notifier
        if notifier is None or not getattr(notifier, "_enabled", False):
            return
        # Age bucket per the loss-cut policy: <6h young, 6-24h mature,
        # 24h+ aged. Operators want to see which bucket the position
        # is in to interpret the chosen action.
        try:
            age_s = max(0.0, float(time.time()) - float(pos.first_seen_ts or 0.0))
        except (TypeError, ValueError):
            age_s = 0.0
        if age_s < 6 * 3600:
            bucket = "young (&lt;6h)"
        elif age_s < 24 * 3600:
            bucket = "mature (6-24h)"
        else:
            bucket = "aged (24h+)"
        fill_line = (
            f"FILLED {fill_qty}/{qty}"
            if fill_qty > 0
            else "NO FILL (book did not match)"
        )
        # BUG #2: suppress repeat alerts once we've sent
        # ``_alert_suppress_after`` zero-fill attempts at the same price
        # for the same position. The first few alerts let the operator
        # see the issue; quieting after N stops the cycle-per-cycle spam.
        # Price-keyed: a price change is a new situation, so the counter
        # resets implicitly (new key, count=0).
        if fill_qty <= 0:
            rounded_price = round(float(price or 0.0), 4)
            count_key = (pos.platform, pos.market_id, rounded_price)
            count = self._failed_attempt_count.get(count_key, 0) + 1
            self._failed_attempt_count[count_key] = count
            if count > self._alert_suppress_after:
                logger.info(
                    "stranded_reconciler.alert_suppressed",
                    platform=pos.platform, market_id=pos.market_id,
                    price=rounded_price, attempts=count,
                    action=action, status=status,
                )
                return
        else:
            # Real fill — clear any prior suppression counter for this
            # position so future failures alert fresh.
            for k in [k for k in self._failed_attempt_count
                      if k[0] == pos.platform and k[1] == pos.market_id]:
                self._failed_attempt_count.pop(k, None)
        from ..notifiers.fmt import DIVIDER as _DIV
        status_icon = "\U0001f7e2" if "fill" in status.lower() else "\U0001f7e1"
        msg = (
            f"\U0001f504 <b>RECONCILER ATTEMPT</b>\n"
            f"{_DIV}\n"
            f"\U0001f4cd Position: <code>{_html(pos.platform)}:{_html(pos.market_id)}</code>\n"
            f"  Held: <code>{abs(int(pos.qty))}</code> {_html(pos.side.upper())}  |  Bucket: <code>{_html(bucket)}</code>\n"
            f"\n"
            f"⚡ Action: BUY <code>{qty}</code> {_html(side.upper())} on <b>{_html(venue.upper())}</b>\n"
            f"  Market: <code>{_html(market_id)}</code>\n"
            f"  Price: <code>${price:.2f}</code>  |  Status: <code>{_html(status)}</code>\n"
            f"{_DIV}\n"
            f"{status_icon} Result: {_html(fill_line)}"
        )
        try:
            await notifier.send(
                msg,
                dedup_key=f"strand_attempt_{pos.platform}_{pos.market_id}_{venue}_{market_id}",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "stranded_reconciler.notify_attempt_failed",
                platform=pos.platform, market_id=pos.market_id, err=str(exc),
            )

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

    async def _missing_leg_price_still_valid(
        self,
        canonical_id: str,
        venue: str,
        side: str,
        decision_price: float,
        pos: StrandedPosition,
        key: tuple,
        *,
        tolerance: float = 0.02,
        max_age_s: float = 60.0,
    ) -> bool:
        """Reprice the missing-leg ask from the current PriceStore quote.

        Returns True when:
          - we can fetch a quote no older than ``max_age_s``, AND
          - the live ask is within ``tolerance`` of ``decision_price``.

        Returns False when the quote is missing/stale or the live ask has
        moved beyond tolerance — in both cases the close attempt is
        suppressed (no FOK is submitted) and the result/log line carries
        the actionable detail. When the gap exceeds the hopeless
        threshold we also flip the position to HOLD_TO_SETTLE so future
        cycles don't reattempt until the operator intervenes.
        """
        store = self._price_store
        if store is None:
            # Nothing to reprice against; keep legacy behavior so the
            # close attempt can still proceed (unit tests / dev paths).
            return True
        try:
            quotes = await store.get_all_for_market(canonical_id)
        except Exception as exc:
            logger.warning(
                "stranded_reconciler.reprice_fetch_failed",
                canonical_id=canonical_id, venue=venue, err=str(exc),
            )
            pos.auto_close_result = f"reprice fetch failed: {exc}"
            self._mark_close_attempt(key)
            return False
        pp = (quotes or {}).get(venue)
        if pp is None:
            logger.info(
                "stranded_reconciler.reprice_no_quote",
                canonical_id=canonical_id, venue=venue,
                pos_platform=pos.platform, pos_market_id=pos.market_id,
            )
            pos.auto_close_result = (
                f"reprice: no live quote for {venue} on {canonical_id}, skipping"
            )
            self._mark_close_attempt(key)
            return False
        quote_age = float(getattr(pp, "age_seconds", 0.0) or 0.0)
        if quote_age > max_age_s:
            logger.info(
                "stranded_reconciler.reprice_stale",
                canonical_id=canonical_id, venue=venue,
                quote_age_s=round(quote_age, 1), max_age_s=max_age_s,
                pos_platform=pos.platform, pos_market_id=pos.market_id,
            )
            pos.auto_close_result = (
                f"stale price data ({quote_age:.0f}s > {max_age_s:.0f}s), "
                f"skipping close attempt"
            )
            self._mark_close_attempt(key)
            return False
        if str(side).lower() == "no":
            live_ask = float(getattr(pp, "no_ask", 0.0) or pp.no_price or 0.0)
        else:
            live_ask = float(getattr(pp, "yes_ask", 0.0) or pp.yes_price or 0.0)
        if live_ask <= 0:
            logger.info(
                "stranded_reconciler.reprice_no_ask",
                canonical_id=canonical_id, venue=venue, side=side,
                pos_platform=pos.platform, pos_market_id=pos.market_id,
            )
            pos.auto_close_result = (
                f"reprice: live ask=0 on {venue} {side.upper()}, skipping"
            )
            self._mark_close_attempt(key)
            return False
        gap = abs(live_ask - decision_price)
        if gap > tolerance:
            # Compute what we could afford to pay: $1 settle - cost we
            # already sunk - tolerable fee. Display so the operator can
            # see *why* the close cannot work at the current ask.
            cps = (abs(float(pos.cost_basis_usd or 0.0))
                   / max(abs(float(pos.qty or 0.0)), 1e-9))
            max_affordable = max(0.0, 1.0 - cps - 0.02)  # 2¢ fee assumption
            logger.info(
                "stranded_reconciler.reprice_moved",
                canonical_id=canonical_id, venue=venue, side=side,
                decision_price=round(decision_price, 4),
                current_ask=round(live_ask, 4),
                max_affordable=round(max_affordable, 4),
                gap=round(gap, 4), tolerance=tolerance,
                pos_platform=pos.platform, pos_market_id=pos.market_id,
            )
            pos.auto_close_result = (
                f"close unprofitable: current_ask=${live_ask:.4f} "
                f"> max_affordable=${max_affordable:.4f} "
                f"(decision was ${decision_price:.4f}, gap={gap*100:.1f}¢), skipping"
            )
            self._mark_close_attempt(key)
            # Permanently unprofitable → flip to HOLD_TO_SETTLE so we
            # stop reattempting. 15% gap of the $1 settlement value.
            if (cps + live_ask) - 1.0 >= 0.15:
                pos.mitigation_action = "hold_to_settle"
                pos.auto_close_attempted = True
                logger.info(
                    "stranded_reconciler.flipped_to_hold",
                    canonical_id=canonical_id, venue=venue,
                    combined_cost=round(cps + live_ask, 4),
                    pos_platform=pos.platform, pos_market_id=pos.market_id,
                )
            return False
        return True

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
        try:
            net_edge_cents = float(info.get("net_edge_cents") or 0.0)
        except (TypeError, ValueError):
            net_edge_cents = 0.0
        adapter = self._adapters.get(venue) if venue else None
        key = (pos.platform, pos.market_id)
        if adapter is None or not hasattr(adapter, "place_fok") or qty <= 0:
            pos.auto_close_attempted = True
            self._mark_close_attempt(key)
            pos.auto_close_result = (
                f"COMPLETE_ARB blocked: venue={venue!r} has no place_fok"
            )
            return
        # BUG #2 follow-up: re-validate the missing-leg price against the
        # CURRENT price_store at the moment we're about to send the order
        # — the decision was computed earlier in the cycle and the book
        # may have already moved. If the live ask doesn't match what the
        # decision promised within a small tolerance, skip the attempt
        # and log why. Prevents the GOP_SENATE_2026 loop where a
        # decision-time $0.48 was submitted against a $0.55 live book.
        if not await self._missing_leg_price_still_valid(
            canonical_id, venue, side, price, pos, key,
        ):
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
            self._mark_close_attempt(key)
            fill = float(getattr(order, "fill_qty", 0) or 0)
            fill_px = float(getattr(order, "fill_price", 0) or 0)
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
            if fill > 0:
                # Edge per pair captured in cents. The position itself
                # is now part of a real arb — net PnL approximates
                # qty * (1 - cps - fill_px), pre-fees.
                cps = (abs(float(pos.cost_basis_usd or 0.0))
                       / max(abs(float(pos.qty or 0.0)), 1e-9))
                pnl = fill * (1.0 - cps - fill_px)
                await self._notify_close(
                    pos, "COMPLETE_ARB", fill, fill_px, str(status), pnl,
                )
            else:
                # Operator-visibility: alert on EVERY attempt — including
                # zero-fill IOC rejects — so the reconciler is never silent
                # about active mitigation work. Per-position dedup over the
                # close-retry window keeps the chat clean when the same
                # position keeps not filling.
                await self._notify_attempt(
                    pos, "COMPLETE_ARB", venue, market_id, side,
                    price, qty, str(status), fill,
                    net_edge_after_fees_cents=net_edge_cents,
                )
        except Exception as exc:
            pos.auto_close_attempted = True
            self._mark_close_attempt(key)
            pos.auto_close_result = f"COMPLETE_ARB exception: {exc}"
            logger.warning(
                "stranded_reconciler.complete_arb_failed",
                pos_platform=pos.platform, pos_market_id=pos.market_id,
                venue=venue, err=str(exc),
            )
            await self._notify_attempt(
                pos, "COMPLETE_ARB", venue, market_id, side,
                price, qty, f"EXCEPTION:{type(exc).__name__}", 0.0,
                net_edge_after_fees_cents=net_edge_cents,
            )

    async def _execute_close_now(
        self, pos: StrandedPosition, decision: Dict[str, Any],
    ) -> None:
        """Place a market sell (IOC) on the position's own venue."""
        adapter = self._adapters.get(pos.platform)
        key = (pos.platform, pos.market_id)
        if adapter is None or not hasattr(adapter, "place_unwind_sell"):
            pos.auto_close_attempted = True
            pos.auto_close_result = "CLOSE_NOW blocked: no place_unwind_sell"
            self._mark_close_attempt(key)
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
            self._mark_close_attempt(key)
            fill = float(getattr(order, "fill_qty", 0) or 0)
            fill_px = float(getattr(order, "fill_price", 0) or 0)
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
            if fill > 0:
                pnl = (fill * fill_px) - (
                    abs(float(pos.cost_basis_usd or 0.0))
                    * (fill / max(abs(float(pos.qty or 0.0)), 1e-9))
                )
                await self._notify_close(
                    pos, "CLOSE_NOW", fill, fill_px, str(status), pnl,
                )
        except Exception as exc:
            pos.auto_close_attempted = True
            self._mark_close_attempt(key)
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
        key = (pos.platform, pos.market_id)
        try:
            order = await adapter.place_unwind_sell(
                arb_id=self._strand_arb_id(pos.platform, pos.market_id),
                market_id=pos.market_id,
                canonical_id=pos.market_id,
                side=pos.side.lower(),
                qty=int(abs(pos.qty)),
            )
            pos.auto_close_attempted = True
            self._mark_close_attempt(key)
            fill = float(getattr(order, "fill_qty", 0) or 0)
            fill_px = float(getattr(order, "fill_price", 0) or 0)
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
            if fill > 0:
                pnl = (fill * fill_px) - (
                    abs(float(pos.cost_basis_usd or 0.0))
                    * (fill / max(abs(float(pos.qty or 0.0)), 1e-9))
                )
                await self._notify_close(
                    pos, "CLOSE_MARKET", fill, fill_px, str(order.status.value), pnl,
                )
        except Exception as exc:
            pos.auto_close_attempted = True
            self._mark_close_attempt(key)
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
    notifier=None, store=None,
) -> StrandedPositionReconciler:
    """Build a reconciler with config sourced from env vars.

    Env vars:
      - STRANDED_RECONCILE_INTERVAL_S       (default 30 — matches auto_executor cadence)
      - STRANDED_AUTO_CLOSE                 (default false — operator review)
      - STRANDED_AUTO_CLOSE_MAX_USD         (default 30 — covers ~$27 poly midterms)
      - STRANDED_AUTO_CLOSE_MAX_SPREAD_BPS  (default 1000 = 10c wide)
      - STRANDED_PENNY_COST_USD             (default 0.05 — below this, let_settle)
      - STRANDED_LET_SETTLE_WINDOW_S        (default 604800 = 7d to expiry; informational)
      - STRANDED_CLOSE_RETRY_WINDOW_S       (default 300 — 1 close attempt per position per 5 min)
    """
    try:
        interval = float(os.getenv("STRANDED_RECONCILE_INTERVAL_S", "30") or "30")
    except (TypeError, ValueError):
        interval = 30.0
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
    try:
        close_retry_window = float(
            os.getenv("STRANDED_CLOSE_RETRY_WINDOW_S", "300") or "300"
        )
    except (TypeError, ValueError):
        close_retry_window = 300.0
    return StrandedPositionReconciler(
        config=config,
        adapters=adapters,
        engine=engine,
        forecastex_client=forecastex_client,
        price_store=price_store,
        market_map_provider=market_map_provider,
        store=store,
        interval_s=interval,
        auto_close=auto_close,
        max_auto_close_notional_usd=max_usd,
        max_auto_close_spread_bps=max_bps,
        penny_cost_threshold_usd=penny,
        let_settle_window_seconds=let_settle_window,
        notifier=notifier,
        close_retry_window_seconds=close_retry_window,
    )
