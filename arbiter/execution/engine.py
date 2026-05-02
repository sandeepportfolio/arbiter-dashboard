"""
Execution engine with re-quote checks, concurrent legs, and recovery hooks.
"""
from __future__ import annotations

import asyncio
import json
import logging
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
    timestamp: float = 0.0
    notes: List[str] = field(default_factory=list)
    # Markdown post-mortem populated by ``_build_inline_analysis`` right before
    # the audit write. Empty until the trade reaches a recordable state.
    analysis_md: str = ""

    def to_dict(self) -> dict:
        return {
            "arb_id": self.arb_id,
            "opportunity": self.opportunity.to_dict(),
            "leg_yes": self.leg_yes.to_dict(),
            "leg_no": self.leg_no.to_dict(),
            "status": self.status,
            "realized_pnl": round(self.realized_pnl, 4),
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
        self._max_daily_trades: int = 100
        self._max_daily_loss: float = -50.0
        self._max_total_exposure: float = 500.0

    def check_trade(self, opp: ArbitrageOpportunity) -> Tuple[bool, str]:
        if opp.status not in {"tradable", "manual"}:
            return False, f"Opportunity not ready: {opp.status}"
        if opp.confidence < self.config.confidence_threshold and not opp.requires_manual:
            return False, f"Low confidence: {opp.confidence:.2f}"
        if opp.net_edge_cents < self.config.min_edge_cents:
            return False, f"Edge too thin: {opp.net_edge_cents:.2f}¢"
        if opp.quote_age_seconds > self.config.max_quote_age_seconds:
            return False, f"Stale quote: {opp.quote_age_seconds:.2f}s"
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

    async def execute_opportunity(self, opp: ArbitrageOpportunity) -> Optional[ArbExecution]:
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
                        metadata={"gate": gate_context},
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
        await self.balance_monitor.notifier.send(
            f"<b>ARBITER manual trade</b>\n\n{instructions}",
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
                    logger.warning("ExecutionStore insert_incident (resolve) failed: %s", exc)
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

        yes_price = current_yes.yes_price
        no_price = current_no.no_price
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
        if net_edge_cents < self.scanner_config.min_edge_cents:
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

        # Insert a placeholder execution_arbs row so the FK from
        # execution_orders.arb_id is satisfied while legs are still in flight.
        # The later record_arb call will upsert the final status. Without
        # this the per-leg upsert_order calls fired during execution fail
        # with a FK violation and mid-flight order state is lost.
        if self.store is not None:
            try:
                await self.store.record_arb_stub(
                    arb_id,
                    opp.canonical_id,
                    opportunity=opp,
                    net_edge=getattr(opp, "net_edge", None),
                )
            except Exception as exc:
                logger.warning("ExecutionStore record_arb_stub failed: %s", exc)

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
        # risking any capital. Require at least 0.5¢ net edge after fees.
        MIN_NET_EDGE_CENTS = 0.5
        total_cost = opp.yes_price + opp.no_price
        gross_edge = 1.0 - total_cost
        qty = max(1, int(opp.suggested_qty or 1))
        yes_fee = compute_fee(opp.yes_platform, opp.yes_price, qty, opp.yes_fee_rate)
        no_fee = compute_fee(opp.no_platform, opp.no_price, qty, opp.no_fee_rate)
        # Per-contract fees
        yes_fee_per = yes_fee / qty if qty > 0 else 0.0
        no_fee_per = no_fee / qty if qty > 0 else 0.0
        net_edge_after_fees = gross_edge - yes_fee_per - no_fee_per
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
            # Determine primary/secondary: Kalshi (FOK) goes first because
            # it gives an immediate fill-or-kill result.
            if opp.yes_platform == "kalshi":
                primary_side, secondary_side = "yes", "no"
            elif opp.no_platform == "kalshi":
                primary_side, secondary_side = "no", "yes"
            else:
                # Neither is Kalshi — default to YES first
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

            # Step 1: Execute the primary leg (Kalshi FOK)
            primary_leg = await self._place_order_for_leg(
                arb_id, primary_platform, primary_market,
                opp.canonical_id, primary_side, primary_fok_price, effective_qty,
            )

            # Step 2: Only proceed if primary leg FILLED
            if primary_leg.status != OrderStatus.FILLED:
                # Primary didn't fill (FOK rejected) — zero exposure, clean exit
                logger.info(
                    "  ✗ PRIMARY %s did not fill (status=%s) — $0.00 exposure, skipping secondary",
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
                # Primary filled — now execute secondary
                logger.info(
                    "  ✓ PRIMARY %s FILLED: %d contracts @ $%.2f = $%.2f spent on %s → proceeding to %s",
                    primary_side.upper(), primary_leg.fill_qty, primary_leg.fill_price,
                    primary_leg.fill_qty * primary_leg.fill_price,
                    primary_platform, secondary_side.upper(),
                )

                # Inter-leg delay — give the secondary venue's order book a
                # chance to refresh after the cross-venue print from the
                # primary leg. Default 500ms; tune via INTER_LEG_DELAY_MS.
                if self._inter_leg_delay_ms > 0:
                    await asyncio.sleep(self._inter_leg_delay_ms / 1000.0)

                # Walk the secondary book and price the IOC at the most
                # generous limit we can afford while still preserving a 1¢
                # floor of edge.  Using the walked price exactly was the
                # source of every soft-naked-leg event observed in production
                # — by the time the order arrived at the secondary venue the
                # top level had moved by a tick or two and the FOK / IOC
                # killed against a thinner book than we'd seen.  Now we
                # always pay UP TO ``max_affordable_secondary`` (the price at
                # which our edge collapses) and the venue fills us at limit-
                # or-better, so a 1-2¢ shift between detection and submit
                # gets absorbed by the slippage budget instead of leaving us
                # with a one-leg position.
                secondary_fok_price = secondary_price
                secondary_adapter = self.adapters.get(secondary_platform)
                walked_exec_price = None
                if secondary_adapter is not None and hasattr(
                    secondary_adapter, "best_executable_price",
                ):
                    s_fillable, s_exec = await secondary_adapter.best_executable_price(
                        secondary_market, secondary_side, effective_qty,
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

                # IOC fills at limit-or-better, so the optimal limit is
                # ``max_affordable_secondary`` — we never pay more than the
                # book actually shows but we can absorb a 1-3¢ shift between
                # walk and submit without leaving a naked leg.  Floor at the
                # walked price so a stale walk (e.g., max_affordable lower
                # than the visible-book executable price) doesn't underbid
                # the actual market.
                buffered_limit = (
                    max_affordable_secondary
                    if max_affordable_secondary > secondary_fok_price
                    else secondary_fok_price
                )

                logger.info(
                    "  Secondary IOC limit: walked=%.4f buffered=%.4f "
                    "max_affordable=%.4f primary_fill=%.4f",
                    secondary_fok_price, buffered_limit,
                    max_affordable_secondary, primary_fill_price,
                )

                # Recompute the actual post-walk net edge for visibility.
                if walked_exec_price is not None and walked_exec_price > secondary_price:
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

                secondary_leg = await self._place_order_for_leg(
                    arb_id, secondary_platform, secondary_market,
                    opp.canonical_id, secondary_side, buffered_limit, effective_qty,
                    use_ioc=True,
                )

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
                    try:
                        await self._record_incident(
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
                                "secondary_platform": secondary_platform,
                                "secondary_side": secondary_side,
                                "secondary_status": secondary_leg.status.value,
                                "secondary_filled_qty": secondary_leg.fill_qty,
                                "secondary_unfilled_qty": secondary_unfilled_qty,
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

            # Assign to leg_yes / leg_no based on which side was primary
            if primary_side == "yes":
                leg_yes, leg_no = primary_leg, secondary_leg
            else:
                leg_yes, leg_no = secondary_leg, primary_leg

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
        if leg_yes.status in terminal_failed or leg_no.status in terminal_failed:
            if leg_yes.status in surviving_statuses or leg_no.status in surviving_statuses:
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
        if needs_recovery:
            recovery_notes, unwind_pnl = await self._recover_one_leg_risk(
                arb_id, opp, leg_yes, leg_no,
            )
            notes.extend(recovery_notes)
            # Book the realized loss from the unwind into the arb's P&L so
            # reconciliation drift no longer fires for the unrecorded
            # auto-unwind cost. This is the only place ArbExecution.realized_pnl
            # is mutated post-construction; do it BEFORE record_arb so the
            # persisted row reflects the true realized P&L.
            if unwind_pnl != 0.0:
                execution.realized_pnl = float(execution.realized_pnl) + unwind_pnl
                logger.info(
                    "ARB %s realized_pnl updated by unwind: $%.4f (total now $%.4f)",
                    arb_id, unwind_pnl, execution.realized_pnl,
                )

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
                logger.warning("ExecutionStore record_arb failed: %s", exc)
        return execution

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
    ) -> Order:
        """Dispatch a leg through self.adapters[platform], wrapped in asyncio.wait_for (EXEC-05).

        On local timeout, best-effort cancel through the same adapter.
        Every state transition is persisted via self.store.upsert_order (EXEC-02 / D-16).

        ``use_ioc=True`` selects ``place_ioc`` (immediate-or-cancel) instead
        of ``place_fok``.  Used by the secondary leg of a cross-venue arb so
        a stale book on the secondary doesn't trigger an FOK reject and
        leave the primary naked — IOC accepts a partial fill and the engine
        unwinds the unfilled excess on the primary.
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
                )
            order = partial

        # EXEC-02 / D-16: persist every state transition. Store is optional
        # (dev mode without Postgres) — failures here MUST NOT break execution.
        if self.store is not None:
            try:
                client_order_id = self._derive_client_order_id(order)
                await self.store.upsert_order(
                    order, arb_id=arb_id, client_order_id=client_order_id,
                )
            except Exception as exc:
                logger.warning("ExecutionStore upsert_order failed: %s", exc)
        return order

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

        yes_filled = leg_yes.status == OrderStatus.FILLED
        no_filled = leg_no.status == OrderStatus.FILLED

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
        unhedged_leg = None
        unhedged_qty = 0
        unhedged_paired_price = 0.0  # the secondary's fill price at the time, for PnL accounting
        if yes_filled ^ no_filled:
            unhedged_leg = leg_yes if yes_filled else leg_no
            unhedged_qty = int(unhedged_leg.fill_qty or 0)
            unhedged_paired_price = 0.0
        elif yes_filled and no_filled:
            yes_qty = int(leg_yes.fill_qty or 0)
            no_qty = int(leg_no.fill_qty or 0)
            if yes_qty != no_qty:
                # Whichever side over-filled is the one that needs unwinding.
                if yes_qty > no_qty:
                    unhedged_leg = leg_yes
                    unhedged_qty = yes_qty - no_qty
                    unhedged_paired_price = float(leg_no.fill_price)
                else:
                    unhedged_leg = leg_no
                    unhedged_qty = no_qty - yes_qty
                    unhedged_paired_price = float(leg_yes.fill_price)
                logger.info(
                    "  ⚖ Secondary IOC partial-fill detected: "
                    "yes=%d no=%d → unwinding %d %s on %s",
                    yes_qty, no_qty, unhedged_qty,
                    unhedged_leg.side.upper(), unhedged_leg.platform,
                )

        if unhedged_leg is not None and unhedged_qty > 0:
            filled_leg = unhedged_leg
            adapter = self.adapters.get(filled_leg.platform)
            if adapter is not None and hasattr(adapter, "place_unwind_sell"):
                unwind_target = unhedged_qty
                if unwind_target > 0:
                    try:
                        unwind_order = await adapter.place_unwind_sell(
                            f"{arb_id}-UNWIND",
                            filled_leg.market_id,
                            filled_leg.canonical_id,
                            filled_leg.side,
                            unwind_target,
                        )
                    except Exception as exc:
                        logger.error(
                            "auto_unwind.exception platform=%s side=%s err=%s",
                            filled_leg.platform, filled_leg.side, exc,
                        )
                        notes.append(f"unwind-{filled_leg.side}:exception")
                    else:
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
                logger.warning("ExecutionStore cancel-upsert failed: %s", exc)
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
        # EXEC-02 / D-16: persist the incident to Postgres if a store is wired.
        if self.store is not None:
            try:
                await self.store.insert_incident(incident)
            except Exception as exc:
                logger.warning("ExecutionStore insert_incident failed: %s", exc)
        return incident

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
