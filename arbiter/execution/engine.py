"""
Execution engine with re-quote checks, concurrent legs, and recovery hooks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional, Tuple

import aiohttp
from structlog.contextvars import bind_contextvars, clear_contextvars

from ..audit.math_auditor import MathAuditor
from ..config.settings import ArbiterConfig, ScannerConfig
from ..monitor.balance import BalanceMonitor
from ..scanner.arbitrage import ArbitrageOpportunity, compute_fee
from ..utils.price_store import PricePoint, PriceStore

if TYPE_CHECKING:
    from ..config.settings import SafetyConfig
    from ..safety.supervisor import SafetySupervisor
    from .adapters.base import PlatformAdapter
    from .store import ExecutionStore

logger = logging.getLogger("arbiter.execution")

# Cap in-memory execution history to prevent unbounded growth across 24/7 runs.
# Persistent history lives in ExecutionStore (PostgreSQL); this is the dashboard
# / equity-curve buffer.
MAX_EXECUTION_HISTORY = 1000

# `_recent_signatures` is a per-process dedup map (opp.key():status -> last_seen_ts)
# preventing duplicate execution attempts within 30s. Old entries must be evicted
# so the dict doesn't grow without bound across long runs.
SIGNATURE_DEDUP_WINDOW_S = 30.0
SIGNATURE_PRUNE_INTERVAL_S = 300.0


def _trim_executions(executions: List["ArbExecution"]) -> None:
    """Trim in-place to MAX_EXECUTION_HISTORY. Cheap O(1) when under cap."""
    overflow = len(executions) - MAX_EXECUTION_HISTORY
    if overflow > 0:
        del executions[:overflow]


def _build_inline_analysis(execution: "ArbExecution") -> str:
    """Run the trade analyzer against an in-memory ArbExecution.

    The analyzer normally reads DB rows; here we synthesize the same dict
    shape from the in-process objects so a fresh terminal arb can be analyzed
    before its audit row is committed.
    """
    from ..analysis.trade_analyzer import TradeAnalyzerInput, analyze_trade

    def _order_to_row(order: "Order") -> Dict[str, Any]:
        return {
            "order_id": order.order_id,
            "platform": order.platform,
            "side": order.side,
            "price": float(order.price),
            "quantity": float(order.quantity),
            "status": order.status.value,
            "fill_price": float(order.fill_price),
            "fill_qty": float(order.fill_qty),
            "error": order.error or "",
            "submitted_at": order.timestamp or None,
            "terminal_at": None,
        }

    opp_dict: Dict[str, Any] = {}
    if execution.opportunity is not None and hasattr(execution.opportunity, "to_dict"):
        try:
            opp_dict = execution.opportunity.to_dict()
        except Exception:  # noqa: BLE001 - opportunistic, fall back to empty
            opp_dict = {}

    data = TradeAnalyzerInput(
        arb_id=execution.arb_id,
        canonical_id=getattr(execution.opportunity, "canonical_id", "") or "",
        status=execution.status,
        realized_pnl=float(execution.realized_pnl or 0),
        net_edge=getattr(execution.opportunity, "net_edge", None),
        is_simulation=execution.status == "simulated",
        created_at=None,
        closed_at=None,
        opportunity=opp_dict,
        orders=[_order_to_row(execution.leg_yes), _order_to_row(execution.leg_no)],
        fills=[],
        incidents=[],
    )
    return analyze_trade(data)


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ABORTED = "aborted"
    SIMULATED = "simulated"


@dataclass
class Order:
    order_id: str
    platform: str
    market_id: str
    canonical_id: str
    side: str
    price: float
    quantity: int
    status: OrderStatus
    fill_price: float = 0.0
    fill_qty: int = 0
    timestamp: float = 0.0
    error: str = ""
    # CR-02: adapter populates with the engine-chosen client_order_id
    # (e.g. ARB-000042-YES-deadbeef); engine threads this into
    # ExecutionStore.upsert_order(client_order_id=...) so the DB column
    # holds the real idempotency key, not the platform-assigned order_id.
    external_client_order_id: Optional[str] = None
    # C3: when True, the engine timed out on this leg and could NOT obtain
    # positive proof that the order did not fill at the platform (lookup
    # raised, lookup found orphans that we cancelled, cancel itself failed,
    # or lookup returned empty without a verified no-fill check). The
    # fallback chain in ``_execute_with_fallbacks`` MUST refuse to retry
    # with a new client_order_id when this flag is set — a racing fill
    # would stack with the retry and produce double exposure.
    idempotency_ambiguous: bool = False

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "platform": self.platform,
            "market_id": self.market_id,
            "canonical_id": self.canonical_id,
            "side": self.side,
            "price": round(self.price, 4),
            "quantity": self.quantity,
            "status": self.status.value,
            "fill_price": round(self.fill_price, 4),
            "fill_qty": self.fill_qty,
            "timestamp": self.timestamp,
            "error": self.error,
            "external_client_order_id": self.external_client_order_id,
        }

    def to_audit_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "platform": self.platform,
            "market_id": self.market_id,
            "canonical_id": self.canonical_id,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "status": self.status.value,
            "fill_price": self.fill_price,
            "fill_qty": self.fill_qty,
            "timestamp": self.timestamp,
            "error": self.error,
            "external_client_order_id": self.external_client_order_id,
        }


@dataclass
class ExecutionIncident:
    incident_id: str
    arb_id: str
    canonical_id: str
    severity: str
    message: str
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: str = "open"
    resolved_at: float = 0.0
    resolution_note: str = ""

    def to_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "arb_id": self.arb_id,
            "canonical_id": self.canonical_id,
            "severity": self.severity,
            "message": self.message,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "status": self.status,
            "resolved_at": self.resolved_at,
            "resolution_note": self.resolution_note,
        }


@dataclass
class ManualPosition:
    position_id: str
    canonical_id: str
    description: str
    instructions: str
    yes_platform: str
    no_platform: str
    quantity: int
    yes_price: float
    no_price: float
    status: str = "awaiting-entry"
    timestamp: float = 0.0
    updated_at: float = 0.0
    entry_confirmed_at: float = 0.0
    closed_at: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "canonical_id": self.canonical_id,
            "description": self.description,
            "instructions": self.instructions,
            "yes_platform": self.yes_platform,
            "no_platform": self.no_platform,
            "quantity": self.quantity,
            "yes_price": round(self.yes_price, 4),
            "no_price": round(self.no_price, 4),
            "status": self.status,
            "timestamp": self.timestamp,
            "updated_at": self.updated_at,
            "entry_confirmed_at": self.entry_confirmed_at,
            "closed_at": self.closed_at,
            "note": self.note,
        }


@dataclass
class ArbExecution:
    arb_id: str
    opportunity: ArbitrageOpportunity
    leg_yes: Order
    leg_no: Order
    status: str = "pending"
    realized_pnl: float = 0.0
    # Naked-leg unwind PnL, tracked separately from arb edge (audit 2026-05).
    # ``realized_pnl`` = arb_pnl + unwind_pnl; consumers that need the pure
    # arbitrage PnL (e.g. the profitability gate) compute ``arb_pnl``
    # property below.  Pre-migration trades default to 0 and so report their
    # full realized_pnl as arb_pnl, which is a known mis-attribution for
    # historical rows only.
    unwind_pnl: float = 0.0
    timestamp: float = 0.0
    notes: List[str] = field(default_factory=list)
    # Markdown post-mortem populated by ``_build_inline_analysis`` right before
    # the audit write. Empty until the trade reaches a recordable state.
    analysis_md: str = ""

    @property
    def arb_pnl(self) -> float:
        """Pure arbitrage PnL: total realized minus naked-leg unwind PnL.

        Both legs filled => arb_pnl > 0 reflects captured edge.
        Naked-leg auto-unwind => unwind_pnl can be ±; arb_pnl is what would
        have been realized if the secondary had filled paired.
        """
        return float(self.realized_pnl) - float(self.unwind_pnl)

    def to_dict(self) -> dict:
        return {
            "arb_id": self.arb_id,
            "opportunity": self.opportunity.to_dict(),
            "leg_yes": self.leg_yes.to_dict(),
            "leg_no": self.leg_no.to_dict(),
            "status": self.status,
            "realized_pnl": round(self.realized_pnl, 4),
            "unwind_pnl": round(self.unwind_pnl, 4),
            "arb_pnl": round(self.arb_pnl, 4),
            "timestamp": self.timestamp,
            "notes": self.notes,
            "analysis_md": self.analysis_md,
            # RetryScheduler attaches this attribute dynamically when a failed
            # arb finishes its retry chain (see retry_scheduler.py:387).
            "failure_details": getattr(self, "failure_details", None),
        }

    def to_audit_dict(self) -> dict:
        return {
            "arb_id": self.arb_id,
            "opportunity": self.opportunity.to_audit_dict(),
            "leg_yes": self.leg_yes.to_audit_dict(),
            "leg_no": self.leg_no.to_audit_dict(),
            "status": self.status,
            "realized_pnl": self.realized_pnl,
            "unwind_pnl": self.unwind_pnl,
            "arb_pnl": self.arb_pnl,
            "timestamp": self.timestamp,
            "notes": list(self.notes),
        }


class RiskManager:
    def __init__(
        self,
        config: ScannerConfig,
        safety_config: Optional["SafetyConfig"] = None,
    ):
        self.config = config
        self._safety_config = safety_config
        # Plan 03-02 (SAFE-02): per-platform exposure ceiling. When no
        # SafetyConfig is supplied (legacy callers / tests), fall back to
        # +inf so existing behaviour is preserved.
        self._max_platform_exposure: float = (
            safety_config.max_platform_exposure_usd
            if safety_config is not None
            else float("inf")
        )
        self._open_positions: Dict[str, float] = {}
        # Plan 03-02: aggregate exposure by platform (keyed by platform name,
        # e.g. "kalshi", "polymarket"). Populated via record_trade when
        # callers supply a platform kwarg; unused by legacy callers.
        self._platform_exposures: Dict[str, float] = {}
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        # Env-driven risk limits (2026-05-23 audit). Defaults preserve the
        # historical hard-coded values so existing deployments behave
        # identically without env changes.
        import os as _os_risk
        def _risk_int(name: str, default: int) -> int:
            try:
                return int(_os_risk.getenv(name, "") or default)
            except (TypeError, ValueError):
                return default
        def _risk_float(name: str, default: float) -> float:
            try:
                return float(_os_risk.getenv(name, "") or default)
            except (TypeError, ValueError):
                return default
        self._max_daily_trades: int = _risk_int("MAX_DAILY_TRADES", 100)
        self._max_daily_loss: float = _risk_float("MAX_DAILY_LOSS_USD", -50.0)
        self._max_total_exposure: float = _risk_float(
            "MAX_TOTAL_EXPOSURE_USD", 500.0,
        )
        # Daily-window anchor for MAX_DAILY_TRADES / MAX_DAILY_LOSS_USD.
        # Without this the "daily" counters were lifetime counters: after
        # 100 trades the engine halted permanently (live 2026-07-10 20:04Z,
        # "Daily trade limit reached" with no reset path short of a container
        # restart — which silently zeroed them anyway, so the lifetime
        # reading provided no real guarantee either). The window rolls at
        # UTC midnight; positional exposure caps are NOT daily and are
        # deliberately untouched by the rollover.
        self._daily_window_day: str = self._utc_day()

    @staticmethod
    def _utc_day() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _roll_daily_window(self) -> None:
        today = self._utc_day()
        if today != self._daily_window_day:
            logger.warning(
                "RiskManager: daily window rolled %s -> %s "
                "(resetting daily_trades=%d, daily_pnl=%.4f)",
                self._daily_window_day, today,
                self._daily_trades, self._daily_pnl,
            )
            self._daily_window_day = today
            self._daily_trades = 0
            self._daily_pnl = 0.0

    def check_trade(self, opp: ArbitrageOpportunity) -> Tuple[bool, str]:
        if opp.status not in {"tradable", "manual"}:
            return False, f"Opportunity not ready: {opp.status}"
        if opp.confidence < self.config.confidence_threshold and not opp.requires_manual:
            return False, f"Low confidence: {opp.confidence:.2f}"
        # PROFITABILITY-01 (2026-05-28): pair-aware edge floor so K×FX / P×FX
        # opps with venue-specific lower fees (FX flat 0.5¢/contract vs Kalshi
        # quadratic) aren't auto-rejected by the legacy 7¢ global floor. The
        # scanner already uses min_edge_for_pair; the executor preflight too.
        # The risk manager was the last gate still reading min_edge_cents
        # directly — every K×FX opp was reaching engine.execute_opportunity
        # and being rejected here with "Edge too thin" + 5-min cooldown.
        pair_min_edge = self.config.min_edge_for_pair(
            opp.yes_platform, opp.no_platform,
        )
        if opp.net_edge_cents < pair_min_edge:
            return False, f"Edge too thin: {opp.net_edge_cents:.2f}¢"
        if opp.quote_age_seconds > self.config.max_quote_age_seconds:
            return False, f"Stale quote: {opp.quote_age_seconds:.2f}s"
        self._roll_daily_window()
        if self._daily_trades >= self._max_daily_trades:
            return False, "Daily trade limit reached"
        if self._daily_pnl <= self._max_daily_loss:
            return False, "Daily loss limit reached"

        exposure = opp.suggested_qty * (opp.yes_price + opp.no_price)
        existing = self._open_positions.get(opp.canonical_id, 0.0)
        if existing + exposure > self.config.max_position_usd:
            return False, "Per-market exposure limit exceeded"

        # Plan 03-02 (SAFE-02): per-platform exposure ceiling. Each leg
        # of a cross-platform arb lands on a different venue; we check
        # BOTH legs independently against SafetyConfig.max_platform_exposure_usd.
        # Fires after per-market so the most-specific limit rules first.
        yes_leg_exposure = opp.suggested_qty * opp.yes_price
        no_leg_exposure = opp.suggested_qty * opp.no_price
        leg_plan = [
            (opp.yes_platform, yes_leg_exposure),
            (opp.no_platform, no_leg_exposure),
        ]
        # Aggregate same-platform legs (defensive — cross-platform arbs
        # shouldn't land on the same venue, but the scanner filter might
        # evolve).
        platform_add: Dict[str, float] = {}
        for platform, leg_exposure in leg_plan:
            platform_add[platform] = platform_add.get(platform, 0.0) + leg_exposure
        for platform, add in platform_add.items():
            existing_platform = self._platform_exposures.get(platform, 0.0)
            if existing_platform + add > self._max_platform_exposure:
                return False, f"Per-platform exposure limit exceeded on {platform}"

        total_exposure = sum(self._open_positions.values()) + exposure
        if total_exposure > self._max_total_exposure:
            return False, "Total exposure limit exceeded"
        return True, "approved"

    def record_trade(
        self,
        canonical_id: str,
        exposure: float,
        pnl: float = 0.0,
        *,
        platform: Optional[str] = None,
        yes_platform: Optional[str] = None,
        no_platform: Optional[str] = None,
        yes_exposure: float = 0.0,
        no_exposure: float = 0.0,
    ):
        self._open_positions[canonical_id] = self._open_positions.get(canonical_id, 0.0) + exposure
        self._daily_pnl += pnl
        self._daily_trades += 1
        # Plan 03-02: per-platform accounting. Two modes:
        #   (A) single platform + full exposure (test helper / simple cases):
        #       record_trade(id, 250.0, platform="kalshi")
        #   (B) cross-platform arb leg split:
        #       record_trade(id, total, yes_platform=..., no_platform=...,
        #                    yes_exposure=..., no_exposure=...)
        # Legacy callers pass none → no per-platform side effect.
        if platform is not None:
            self._platform_exposures[platform] = (
                self._platform_exposures.get(platform, 0.0) + exposure
            )
        if yes_platform is not None and yes_exposure:
            self._platform_exposures[yes_platform] = (
                self._platform_exposures.get(yes_platform, 0.0) + yes_exposure
            )
        if no_platform is not None and no_exposure:
            self._platform_exposures[no_platform] = (
                self._platform_exposures.get(no_platform, 0.0) + no_exposure
            )

    def release_trade(
        self,
        canonical_id: str,
        exposure: float,
        pnl: float = 0.0,
        *,
        platform: Optional[str] = None,
        yes_platform: Optional[str] = None,
        no_platform: Optional[str] = None,
        yes_exposure: float = 0.0,
        no_exposure: float = 0.0,
    ):
        remaining = max(self._open_positions.get(canonical_id, 0.0) - exposure, 0.0)
        if remaining > 0:
            self._open_positions[canonical_id] = remaining
        else:
            self._open_positions.pop(canonical_id, None)
        self._daily_pnl += pnl
        # Plan 03-02: mirror per-platform subtraction; pop key when it drops
        # to zero or below (negatives should not linger in accounting).
        def _decrement(platform_name: str, amount: float) -> None:
            if not platform_name or not amount:
                return
            current = self._platform_exposures.get(platform_name, 0.0)
            new_value = current - amount
            if new_value > 0:
                self._platform_exposures[platform_name] = new_value
            else:
                self._platform_exposures.pop(platform_name, None)

        if platform is not None:
            _decrement(platform, exposure)
        if yes_platform is not None and yes_exposure:
            _decrement(yes_platform, yes_exposure)
        if no_platform is not None and no_exposure:
            _decrement(no_platform, no_exposure)


class ExecutionEngine:
    def __init__(
        self,
        config: ArbiterConfig,
        balance_monitor: BalanceMonitor,
        price_store: Optional[PriceStore] = None,
        collectors: Optional[Dict[str, Any]] = None,
        adapters: Optional[Dict[str, "PlatformAdapter"]] = None,
        store: Optional["ExecutionStore"] = None,
        execution_timeout_s: float = 10.0,
        *,
        safety: Optional["SafetySupervisor"] = None,
    ):
        self.config = config
        self.scanner_config = config.scanner
        self.balance_monitor = balance_monitor
        self.price_store = price_store
        self.risk = RiskManager(config.scanner, safety_config=getattr(config, "safety", None))
        self._running = False
        # Capped at MAX_EXECUTION_HISTORY to prevent unbounded memory growth in
        # 24/7 operation. Operators querying history beyond this should pull from
        # the persistent ExecutionStore (PostgreSQL) which retains everything.
        self._executions: List[ArbExecution] = []
        self._execution_count = 0
        self._collectors = collectors or {}
        self._own_session: Optional[aiohttp.ClientSession] = None
        self._poly_clob_client = None
        self._heartbeat_running = False
        self._subscribers: List[asyncio.Queue] = []
        self._incident_subscribers: List[asyncio.Queue] = []
        self._incidents: Deque[ExecutionIncident] = deque(maxlen=200)
        self._manual_positions: Deque[ManualPosition] = deque(maxlen=200)
        self._recent_signatures: Dict[str, float] = {}
        self._signatures_last_pruned: float = time.time()
        # Per-canonical lock map: prevents two opportunities for the same
        # canonical_id from racing through execute_opportunity concurrently.
        # The 5-second signature dedup window catches re-emits of the same
        # opp, but two *different* opps for the same market arriving > 5s
        # apart could still both pass the per-market exposure check
        # because _open_positions is mutated only after the trade records.
        # (2026-05-23 audit finding #10.)
        self._canonical_locks: Dict[str, asyncio.Lock] = {}
        self._canonical_locks_guard: asyncio.Lock = asyncio.Lock()
        self._aborted_count = 0
        self._manual_count = 0
        self._recovery_count = 0
        # Post-trade naked-leg tracking (every event where one leg
        # confirms FILLED while the other is non-FILLED at the moment we
        # finalize the execution record — i.e. either terminally failed
        # OR still SUBMITTED/resting). Surfaced via .stats.
        self._naked_leg_count = 0
        self._naked_leg_exposure_usd = 0.0
        import os as _os_init
        _p5 = float(_os_init.getenv("PHASE5_MAX_ORDER_USD", "0") or "0")
        _mp = float(_os_init.getenv("MAX_POSITION_USD", "0") or "0")
        _audit_cap = _p5 or _mp or config.scanner.max_position_usd
        self._auditor = MathAuditor(
            max_position_usd=_audit_cap,
        )
        self._trade_gate = None
        # Plan 02-06 integration: adapters + store + per-leg timeout
        self.adapters: Dict[str, "PlatformAdapter"] = adapters or {}
        self.store: Optional["ExecutionStore"] = store
        self.execution_timeout_s: float = execution_timeout_s
        # Plan 03-01: late-injected reference to SafetySupervisor for the
        # one-leg hook (plan 03-03) and shutdown trip (plan 03-05).
        self._safety: Optional["SafetySupervisor"] = safety
        # C4.2: when any ExecutionStore write raises, _handle_db_failure
        # sets this flag and trips the SafetySupervisor. While True the
        # engine refuses to place new orders — a live trade with no
        # audit trail is worse than a missed opportunity.
        self._db_write_failed: bool = False
        # INTER_LEG_DELAY_MS: pause between primary fill and secondary
        # placement so the second venue's orderbook can stabilize after
        # the first leg moves the cross-venue price. Default 500ms.
        try:
            self._inter_leg_delay_ms = float(
                _os_init.getenv("INTER_LEG_DELAY_MS", "500") or "500"
            )
        except (TypeError, ValueError):
            self._inter_leg_delay_ms = 500.0
        self._inter_leg_delay_ms = max(0.0, self._inter_leg_delay_ms)

        # SECONDARY_REQUOTE_MAX_ATTEMPTS / SECONDARY_REQUOTE_DELAY_MS:
        # after a CLEAN secondary failure (or an EDGE-LOST pre-submit
        # abort), re-walk the secondary book up to N times, DELAY ms
        # apart, and retry an IOC while the walked price is affordable.
        # Books that moved one tick between walk and submit routinely
        # come back within a second — one stale IOC + a doomed same-price
        # FOK was the single biggest naked-leg factory (93 naked arbs of
        # 402 executions). 0 attempts restores the old behavior.
        try:
            self._secondary_requote_attempts = int(
                _os_init.getenv("SECONDARY_REQUOTE_MAX_ATTEMPTS", "2") or "2"
            )
        except (TypeError, ValueError):
            self._secondary_requote_attempts = 2
        self._secondary_requote_attempts = max(0, self._secondary_requote_attempts)
        try:
            self._secondary_requote_delay_ms = float(
                _os_init.getenv("SECONDARY_REQUOTE_DELAY_MS", "250") or "250"
            )
        except (TypeError, ValueError):
            self._secondary_requote_delay_ms = 250.0
        self._secondary_requote_delay_ms = max(0.0, self._secondary_requote_delay_ms)

        # SMART_UNWIND_TIMEOUT_S: how long the recovery loop rests a
        # break-even SELL before falling back to the market-IOC panic
        # sell.  Default 30s — long enough for a buyer to take the bid
        # at our cost basis on most sports/political markets, short
        # enough that residual exposure stays bounded.  Set to 0 to
        # disable smart-unwind entirely (always market-sells).
        try:
            self._smart_unwind_timeout_s = float(
                _os_init.getenv("SMART_UNWIND_TIMEOUT_S", "30") or "30"
            )
        except (TypeError, ValueError):
            self._smart_unwind_timeout_s = 30.0
        self._smart_unwind_timeout_s = max(0.0, self._smart_unwind_timeout_s)

        # C2 — POST-SUBMIT POLL:
        # When ``_place_order_for_leg`` returns SUBMITTED (order accepted
        # onto the venue book but unfilled), poll ``adapter.get_order``
        # until the order reaches a terminal status or this hard timeout
        # fires.  Without this, a venue that fills the order later leaves
        # the engine's DB row + RiskManager exposure pegged to SUBMITTED
        # forever — silent divergence from venue reality.
        try:
            self._submit_poll_timeout_s = float(
                _os_init.getenv("SUBMIT_POLL_TIMEOUT_S", "10") or "10"
            )
        except (TypeError, ValueError):
            self._submit_poll_timeout_s = 10.0
        self._submit_poll_timeout_s = max(0.0, self._submit_poll_timeout_s)
        self._submit_poll_interval_s = 0.5
        # ForecastEx fills always confirm on the first ~0.5s poll (live:
        # 100/100 fills confirmed sub-second). A failed FX secondary should
        # not leave the primary naked for the full kalshi window — a tighter
        # FX-scoped timeout shrinks the naked window (live 2026-07-10:
        # ARB-000941's kalshi YES sat naked ~64s while FX retries burned the
        # 15s-per-attempt poll). Env-overridable.
        try:
            self._submit_poll_timeout_forecastex_s = float(
                _os_init.getenv("SUBMIT_POLL_TIMEOUT_FORECASTEX_S", "3") or "3"
            )
        except (TypeError, ValueError):
            self._submit_poll_timeout_forecastex_s = 3.0
        self._submit_poll_timeout_forecastex_s = max(
            0.0, self._submit_poll_timeout_forecastex_s
        )

    def set_trade_gate(self, gate) -> None:
        self._trade_gate = gate

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        return queue

    def subscribe_incidents(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._incident_subscribers.append(queue)
        return queue

    async def _get_canonical_lock(self, canonical_id: str) -> asyncio.Lock:
        """Return (creating on demand) the per-canonical execution lock."""
        async with self._canonical_locks_guard:
            lock = self._canonical_locks.get(canonical_id)
            if lock is None:
                lock = asyncio.Lock()
                self._canonical_locks[canonical_id] = lock
            return lock

    async def execute_opportunity(self, opp: ArbitrageOpportunity) -> Optional[ArbExecution]:
        # Per-canonical serialization: two opportunities for the same market
        # cannot be executed concurrently. Prevents the race where the
        # per-market exposure check passes for both because _open_positions
        # is mutated only after each trade completes.
        canonical_lock = await self._get_canonical_lock(opp.canonical_id)
        async with canonical_lock:
            return await self._execute_opportunity_locked(opp)

    async def _execute_opportunity_locked(self, opp: ArbitrageOpportunity) -> Optional[ArbExecution]:
        approved, reason = self.risk.check_trade(opp)
        if not approved:
            # Plan 03-02 (SAFE-02): surface every risk-rejection as a
            # structured `order_rejected` ExecutionIncident so operators
            # can see safety decisions in real time via the existing
            # incident WebSocket event (no new event type).
            await self._emit_rejection_incident(opp, reason)
            logger.info("Trade rejected by risk manager: %s", reason)
            return None

        signature = f"{opp.key()}:{opp.status}"
        now_ts = time.time()
        last_seen = self._recent_signatures.get(signature, 0.0)
        if now_ts - last_seen < SIGNATURE_DEDUP_WINDOW_S:
            return None
        self._recent_signatures[signature] = now_ts
        # Prune the dedup map periodically so it can't grow without bound.
        # Cheap: only walks the dict every SIGNATURE_PRUNE_INTERVAL_S seconds.
        if now_ts - self._signatures_last_pruned > SIGNATURE_PRUNE_INTERVAL_S:
            cutoff = now_ts - SIGNATURE_DEDUP_WINDOW_S * 4
            self._recent_signatures = {
                key: ts for key, ts in self._recent_signatures.items() if ts > cutoff
            }
            self._signatures_last_pruned = now_ts

        # Bump counter + bind contextvars early (OPS-01 / Pitfall 6) — every
        # downstream log line will carry arb_id + canonical_id until the finally
        # block clears them.
        self._execution_count += 1
        arb_id = f"ARB-{self._execution_count:06d}"

        clear_contextvars()
        bind_contextvars(
            arb_id=arb_id,
            canonical_id=opp.canonical_id,
            platform_yes=opp.yes_platform,
            platform_no=opp.no_platform,
        )
        try:
            # ── SAFETY: Reject non-confirmed mappings at execution level ──
            # Defense-in-depth: even if scanner somehow marks a non-confirmed
            # mapping as tradable, the engine MUST refuse to trade it.
            if opp.mapping_status != "confirmed":
                logger.warning(
                    "REJECTED %s: mapping_status=%s (only confirmed mappings can trade). canonical=%s",
                    arb_id, opp.mapping_status, opp.canonical_id,
                )
                self._aborted_count += 1
                return None

            # ── SAFETY: Require identical resolution criteria for live trading ──
            # Defense-in-depth: even if scanner somehow marks a mapping as
            # tradable, the engine verifies resolution_match_status == "identical".
            # This prevents any mapping with unverified resolution criteria
            # from reaching real-money execution.
            from ..config.settings import get_market_mapping
            live_mapping = get_market_mapping(opp.canonical_id) or {}
            res_match = str(live_mapping.get("resolution_match_status", "pending_operator_review")).lower()
            if res_match != "identical":
                logger.warning(
                    "REJECTED %s: resolution_match_status=%s (only identical resolution "
                    "criteria can trade live). canonical=%s",
                    arb_id, res_match, opp.canonical_id,
                )
                self._aborted_count += 1
                return None

            gate_allowed, gate_reason, gate_context = await self._check_trade_gate(opp)
            if not gate_allowed:
                self._aborted_count += 1
                if not self.scanner_config.dry_run:
                    await self._record_incident(
                        arb_id,
                        opp,
                        "warning",
                        f"Trade gate blocked execution: {gate_reason}",
                        metadata={"event_type": "trade_gate_blocked", "gate": gate_context},
                    )
                return None

            if opp.requires_manual:
                if not await self._audit_opportunity(arb_id, opp):
                    self._aborted_count += 1
                    return None
                execution = await self._queue_manual_execution(arb_id, opp)
                await self._audit_execution(execution)
                await self._publish_execution(execution)
                return execution

            requoted = await self._pre_trade_requote(arb_id, opp)
            if not requoted:
                self._aborted_count += 1
                return None

            if not await self._audit_opportunity(arb_id, requoted):
                self._aborted_count += 1
                return None

            if self.scanner_config.dry_run:
                execution = await self._simulate_execution(arb_id, requoted)
            else:
                execution = await self._live_execution(arb_id, requoted)

            # _live_execution returns None when it pre-aborts before placing
            # any order — e.g. the orderbook is too thin to fill the requested
            # qty, or the book-walked price would push net edge below the
            # minimum. No order was placed, so there is nothing to audit,
            # publish, or alert on; treat it as a clean abort.
            if execution is None:
                self._aborted_count += 1
                return None

            await self._audit_execution(execution)
            await self._publish_execution(execution)

            # Send detailed Telegram notification with execution result
            try:
                await self.balance_monitor.alert_execution_result(
                    arb_id=execution.arb_id,
                    opp=execution.opportunity,
                    status=execution.status,
                    leg_yes=execution.leg_yes,
                    leg_no=execution.leg_no,
                    realized_pnl=execution.realized_pnl,
                )
            except Exception as exc:
                logger.warning("Telegram execution result alert failed: %s", exc)

            return execution
        finally:
            clear_contextvars()

    async def _queue_manual_execution(self, arb_id: str, opp: ArbitrageOpportunity) -> ArbExecution:
        now = time.time()
        instructions = (
            f"Manual workflow required. Buy YES on {opp.yes_platform.upper()} at ${opp.yes_price:.2f} "
            f"and buy NO on {opp.no_platform.upper()} at ${opp.no_price:.2f} for {opp.suggested_qty} contracts. "
            "Confirm the manual leg in the dashboard before hedging or unwinding."
        )
        manual_position = ManualPosition(
            position_id=f"MANUAL-{arb_id}",
            canonical_id=opp.canonical_id,
            description=opp.description,
            instructions=instructions,
            yes_platform=opp.yes_platform,
            no_platform=opp.no_platform,
            quantity=opp.suggested_qty,
            yes_price=opp.yes_price,
            no_price=opp.no_price,
            status="awaiting-entry",
            timestamp=now,
            updated_at=now,
        )
        self._manual_positions.appendleft(manual_position)
        self._manual_count += 1
        from ..notifiers.fmt import DIVIDER as _DIV, h as _h
        await self.balance_monitor.notifier.send(
            f"\U0001f4cb <b>MANUAL TRADE REQUIRED</b>\n"
            f"{_DIV}\n"
            f"{_h(instructions)}\n"
            f"{_DIV}\n"
            f"⚠️ <i>Execute both legs manually on the venues.</i>",
        )

        leg_yes = Order(
            order_id=f"{arb_id}-YES-MANUAL",
            platform=opp.yes_platform,
            market_id=opp.yes_market_id,
            canonical_id=opp.canonical_id,
            side="yes",
            price=opp.yes_price,
            quantity=opp.suggested_qty,
            status=OrderStatus.PENDING,
            timestamp=now,
            error="Manual execution required",
        )
        leg_no = Order(
            order_id=f"{arb_id}-NO-MANUAL",
            platform=opp.no_platform,
            market_id=opp.no_market_id,
            canonical_id=opp.canonical_id,
            side="no",
            price=opp.no_price,
            quantity=opp.suggested_qty,
            status=OrderStatus.PENDING,
            timestamp=now,
            error="Manual execution required",
        )
        execution = ArbExecution(
            arb_id=arb_id,
            opportunity=opp,
            leg_yes=leg_yes,
            leg_no=leg_no,
            status="manual_pending",
            realized_pnl=0.0,
            timestamp=now,
            notes=["Manual workflow queued"],
        )
        self._executions.append(execution)
        _trim_executions(self._executions)
        return execution

    async def update_manual_position(self, position_id: str, action: str, note: str = "") -> Optional[ManualPosition]:
        action = str(action or "").strip().lower()
        if action not in {"mark_entered", "mark_closed", "cancel"}:
            raise ValueError(f"Unsupported manual position action: {action or 'unknown'}")

        now = time.time()
        for index, existing in enumerate(self._manual_positions):
            if existing.position_id != position_id:
                continue

            if action == "mark_entered" and existing.status in {"closed", "cancelled"}:
                raise ValueError(f"Cannot mark {existing.status} manual position as entered")
            if action == "mark_closed" and existing.status == "cancelled":
                raise ValueError("Cannot close a cancelled manual position")
            if action == "cancel" and existing.status == "closed":
                raise ValueError("Cannot cancel a closed manual position")

            status = existing.status
            entry_confirmed_at = existing.entry_confirmed_at
            closed_at = existing.closed_at
            if action == "mark_entered":
                status = "entered"
                entry_confirmed_at = existing.entry_confirmed_at or now
            elif action == "mark_closed":
                status = "closed"
                entry_confirmed_at = existing.entry_confirmed_at or now
                closed_at = now
            elif action == "cancel":
                status = "cancelled"
                closed_at = existing.closed_at or now

            updated = replace(
                existing,
                status=status,
                updated_at=now,
                entry_confirmed_at=entry_confirmed_at,
                closed_at=closed_at,
                note=self._merge_note(existing.note, note),
            )
            self._manual_positions[index] = updated

            execution = self._update_manual_execution(updated, note)
            if execution is not None:
                await self._publish_execution(execution)
            return updated
        return None

    async def resolve_incident(self, incident_id: str, note: str = "") -> Optional[ExecutionIncident]:
        now = time.time()
        for index, existing in enumerate(self._incidents):
            if existing.incident_id != incident_id:
                continue
            updated = replace(
                existing,
                status="resolved",
                resolved_at=existing.resolved_at or now,
                resolution_note=self._merge_note(existing.resolution_note, note),
            )
            self._incidents[index] = updated
            for subscriber in list(self._incident_subscribers):
                try:
                    subscriber.put_nowait(updated)
                except asyncio.QueueFull:
                    logger.debug("Skipping slow incident subscriber")
            # EXEC-02 / D-16: mirror the resolution to Postgres
            # (insert_incident's ON CONFLICT handles the update path).
            if self.store is not None:
                try:
                    await self.store.insert_incident(updated)
                except Exception as exc:
                    await self._handle_db_failure(
                        op="insert_incident_resolve",
                        arb_id=getattr(updated, "arb_id", None),
                        canonical_id=getattr(updated, "canonical_id", None),
                        exc=exc,
                    )
            return updated
        return None

    async def _pre_trade_requote(self, arb_id: str, opp: ArbitrageOpportunity) -> Optional[ArbitrageOpportunity]:
        if not self.price_store:
            return opp

        current_yes = await self.price_store.get(opp.yes_platform, opp.canonical_id)
        current_no = await self.price_store.get(opp.no_platform, opp.canonical_id)
        if not current_yes or not current_no:
            await self._record_incident(arb_id, opp, "warning", "Missing fresh quotes during pre-trade re-quote")
            return None

        age = max(current_yes.age_seconds, current_no.age_seconds)
        if age > self.scanner_config.max_quote_age_seconds:
            await self._record_incident(
                arb_id,
                opp,
                "warning",
                f"Quotes became stale before execution ({age:.2f}s)",
            )
            return None

        # CV-04 (2026-06-05): tighter FX-specific freshness ceiling. The
        # 5/28 K×FX abort cascade (16 trades, all aborted at the
        # secondary-leg adverse-move guard) traced back to an FX leg
        # whose snapshot was several seconds old by the time the
        # primary filled — IBKR's Client Portal feed for FORECASTX
        # binaries can stretch quiet for 5-10s on low-volume markets.
        # Refuse to cross the primary when the FX-side price is stale
        # relative to its venue's update cadence, even though the
        # global 15s ceiling allows it.
        fx_age_max = float(getattr(
            self.scanner_config, "max_quote_age_seconds_forecastex",
            self.scanner_config.max_quote_age_seconds,
        ))
        if opp.yes_platform == "forecastex" and current_yes.age_seconds > fx_age_max:
            await self._record_incident(
                arb_id,
                opp,
                "warning",
                f"ForecastEx YES quote stale ({current_yes.age_seconds:.2f}s > {fx_age_max:.1f}s)",
                metadata={"event_type": "fx_quote_stale", "side": "yes"},
            )
            return None
        if opp.no_platform == "forecastex" and current_no.age_seconds > fx_age_max:
            await self._record_incident(
                arb_id,
                opp,
                "warning",
                f"ForecastEx NO quote stale ({current_no.age_seconds:.2f}s > {fx_age_max:.1f}s)",
                metadata={"event_type": "fx_quote_stale", "side": "no"},
            )
            return None

        yes_price = current_yes.yes_price
        no_price = current_no.no_price

        # Quote-sanity check: compare the PriceStore quote against a fresh BBO
        # from the platform adapter. Polymarket rejections trace back to stale quotes.
        if not await self._sanity_check_quote(arb_id, opp, "yes", current_yes, yes_price):
            return None
        if not await self._sanity_check_quote(arb_id, opp, "no", current_no, no_price):
            return None
        if abs(yes_price - opp.yes_price) > self.scanner_config.slippage_tolerance or abs(no_price - opp.no_price) > self.scanner_config.slippage_tolerance:
            await self._record_incident(
                arb_id,
                opp,
                "warning",
                "Slippage exceeded tolerance during re-quote",
                metadata={
                    "original_yes": opp.yes_price,
                    "current_yes": yes_price,
                    "original_no": opp.no_price,
                    "current_no": no_price,
                },
            )
            return None

        gross_edge = 1.0 - yes_price - no_price
        total_fees = (
            compute_fee(opp.yes_platform, yes_price, opp.suggested_qty, current_yes.fee_rate)
            + compute_fee(opp.no_platform, no_price, opp.suggested_qty, current_no.fee_rate)
        ) / max(opp.suggested_qty, 1)
        net_edge = gross_edge - total_fees
        net_edge_cents = net_edge * 100.0
        pair_min_edge = self.scanner_config.min_edge_for_pair(
            opp.yes_platform, opp.no_platform,
        )
        if net_edge_cents < pair_min_edge:
            await self._record_incident(
                arb_id,
                opp,
                "warning",
                f"Edge collapsed below threshold after re-quote ({net_edge_cents:.2f}¢)",
            )
            return None

        return replace(
            opp,
            yes_price=yes_price,
            no_price=no_price,
            yes_market_id=current_yes.yes_market_id or opp.yes_market_id,
            no_market_id=current_no.no_market_id or opp.no_market_id,
            gross_edge=gross_edge,
            total_fees=total_fees,
            net_edge=net_edge,
            net_edge_cents=net_edge_cents,
            max_profit_usd=round(net_edge * opp.suggested_qty, 4),
            quote_age_seconds=age,
            timestamp=time.time(),
            yes_fee_rate=current_yes.fee_rate,
            no_fee_rate=current_no.fee_rate,
        )


    async def _sanity_check_quote(
        self,
        arb_id: str,
        opp: ArbitrageOpportunity,
        side: str,
        price_point: PricePoint,
        store_price: float,
    ) -> bool:
        """Verify store_price matches a fresh BBO from the platform adapter.

        Returns True when the quote is sane (or the adapter can't supply a
        fresh BBO). Returns False when discrepancy exceeds tolerance.
        """
        QUOTE_DESYNC_TOLERANCE = 0.02  # 2c

        platform = opp.yes_platform if side == "yes" else opp.no_platform
        adapter = self.adapters.get(platform)
        if adapter is None or not hasattr(adapter, "_get_bbo"):
            return True

        market_id = (
            getattr(price_point, "yes_market_id", "") if side == "yes"
            else getattr(price_point, "no_market_id", "")
        ) or opp.canonical_id

        try:
            best_bid, best_ask = await adapter._get_bbo(market_id)
        except Exception as exc:
            logger.debug(
                "  quote_sanity.bbo_fetch_failed platform=%s market=%s err=%s",
                platform, market_id, exc,
            )
            return True

        if best_bid <= 0.0 and best_ask <= 0.0:
            return True

        if side == "yes":
            fresh_ask = best_ask if best_ask > 0.0 else (1.0 - best_bid)
        else:
            fresh_ask = (1.0 - best_bid) if best_bid > 0.0 else (1.0 - best_ask)

        delta = abs(store_price - fresh_ask)
        if delta > QUOTE_DESYNC_TOLERANCE:
            logger.warning(
                "  quote_sanity.desync arb_id=%s platform=%s side=%s "
                "market=%s store=%.4f fresh=%.4f delta=%.4f — aborting",
                arb_id, platform, side, market_id,
                store_price, fresh_ask, delta,
            )
            await self._record_incident(
                arb_id, opp, "warning",
                f"Quote desync on {platform} {side}: store={store_price:.4f} "
                f"fresh={fresh_ask:.4f} delta={delta:.4f}",
                metadata={
                    "platform": platform,
                    "side": side,
                    "market_id": market_id,
                    "store_price": store_price,
                    "fresh_ask": fresh_ask,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                },
            )
            return False
        return True

    async def _audit_opportunity(self, arb_id: str, opp: ArbitrageOpportunity) -> bool:
        audit_result = self._auditor.audit_opportunity(opp.to_audit_dict())
        if audit_result.passed:
            return True

        severities = {flag.severity for flag in audit_result.flags}
        severity = "critical" if "critical" in severities else "warning"
        top_messages = "; ".join(flag.message for flag in audit_result.flags[:3])
        await self._record_incident(
            arb_id,
            opp,
            severity,
            "Shadow math audit rejected opportunity before execution",
            metadata={
                "audit": audit_result.to_dict(),
                "summary": top_messages,
            },
        )
        return False

    async def _audit_execution(self, execution: ArbExecution) -> None:
        audit_result = self._auditor.audit_execution(execution.to_audit_dict())
        if audit_result.passed:
            return

        severities = {flag.severity for flag in audit_result.flags}
        # Downgrade to warning for non-terminal states (submitted, partial)
        # where the trade may still succeed. Only use critical when both legs
        # have terminal outcomes AND the audit found critical flags.
        is_terminal = execution.status in ("filled", "failed")
        severity = "critical" if ("critical" in severities and is_terminal) else "warning"
        await self.record_incident(
            arb_id=execution.arb_id,
            canonical_id=execution.opportunity.canonical_id,
            severity=severity,
            message=f"Shadow execution audit flagged (trade status={execution.status})",
            metadata={"audit": audit_result.to_dict()},
        )

    async def _simulate_execution(self, arb_id: str, opp: ArbitrageOpportunity) -> ArbExecution:
        now = time.time()
        leg_yes = Order(
            order_id=f"{arb_id}-YES",
            platform=opp.yes_platform,
            market_id=opp.yes_market_id,
            canonical_id=opp.canonical_id,
            side="yes",
            price=opp.yes_price,
            quantity=opp.suggested_qty,
            status=OrderStatus.SIMULATED,
            fill_price=opp.yes_price,
            fill_qty=opp.suggested_qty,
            timestamp=now,
        )
        leg_no = Order(
            order_id=f"{arb_id}-NO",
            platform=opp.no_platform,
            market_id=opp.no_market_id,
            canonical_id=opp.canonical_id,
            side="no",
            price=opp.no_price,
            quantity=opp.suggested_qty,
            status=OrderStatus.SIMULATED,
            fill_price=opp.no_price,
            fill_qty=opp.suggested_qty,
            timestamp=now,
        )
        execution = ArbExecution(
            arb_id=arb_id,
            opportunity=opp,
            leg_yes=leg_yes,
            leg_no=leg_no,
            status="simulated",
            realized_pnl=opp.net_edge * opp.suggested_qty,
            timestamp=now,
        )
        self._executions.append(execution)
        _trim_executions(self._executions)
        self.risk.record_trade(
            opp.canonical_id,
            opp.suggested_qty * (opp.yes_price + opp.no_price),
            execution.realized_pnl,
            yes_platform=opp.yes_platform,
            no_platform=opp.no_platform,
            yes_exposure=opp.suggested_qty * opp.yes_price,
            no_exposure=opp.suggested_qty * opp.no_price,
        )
        return execution

    async def _live_execution(self, arb_id: str, opp: ArbitrageOpportunity) -> ArbExecution:
        now = time.time()

        # C5.1 first-leg atomicity: the legacy ``record_arb_stub`` call used
        # to live here, committing an ``execution_arbs`` row before any leg
        # was placed.  Anything between that commit and the first
        # ``_place_order_for_leg`` (book-walks, depth checks, profitability
        # re-validation) could ``return None`` or raise, leaving an arb stub
        # with zero legs — 169 of 295 prod phantoms followed this pattern.
        # The stub is now written atomically with the first leg's
        # ``execution_orders`` row (see ``atomic_arb_stub_opp`` in
        # ``_place_order_for_leg``) so the parent cannot exist without at
        # least one leg.  Early-return paths below place no orders and now
        # leave no DB rows behind.

        # ── Sequential leg execution (naked-position prevention) ─────
        # Execute legs SEQUENTIALLY instead of concurrently to prevent
        # naked positions:
        #   1. Fire the PRIMARY leg first (Kalshi = FOK, instant feedback).
        #   2. Only if primary fills → fire the SECONDARY leg (Polymarket).
        #   3. If secondary fails → log incident for manual resolution.
        # With FOK on Kalshi, the primary either fills completely or not
        # at all — no partial exposure risk on leg 1.

        # ── Pre-execution profitability gate ─────
        # Verify the arb is genuinely profitable after ALL fees before
        # risking any capital. Reads through scanner_config.min_edge_cents
        # so the engine, scanner, and AutoExecutor preflight all share the
        # MIN_EDGE_CENTS env var (default 7.0 per 2026-05 forensic audit:
        # median Poly adverse move 3.66¢).
        # ── Pre-trade live-balance gate (2026-05-23 audit finding #3) ─
        # Verify each platform still has enough balance to cover the leg
        # notional PLUS the configured reserve floor. The scanner sizes
        # against a cached BalanceMonitor snapshot which can be up to 30s
        # stale; this re-checks against the most recent snapshot before we
        # commit any capital. Skips silently when the snapshot is missing
        # (no balance feed configured) so dev/test paths stay unblocked.
        import os as _os_bal_gate
        try:
            _min_reserve_usd = float(
                _os_bal_gate.getenv("MIN_PLATFORM_RESERVE_USD", "20") or "20"
            )
        except (TypeError, ValueError):
            _min_reserve_usd = 20.0
        _bal_qty = max(1, int(opp.suggested_qty or 1))
        _leg_notionals = {
            opp.yes_platform: opp.yes_price * _bal_qty,
            opp.no_platform: opp.no_price * _bal_qty,
        }
        _balances_snap = getattr(self.balance_monitor, "_balances", {}) or {}
        for _bp_platform, _bp_notional in _leg_notionals.items():
            _snap = _balances_snap.get(_bp_platform)
            if _snap is None:
                continue
            _avail = float(getattr(_snap, "balance", 0.0) or 0.0)
            _required = float(_bp_notional) + _min_reserve_usd
            if _avail < _required:
                logger.warning(
                    "REJECTED %s: insufficient balance on %s "
                    "(available=$%.2f, required=$%.2f notional + $%.2f reserve). "
                    "canonical=%s",
                    arb_id, _bp_platform, _avail, _bp_notional,
                    _min_reserve_usd, opp.canonical_id,
                )
                try:
                    await self._record_incident(
                        arb_id, opp, "warning",
                        f"Pre-trade balance gate: {_bp_platform} short",
                        metadata={
                            "event_type": "balance_gate_reject",
                            "platform": _bp_platform,
                            "available_usd": _avail,
                            "leg_notional_usd": _bp_notional,
                            "reserve_floor_usd": _min_reserve_usd,
                            "qty": _bal_qty,
                        },
                    )
                except Exception as _exc:  # noqa: BLE001
                    logger.warning("balance_gate_reject incident emit failed: %s", _exc)
                self._aborted_count += 1
                return None

        # Pair-aware floor: matches scanner + risk-manager + auto-executor.
        MIN_NET_EDGE_CENTS = float(self.scanner_config.min_edge_for_pair(
            opp.yes_platform, opp.no_platform,
        ))
        total_cost = opp.yes_price + opp.no_price
        gross_edge = 1.0 - total_cost
        qty = max(1, int(opp.suggested_qty or 1))
        yes_fee = compute_fee(opp.yes_platform, opp.yes_price, qty, opp.yes_fee_rate)
        no_fee = compute_fee(opp.no_platform, opp.no_price, qty, opp.no_fee_rate)
        # Per-contract fees
        yes_fee_per = yes_fee / qty if qty > 0 else 0.0
        no_fee_per = no_fee / qty if qty > 0 else 0.0
        net_edge_after_fees = gross_edge - yes_fee_per - no_fee_per
        # Hoisted out of the else-branch below so the re-raise at the
        # bottom of execute_opportunity always has a definition. Without
        # this, the unprofitability early-return path falls through to
        # the record_arb / re-raise tail and hits an UnboundLocalError
        # on _secondary_exception (engine.py:2084). The variable is set
        # only by the secondary-leg try/except block; for IF-branch
        # ABORTED-legs and for paths that finish cleanly, it stays None.
        _secondary_exception: Optional[BaseException] = None
        if net_edge_after_fees * 100 < MIN_NET_EDGE_CENTS:
            logger.info(
                "Profitability gate: net_edge=%.4f (%.2f¢) below minimum %.1f¢, aborting",
                net_edge_after_fees, net_edge_after_fees * 100, MIN_NET_EDGE_CENTS,
            )
            leg_yes = Order(
                order_id=f"{arb_id}-YES-UNPROFITABLE",
                platform=opp.yes_platform, market_id=opp.yes_market_id,
                canonical_id=opp.canonical_id, side="yes",
                price=opp.yes_price, quantity=opp.suggested_qty,
                status=OrderStatus.ABORTED, timestamp=time.time(),
                error=f"Net edge {net_edge_after_fees*100:.2f}¢ below {MIN_NET_EDGE_CENTS}¢ minimum",
            )
            leg_no = Order(
                order_id=f"{arb_id}-NO-UNPROFITABLE",
                platform=opp.no_platform, market_id=opp.no_market_id,
                canonical_id=opp.canonical_id, side="no",
                price=opp.no_price, quantity=opp.suggested_qty,
                status=OrderStatus.ABORTED, timestamp=time.time(),
                error=f"Net edge {net_edge_after_fees*100:.2f}¢ below {MIN_NET_EDGE_CENTS}¢ minimum",
            )
            # Skip straight to status determination below
        else:
            # Exception-escape guard: any raise between primary fill and
            # secondary order construction (best_executable_price, compute_fee,
            # adapter call) used to escape _live_execution entirely, leaving
            # primary FILLED in DB with no secondary row and no naked incident.
            # The try/except around the secondary block below captures the
            # exception into _secondary_exception (hoisted above the IF/ELSE
            # so the re-raise tail always has a definition), builds an
            # ABORTED ``-EXCEPTION`` placeholder so the downstream naked
            # detection / supervisor fanout still fire, and we re-raise
            # after record_arb completes.
            # POLY-FIRST STRATEGY: Execute Polymarket first because it
            # reprices faster than Kalshi. Historical data shows 0/118
            # trades completed both legs with Kalshi-first — the Polymarket
            # book always moved past profitability before the secondary
            # could fill. By filling the fast market first, the slow market
            # (Kalshi) is more likely to still have the stale price.
            # Backward-compat: set EXECUTION_ORDER=kalshi_first to revert.
            import os as _os_exec_order
            _exec_order = _os_exec_order.environ.get("EXECUTION_ORDER", "poly_first")
            if _exec_order == "kalshi_first":
                # Legacy behavior
                if opp.yes_platform == "kalshi":
                    primary_side, secondary_side = "yes", "no"
                elif opp.no_platform == "kalshi":
                    primary_side, secondary_side = "no", "yes"
                else:
                    primary_side, secondary_side = "yes", "no"
            else:
                # Poly-first (default)
                if opp.yes_platform == "polymarket":
                    primary_side, secondary_side = "yes", "no"
                elif opp.no_platform == "polymarket":
                    primary_side, secondary_side = "no", "yes"
                elif opp.yes_platform == "polymarket_us":
                    primary_side, secondary_side = "yes", "no"
                elif opp.no_platform == "polymarket_us":
                    primary_side, secondary_side = "no", "yes"
                else:
                    # Neither is Polymarket — default to YES first
                    primary_side, secondary_side = "yes", "no"

            primary_platform = getattr(opp, f"{primary_side}_platform")
            primary_market = getattr(opp, f"{primary_side}_market_id")
            primary_price = getattr(opp, f"{primary_side}_price")
            secondary_platform = getattr(opp, f"{secondary_side}_platform")
            secondary_market = getattr(opp, f"{secondary_side}_market_id")
            secondary_price = getattr(opp, f"{secondary_side}_price")

            total_cost_per = opp.yes_price + opp.no_price
            total_cost_usd = total_cost_per * qty
            expected_profit_usd = net_edge_after_fees * qty
            logger.info(
                "═══ TRADE %s ═══ %s\n"
                "  PRIMARY:   BUY %s on %s @ $%.2f × %d = $%.2f\n"
                "  SECONDARY: BUY %s on %s @ $%.2f × %d = $%.2f\n"
                "  COST/PAIR: $%.4f | TOTAL COST: $%.2f\n"
                "  GROSS EDGE: %.2f¢ | FEES: %.2f¢ (YES %.2f¢ + NO %.2f¢) | NET EDGE: %.2f¢\n"
                "  EXPECTED PROFIT: $%.2f on %d contracts\n"
                "  MARKET IDs: %s (YES) / %s (NO)",
                arb_id, opp.description,
                primary_side.upper(), primary_platform, primary_price, qty, primary_price * qty,
                secondary_side.upper(), secondary_platform, secondary_price, qty, secondary_price * qty,
                total_cost_per, total_cost_usd,
                gross_edge * 100, (yes_fee_per + no_fee_per) * 100, yes_fee_per * 100, no_fee_per * 100,
                net_edge_after_fees * 100,
                expected_profit_usd, qty,
                opp.yes_market_id, opp.no_market_id,
            )

            # Step 0: Pre-flight orderbook depth check (EXEC-03)
            # Both adapters expose check_depth(market_id, side, qty) → (sufficient, best_price).
            # Reduce quantity progressively if the full amount isn't available.
            effective_qty = qty
            primary_adapter = self.adapters.get(primary_platform)
            if primary_adapter is not None and hasattr(primary_adapter, "check_depth"):
                depth_ok = False
                try_qty = qty
                while try_qty >= 1:
                    sufficient, best_price = await primary_adapter.check_depth(
                        primary_market, primary_side, try_qty,
                    )
                    if sufficient:
                        depth_ok = True
                        effective_qty = try_qty
                        if try_qty < qty:
                            logger.info(
                                "  ↓ Reduced qty %d→%d (orderbook depth insufficient for full size)",
                                qty, try_qty,
                            )
                        break
                    try_qty = try_qty // 2
                if not depth_ok:
                    logger.info(
                        "  ✗ SKIP: %s orderbook has no depth even for qty=1 on %s side (best=%.4f). "
                        "No order placed — zero exposure.",
                        primary_platform, primary_side, best_price,
                    )
                    # Return None — auto_executor will set cooldown for this canonical_id
                    return None

            # Step 0b: Resolve a FOK-safe primary price by walking the book.
            # Using `primary_price` (the opportunity quote, typically top-of-book)
            # is what caused 7/10 production trades to fail with Kalshi 409
            # `fill_or_kill_insufficient_resting_volume` — the level was thinner
            # than expected. `best_executable_price` returns the worst price
            # required to absorb effective_qty across all visible levels;
            # placing the FOK at that price means the order can sweep deeper
            # into the book if needed but still respects our profitability
            # gate (validated below).
            primary_fok_price = primary_price
            if primary_adapter is not None and hasattr(
                primary_adapter, "best_executable_price",
            ):
                fillable, exec_price = await primary_adapter.best_executable_price(
                    primary_market, primary_side, effective_qty,
                )
                logger.info(
                    "  book_walk.primary platform=%s market=%s side=%s "
                    "fillable=%s exec_price=%.4f quoted=%.4f qty=%d",
                    primary_platform, primary_market, primary_side,
                    fillable, float(exec_price), float(primary_price), effective_qty,
                )
                if fillable and exec_price > 0:
                    primary_fok_price = exec_price
                    if exec_price > primary_price:
                        # We're paying worse than the quoted opportunity price.
                        # Re-validate net-edge using the actual executable price
                        # so we never knowingly cross a slippage threshold that
                        # eats the entire arb.
                        primary_yes_price = (
                            exec_price if primary_side == "yes" else opp.yes_price
                        )
                        primary_no_price = (
                            exec_price if primary_side == "no" else opp.no_price
                        )
                        revised_total = primary_yes_price + primary_no_price
                        revised_gross = 1.0 - revised_total
                        revised_yes_fee = compute_fee(
                            opp.yes_platform, primary_yes_price,
                            effective_qty, opp.yes_fee_rate,
                        ) / max(effective_qty, 1)
                        revised_no_fee = compute_fee(
                            opp.no_platform, primary_no_price,
                            effective_qty, opp.no_fee_rate,
                        ) / max(effective_qty, 1)
                        revised_net = revised_gross - revised_yes_fee - revised_no_fee
                        if revised_net * 100 < MIN_NET_EDGE_CENTS:
                            logger.info(
                                "  ✗ SKIP: best_executable_price=%.4f exceeds quoted "
                                "%.4f and revised net edge %.2f¢ < %.1f¢ minimum",
                                exec_price, primary_price,
                                revised_net * 100, MIN_NET_EDGE_CENTS,
                            )
                            return None
                        logger.info(
                            "  ↑ FOK price walked %.4f→%.4f (book deeper than top-of-book); "
                            "revised net edge %.2f¢",
                            primary_price, exec_price, revised_net * 100,
                        )

            # Step 0c: FOK slippage buffer (FILL-01).
            # Post-2026-05-14 zero-fill streak: 214 consecutive FOK orders
            # came back ORDER_STATE_NEW and were KILLED by the venue within
            # ~750ms. Engine's best_executable_price() walks the book to
            # find a sufficient-depth limit, but ~500-900ms of HTTP
            # latency from walk-to-venue lets the touch move up by one
            # tick before our order arrives — at which point the FOK
            # cannot fill at the stale limit and the venue kills it.
            #
            # Fix: lift the FOK limit by FOK_SLIPPAGE_TICKS ticks (default
            # 1, configurable via env) so the order can absorb a one-
            # level adverse move. Re-validate net edge against
            # MIN_NET_EDGE_CENTS using the buffered price; if the buffer
            # would cross our minimum-edge floor we abort the buffer
            # (send at the walked price; still better than skipping).
            #
            # The slippage budget is bounded: max wire-price change is
            # FOK_SLIPPAGE_TICKS * 0.01 = $0.01 per contract by default.
            # On effective_qty=11 that's $0.11 of additional cost, a tiny
            # fraction of the 7¢+ min net edge requirement.
            # Slippage buffer is per-platform configurable because the
            # platforms have different rates of book churn on fast
            # sports markets: Polymarket's order book on MLB games can
            # move 1-2 ticks in the 500ms walk-to-place latency
            # window (verified live 2026-05-26: ARB-000683 had
            # book_walk@22:44:13.995 say fillable at $0.70, place
            # order at $0.71 @22:44:14.547, IOC expired with fill=0
            # because the $0.70 ask was gone by the time the order
            # arrived). Kalshi's auction book is steadier — 1 tick is
            # plenty there. Default raised from 1→2 ticks after the
            # 50-trade zero-fill streak audit.
            #
            # Resolution order:
            #   1. FOK_SLIPPAGE_TICKS_<PLATFORM>  (e.g. POLYMARKET, KALSHI, FORECASTEX)
            #   2. FOK_SLIPPAGE_TICKS             (global default)
            #   3. hard default = 2 ticks
            platform_key = (primary_platform or "").upper()
            per_platform_env = f"FOK_SLIPPAGE_TICKS_{platform_key}"
            try:
                _raw = (
                    os.getenv(per_platform_env)
                    or os.getenv("FOK_SLIPPAGE_TICKS")
                    or "2"
                )
                fok_slippage_ticks = int(_raw)
            except (TypeError, ValueError):
                fok_slippage_ticks = 2
            fok_slippage_ticks = max(0, fok_slippage_ticks)
            if fok_slippage_ticks > 0:
                tick = 0.01  # Both Kalshi and Polymarket use 1¢ ticks.
                slippage = tick * fok_slippage_ticks
                buffered_price = min(primary_fok_price + slippage, 0.99)
                buffered_yes = (
                    buffered_price if primary_side == "yes" else opp.yes_price
                )
                buffered_no = (
                    buffered_price if primary_side == "no" else opp.no_price
                )
                buffered_total = buffered_yes + buffered_no
                buffered_gross = 1.0 - buffered_total
                buffered_yes_fee = compute_fee(
                    opp.yes_platform, buffered_yes,
                    effective_qty, opp.yes_fee_rate,
                ) / max(effective_qty, 1)
                buffered_no_fee = compute_fee(
                    opp.no_platform, buffered_no,
                    effective_qty, opp.no_fee_rate,
                ) / max(effective_qty, 1)
                buffered_net = buffered_gross - buffered_yes_fee - buffered_no_fee
                if buffered_net * 100 >= MIN_NET_EDGE_CENTS:
                    logger.info(
                        "  ↑ FOK slippage buffer applied: walked=%.4f + %d tick(s) "
                        "= %.4f; revised net edge %.2f¢ ≥ %.1f¢ minimum",
                        primary_fok_price, fok_slippage_ticks, buffered_price,
                        buffered_net * 100, MIN_NET_EDGE_CENTS,
                    )
                    primary_fok_price = buffered_price
                else:
                    logger.info(
                        "  · FOK slippage buffer skipped: buffered net %.2f¢ < "
                        "%.1f¢ minimum; sending at walked price %.4f without buffer",
                        buffered_net * 100, MIN_NET_EDGE_CENTS, primary_fok_price,
                    )

            # Step 1: Execute the primary leg.
            #
            # FILL-02 (2026-05-27 audit): 100% of recent Polymarket-primary
            # trades (ARB-000683..690) failed with ``ORDER_STATE_EXPIRED``
            # fill_qty=0 EVEN WHEN the live book at terminal poll showed
            # asks at or below our buffered FOK limit. Polymarket's
            # matching engine routinely kills FOK orders on fast-moving
            # MLB sports markets because true depth at our limit is
            # thinner than the book display suggests by the time the
            # order reaches the matching engine. FOK = all-or-nothing,
            # so even 1 contract short kills the entire 11-contract order.
            #
            # Switch the PRIMARY leg to IOC (immediate-or-cancel) and
            # accept partial fills. The secondary is then sized to the
            # ACTUAL primary fill_qty (not the originally-intended
            # effective_qty). This guarantees we capture whatever depth
            # IS available on Polymarket without losing 100% of
            # opportunities to FOK rejection. Tail risk (secondary
            # partial fill on the scaled-down qty) is absorbed by the
            # existing naked-leg recovery infrastructure exactly as
            # before, but the base rate of "zero fill" drops from
            # ~100% to whatever fraction of opportunities have
            # truly-zero depth at execution time (~0%).
            #
            # Opt-out: EXECUTOR_PRIMARY_TIF=FOK preserves legacy
            # behavior for venues/operators that prefer atomic fills.
            # Per-platform override: EXECUTOR_PRIMARY_TIF_<PLATFORM>
            # (e.g. ..._POLYMARKET=IOC, ..._KALSHI=FOK).
            primary_platform_key = (primary_platform or "").upper()
            primary_tif_env = (
                os.getenv(f"EXECUTOR_PRIMARY_TIF_{primary_platform_key}")
                or os.getenv("EXECUTOR_PRIMARY_TIF")
                or "IOC"
            ).strip().upper()
            primary_use_ioc = primary_tif_env != "FOK"
            logger.info(
                "  primary tif resolution: platform=%s tif=%s use_ioc=%s "
                "(EXECUTOR_PRIMARY_TIF_%s / EXECUTOR_PRIMARY_TIF env)",
                primary_platform, primary_tif_env, primary_use_ioc,
                primary_platform_key,
            )

            primary_leg = await self._place_order_for_leg(
                arb_id, primary_platform, primary_market,
                opp.canonical_id, primary_side, primary_fok_price, effective_qty,
                use_ioc=primary_use_ioc,
                atomic_arb_stub_opp=opp,
                atomic_arb_stub_net_edge=getattr(opp, "net_edge", None),
            )

            # Step 2: Accept partial primary fills when IOC.
            # Zero fill is still a clean exit (no exposure). Any fill
            # > 0 means we proceed with the secondary at the actual
            # primary fill_qty.
            primary_actual_qty = int(primary_leg.fill_qty or 0)
            if primary_actual_qty <= 0:
                logger.info(
                    "  ✗ PRIMARY %s did not fill (status=%s, fill_qty=0) — "
                    "$0.00 exposure, skipping secondary",
                    primary_side.upper(), primary_leg.status.value,
                )
                secondary_leg = Order(
                    order_id=f"{arb_id}-{secondary_side.upper()}-SKIPPED",
                    platform=secondary_platform,
                    market_id=secondary_market,
                    canonical_id=opp.canonical_id,
                    side=secondary_side,
                    price=secondary_price,
                    quantity=effective_qty,
                    status=OrderStatus.ABORTED,
                    timestamp=time.time(),
                    error="Skipped: primary leg did not fill (sequential execution)",
                )
            else:
                # If primary partially filled, scale secondary to the
                # actual filled quantity so the legs balance. The
                # remainder of the originally-intended effective_qty
                # is forgone — we'd rather complete the arb on what
                # primary actually got than leave it naked chasing the
                # missing depth.
                if primary_actual_qty < effective_qty:
                    logger.warning(
                        "  ⚠ PRIMARY %s PARTIAL FILL: got %d of %d intended "
                        "contracts on %s @ $%.4f → scaling secondary to %d",
                        primary_side.upper(),
                        primary_actual_qty, effective_qty,
                        primary_platform, primary_leg.fill_price,
                        primary_actual_qty,
                    )
                    effective_qty = primary_actual_qty
                # Primary filled — now execute secondary
                logger.info(
                    "  ✓ PRIMARY %s FILLED: %d contracts @ $%.2f = $%.2f spent on %s → proceeding to %s",
                    primary_side.upper(), primary_leg.fill_qty, primary_leg.fill_price,
                    primary_leg.fill_qty * primary_leg.fill_price,
                    primary_platform, secondary_side.upper(),
                )

                try:
                    # No inter-leg delay — every millisecond between primary fill
                    # and secondary submit is a window for the secondary book to
                    # move past our limit price and turn the IOC into a non-
                    # marketable order that Polymarket REJECTS outright.  Live
                    # logs show a 500ms delay producing 5-10¢ book shifts on
                    # fast-settling sports markets, which is enough to make the
                    # secondary IOC reject and strand the primary as naked.
                    # Skip the delay entirely; the recovery loop is the safety
                    # net if the secondary still moves under us.
                    if self._inter_leg_delay_ms > 0:
                        await asyncio.sleep(self._inter_leg_delay_ms / 1000.0)

                    # Walk the secondary book IMMEDIATELY before submit — the
                    # walk-to-submit latency was the single biggest source of
                    # rejected secondaries (Polymarket's IOC requires the limit
                    # to be marketable on receipt, and a 500ms-stale walk often
                    # priced us BELOW the live ASK after the book moved up).
                    # Place the IOC at the walked price exactly: walked is the
                    # WORST price needed to absorb effective_qty given the book
                    # we just observed, so submitting AT walked corresponds to
                    # the most aggressive marketable limit we can place without
                    # paying through the spread.
                    secondary_fok_price = secondary_price
                    secondary_adapter = self.adapters.get(secondary_platform)
                    walked_exec_price = None
                    if secondary_adapter is not None and hasattr(
                        secondary_adapter, "best_executable_price",
                    ):
                        s_fillable, s_exec = await secondary_adapter.best_executable_price(
                            secondary_market, secondary_side, effective_qty,
                        )
                        logger.info(
                            "  book_walk.secondary platform=%s market=%s side=%s "
                            "fillable=%s exec_price=%.4f quoted=%.4f qty=%d",
                            secondary_platform, secondary_market, secondary_side,
                            s_fillable, float(s_exec), float(secondary_price), effective_qty,
                        )
                        if s_fillable and s_exec > 0:
                            walked_exec_price = s_exec
                            secondary_fok_price = s_exec

                    # Compute the absolute most we can pay on the secondary while
                    # still booking AT LEAST ``MIN_NET_EDGE_CENTS`` of net edge
                    # against the primary's actual fill price.  This is the IOC
                    # limit ceiling — anything above and the trade is unprofitable
                    # so we'd rather take the naked-leg recovery path than fill at
                    # a guaranteed loss.
                    primary_fill_price = float(primary_leg.fill_price)
                    primary_yes_price = (
                        primary_fill_price if primary_side == "yes" else 0.0
                    )
                    primary_no_price = (
                        primary_fill_price if primary_side == "no" else 0.0
                    )
                    # Re-derive fees per unit at the qty we actually filled.
                    primary_fee_per = (
                        compute_fee(
                            opp.yes_platform if primary_side == "yes" else opp.no_platform,
                            primary_fill_price,
                            int(primary_leg.fill_qty) or effective_qty,
                            opp.yes_fee_rate if primary_side == "yes" else opp.no_fee_rate,
                        )
                        / max(int(primary_leg.fill_qty) or effective_qty, 1)
                    )
                    # Worst price we'd take on the secondary at exactly break-even,
                    # before any safety margin.  Solve:
                    #   1 - primary - secondary - primary_fee - secondary_fee >= MIN_EDGE/100
                    # Approximating secondary_fee at the walked/quoted price (the
                    # fee curve is shallow over the small slippage window we
                    # tolerate, so this approximation is fine).
                    approx_sec_fee_per = (
                        compute_fee(
                            secondary_platform,
                            secondary_fok_price,
                            effective_qty,
                            opp.yes_fee_rate if secondary_side == "yes" else opp.no_fee_rate,
                        )
                        / max(effective_qty, 1)
                    )
                    edge_floor = MIN_NET_EDGE_CENTS / 100.0
                    max_affordable_secondary = max(
                        1.0
                        - primary_fill_price
                        - primary_fee_per
                        - approx_sec_fee_per
                        - edge_floor,
                        0.0,
                    )

                    # IOC fills at LIMIT-OR-BETTER, so the most-aggressive
                    # marketable limit we can place without crossing our edge
                    # floor is ``max_affordable_secondary`` itself.  We pay
                    # only what the actual book asks (= walked); the higher
                    # limit just guarantees marketability over a wider window
                    # of book movement.  Earlier strategies (walked + buffer
                    # capped at max_affordable) under-priced the order and
                    # left ~5¢ of book-shift headroom uncovered, so the IOC
                    # would expire when the top YES bid moved by even one
                    # tick between walk and submit.  Now we always submit at
                    # the maximum profitable price; Polymarket fills us at
                    # the visible book level (walked) and the smart-unwind
                    # path catches whatever slips through.  Abort only when
                    # the walked price already crosses our edge floor — at
                    # that point the trade is guaranteed-loss before any
                    # book movement, so we want the recovery path instead.
                    # Only hard-abort if walked price exceeds max_affordable by
                    # more than 5¢ ("hopeless" threshold).  If within 5¢, still
                    # try the IOC at max_affordable — the book might move back
                    # by the time the order hits.
                    if secondary_platform == "polymarket":
                        HOPELESS_THRESHOLD = 0.0
                    else:
                        HOPELESS_THRESHOLD = 0.01
                    abort_secondary = secondary_fok_price > (max_affordable_secondary + HOPELESS_THRESHOLD)
                    buffered_limit = max_affordable_secondary if not abort_secondary else secondary_fok_price

                    logger.info(
                        "  Secondary IOC limit: walked=%.4f buffered=%.4f "
                        "max_affordable=%.4f primary_fill=%.4f abort=%s",
                        secondary_fok_price, buffered_limit,
                        max_affordable_secondary, primary_fill_price, abort_secondary,
                    )

                    # Break-even ceiling for the requote loop's final
                    # attempt: max_affordable already subtracts the edge
                    # floor, so adding it back is the 0¢-net price.
                    break_even_secondary = max_affordable_secondary + edge_floor

                    # Recompute the actual post-walk net edge for visibility.
                    if (
                        not abort_secondary
                        and walked_exec_price is not None
                        and walked_exec_price > secondary_price
                    ):
                        actual_yes_price = (
                            primary_fill_price if primary_side == "yes" else walked_exec_price
                        )
                        actual_no_price = (
                            primary_fill_price if primary_side == "no" else walked_exec_price
                        )
                        actual_total = actual_yes_price + actual_no_price
                        actual_gross = 1.0 - actual_total
                        actual_yes_fee = compute_fee(
                            opp.yes_platform, actual_yes_price,
                            effective_qty, opp.yes_fee_rate,
                        ) / max(effective_qty, 1)
                        actual_no_fee = compute_fee(
                            opp.no_platform, actual_no_price,
                            effective_qty, opp.no_fee_rate,
                        ) / max(effective_qty, 1)
                        actual_net = actual_gross - actual_yes_fee - actual_no_fee
                        logger.info(
                            "  ↑ Secondary price walked %.4f→%.4f; "
                            "post-walk net edge %.2f¢",
                            secondary_price, walked_exec_price, actual_net * 100,
                        )

                    if abort_secondary:
                        # EDGE-LOST entry: the walked book is unaffordable
                        # RIGHT NOW, but one-tick moves routinely bounce
                        # back within the requote window. Enter the loop
                        # without an initial attempt; only if it never
                        # comes back do we construct the ABORTED leg (the
                        # path ~35 naked arbs took unconditionally before).
                        secondary_leg = await self._execute_with_fallbacks(
                            arb_id=arb_id,
                            secondary_platform=secondary_platform,
                            secondary_market=secondary_market,
                            canonical_id=opp.canonical_id,
                            secondary_side=secondary_side,
                            initial_price=buffered_limit,
                            qty=effective_qty,
                            max_affordable_price=max_affordable_secondary,
                            primary_leg=primary_leg,
                            primary_platform=primary_platform,
                            opp=opp,
                            break_even_price=break_even_secondary,
                            skip_initial_attempt=True,
                        )
                        if secondary_leg is None:
                            logger.error(
                                "  ✗ ABORT secondary: walked=%.4f > max_affordable=%.4f "
                                "(primary_fill=%.4f) and requote never recovered. "
                                "Primary will be unwound on %s.",
                                secondary_fok_price, max_affordable_secondary,
                                primary_fill_price, primary_platform,
                            )
                            secondary_leg = Order(
                                order_id=f"{arb_id}-{secondary_side.upper()}-EDGE-LOST",
                                platform=secondary_platform,
                                market_id=secondary_market,
                                canonical_id=opp.canonical_id,
                                side=secondary_side,
                                price=secondary_fok_price,
                                quantity=effective_qty,
                                status=OrderStatus.ABORTED,
                                timestamp=time.time(),
                                error=(
                                    f"Secondary live-book exec price {secondary_fok_price:.4f} "
                                    f"exceeds max-affordable {max_affordable_secondary:.4f} "
                                    f"after primary fill at {primary_fill_price:.4f} "
                                    f"(requote attempts exhausted)"
                                ),
                            )
                    else:
                        secondary_leg = await self._execute_with_fallbacks(
                            arb_id=arb_id,
                            secondary_platform=secondary_platform,
                            secondary_market=secondary_market,
                            canonical_id=opp.canonical_id,
                            secondary_side=secondary_side,
                            initial_price=buffered_limit,
                            qty=effective_qty,
                            max_affordable_price=max_affordable_secondary,
                            primary_leg=primary_leg,
                            primary_platform=primary_platform,
                            opp=opp,
                            break_even_price=break_even_secondary,
                        )
                except Exception as _secondary_exc:
                    # Silent-naked bug fix: anything in the block above —
                    # ``best_executable_price``, ``compute_fee``, the adapter
                    # call inside ``_execute_with_fallbacks`` — can raise and
                    # previously escaped ``_live_execution`` outright, leaving
                    # the primary FILLED in DB with no secondary row and no
                    # naked incident. Build an ABORTED ``-EXCEPTION``
                    # placeholder so the downstream naked detection and
                    # ``_recover_one_leg_risk`` supervisor fanout still fire;
                    # the original exception is re-raised after ``record_arb``
                    # so callers still see the failure.
                    logger.exception(
                        "Secondary execution raised after primary %s FILLED on %s "
                        "(arb=%s); building ABORTED-EXCEPTION placeholder. "
                        "exc=%s: %s",
                        primary_side.upper(), primary_platform, arb_id,
                        type(_secondary_exc).__name__, _secondary_exc,
                    )
                    secondary_leg = Order(
                        order_id=f"{arb_id}-{secondary_side.upper()}-EXCEPTION",
                        platform=secondary_platform,
                        market_id=secondary_market,
                        canonical_id=opp.canonical_id,
                        side=secondary_side,
                        price=secondary_price,
                        quantity=effective_qty,
                        status=OrderStatus.ABORTED,
                        timestamp=time.time(),
                        error=(
                            f"Secondary execution raised before order construction: "
                            f"{type(_secondary_exc).__name__}: {_secondary_exc}"
                        ),
                    )
                    primary_filled_usd = (
                        float(primary_leg.fill_qty) * float(primary_leg.fill_price)
                    )
                    try:
                        await self._record_incident(
                            arb_id,
                            opp,
                            "critical",
                            (
                                "Secondary execution raised before order construction "
                                f"({type(_secondary_exc).__name__}) — naked position"
                            ),
                            metadata={
                                "event_type": "secondary_execution_exception",
                                "primary_platform": primary_platform,
                                "primary_side": primary_side,
                                "primary_filled_qty": primary_leg.fill_qty,
                                "primary_filled_price": primary_leg.fill_price,
                                "primary_exposure_usd": primary_filled_usd,
                                "secondary_platform": secondary_platform,
                                "secondary_side": secondary_side,
                                "exception_type": type(_secondary_exc).__name__,
                                "exception_message": str(_secondary_exc),
                            },
                        )
                    except Exception as _inc_exc:  # noqa: BLE001
                        logger.warning(
                            "secondary_execution_exception incident emit failed: %s",
                            _inc_exc,
                        )
                    _secondary_exception = _secondary_exc

                if secondary_leg.status not in {OrderStatus.FILLED, OrderStatus.SUBMITTED, OrderStatus.PARTIAL}:
                    # Secondary failed — we have a naked position. Log critical
                    # incident for manual resolution. Do NOT auto-unwind.
                    # Recovery path (_recover_one_leg_risk) creates the
                    # structured incident; here we just log + count.
                    exposure_usd = float(primary_leg.fill_qty) * float(primary_leg.fill_price)
                    self._naked_leg_count += 1
                    self._naked_leg_exposure_usd += exposure_usd
                    logger.error(
                        "NAKED POSITION: secondary %s FAILED (status=%s) after primary %s filled on %s. "
                        "Exposure: %d contracts @ %.4f = $%.2f. Manual resolution required. "
                        "Cumulative naked legs: %d events / $%.2f exposure.",
                        secondary_side.upper(), secondary_leg.status.value,
                        primary_side.upper(), primary_platform,
                        primary_leg.fill_qty, primary_leg.fill_price,
                        exposure_usd,
                        self._naked_leg_count, self._naked_leg_exposure_usd,
                    )
                elif secondary_leg.status != OrderStatus.FILLED:
                    # Soft-naked: secondary accepted (SUBMITTED/PARTIAL) but
                    # not FILLED. Primary is fully on the books while the
                    # hedge is resting / partially filled — real exposure
                    # until the secondary clears. Three of the first ten
                    # production trades hit this case and were not flagged
                    # by the FAILED-only check above. _recover_one_leg_risk
                    # is NOT triggered here (status will be "submitted",
                    # not "recovering"), so emit the critical incident
                    # directly so operators are paged.
                    primary_filled_usd = float(primary_leg.fill_qty) * float(primary_leg.fill_price)
                    secondary_unfilled_qty = max(
                        float(secondary_leg.quantity) - float(secondary_leg.fill_qty), 0.0,
                    )
                    self._naked_leg_count += 1
                    self._naked_leg_exposure_usd += primary_filled_usd
                    logger.error(
                        "SOFT NAKED POSITION: primary %s FILLED on %s (%d @ $%.4f = $%.2f) "
                        "but secondary %s on %s only %s (filled %d/%d). "
                        "Exposed until secondary clears. "
                        "Cumulative naked legs: %d events / $%.2f exposure.",
                        primary_side.upper(), primary_platform,
                        primary_leg.fill_qty, primary_leg.fill_price, primary_filled_usd,
                        secondary_side.upper(), secondary_platform,
                        secondary_leg.status.value,
                        secondary_leg.fill_qty, secondary_leg.quantity,
                        self._naked_leg_count, self._naked_leg_exposure_usd,
                    )
                    soft_incident = None
                    try:
                        soft_incident = await self._record_incident(
                            arb_id,
                            opp,
                            "critical",
                            "Soft naked position: secondary leg accepted but not filled",
                            metadata={
                                "event_type": "soft_naked_leg",
                                "primary_platform": primary_platform,
                                "primary_side": primary_side,
                                "primary_filled_qty": primary_leg.fill_qty,
                                "primary_filled_price": primary_leg.fill_price,
                                "primary_exposure_usd": primary_filled_usd,
                                "exposure_usd": primary_filled_usd,
                                "secondary_platform": secondary_platform,
                                "secondary_side": secondary_side,
                                "secondary_status": secondary_leg.status.value,
                                "secondary_filled_qty": secondary_leg.fill_qty,
                                "secondary_unfilled_qty": secondary_unfilled_qty,
                                "recommended_unwind": (
                                    f"Wait for resting {secondary_side.upper()} on "
                                    f"{secondary_platform.upper()} to fill, or cancel and "
                                    f"hedge {primary_side.upper()} exposure on {primary_platform.upper()}"
                                ),
                                "cumulative_naked_count": self._naked_leg_count,
                                "cumulative_naked_exposure_usd": round(
                                    self._naked_leg_exposure_usd, 2,
                                ),
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "soft_naked_leg incident emit failed: %s", exc,
                        )
                    # 2026-05-23 audit finding #4 (CRITICAL): soft-naked must
                    # fan out to Telegram via SafetySupervisor, identical to
                    # the hard-naked case. Without this, the operator hears
                    # nothing until the resting secondary either fills or is
                    # manually cleared — exposure can sit on the books
                    # indefinitely with only an incident-WS event.
                    if self._safety is not None and soft_incident is not None:
                        try:
                            await self._safety.handle_one_leg_exposure(
                                soft_incident, primary_leg, secondary_leg, opp,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.error(
                                "safety.handle_one_leg_exposure (soft naked) raised: %s",
                                exc,
                            )

            # Assign to leg_yes / leg_no based on which side was primary
            if primary_side == "yes":
                leg_yes, leg_no = primary_leg, secondary_leg
            else:
                leg_yes, leg_no = secondary_leg, primary_leg

            # Post-trade validation: verify combined fill prices are profitable
            if (
                leg_yes.status == OrderStatus.FILLED
                and leg_no.status == OrderStatus.FILLED
            ):
                actual_yes_cost = float(leg_yes.fill_price)
                actual_no_cost = float(leg_no.fill_price)
                actual_total_cost = actual_yes_cost + actual_no_cost
                actual_yes_fee = compute_fee(
                    opp.yes_platform, actual_yes_cost,
                    effective_qty, opp.yes_fee_rate,
                ) / max(effective_qty, 1)
                actual_no_fee = compute_fee(
                    opp.no_platform, actual_no_cost,
                    effective_qty, opp.no_fee_rate,
                ) / max(effective_qty, 1)
                actual_fees_per = actual_yes_fee + actual_no_fee
                actual_profit_per = 1.0 - actual_total_cost - actual_fees_per
                if actual_profit_per < 0:
                    logger.error(
                        "POST-TRADE ALERT: fills are unprofitable! "
                        "total=%.4f fees=%.4f profit=%.4f cents",
                        actual_total_cost, actual_fees_per,
                        actual_profit_per * 100,
                    )
                else:
                    logger.info(
                        "POST-TRADE OK: total=%.4f fees=%.4f profit=%.2f cents",
                        actual_total_cost, actual_fees_per,
                        actual_profit_per * 100,
                    )

        # Plan 03-08 (SAFE-02 gap closure — closes the
        # 03-VERIFICATION.md "Per-platform exposure tracking fires on
        # filled status only" gap):
        # We must decide status AND record per-platform exposure BEFORE
        # dispatching to _recover_one_leg_risk, because the recovery
        # path mutates leg.status to CANCELLED on success and also
        # (post Task 2) releases the reservation. If we recorded after
        # recovery, the release would fire against an empty reservation
        # and the record would follow — creating a net mis-accounting.
        surviving_statuses = {
            OrderStatus.FILLED,
            OrderStatus.PARTIAL,
            OrderStatus.SUBMITTED,
        }
        terminal_failed = {
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
            OrderStatus.ABORTED,
        }

        status = "submitted"
        notes: List[str] = []
        needs_recovery = False
        # FILL-02 (2026-05-27): IOC-terminal venue responses can return
        # status=FILLED with fill_qty=0 (the IOC was accepted but
        # matched zero contracts). That's NOT exposure — there's
        # nothing to recover. Treat such a leg as a non-survivor for
        # recovery routing. SUBMITTED/PARTIAL legs STILL need recovery
        # because they may still fill on the venue side; only the
        # FILLED-with-zero-qty terminal case is the no-op.
        def _is_survivor(leg) -> bool:
            if leg.status not in surviving_statuses:
                return False
            # FILLED is a terminal status — if it filled zero, it
            # represents no exposure. SUBMITTED/PARTIAL are non-
            # terminal; even fill_qty=0 means the order is resting
            # on the venue and could fill, so recover (cancel) it.
            if leg.status == OrderStatus.FILLED:
                return float(leg.fill_qty or 0) > 0
            return True
        yes_has_exposure = _is_survivor(leg_yes)
        no_has_exposure = _is_survivor(leg_no)
        if leg_yes.status in terminal_failed or leg_no.status in terminal_failed:
            if yes_has_exposure or no_has_exposure:
                status = "recovering"
                needs_recovery = True
            else:
                status = "failed"
        elif leg_yes.status == OrderStatus.PARTIAL or leg_no.status == OrderStatus.PARTIAL:
            status = "recovering"
            needs_recovery = True
        elif leg_yes.status == OrderStatus.FILLED and leg_no.status == OrderStatus.FILLED:
            status = "filled"
        elif (
            (leg_yes.status == OrderStatus.FILLED
             and leg_no.status in {OrderStatus.SUBMITTED, OrderStatus.PENDING}
             and float(leg_no.fill_qty) < float(leg_no.quantity))
            or
            (leg_no.status == OrderStatus.FILLED
             and leg_yes.status in {OrderStatus.SUBMITTED, OrderStatus.PENDING}
             and float(leg_yes.fill_qty) < float(leg_yes.quantity))
        ):
            # Soft-naked: one leg confirmed FILLED while the other is
            # platform-accepted but unfilled (fill_qty < quantity). 3/10
            # production trades hit this case and fell through to
            # ``status="submitted"`` with no recovery, leaving real exposure
            # on the filled side until manual intervention. Treat as
            # "recovering" so _recover_one_leg_risk runs (cancels the
            # resting leg + attempts reverse-order unwind on the filled
            # leg).
            status = "recovering"
            needs_recovery = True

        realized_pnl = opp.net_edge * min(max(leg_yes.fill_qty, 0), max(leg_no.fill_qty, 0)) if status in {"filled", "submitted"} else 0.0
        yes_cost = leg_yes.fill_qty * leg_yes.fill_price if leg_yes.fill_qty > 0 else 0.0
        no_cost = leg_no.fill_qty * leg_no.fill_price if leg_no.fill_qty > 0 else 0.0
        total_spent = yes_cost + no_cost
        status_emoji = {"filled": "✅", "submitted": "⏳", "recovering": "⚠️", "failed": "❌"}.get(status, "❓")
        logger.info(
            "═══ RESULT %s ═══ %s %s\n"
            "  YES leg: %s on %s — %d @ $%.2f = $%.2f\n"
            "  NO  leg: %s on %s — %d @ $%.2f = $%.2f\n"
            "  TOTAL SPENT: $%.2f | REALIZED P&L: $%.4f | STATUS: %s",
            arb_id, status_emoji, status.upper(),
            leg_yes.status.value, opp.yes_platform, leg_yes.fill_qty, leg_yes.fill_price, yes_cost,
            leg_no.status.value, opp.no_platform, leg_no.fill_qty, leg_no.fill_price, no_cost,
            total_spent, realized_pnl, status,
        )

        execution = ArbExecution(
            arb_id=arb_id,
            opportunity=opp,
            leg_yes=leg_yes,
            leg_no=leg_no,
            status=status,
            realized_pnl=realized_pnl,
            timestamp=now,
            notes=notes,
        )
        self._executions.append(execution)
        _trim_executions(self._executions)

        # Plan 03-08 (SAFE-02): per-platform exposure recording.
        #   * "submitted"/"filled" → both legs have real exposure; record
        #     full split (mirrors _simulate_execution verbatim).
        #   * "recovering" → exactly one leg is the survivor (the other
        #     side is FAILED/CANCELLED/ABORTED); record ONLY the
        #     survivor's exposure via single-platform `platform=` kwarg.
        #     Task 2's release_trade hook inside _recover_one_leg_risk
        #     will free this reservation if the survivor is then
        #     successfully cancelled (SUBMITTED→CANCELLED transition).
        #   * "failed" → both rejected; no exposure to track.
        if status in {"submitted", "filled"}:
            self.risk.record_trade(
                opp.canonical_id,
                opp.suggested_qty * (opp.yes_price + opp.no_price),
                execution.realized_pnl,
                yes_platform=opp.yes_platform,
                no_platform=opp.no_platform,
                yes_exposure=opp.suggested_qty * opp.yes_price,
                no_exposure=opp.suggested_qty * opp.no_price,
            )
        elif status == "recovering":
            # Determine the survivor PRE-recovery (recovery mutates
            # leg.status to CANCELLED on success, destroying this info).
            surviving_platform: Optional[str] = None
            surviving_exposure: float = 0.0
            if leg_yes.status in surviving_statuses and leg_no.status not in surviving_statuses:
                surviving_platform = opp.yes_platform
                surviving_exposure = opp.suggested_qty * opp.yes_price
            elif leg_no.status in surviving_statuses and leg_yes.status not in surviving_statuses:
                surviving_platform = opp.no_platform
                surviving_exposure = opp.suggested_qty * opp.no_price
            else:
                # Edge case: both legs in a surviving state (e.g. both
                # PARTIAL or one PARTIAL + one SUBMITTED) — record the
                # full split; the recovery loop's release_trade hook will
                # rebalance per leg as each cancel confirms.
                self.risk.record_trade(
                    opp.canonical_id,
                    opp.suggested_qty * (opp.yes_price + opp.no_price),
                    execution.realized_pnl,
                    yes_platform=opp.yes_platform,
                    no_platform=opp.no_platform,
                    yes_exposure=opp.suggested_qty * opp.yes_price,
                    no_exposure=opp.suggested_qty * opp.no_price,
                )
            if surviving_platform is not None:
                self.risk.record_trade(
                    opp.canonical_id,
                    surviving_exposure,
                    execution.realized_pnl,
                    platform=surviving_platform,
                )

        # Recovery runs AFTER recording so Task 2's release_trade hook
        # can free the survivor's reservation if its cancel succeeds.
        unwind_pnl: float = 0.0
        if needs_recovery:
            recovery_notes, unwind_pnl = await self._recover_one_leg_risk(
                arb_id, opp, leg_yes, leg_no,
            )
            notes.extend(recovery_notes)
            # Book the realized loss from the unwind into the arb's P&L so
            # reconciliation drift no longer fires for the unrecorded
            # auto-unwind cost. Track the unwind component SEPARATELY on
            # ``execution.unwind_pnl`` so reporting can distinguish arb
            # edge (both legs filled, paired) from directional naked-leg
            # payouts (audit 2026-05).  ``realized_pnl`` continues to hold
            # the TOTAL so existing reconciliation logic stays correct.
            if unwind_pnl != 0.0:
                execution.unwind_pnl = float(execution.unwind_pnl) + unwind_pnl
                execution.realized_pnl = float(execution.realized_pnl) + unwind_pnl
                logger.info(
                    "ARB %s unwind_pnl=$%.4f; total realized_pnl now $%.4f "
                    "(arb_pnl=$%.4f)",
                    arb_id, unwind_pnl, execution.realized_pnl, execution.arb_pnl,
                )

        # Daily-loss kill switch: roll the unwind loss into RiskManager's
        # daily P&L (record_trade already booked the entry pnl) and trip
        # SafetySupervisor when cumulative loss crosses the configured
        # ceiling. Halts the system, not just this trade.
        await self._maybe_halt_on_daily_loss(unwind_pnl=unwind_pnl)

        # Generate a deterministic markdown post-mortem so every arb — win,
        # loss, naked recovery, or gate-blocked — has a human-readable
        # explanation the moment it's persisted. Best-effort; failures must
        # not block the audit write below.
        try:
            execution.analysis_md = _build_inline_analysis(execution)
        except Exception as exc:  # noqa: BLE001 - never block on analysis
            logger.warning("trade_analyzer inline build failed for %s: %s", arb_id, exc)
            execution.analysis_md = ""

        # EXEC-02 / D-16: persist the completed arb execution.
        if self.store is not None:
            try:
                await self.store.record_arb(execution)
            except Exception as exc:
                await self._handle_db_failure(
                    op="record_arb",
                    arb_id=arb_id,
                    canonical_id=getattr(opp, "canonical_id", None),
                    exc=exc,
                )
        # Re-raise the secondary-path exception (if any) AFTER the audit
        # trail is durably persisted and the supervisor fanout has fired.
        # Callers (auto_executor / api) see the failure and can take
        # downstream action; the engine has already booked the naked
        # exposure and notified operators.
        if _secondary_exception is not None:
            raise _secondary_exception
        return execution

    async def _execute_with_fallbacks(
        self,
        arb_id: str,
        secondary_platform: str,
        secondary_market: str,
        canonical_id: str,
        secondary_side: str,
        initial_price: float,
        qty: int,
        max_affordable_price: float,
        primary_leg: "Order",
        primary_platform: str,
        opp: "ArbitrageOpportunity",
        break_even_price: Optional[float] = None,
        skip_initial_attempt: bool = False,
    ) -> Optional["Order"]:
        """Execute secondary leg with requote-then-retry fallbacks.

        Chain:
        1. IOC at walked/buffered price (skipped when ``skip_initial_attempt``
           — the EDGE-LOST entry, where the pre-submit walk already showed
           an unaffordable book so a marketable order can't exist yet).
        2. Requote loop (``SECONDARY_REQUOTE_MAX_ATTEMPTS``): re-walk the
           live book; while walked <= max_affordable, retry an IOC at
           max_affordable. On the FINAL attempt only, ``break_even_price``
           (when provided) raises the ceiling — completing the arb at
           >= 0¢ net always beats a panic unwind, but the edge floor is
           never silently given up before the last try.
        3. FOK at max_affordable (legacy last rung; venues occasionally
           fill FOK where the IOC just missed).

        Any leg returning ``idempotency_ambiguous`` stops the chain dead —
        retrying with a fresh client_order_id could stack exposure on a
        latent fill (C3, unchanged).

        Returns None ONLY when ``skip_initial_attempt`` is set and the loop
        never found an affordable book — nothing was submitted, so the call
        site constructs its ABORTED EDGE-LOST leg exactly as before.
        """
        # Uses module-level logger = logging.getLogger("arbiter.execution")

        order: Optional[Order] = None
        requote_notes: List[str] = []

        if not skip_initial_attempt:
            # Attempt 1: IOC at walked/buffered price
            order = await self._place_order_for_leg(
                arb_id, secondary_platform, secondary_market,
                canonical_id, secondary_side, initial_price, qty,
                use_ioc=True,
            )
            if order.status in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
                logger.info(
                    "  ✓ FALLBACK-1 (IOC at walked): %s filled %d/%d @ %.4f",
                    secondary_side.upper(), order.fill_qty, qty, order.fill_price,
                )
                return order

            # C3: fail-closed gate. When the timeout-recovery path could not
            # prove the platform never received (or never filled) attempt 1's
            # order, firing attempt 2 with a fresh client_order_id risks
            # stacking new exposure on top of a latent fill from attempt 1.
            # Refuse the retry; the operator must reconcile manually.
            if order.idempotency_ambiguous:
                logger.error(
                    "  ✗ FALLBACK SKIPPED: attempt 1 returned idempotency_ambiguous=True"
                    " for %s on %s (status=%s). Refusing retry to avoid double-submit."
                    " Manual reconciliation required.",
                    secondary_side.upper(), secondary_platform, order.status.value,
                )
                order.error += "; fallback retry suppressed - idempotency unproven"
                return order

        # Requote loop: the book that moved one tick between walk and
        # submit routinely comes back within a second. Re-walk and retry
        # while affordable instead of declaring the primary naked.
        adapter = self.adapters.get(secondary_platform)
        can_walk = adapter is not None and hasattr(adapter, "best_executable_price")
        attempts = int(self._secondary_requote_attempts)
        for attempt in range(1, attempts + 1):
            if not can_walk:
                break
            if self._secondary_requote_delay_ms > 0:
                await asyncio.sleep(self._secondary_requote_delay_ms / 1000.0)
            try:
                fillable, walked = await adapter.best_executable_price(
                    secondary_market, secondary_side, qty,
                )
                walked = float(walked)
            except Exception as exc:
                logger.warning(
                    "  ↻ REQUOTE-%d walk failed on %s: %s",
                    attempt, secondary_platform, exc,
                )
                requote_notes.append(f"RQ{attempt}:walk-error")
                continue
            if not fillable or walked <= 0:
                requote_notes.append(f"RQ{attempt}:no-book")
                continue
            is_final = attempt == attempts
            ceiling = max_affordable_price
            if (
                is_final
                and break_even_price is not None
                and break_even_price > ceiling
            ):
                # Final attempt: accept down to break-even. Filling at 0¢
                # net edge beats paying the spread + fees on an unwind.
                ceiling = float(break_even_price)
            if walked > ceiling + 1e-9:
                requote_notes.append(f"RQ{attempt}:walked={walked:.4f}>cap={ceiling:.4f}")
                continue
            logger.info(
                "  ↻ REQUOTE-%d: book back within reach (walked=%.4f <= %.4f)"
                " — retrying IOC on %s",
                attempt, walked, ceiling, secondary_platform,
            )
            retry = await self._place_order_for_leg(
                f"{arb_id}-RQ{attempt}", secondary_platform, secondary_market,
                canonical_id, secondary_side, ceiling, qty,
                use_ioc=True,
                persist_arb_id=arb_id,
            )
            if retry.status in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
                logger.info(
                    "  ✓ REQUOTE-%d: filled %d/%d @ %.4f",
                    attempt, retry.fill_qty, qty, retry.fill_price,
                )
                return retry
            if retry.idempotency_ambiguous:
                logger.error(
                    "  ✗ REQUOTE-%d returned idempotency_ambiguous — chain stops.",
                    attempt,
                )
                retry.error += "; requote retry suppressed - idempotency unproven"
                return retry
            requote_notes.append(f"RQ{attempt}:{retry.status.value}")
            order = retry

        if skip_initial_attempt and order is None:
            # EDGE-LOST entry and the book never came back: nothing was
            # submitted, no venue state exists. Signal the call site to
            # construct its ABORTED leg (a FOK here would be doomed —
            # the walk just proved the book unaffordable).
            logger.info(
                "  ✗ REQUOTE exhausted with no affordable book on %s (%s)",
                secondary_platform,
                "; ".join(requote_notes) or "no walks",
            )
            return None

        # Legacy final rung: FOK at max affordable (some venues handle
        # FOK differently and the book may flicker back at submit time).
        logger.info(
            "  ↻ FALLBACK-FOK: trying FOK at max_affordable=%.4f",
            max_affordable_price,
        )
        order3 = await self._place_order_for_leg(
            f"{arb_id}-F3", secondary_platform, secondary_market,
            canonical_id, secondary_side, max_affordable_price, qty,
            use_ioc=False,  # FOK
            persist_arb_id=arb_id,
        )
        if order3.status in {OrderStatus.FILLED}:
            logger.info(
                "  ✓ FALLBACK-FOK: filled %d @ %.4f",
                order3.fill_qty, order3.fill_price,
            )
            return order3

        # All attempts failed — return the first failed order with an
        # aggregated error trail for downstream logging/recovery.
        logger.error(
            "  ✗ ALL FALLBACKS EXHAUSTED for %s on %s. "
            "Primary %s on %s is now naked.",
            secondary_side.upper(), secondary_platform,
            "YES" if secondary_side == "no" else "NO",
            primary_platform,
        )
        final = order if order is not None else order3
        final.error = (
            f"All fallbacks exhausted: "
            f"IOC@{initial_price:.4f} -> "
            f"{order.status.value if order is not None else 'skipped'}, "
            f"requote[{'; '.join(requote_notes) or 'none'}], "
            f"FOK@{max_affordable_price:.4f} -> {order3.status.value}"
        )
        return final

    async def _place_order_for_leg(
        self,
        arb_id: str,
        platform: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
        *,
        use_ioc: bool = False,
        atomic_arb_stub_opp: Optional["ArbitrageOpportunity"] = None,
        atomic_arb_stub_net_edge: Optional[float] = None,
        persist_arb_id: Optional[str] = None,
    ) -> Order:
        """Dispatch a leg through self.adapters[platform], wrapped in asyncio.wait_for (EXEC-05).

        On local timeout, best-effort cancel through the same adapter.
        Every state transition is persisted via self.store.upsert_order (EXEC-02 / D-16).

        ``use_ioc=True`` selects ``place_ioc`` (immediate-or-cancel) instead
        of ``place_fok``.  Used by the secondary leg of a cross-venue arb so
        a stale book on the secondary doesn't trigger an FOK reject and
        leave the primary naked — IOC accepts a partial fill and the engine
        unwinds the unfilled excess on the primary.

        ``persist_arb_id``: the PARENT arb row to key DB writes to when
        ``arb_id`` is a derived attempt id (requote ``-RQ{n}`` / FOK ``-F3``
        retries pass a suffixed id so each attempt gets a fresh venue-side
        idempotency key). ``execution_orders.arb_id`` is an FK into
        ``execution_arbs`` — persisting the derived id has no parent row and
        the write fails, arming the kill switch mid-execution (live incident
        ARB-000919, 2026-07-06: RQ1's FK violation aborted every later
        fallback plus the auto-unwind). Defaults to ``arb_id`` (a real
        parent) at all non-retry call sites.
        """
        adapter = self.adapters.get(platform)
        if adapter is None:
            return Order(
                order_id=f"{arb_id}-{side.upper()}-NOADAPTER",
                platform=platform,
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=price,
                quantity=qty,
                status=OrderStatus.FAILED,
                timestamp=time.time(),
                error=f"No adapter configured for platform: {platform}",
            )

        # C4.2: refuse to send orders to the platform once a prior store
        # write has failed — placing live orders without an audit trail
        # is worse than a missed opportunity. The kill switch is already
        # armed; an operator must reset before trading resumes.
        if self._db_write_failed:
            logger.critical(
                "_place_order_for_leg blocked: db_write_failed flag is set",
            )
            return Order(
                order_id=f"{arb_id}-{side.upper()}-DBFAIL",
                platform=platform,
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=price,
                quantity=qty,
                status=OrderStatus.ABORTED,
                timestamp=time.time(),
                error="DB write previously failed -- execution blocked by safety gate",
            )

        # Pick the placement method: IOC for the secondary leg of a
        # cross-venue arb, FOK by default.  Adapters that don't expose an
        # async place_ioc fall back to place_fok (legacy polymarket and
        # MagicMock-based test adapters land here — MagicMock auto-creates
        # any attribute name so a plain ``hasattr`` check would route to a
        # synchronous mock and crash on await).
        import inspect as _inspect
        candidate_ioc = getattr(adapter, "place_ioc", None) if use_ioc else None
        place = (
            candidate_ioc
            if candidate_ioc is not None
               and _inspect.iscoroutinefunction(candidate_ioc)
            else adapter.place_fok
        )

        try:
            order = await asyncio.wait_for(
                place(arb_id, market_id, canonical_id, side, price, qty),
                timeout=self.execution_timeout_s,
            )
        except asyncio.TimeoutError:
            # EXEC-05: local timeout fired. Best-effort recovery: ask the
            # adapter to surface any orders we placed under this arb_id+side
            # prefix (the request may have reached the platform but the
            # response got lost), then cancel each match. Synthetic
            # ``partial.order_id`` is the DB row PK only — it must NOT be
            # passed to adapter.cancel_order (CR-01: that always 404s on
            # Kalshi). The new code calls list_open_orders_by_client_id
            # first, then cancel_order on each REAL order returned.
            #
            # C3: every timeout exit sets ``idempotency_ambiguous=True``
            # so ``_execute_with_fallbacks`` refuses to retry with a new
            # client_order_id. Until we add a true "filled-by-client-id"
            # query to the adapter Protocol, ``list_open_orders_by_client_id``
            # cannot prove "no fill" by itself: Kalshi's open-orders endpoint
            # only returns RESTING orders, so an empty result is consistent
            # with both "platform never received" AND "filled and reaped".
            # The cancel-after-timeout path is also ambiguous: a cancel
            # returning success does not guarantee that a partial fill
            # didn't race ahead of it. Fail-closed across the board.
            partial = Order(
                order_id=f"{arb_id}-{side.upper()}-{platform.upper()}",
                platform=platform,
                market_id=market_id,
                canonical_id=canonical_id,
                side=side,
                price=price,
                quantity=qty,
                status=OrderStatus.PENDING,
                timestamp=time.time(),
                error=f"local timeout after {self.execution_timeout_s}s",
                idempotency_ambiguous=True,
            )
            cancelled_any = False
            prefix = f"{arb_id}-{side.upper()}-"
            try:
                open_orders = await adapter.list_open_orders_by_client_id(prefix)
            except Exception as exc:
                logger.warning(
                    "timeout_recovery.lookup_failed platform=%s arb_id=%s prefix=%s err=%s",
                    platform, arb_id, prefix, exc,
                )
                open_orders = []
            found_count = len(open_orders)
            # CR-02 thread-through: when the lookup surfaces a real order, its
            # external_client_order_id is the engine-chosen ARB-prefixed key
            # (the same value Kalshi stored as client_order_id). Propagate it
            # to the synthetic partial so the persisted DB row carries the
            # real idempotency key, not NULL.
            for real in open_orders:
                if real.external_client_order_id:
                    partial.external_client_order_id = real.external_client_order_id
                    break
            for real in open_orders:
                try:
                    if await adapter.cancel_order(real):
                        cancelled_any = True
                except Exception as cancel_exc:
                    logger.warning(
                        "timeout_recovery.cancel_raised platform=%s order_id=%s err=%s",
                        platform, real.order_id, cancel_exc,
                    )
            if cancelled_any:
                partial.status = OrderStatus.CANCELLED
                partial.error += (
                    f"; cancelled {found_count} orphaned order(s)"
                    " found by client_order_id prefix"
                )
            elif found_count > 0:
                partial.status = OrderStatus.FAILED
                partial.error += (
                    f"; found {found_count} orphaned order(s) but cancel failed"
                    " - manual reconciliation required"
                )
            else:
                partial.status = OrderStatus.FAILED
                partial.error += (
                    "; no matching open order found"
                    " - platform may have rejected or never received"
                    " - retry blocked: idempotency unproven"
                )
            order = partial

        # C2 — POST-SUBMIT POLL:
        # If place returned SUBMITTED (accepted onto the book, not yet
        # matched), poll the venue until the order reaches a terminal
        # status or the hard timeout fires.  Otherwise the engine
        # records SUBMITTED as the final state and never re-checks — if
        # the venue fills the order seconds later, the DB row + risk
        # exposure drift away from venue reality forever.
        if order.status == OrderStatus.SUBMITTED:
            order = await self._poll_submitted_to_terminal(adapter, order, arb_id)

        # EXEC-02 / D-16: persist every state transition. Store is optional
        # (dev mode without Postgres) — failures escalate to _handle_db_failure
        # which arms the kill switch and blocks further placements (C4.2).
        #
        # First-leg atomicity (C5.1): when ``atomic_arb_stub_opp`` is provided,
        # write the parent ``execution_arbs`` row in the SAME transaction as
        # this leg's ``execution_orders`` row.  Previously the engine wrote
        # ``record_arb_stub`` early in ``_live_execution`` and then did a lot
        # of work (book-walks, depth checks, profitability re-validation)
        # before the first leg call — any early return / crash in that window
        # left an orphan arb stub with zero legs (169 of 295 prod phantoms).
        # The atomic variant collapses that window to zero: the stub cannot
        # exist without its first leg.
        if self.store is not None:
            db_arb_id = persist_arb_id or arb_id
            try:
                client_order_id = self._derive_client_order_id(order)
                if atomic_arb_stub_opp is not None:
                    await self.store.record_arb_stub_with_leg(
                        arb_id=db_arb_id,
                        canonical_id=canonical_id,
                        first_leg=order,
                        opportunity=atomic_arb_stub_opp,
                        net_edge=atomic_arb_stub_net_edge,
                        client_order_id=client_order_id,
                    )
                else:
                    await self.store.upsert_order(
                        order, arb_id=db_arb_id, client_order_id=client_order_id,
                    )
            except Exception as exc:
                await self._handle_db_failure(
                    op="record_arb_stub_with_leg" if atomic_arb_stub_opp is not None else "upsert_order",
                    arb_id=db_arb_id,
                    canonical_id=canonical_id,
                    exc=exc,
                )
        return order

    def _poll_timeout_for_platform(self, platform: str) -> float:
        """Post-submit poll timeout for a platform. ForecastEx gets a
        tighter window (fills confirm sub-second) so a failed FX secondary
        doesn't leave the primary naked for the full kalshi window."""
        if str(platform or "").lower() == "forecastex":
            return float(getattr(self, "_submit_poll_timeout_forecastex_s", 3.0))
        return float(getattr(self, "_submit_poll_timeout_s", 10.0))

    async def _poll_submitted_to_terminal(
        self,
        adapter: "PlatformAdapter",
        order: Order,
        arb_id: str,
    ) -> Order:
        """C2: bounded poll of a SUBMITTED order until terminal or timeout.

        Returns the latest ``Order`` seen.  If the adapter doesn't expose
        an async ``get_order`` (legacy / mock adapters), silently no-ops
        and returns ``order`` unchanged — the legacy SUBMITTED-flows-
        through-to-recovery path is preserved for those adapters.

        Transient ``get_order`` exceptions are swallowed and the poll
        loop retries on the next interval; only a terminal status or the
        hard timeout exits the loop.
        """
        timeout = self._poll_timeout_for_platform(
            getattr(order, "platform", "") or getattr(adapter, "platform", "")
        )
        if timeout <= 0:
            return order
        import inspect as _inspect
        candidate = getattr(adapter, "get_order", None)
        if candidate is None or not _inspect.iscoroutinefunction(candidate):
            return order

        interval = float(getattr(self, "_submit_poll_interval_s", 0.5))
        # Defensive: a zero/negative interval would spin forever.
        if interval <= 0:
            interval = 0.5

        terminal_statuses = {
            OrderStatus.FILLED,
            OrderStatus.PARTIAL,
            OrderStatus.CANCELLED,
            OrderStatus.FAILED,
            OrderStatus.ABORTED,
        }
        # Bound on WALL-CLOCK time, not sum-of-sleeps: get_order RTT is
        # ~one interval, so accumulating only the sleep made a 10s timeout
        # run ~15-17s wall (live 2026-07-10), extending the naked window.
        deadline = time.monotonic() + timeout
        current = order
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                updated = await adapter.get_order(current)
            except Exception as exc:
                logger.debug(
                    "post_submit_poll.get_order_failed arb=%s err=%s",
                    arb_id, exc,
                )
                continue
            # Carry forward the freshest venue snapshot — even still-
            # SUBMITTED updates may contain a refined fill_qty/price.
            current = updated
            if current.status in terminal_statuses:
                return current
        logger.warning(
            "post_submit_poll.timeout arb=%s order=%s — leg still SUBMITTED after %.1fs",
            arb_id, current.order_id, timeout,
        )
        # Cancel the still-SUBMITTED order so it doesn't fill later while
        # the engine has moved on. If cancel returns False or raises we
        # have no proof the order is dead — mark ambiguous so the fallback
        # chain refuses to retry. Either way, re-poll get_order: a race
        # between fill and cancel can leave the order FILLED, and the
        # downstream accounting must see the real fill, not a phantom
        # cancel.
        try:
            cancel_ok = await adapter.cancel_order(current)
        except Exception as exc:
            logger.warning(
                "post_submit_poll.cancel_raised arb=%s order=%s err=%s",
                arb_id, current.order_id, exc,
            )
            cancel_ok = False

        # Re-poll once to learn the post-cancel reality (CANCELLED, FILLED
        # if the order filled in the race window, or still SUBMITTED if
        # the venue dropped the cancel on the floor).
        try:
            refreshed = await adapter.get_order(current)
            current = refreshed
        except Exception as exc:
            logger.debug(
                "post_submit_poll.recheck_failed arb=%s err=%s",
                arb_id, exc,
            )

        # Fail-closed: if the order is still not in a terminal state OR
        # the cancel call itself failed, treat as idempotency-ambiguous.
        # A FILLED re-poll (real proof of fill) is positive evidence and
        # clears ambiguity.
        if current.status not in terminal_statuses or not cancel_ok:
            if current.status != OrderStatus.FILLED:
                current.idempotency_ambiguous = True
                logger.warning(
                    "post_submit_poll.ambiguous arb=%s order=%s status=%s cancel_ok=%s",
                    arb_id, current.order_id, current.status.value, cancel_ok,
                )
        return current

    @staticmethod
    def _derive_client_order_id(order: Order) -> Optional[str]:
        """Return the adapter-populated client_order_id, or None.

        Kalshi adapter populates ``Order.external_client_order_id`` with the
        ``ARB-{n}-{SIDE}-{hex}`` string used as the Kalshi idempotency key
        (the value sent to Kalshi as ``client_order_id`` in the order body).
        Polymarket has no client_order_id concept and leaves the field None.
        The previous ``-`` heuristic on ``order.order_id`` was unsound because
        Kalshi's server-assigned order_ids also contain ``-``, causing the
        DB ``client_order_id`` column to be populated with the platform id
        rather than the engine-chosen idempotency key (CR-02).
        """
        return order.external_client_order_id

    @staticmethod
    def _derive_arb_id_from_order(order: Order) -> Optional[str]:
        if order.order_id and order.order_id.startswith("ARB-"):
            parts = order.order_id.split("-")
            if len(parts) >= 2:
                return f"{parts[0]}-{parts[1]}"
        return None

    @staticmethod
    def _leg_possibly_live(leg: Order) -> bool:
        """A dead-looking leg whose venue state is not actually proven.

        Markers: the C3 ``idempotency_ambiguous`` flag (every local-timeout
        exit sets it) or error text produced by the orphan-cancel and
        broker-id-loss paths ("cancel failed", "MAY be live").
        """
        if getattr(leg, "idempotency_ambiguous", False):
            return True
        err = (leg.error or "").lower()
        return "cancel failed" in err or "may be live" in err or "may still be live" in err

    async def _verify_leg_dead(self, leg: Order) -> Optional[bool]:
        """Re-read a possibly-live leg at the venue before unwinding against it.

        Returns True when the venue proves the leg dead with zero fills,
        False when the venue reports fills (``leg`` is mutated with the
        fresh state so callers can re-pair), and None when the state is
        still unprovable — callers MUST fail closed on None.
        """
        adapter = self.adapters.get(leg.platform)
        if adapter is None or not hasattr(adapter, "get_order"):
            return None
        try:
            refreshed = await adapter.get_order(leg)
        except Exception as exc:
            logger.error(
                "verify_before_unwind.get_order_failed platform=%s order=%s err=%s",
                leg.platform, leg.order_id, exc,
            )
            return None
        target = refreshed if refreshed is not None else leg
        if float(getattr(target, "fill_qty", 0) or 0) > 0:
            return False
        if getattr(target, "idempotency_ambiguous", False):
            return None
        if target.status in {OrderStatus.CANCELLED, OrderStatus.FAILED, OrderStatus.ABORTED}:
            return True
        # Anything still non-terminal at the venue is not proven dead.
        return None

    async def _recover_one_leg_risk(self, arb_id: str, opp: ArbitrageOpportunity, leg_yes: Order, leg_no: Order) -> Tuple[List[str], float]:
        """Handle one-leg exposure (SAFE-03, plan 03-03).

        Classifies the legs: if exactly one is FILLED and the other is not,
        we have a naked position. Emit a structured ``one_leg_exposure``
        incident with the full operator-facing metadata, then hand off to
        ``SafetySupervisor.handle_one_leg_exposure`` (when wired) so the
        supervisor fires the Telegram + dedicated WS channels.

        The generic "Partial fill or one-leg risk detected" incident is
        preserved for the fallback case (both filled / both failed / both
        cancelled) so an operator never sees a silent recovery path.

        Cancel-still-open loop at the tail is unchanged — the still-open leg
        (if any) is best-effort cancelled after the incident fanout.

        Returns
        -------
        (notes, unwind_pnl)
            ``notes`` are the per-step outcome strings (cancel-yes:ok,
            unwind-no:filled(10/10), …) attached to the ArbExecution.
            ``unwind_pnl`` is the realized P&L from the auto-unwind
            (negative when the unwind sells back at a worse price than the
            original fill). Booked into ArbExecution.realized_pnl by the
            caller so reconciliation drift accounts for the unwind cost
            instead of leaving it as silent slippage.
        """
        self._recovery_count += 1
        notes: List[str] = []
        unwind_pnl: float = 0.0

        # FILL-02 (2026-05-27): with IOC primary, an order can return
        # status=FILLED with fill_qty=0 (the IOC was accepted by the
        # venue but matched zero contracts). Zero-fill is NOT exposure
        # — require non-zero fill_qty for the naked-leg trigger to fire.
        # Otherwise every zero-fill IOC produced a bogus
        # ``one_leg_exposure`` critical incident with exposure_usd=0.00,
        # blocking the readiness gate and gating live trading.
        yes_filled = (
            leg_yes.status == OrderStatus.FILLED
            and float(leg_yes.fill_qty or 0) > 0
        )
        no_filled = (
            leg_no.status == OrderStatus.FILLED
            and float(leg_no.fill_qty or 0) > 0
        )

        if yes_filled ^ no_filled:
            # Classic naked position: exactly one side confirmed filled.
            filled_leg = leg_yes if yes_filled else leg_no
            failed_leg = leg_no if yes_filled else leg_yes
            exposure_usd = float(filled_leg.fill_qty) * float(filled_leg.fill_price)
            recommended_unwind = (
                f"Sell {filled_leg.fill_qty} {filled_leg.side.upper()} on "
                f"{filled_leg.platform.upper()} at market to close exposure"
            )
            incident = await self._record_incident(
                arb_id,
                opp,
                "critical",
                "One-leg exposure detected — naked position requires unwind",
                metadata={
                    "event_type": "one_leg_exposure",
                    "filled_platform": filled_leg.platform,
                    "filled_side": filled_leg.side,
                    "filled_qty": filled_leg.fill_qty,
                    "filled_price": filled_leg.fill_price,
                    "exposure_usd": exposure_usd,
                    "failed_platform": failed_leg.platform,
                    "failed_reason": getattr(failed_leg, "error", None)
                    or str(failed_leg.status),
                    "recommended_unwind": recommended_unwind,
                },
            )
            if self._safety is not None:
                try:
                    await self._safety.handle_one_leg_exposure(
                        incident, filled_leg, failed_leg, opp,
                    )
                except Exception as exc:
                    # Supervisor hook failures must not block the cancel loop.
                    logger.error(
                        "safety.handle_one_leg_exposure raised: %s", exc,
                    )
        else:
            # Fallback: neither side isolated cleanly (e.g. both filled,
            # both failed, or partial). Keep the pre-existing generic
            # incident so the recovery path stays visible in ops logs.
            await self._record_incident(
                arb_id,
                opp,
                "critical",
                "Partial fill or one-leg risk detected, starting recovery",
                metadata={"leg_yes": leg_yes.to_dict(), "leg_no": leg_no.to_dict()},
            )

        for leg in (leg_yes, leg_no):
            if leg.status in {OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIAL}:
                # Snapshot pre-cancel status: _cancel_order mutates leg.status
                # to OrderStatus.CANCELLED on success (engine.py:1063), so we
                # must capture what the leg WAS before deciding whether the
                # Task 1 _live_execution edit booked a per-platform
                # reservation for it.
                original_status = leg.status
                cancelled = await self._cancel_order(leg, arb_id=arb_id)
                notes.append(f"cancel-{leg.side}:{'ok' if cancelled else 'failed'}")
                # Plan 03-08 (SAFE-02 gap closure): if a previously
                # SUBMITTED or PARTIAL leg's cancel succeeded, release
                # the per-platform reservation that Task 1's
                # _live_execution edit booked. PENDING legs were never
                # recorded (place_fok had not returned), so they have
                # nothing to release. Failed cancels (cancelled=False)
                # mean the resting order may still exist at the venue
                # — the exposure is still real, do not release.
                if cancelled and original_status in {
                    OrderStatus.SUBMITTED,
                    OrderStatus.PARTIAL,
                }:
                    # Release the unfilled notional. For PARTIAL legs
                    # the filled portion stays booked (this is a known
                    # simplification — full PARTIAL accounting is
                    # outside the SAFE-02 gap-closure scope).
                    unfilled_qty = max(leg.quantity - leg.fill_qty, 0)
                    if unfilled_qty > 0:
                        self.risk.release_trade(
                            opp.canonical_id,
                            unfilled_qty * leg.price,
                            platform=leg.platform,
                        )

        # Reverse-order unwind on the over-filled leg (best-effort). Closes
        # whatever portion of the primary is unhedged after the secondary
        # came back.  Three cases:
        #   1. yes_filled XOR no_filled  → secondary CANCELLED entirely:
        #      unwind ALL of the primary's filled qty.
        #   2. both filled, qty mismatch → secondary IOC partially filled:
        #      unwind only the diff (primary.fill_qty - secondary.fill_qty)
        #      so the matched portion stays paired and only the un-paired
        #      excess gets sold.
        #   3. both filled, qty equal    → no naked exposure, skip unwind.
        # Case 2 is what the Polymarket-IOC switch unlocks: previously the
        # secondary either filled-in-full or killed-in-full, so case 2 was
        # impossible.
        # Determine the unhedged exposure across THREE possible patterns:
        #   1. One leg FILLED, other CANCELLED/FAILED  → unwind all of FILLED
        #   2. Both legs have partial-or-full fills with mismatched qty
        #      (the IOC partial-fill pattern)           → unwind only the diff
        #   3. Both legs filled with matching qty       → no exposure to unwind
        # Pattern 2 is unreachable until we wired IOC; FOK either filled or
        # killed entirely so the qty was always 0 or full.
        unhedged_leg = None
        unhedged_qty = 0
        # Treat both FILLED and PARTIAL as "has fills" — IOC reports PARTIAL
        # when only some of the order matched, with fill_qty < quantity.
        any_yes_fill = leg_yes.status in {OrderStatus.FILLED, OrderStatus.PARTIAL} and float(leg_yes.fill_qty or 0) > 0
        any_no_fill = leg_no.status in {OrderStatus.FILLED, OrderStatus.PARTIAL} and float(leg_no.fill_qty or 0) > 0
        if any_yes_fill and not any_no_fill:
            unhedged_leg = leg_yes
            unhedged_qty = int(leg_yes.fill_qty or 0)
        elif any_no_fill and not any_yes_fill:
            unhedged_leg = leg_no
            unhedged_qty = int(leg_no.fill_qty or 0)
        elif any_yes_fill and any_no_fill:
            yes_qty = int(leg_yes.fill_qty or 0)
            no_qty = int(leg_no.fill_qty or 0)
            if yes_qty != no_qty:
                if yes_qty > no_qty:
                    unhedged_leg = leg_yes
                    unhedged_qty = yes_qty - no_qty
                else:
                    unhedged_leg = leg_no
                    unhedged_qty = no_qty - yes_qty
                logger.info(
                    "  ⚖ Secondary IOC partial-fill detected: "
                    "yes=%d no=%d → unwinding %d %s on %s",
                    yes_qty, no_qty, unhedged_qty,
                    unhedged_leg.side.upper(), unhedged_leg.platform,
                )

        # VERIFY-BEFORE-UNWIND (2026-07-06): when the dead-looking
        # counterpart is only AMBIGUOUSLY dead (timeout with failed orphan
        # cancel, unproven-empty lookup, "MAY be live" error), selling the
        # primary now races a latent secondary fill — a late fill after the
        # unwind leaves the book net-short with no owner. Re-verify at the
        # venue first: proven dead -> unwind; proven late-filled -> the arb
        # actually completed (unwind only any remaining diff); unprovable
        # -> block the unwind and route to manual reconciliation with
        # exposure still booked.
        if unhedged_leg is not None and unhedged_qty > 0:
            counterpart = leg_no if unhedged_leg is leg_yes else leg_yes
            if (
                float(counterpart.fill_qty or 0) == 0
                and self._leg_possibly_live(counterpart)
            ):
                verdict = await self._verify_leg_dead(counterpart)
                if verdict is None:
                    notes.append(
                        f"unwind-blocked:ambiguous-secondary({counterpart.platform})"
                    )
                    await self._record_incident(
                        arb_id, opp, "critical",
                        "Unwind blocked: secondary order state unverifiable — "
                        "it MAY still be live at the venue; manual "
                        "reconciliation required before closing the primary",
                        metadata={
                            "event_type": "unwind_blocked_ambiguous_secondary",
                            "ambiguous_platform": counterpart.platform,
                            "ambiguous_order_id": counterpart.order_id,
                            "ambiguous_error": counterpart.error,
                            "filled_platform": unhedged_leg.platform,
                            "filled_side": unhedged_leg.side,
                            "filled_qty": unhedged_leg.fill_qty,
                            "filled_price": unhedged_leg.fill_price,
                            "recommended_action": (
                                "Confirm the secondary order's true state at "
                                f"{counterpart.platform} (id "
                                f"{counterpart.order_id}); if dead, manually "
                                "unwind the primary; if filled, the arb "
                                "completed and needs no unwind"
                            ),
                        },
                    )
                    return notes, unwind_pnl
                if verdict is False:
                    late_qty = int(counterpart.fill_qty or 0)
                    notes.append(f"verify-secondary:late-fill({late_qty})")
                    if late_qty >= unhedged_qty:
                        await self._record_incident(
                            arb_id, opp, "warning",
                            "Late secondary fill discovered during recovery — "
                            "legs are paired; unwind skipped",
                            metadata={
                                "event_type": "late_secondary_fill_balanced",
                                "platform": counterpart.platform,
                                "late_fill_qty": late_qty,
                            },
                        )
                        return notes, unwind_pnl
                    unhedged_qty -= late_qty
                else:
                    notes.append("verify-secondary:dead")

        if unhedged_leg is not None and unhedged_qty > 0:
            filled_leg = unhedged_leg
            adapter = self.adapters.get(filled_leg.platform)
            if adapter is not None and hasattr(adapter, "place_unwind_sell"):
                unwind_target = unhedged_qty
                if unwind_target > 0:
                    # SMART UNWIND: try resting at break-even first, then
                    # fall back to market-IOC.  Currently the recovery path
                    # IMMEDIATELY market-sells at the panic IOC price, which
                    # captures whatever bid happens to be at the top of book
                    # at submit time.  That's gambling on book direction —
                    # we make money when the market drifts in our favor and
                    # lose when it drifts against us.  By first resting at
                    # the original fill_price for ~30s we let buyers come
                    # to us at our cost basis (break-even or better), only
                    # falling back to the panic-sell when the resting order
                    # doesn't get hit.  This converts most "lucky-direction
                    # wins" and "unlucky-direction losses" into reliable
                    # break-even closes.
                    unwind_order = None
                    smart_unwind_raised = False
                    try:
                        unwind_order = await self._smart_unwind(
                            arb_id, filled_leg, unwind_target,
                        )
                    except Exception as exc:
                        smart_unwind_raised = True
                        logger.critical(
                            "auto_unwind.exception platform=%s side=%s err=%s — "
                            "releasing reservation; position becomes a manual-"
                            "review incident",
                            filled_leg.platform, filled_leg.side, exc,
                        )
                        notes.append(f"unwind-{filled_leg.side}:exception")
                    finally:
                        # H7: ensure release_trade runs even when _smart_unwind
                        # raises so the per-platform exposure budget cannot
                        # leak indefinitely.  Without this, a single adapter
                        # contract violation would lock new trading on the
                        # surviving platform until manual intervention.  The
                        # underlying naked position is preserved as a CRITICAL
                        # incident so operators can reconcile it.
                        if smart_unwind_raised:
                            try:
                                self.risk.release_trade(
                                    opp.canonical_id,
                                    unwind_target * float(filled_leg.fill_price),
                                    platform=filled_leg.platform,
                                )
                            except Exception as rel_exc:  # noqa: BLE001
                                logger.error(
                                    "auto_unwind.release_trade_failed err=%s",
                                    rel_exc,
                                )
                            try:
                                await self._record_incident(
                                    arb_id, opp, "critical",
                                    f"Smart-unwind raised on {filled_leg.platform} "
                                    f"{filled_leg.side.upper()} — manual review required",
                                    metadata={
                                        "event_type": "auto_unwind_exception",
                                        "platform": filled_leg.platform,
                                        "side": filled_leg.side,
                                        "target_qty": unwind_target,
                                        "fill_price": float(filled_leg.fill_price),
                                    },
                                )
                            except Exception as inc_exc:  # noqa: BLE001
                                logger.warning(
                                    "auto_unwind_exception incident emit failed: %s",
                                    inc_exc,
                                )
                    if unwind_order is not None:
                        unwound_qty = float(unwind_order.fill_qty or 0)
                        notes.append(
                            f"unwind-{filled_leg.side}:{unwind_order.status.value}"
                            f"({unwound_qty:.0f}/{unwind_target})"
                        )
                        if unwound_qty > 0:
                            # Book the realized loss from the unwind: bought
                            # at filled_leg.fill_price, sold back at
                            # unwind_order.fill_price. This number is what
                            # was previously missing from realized_pnl —
                            # reconciliation drift was correctly flagging it
                            # as unexplained balance change.
                            buy_cost_per = float(filled_leg.fill_price)
                            sell_revenue_per = float(unwind_order.fill_price)
                            unwind_pnl += unwound_qty * (sell_revenue_per - buy_cost_per)
                            self.risk.release_trade(
                                opp.canonical_id,
                                unwound_qty * float(filled_leg.fill_price),
                                platform=filled_leg.platform,
                            )
                            try:
                                await self._record_incident(
                                    arb_id, opp, "warning",
                                    f"Auto-unwind closed {unwound_qty:.0f}/"
                                    f"{unwind_target} contracts on {filled_leg.platform}",
                                    metadata={
                                        "event_type": "auto_unwind",
                                        "platform": filled_leg.platform,
                                        "side": filled_leg.side,
                                        "fill_qty": unwound_qty,
                                        "target_qty": unwind_target,
                                        "panic_price": float(unwind_order.price),
                                        "buy_price": buy_cost_per,
                                        "sell_price": sell_revenue_per,
                                        "realized_pnl": unwound_qty * (sell_revenue_per - buy_cost_per),
                                    },
                                )
                            except Exception as exc:
                                logger.warning(
                                    "auto_unwind incident emit failed: %s", exc,
                                )

                            # Auto-resolve the open critical incidents this arb
                            # raised at one-leg-exposure detection time so the
                            # readiness gate doesn't keep trading frozen on an
                            # exposure that no longer exists.  We only resolve
                            # incidents whose metadata.event_type is one of
                            # the recovery-pair types — never blanket-resolve
                            # by arb_id (a future audit-flagged incident on
                            # the same arb stays open for human review).
                            await self._auto_resolve_recovery_incidents(
                                arb_id,
                                unwound_qty=unwound_qty,
                                unwind_target=unwind_target,
                            )
        return notes, unwind_pnl

    async def _auto_resolve_recovery_incidents(
        self,
        arb_id: str,
        *,
        unwound_qty: float,
        unwind_target: int,
    ) -> None:
        """Mark soft-naked / one-leg-exposure incidents resolved after unwind.

        Called from ``_recover_one_leg_risk`` once an auto-unwind has reduced
        the naked exposure to zero (or to a value the operator can review at
        leisure rather than treating as a live emergency).  Without this the
        readiness gate's ``_check_incidents`` keeps the venue frozen because
        of incidents that describe past — already-recovered — exposure, and
        the auto-executor stops attempting new trades.

        Only auto-resolves recovery-related event types.  Audit, drift, and
        other operator-actionable incidents stay open even if they share
        ``arb_id`` with a recovered position.
        """
        if unwound_qty <= 0:
            return
        recovery_event_types = {
            "one_leg_exposure",
            "soft_naked_leg",
            "auto_unwind",
        }
        unwind_complete = unwound_qty >= unwind_target * 0.99
        note = (
            f"Auto-resolved: unwind closed {unwound_qty:.0f}/{unwind_target} "
            "contracts; naked exposure cleared."
            if unwind_complete
            else f"Auto-resolved: partial unwind closed {unwound_qty:.0f}/"
                 f"{unwind_target}; remaining exposure logged separately."
        )
        for incident in list(self._incidents):
            if incident.arb_id != arb_id:
                continue
            if incident.status == "resolved":
                continue
            event_type = ""
            if isinstance(incident.metadata, dict):
                event_type = str(
                    incident.metadata.get("event_type", "")
                ).lower()
            if event_type not in recovery_event_types:
                continue
            try:
                await self.resolve_incident(incident.incident_id, note=note)
            except Exception as exc:
                logger.warning(
                    "auto-resolve incident %s failed: %s",
                    incident.incident_id, exc,
                )

    async def _smart_unwind(self, arb_id: str, filled_leg: Order, qty: int) -> Order:
        """Two-phase unwind: try resting at break-even, fall back to market.

        Phase 1 (PROFIT-PRESERVING): place a GTC SELL at the original
        ``fill_price`` (break-even — closes the position with zero P&L
        excluding fees).  Poll ``get_order`` for up to ~30s.  If a buyer
        comes in at >= our limit the position closes flat.

        Phase 2 (FALLBACK): if the resting order doesn't fill within the
        timeout, cancel it and run the existing ``place_unwind_sell``
        market-IOC at panic_price=$0.01 (= sell at any bid).  This
        captures whatever the book offers right now — same behaviour as
        before the smart-unwind change.

        Why this matters: the recovery-path losses we observed were
        almost all "market drifted against us in the second between
        primary fill and panic-sell".  Resting briefly lets the market
        come to us at our cost basis, converting most of those losses
        into break-even or small wins.

        Always returns an Order (the resting fill OR the market-IOC
        fallback).  Never raises across this boundary.
        """
        adapter = self.adapters.get(filled_leg.platform)
        # If the adapter doesn't support resting sells, skip Phase 1 and
        # go straight to the market-IOC fallback.
        if adapter is None or not hasattr(adapter, "place_resting_sell"):
            return await adapter.place_unwind_sell(
                f"{arb_id}-UNWIND",
                filled_leg.market_id,
                filled_leg.canonical_id,
                filled_leg.side,
                qty,
            )

        break_even = float(filled_leg.fill_price)
        rest_timeout_s = float(self._smart_unwind_timeout_s)
        rest_poll_interval_s = 1.0

        # Phase 1: rest at break-even.
        try:
            resting = await adapter.place_resting_sell(
                f"{arb_id}-UNWIND-REST",
                filled_leg.market_id,
                filled_leg.canonical_id,
                filled_leg.side,
                break_even,
                qty,
            )
        except Exception as exc:
            logger.warning(
                "smart_unwind.resting_place_failed platform=%s err=%s — falling back to market",
                filled_leg.platform, exc,
            )
            resting = None

        if resting is None or resting.status in {OrderStatus.FAILED, OrderStatus.CANCELLED}:
            # Couldn't even place the resting order — go straight to market.
            return await adapter.place_unwind_sell(
                f"{arb_id}-UNWIND",
                filled_leg.market_id,
                filled_leg.canonical_id,
                filled_leg.side,
                qty,
            )

        if resting.status == OrderStatus.FILLED and resting.fill_qty >= qty * 0.99:
            # Filled instantly — typically means the book already had a bid
            # at or above our break-even.  Best possible outcome.
            logger.info(
                "  ✓ Smart unwind filled INSTANTLY at break-even: %d @ $%.4f",
                resting.fill_qty, resting.fill_price,
            )
            return resting

        # Poll for fill.
        elapsed = 0.0
        while elapsed < rest_timeout_s:
            await asyncio.sleep(rest_poll_interval_s)
            elapsed += rest_poll_interval_s
            try:
                updated = await adapter.get_order(resting)
            except Exception as exc:
                logger.debug(
                    "smart_unwind.get_order_failed err=%s — retrying", exc,
                )
                continue
            if updated.status == OrderStatus.FILLED and updated.fill_qty >= qty * 0.99:
                logger.info(
                    "  ✓ Smart unwind filled at break-even after %.1fs: %d @ $%.4f",
                    elapsed, updated.fill_qty, updated.fill_price,
                )
                return updated
            if updated.status in {OrderStatus.CANCELLED, OrderStatus.FAILED}:
                # Resting order died on its own — fall through to market.
                break

        # Phase 2: cancel and market-sell.
        #
        # C1 race protection: between the last successful poll and the
        # cancel landing on the venue, the resting order may have filled
        # (the poll only catches state changes between calls — a fill
        # that arrives while the cancel is in flight is invisible until
        # we re-read). If we blindly market-sell ``qty`` after cancel,
        # the position closes twice — once flat via the late resting
        # fill, once at panic price via the market-IOC — and the engine
        # ends up net SHORT by ``qty``. Mandatory: re-read state once
        # more AFTER cancel and only market-sell the truly unfilled
        # remainder.
        try:
            await adapter.cancel_order(resting)
        except Exception as exc:
            logger.warning(
                "smart_unwind.cancel_failed err=%s — re-checking resting state before market-sell", exc,
            )

        # Single mandatory post-cancel state read. On adapter failure,
        # fall back to the last-known resting state from the poll loop
        # (``resting`` is the placed-order snapshot if no poll completed,
        # otherwise the last ``updated``).
        post_cancel_state = resting
        try:
            post_cancel_state = await adapter.get_order(resting)
        except Exception as exc:
            logger.warning(
                "smart_unwind.post_cancel_get_failed err=%s — using last-known resting state",
                exc,
            )

        rest_filled_qty = int(post_cancel_state.fill_qty or 0)
        # Full close raced through — resting fully filled in the window.
        # Do NOT send the market-IOC; the position is already flat.
        if (
            post_cancel_state.status == OrderStatus.FILLED
            or rest_filled_qty >= qty
        ):
            logger.info(
                "  ✓ Smart unwind raced cancel — resting filled %d/%d @ $%.4f, "
                "skipping market-IOC",
                rest_filled_qty, qty, float(post_cancel_state.fill_price or 0.0),
            )
            return post_cancel_state

        remaining_qty = qty - rest_filled_qty
        if remaining_qty <= 0:
            return post_cancel_state

        if rest_filled_qty > 0:
            logger.info(
                "  → Smart unwind resting partially filled %d/%d @ $%.4f; "
                "market-selling remainder %d",
                rest_filled_qty, qty,
                float(post_cancel_state.fill_price or 0.0),
                remaining_qty,
            )
        else:
            logger.info(
                "  → Smart unwind resting @ $%.4f did not fill in %.0fs, market-selling %d",
                break_even, rest_timeout_s, remaining_qty,
            )

        market_order = await adapter.place_unwind_sell(
            f"{arb_id}-UNWIND",
            filled_leg.market_id,
            filled_leg.canonical_id,
            filled_leg.side,
            remaining_qty,
        )

        # If the resting order also delivered fills before the cancel,
        # blend the two fills into one Order so realized-P&L accounting
        # reflects BOTH legs of the close (break-even portion + panic-
        # price portion) at a quantity-weighted average price.
        if rest_filled_qty > 0 and int(market_order.fill_qty or 0) > 0:
            market_filled_qty = int(market_order.fill_qty)
            total_filled = rest_filled_qty + market_filled_qty
            blended_price = (
                rest_filled_qty * float(post_cancel_state.fill_price or 0.0)
                + market_filled_qty * float(market_order.fill_price or 0.0)
            ) / max(total_filled, 1)
            market_order.fill_qty = total_filled
            market_order.fill_price = blended_price
            market_order.quantity = qty

        return market_order

    async def _cancel_order(self, order: Order, arb_id: Optional[str] = None) -> bool:
        """Dispatch cancel through self.adapters[order.platform]. Platform-agnostic.

        ``arb_id`` should be passed explicitly when the caller knows it, so the
        cancel-state upsert succeeds even for venue-assigned order_ids
        (e.g. Polymarket "9QHH..." IDs that don't carry the ARB-NNN prefix).
        """
        adapter = self.adapters.get(order.platform)
        if adapter is None:
            logger.warning("No adapter for platform %s on cancel", order.platform)
            return False
        try:
            cancelled = await adapter.cancel_order(order)
        except Exception as exc:
            logger.warning("Adapter %s cancel_order raised: %s", order.platform, exc)
            return False
        if cancelled and self.store is not None:
            order.status = OrderStatus.CANCELLED
            resolved_arb_id = arb_id or self._derive_arb_id_from_order(order)
            try:
                await self.store.upsert_order(order, arb_id=resolved_arb_id)
            except Exception as exc:
                await self._handle_db_failure(
                    op="upsert_order_cancel",
                    arb_id=resolved_arb_id,
                    canonical_id=getattr(order, "canonical_id", None),
                    exc=exc,
                )
        return cancelled

    async def _publish_execution(self, execution: ArbExecution):
        for subscriber in list(self._subscribers):
            try:
                subscriber.put_nowait(execution)
            except asyncio.QueueFull:
                logger.debug("Skipping slow execution subscriber")

    async def record_incident(
        self,
        *,
        arb_id: str,
        canonical_id: str,
        severity: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ExecutionIncident:
        incident = ExecutionIncident(
            incident_id=f"INC-{uuid.uuid4().hex[:8]}",
            arb_id=arb_id,
            canonical_id=canonical_id,
            severity=severity,
            message=message,
            timestamp=time.time(),
            metadata=metadata or {},
        )
        self._incidents.appendleft(incident)
        for subscriber in list(self._incident_subscribers):
            try:
                subscriber.put_nowait(incident)
            except asyncio.QueueFull:
                logger.debug("Skipping slow incident subscriber")
        logger.warning("[%s] %s", severity.upper(), message)
        # Telegram route for critical + stranded_position incidents. The
        # stranded-reconciler docstring promises this routing; without it the
        # reconciler's discoveries are invisible outside the dashboard. We
        # skip event_types already routed by SafetySupervisor to avoid double
        # sends (one_leg_exposure) and kill_switch trips (which fire their
        # own Telegram via supervisor.trip_kill).
        event_type = (incident.metadata or {}).get("event_type", "")
        supervisor_handled = {"one_leg_exposure"}
        # 2026-05-29: extend routing to selected warning event_types so
        # operators see in-band activity (trade-gate blocks, mitigation
        # attempts, circuit transitions) without staring at logs. Other
        # warnings (depth_low, dead_ask, opp_skip) stay silent to avoid
        # chat noise. Per-event dedup at line 3317 prevents floods.
        warning_eligible_event_types = {
            "trade_gate_blocked",
            "complete_arb_attempt",
            "circuit_breaker_open",
            "circuit_breaker_close",
        }
        telegram_eligible = (
            severity == "critical"
            or event_type == "stranded_position"
            or (severity == "warning" and event_type in warning_eligible_event_types)
        ) and event_type not in supervisor_handled
        if telegram_eligible:
            try:
                from ..notifiers.fmt import DIVIDER as _DIV, h as _h, usd as _usd
                meta = incident.metadata or {}

                # Choose icon + header based on event type
                if event_type == "stranded_position":
                    emoji = "\U0001f6a8"
                    header = "STRANDED POSITION"
                elif event_type == "circuit_breaker_open":
                    emoji = "\U0001f534"
                    header = "CIRCUIT BREAKER OPEN"
                elif event_type == "circuit_breaker_close":
                    emoji = "\U0001f7e2"
                    header = "CIRCUIT BREAKER CLOSED"
                elif event_type == "trade_gate_blocked":
                    emoji = "\U0001f6ab"
                    header = "TRADE GATE BLOCKED"
                elif severity == "critical":
                    emoji = "\U0001f6d1"
                    header = "CRITICAL INCIDENT"
                else:
                    emoji = "\U0001f7e1"
                    header = f"WARNING ({_h(event_type)})"

                extra_lines: list[str] = []
                if event_type == "stranded_position":
                    side = str(meta.get("side", "")).upper() or "?"
                    qty_val = meta.get("qty", 0) or 0
                    try:
                        qty_abs = abs(int(qty_val))
                    except (TypeError, ValueError):
                        qty_abs = 0
                    platform = str(meta.get("platform", "")).upper() or "?"
                    title = meta.get("title") or canonical_id
                    extra_lines.append(f"\U0001f4cd Market: <code>{_h(title)}</code>")
                    extra_lines.append(
                        f"  <code>{qty_abs}</code> {side} on <b>{platform}</b>"
                    )
                    mtm = meta.get("mtm_usd")
                    unreal = meta.get("unrealized_usd")
                    if isinstance(mtm, (int, float)):
                        extra_lines.append(f"  MTM: <code>{_usd(float(mtm), signed=True)}</code>")
                    if isinstance(unreal, (int, float)):
                        extra_lines.append(f"  Unrealized: <code>{_usd(float(unreal), signed=True)}</code>")
                elif event_type in ("circuit_breaker_open", "circuit_breaker_close"):
                    venue = str(meta.get("venue", meta.get("platform", ""))).upper()
                    if venue:
                        extra_lines.append(f"\U0001f3e6 Venue: <b>{_h(venue)}</b>")

                msg = (
                    f"{emoji} <b>{header}</b>\n"
                    f"{_DIV}\n"
                    f"<code>{_h(incident.incident_id)}</code>\n"
                    + ("\n".join(extra_lines) + "\n" if extra_lines else "")
                    + f"{_DIV}\n"
                    + f"{_h(message)}"
                )
                notifier = getattr(
                    getattr(self, "balance_monitor", None), "notifier", None
                )
                if notifier is not None:
                    # Dedup on canonical+event so a flapping reconciler/db
                    # doesn't blast Telegram every loop.
                    dedup_key = f"incident:{event_type or severity}:{canonical_id}"
                    await notifier.send(msg, dedup_key=dedup_key)
            except Exception as exc:
                logger.warning(
                    "record_incident: telegram send failed (severity=%s event_type=%s): %s",
                    severity, event_type, exc,
                )
        # EXEC-02 / D-16: persist the incident to Postgres if a store is wired.
        if self.store is not None:
            try:
                await self.store.insert_incident(incident)
            except Exception as exc:
                await self._handle_db_failure(
                    op="insert_incident",
                    arb_id=arb_id,
                    canonical_id=canonical_id,
                    exc=exc,
                )
        return incident

    async def _handle_db_failure(
        self,
        *,
        op: str,
        arb_id: Optional[str],
        canonical_id: Optional[str],
        exc: BaseException,
    ) -> None:
        """C4.2: escalate a swallowed ``ExecutionStore.*`` failure.

        Before this hook every `try/except: logger.warning(...)` around a
        store write would silently drop the audit trail and let the engine
        keep placing live orders. That meant a Postgres bounce could leave
        real money moving with no record of it. This method:

          * logs critical (not warning) — the operator MUST see this;
          * sets ``self._db_write_failed = True`` so ``_place_order_for_leg``
            refuses new placements until the kill switch is reset;
          * broadcasts a critical ``db_write_failure`` incident on the
            in-memory deque (no DB write — the store is broken);
          * trips ``SafetySupervisor`` so adapters cancel every open order
            and the dashboard surfaces the kill state.

        Idempotent: repeat calls log but skip the broadcast / trip so a
        burst of follow-on failures doesn't spam the deque or recurse into
        ``trip_kill``. The ``op="insert_incident"`` case never recurses
        into ``record_incident`` because the incident here is appended
        directly to the deque rather than going through that path.
        """
        logger.critical(
            "ExecutionStore.%s failed -- BLOCKING further execution: %s",
            op, exc,
        )
        already_blocked = self._db_write_failed
        self._db_write_failed = True
        if already_blocked:
            return

        incident = ExecutionIncident(
            incident_id=f"INC-DB-{uuid.uuid4().hex[:8]}",
            arb_id=arb_id or "DB-FAILURE",
            canonical_id=canonical_id or "",
            severity="critical",
            message=f"ExecutionStore.{op} failed: {exc!s}",
            timestamp=time.time(),
            metadata={"event_type": "db_write_failure", "op": op},
        )
        self._incidents.appendleft(incident)
        for subscriber in list(self._incident_subscribers):
            try:
                subscriber.put_nowait(incident)
            except asyncio.QueueFull:
                logger.debug("Skipping slow incident subscriber on db_failure")

        if self._safety is not None:
            try:
                await self._safety.trip_kill(
                    by="execution_engine:db_failure",
                    reason=f"ExecutionStore.{op} write failed",
                )
            except Exception as sup_exc:
                logger.critical(
                    "SafetySupervisor.trip_kill after db_failure raised: %s",
                    sup_exc,
                )

    async def _record_incident(
        self,
        arb_id: str,
        opp: ArbitrageOpportunity,
        severity: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ExecutionIncident:
        return await self.record_incident(
            arb_id=arb_id,
            canonical_id=opp.canonical_id,
            severity=severity,
            message=message,
            metadata=metadata,
        )

    async def _emit_rejection_incident(
        self,
        opp: ArbitrageOpportunity,
        reason: str,
    ) -> ExecutionIncident:
        """Plan 03-02 (SAFE-02): emit a structured ``order_rejected``
        ExecutionIncident whenever RiskManager.check_trade denies an
        opportunity. Incidents flow through the existing incident
        subscription queue to the dashboard's generic ``incident`` WS
        event — plan 03-07 will add a filtered "Rejected orders" sub-view
        without needing a new event type.
        """
        r = reason.lower()
        platform: Optional[str] = None
        if "per-market" in r:
            rejection_type = "per_market"
        elif "per-platform" in r:
            rejection_type = "per_platform"
            # Reason format: "Per-platform exposure limit exceeded on {platform}"
            if " on " in reason:
                platform = reason.rsplit(" on ", 1)[-1].strip() or None
        elif "total exposure" in r:
            rejection_type = "total_exposure"
        elif "daily" in r and "loss" in r:
            rejection_type = "daily_loss"
        elif "daily" in r and "trade" in r:
            rejection_type = "daily_trades"
        elif "stale" in r:
            rejection_type = "stale_quote"
        elif "confidence" in r:
            rejection_type = "low_confidence"
        elif "edge" in r:
            rejection_type = "thin_edge"
        elif "not ready" in r:
            rejection_type = "not_ready"
        else:
            rejection_type = "unknown"

        metadata: Dict[str, Any] = {
            "event_type": "order_rejected",
            "rejection_type": rejection_type,
            "reason": reason,
            "yes_platform": opp.yes_platform,
            "no_platform": opp.no_platform,
            "canonical_id": opp.canonical_id,
            "suggested_qty": opp.suggested_qty,
        }
        if platform:
            metadata["platform"] = platform

        arb_id = f"REJ-{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"
        return await self._record_incident(
            arb_id,
            opp,
            severity="info",
            message=f"Order rejected: {reason}",
            metadata=metadata,
        )

    async def _maybe_halt_on_daily_loss(self, unwind_pnl: float = 0.0) -> None:
        """Trip the kill switch when cumulative daily loss crosses the limit.

        ``RiskManager.check_trade`` already refuses individual trades when the
        threshold is crossed, but rejecting one opportunity is not the same as
        *halting*: the engine keeps picking the next opportunity off the
        scanner queue and grinding through them. This method closes the gap
        by tripping the SafetySupervisor — which cancels every open order
        and refuses execution until an operator resets — once the limit is
        breached.

        Also books unwind P&L (from one-leg recovery sells) into
        ``RiskManager._daily_pnl`` so the check sees the real cumulative
        loss; without this step the unwind cost lands only on
        ``execution.realized_pnl`` and the daily-loss kill is blind to it.
        """
        if unwind_pnl:
            self.risk._daily_pnl += float(unwind_pnl)
        if self.risk._daily_pnl > self.risk._max_daily_loss:
            return
        safety = self._safety
        if safety is None:
            return
        if getattr(safety, "is_armed", False):
            return
        reason = (
            f"Daily loss limit reached: ${self.risk._daily_pnl:.2f} "
            f"<= ${self.risk._max_daily_loss:.2f}"
        )
        try:
            await safety.trip_kill(by="system:daily_loss", reason=reason)
        except Exception as exc:  # pragma: no cover - defensive
            logger.critical(
                "engine: daily-loss trip_kill failed: %s", exc,
            )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._own_session is None or self._own_session.closed:
            self._own_session = aiohttp.ClientSession()
        return self._own_session

    def _get_poly_clob_client(self):
        if self._poly_clob_client is not None:
            return self._poly_clob_client
        poly_cfg = self.config.polymarket
        if not getattr(poly_cfg, "private_key", None):
            return None
        try:
            from py_clob_client.client import ClobClient

            self._poly_clob_client = ClobClient(
                host=poly_cfg.clob_url,
                key=poly_cfg.private_key,
                chain_id=poly_cfg.chain_id,
                signature_type=poly_cfg.signature_type,
                funder=poly_cfg.funder,
            )
            if hasattr(self._poly_clob_client, "create_or_derive_api_creds"):
                creds = self._poly_clob_client.create_or_derive_api_creds()
                if hasattr(self._poly_clob_client, "set_api_creds"):
                    self._poly_clob_client.set_api_creds(creds)
            logger.info("Polymarket ClobClient initialized (sig_type=%d, funder=%s)",
                        poly_cfg.signature_type,
                        poly_cfg.funder[:8] + "..." if poly_cfg.funder else "none")
            return self._poly_clob_client
        except Exception as exc:
            logger.error("Failed to initialize Polymarket CLOB client: %s", exc)
            return None

    async def polymarket_heartbeat_loop(self):
        """
        Dedicated async task sending heartbeat every 5 seconds to prevent
        Polymarket open order auto-cancellation (per D-04).

        Must only start after ClobClient has L2 auth credentials.
        Server cancels ALL open orders if no heartbeat received within 10s.
        """
        self._heartbeat_running = True
        heartbeat_id = None

        # Wait for ClobClient to be ready
        while self._heartbeat_running:
            client = self._get_poly_clob_client()
            if client is not None:
                break
            logger.debug("Heartbeat waiting for ClobClient initialization...")
            await asyncio.sleep(2)

        if not self._heartbeat_running:
            return

        logger.info("Polymarket heartbeat started (interval=5s)")

        while self._heartbeat_running:
            try:
                response = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: client.post_heartbeat(heartbeat_id)
                )
                if isinstance(response, dict):
                    heartbeat_id = response.get("heartbeat_id", heartbeat_id)
                logger.debug("Heartbeat sent (id=%s)", heartbeat_id)
            except asyncio.CancelledError:
                logger.info("Polymarket heartbeat cancelled")
                break
            except Exception as exc:
                logger.error("Heartbeat failed: %s", exc)
            await asyncio.sleep(5)

        logger.info("Polymarket heartbeat stopped")

    def stop_heartbeat(self):
        """Stop the heartbeat loop."""
        self._heartbeat_running = False

    @property
    def execution_history(self) -> List[ArbExecution]:
        return self._executions

    @property
    def incidents(self) -> List[ExecutionIncident]:
        return list(self._incidents)

    @property
    def manual_positions(self) -> List[ManualPosition]:
        return list(self._manual_positions)

    @property
    def equity_curve(self) -> List[dict]:
        running_total = 0.0
        points = []
        for execution in self._executions[-120:]:
            running_total += execution.realized_pnl
            points.append({"timestamp": execution.timestamp, "equity": round(running_total, 4)})
        return points

    @property
    def stats(self) -> dict:
        simulated = sum(1 for execution in self._executions if execution.status == "simulated")
        manual_statuses = {"manual_pending", "manual_entered", "manual_closed", "manual_cancelled"}
        live = sum(1 for execution in self._executions if execution.status not in {"simulated", *manual_statuses})
        total_pnl = sum(execution.realized_pnl for execution in self._executions)
        return {
            "total_executions": len(self._executions),
            "simulated": simulated,
            "live": live,
            "manual": self._manual_count,
            "incidents": len(self._incidents),
            "recoveries": self._recovery_count,
            "aborted": self._aborted_count,
            "total_pnl": round(total_pnl, 2),
            "dry_run": self.scanner_config.dry_run,
            "audit": self._auditor.stats,
            "naked_leg_count": self._naked_leg_count,
            "naked_leg_exposure_usd": round(self._naked_leg_exposure_usd, 2),
        }

    async def run(self, arb_queue: asyncio.Queue):
        self._running = True
        logger.info("Execution engine started (dry_run=%s)", self.scanner_config.dry_run)

        while self._running:
            try:
                opp = await asyncio.wait_for(arb_queue.get(), timeout=5.0)
                # Clamp qty to fit within per-order hardlock caps to avoid
                # adapter rejections when scanner uses a larger position cap.
                import os as _os
                _phase5_cap = float(_os.getenv("PHASE5_MAX_ORDER_USD", "0") or "0")
                _max_pos_cap = float(_os.getenv("MAX_POSITION_USD", "0") or "0")
                max_pos = _phase5_cap or _max_pos_cap or self.scanner_config.max_position_usd
                # Use pair cost (yes + no) to match auditor's _compute_position_size
                price = float(opp.yes_price or 0.01) + float(opp.no_price or 0.01)
                notional = price * opp.suggested_qty
                if notional > max_pos and max_pos > 0:
                    opp.suggested_qty = max(1, int(max_pos / price))
                    opp.max_profit_usd = round(opp.net_edge * opp.suggested_qty, 4)
                result = await self.execute_opportunity(opp)
                if result:
                    await self.balance_monitor.alert_opportunity(result.opportunity)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Execution error: %s", exc)

        logger.info("Execution engine stopped")

    async def stop(self):
        self._running = False
        if self._own_session and not self._own_session.closed:
            await self._own_session.close()

    async def check_trade_gate(self, opp: ArbitrageOpportunity) -> Tuple[bool, str, Dict[str, Any]]:
        """Public wrapper around ``_check_trade_gate`` so upstream callers
        (e.g. AutoExecutor) can probe the gate's verdict WITHOUT going
        through the full ``execute_opportunity`` path. Used to distinguish
        structural denies (profitability, kill-switch) from transient
        execution failures so retry/cooldown logic can react differently.
        """
        return await self._check_trade_gate(opp)

    async def _check_trade_gate(self, opp: ArbitrageOpportunity) -> Tuple[bool, str, Dict[str, Any]]:
        if self._trade_gate is None:
            return True, "no trade gate configured", {}

        result = self._trade_gate(opp)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, tuple):
            if len(result) == 3:
                allowed, reason, context = result
                return bool(allowed), str(reason), dict(context or {})
            if len(result) == 2:
                allowed, reason = result
                return bool(allowed), str(reason), {}
        return bool(result), "trade gate evaluated", {}

    @staticmethod
    def _merge_note(existing: str, note: str) -> str:
        existing = str(existing or "").strip()
        note = str(note or "").strip()
        if not note:
            return existing
        if not existing:
            return note
        if note in existing:
            return existing
        return f"{existing} | {note}"

    def _update_manual_execution(self, position: ManualPosition, note: str = "") -> Optional[ArbExecution]:
        arb_id = position.position_id.replace("MANUAL-", "", 1)
        status_map = {
            "awaiting-entry": "manual_pending",
            "entered": "manual_entered",
            "closed": "manual_closed",
            "cancelled": "manual_cancelled",
        }
        lifecycle_note = {
            "entered": "Manual leg confirmed by operator",
            "closed": "Manual position closed by operator",
            "cancelled": "Manual position cancelled by operator",
        }.get(position.status)

        for execution in self._executions:
            if execution.arb_id != arb_id:
                continue
            previous_status = execution.status
            execution.status = status_map.get(position.status, execution.status)
            if lifecycle_note and lifecycle_note not in execution.notes:
                execution.notes.append(lifecycle_note)
            if note:
                merged = self._merge_note("", note)
                if merged and merged not in execution.notes:
                    execution.notes.append(merged)
            exposure = execution.opportunity.suggested_qty * (
                execution.opportunity.yes_price + execution.opportunity.no_price
            )
            # Plan 03-02: thread per-platform exposure splits through the
            # manual-position lifecycle so both open_positions and
            # _platform_exposures stay in sync.
            yes_leg_exposure = (
                execution.opportunity.suggested_qty * execution.opportunity.yes_price
            )
            no_leg_exposure = (
                execution.opportunity.suggested_qty * execution.opportunity.no_price
            )
            if position.status == "entered" and previous_status != "manual_entered":
                self.risk.record_trade(
                    execution.opportunity.canonical_id,
                    exposure,
                    0.0,
                    yes_platform=execution.opportunity.yes_platform,
                    no_platform=execution.opportunity.no_platform,
                    yes_exposure=yes_leg_exposure,
                    no_exposure=no_leg_exposure,
                )
            if position.status == "closed" and execution.realized_pnl == 0.0:
                execution.realized_pnl = round(
                    execution.opportunity.net_edge * execution.opportunity.suggested_qty,
                    4,
                )
            if position.status == "closed":
                self.risk.release_trade(
                    execution.opportunity.canonical_id,
                    exposure,
                    execution.realized_pnl,
                    yes_platform=execution.opportunity.yes_platform,
                    no_platform=execution.opportunity.no_platform,
                    yes_exposure=yes_leg_exposure,
                    no_exposure=no_leg_exposure,
                )
            elif position.status == "cancelled":
                self.risk.release_trade(
                    execution.opportunity.canonical_id,
                    exposure,
                    0.0,
                    yes_platform=execution.opportunity.yes_platform,
                    no_platform=execution.opportunity.no_platform,
                    yes_exposure=yes_leg_exposure,
                    no_exposure=no_leg_exposure,
                )
            return execution
        return None
