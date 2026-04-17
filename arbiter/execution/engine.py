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
        self._aborted_count = 0
        self._manual_count = 0
        self._recovery_count = 0
        self._auditor = MathAuditor(
            max_position_usd=config.scanner.max_position_usd,
            predictit_cap=config.scanner.predictit_cap,
        )
        self._trade_gate = None
        # Plan 02-06 integration: adapters + store + per-leg timeout
        self.adapters: Dict[str, "PlatformAdapter"] = adapters or {}
        self.store: Optional["ExecutionStore"] = store
        self.execution_timeout_s: float = execution_timeout_s
        # Plan 03-01: late-injected reference to SafetySupervisor for the
        # one-leg hook (plan 03-03) and shutdown trip (plan 03-05).
        self._safety: Optional["SafetySupervisor"] = safety

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
        last_seen = self._recent_signatures.get(signature, 0.0)
        if time.time() - last_seen < 30.0:
            return None
        self._recent_signatures[signature] = time.time()

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
        severity = "critical" if "critical" in severities else "warning"
        await self.record_incident(
            arb_id=execution.arb_id,
            canonical_id=execution.opportunity.canonical_id,
            severity=severity,
            message="Shadow execution audit flagged the completed trade state",
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
        yes_task = asyncio.create_task(
            self._place_order_for_leg(arb_id, opp.yes_platform, opp.yes_market_id, opp.canonical_id, "yes", opp.yes_price, opp.suggested_qty)
        )
        no_task = asyncio.create_task(
            self._place_order_for_leg(arb_id, opp.no_platform, opp.no_market_id, opp.canonical_id, "no", opp.no_price, opp.suggested_qty)
        )
        leg_yes, leg_no = await asyncio.gather(yes_task, no_task)

        status = "submitted"
        notes: List[str] = []
        if leg_yes.status in {OrderStatus.FAILED, OrderStatus.CANCELLED, OrderStatus.ABORTED} or leg_no.status in {
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
            OrderStatus.ABORTED,
        }:
            if leg_yes.status in {OrderStatus.FILLED, OrderStatus.PARTIAL, OrderStatus.SUBMITTED} or leg_no.status in {
                OrderStatus.FILLED,
                OrderStatus.PARTIAL,
                OrderStatus.SUBMITTED,
            }:
                status = "recovering"
                notes.extend(await self._recover_one_leg_risk(arb_id, opp, leg_yes, leg_no))
            else:
                status = "failed"
        elif leg_yes.status == OrderStatus.PARTIAL or leg_no.status == OrderStatus.PARTIAL:
            status = "recovering"
            notes.extend(await self._recover_one_leg_risk(arb_id, opp, leg_yes, leg_no))
        elif leg_yes.status == OrderStatus.FILLED and leg_no.status == OrderStatus.FILLED:
            status = "filled"

        execution = ArbExecution(
            arb_id=arb_id,
            opportunity=opp,
            leg_yes=leg_yes,
            leg_no=leg_no,
            status=status,
            realized_pnl=opp.net_edge * min(max(leg_yes.fill_qty, 0), max(leg_no.fill_qty, 0)) if status in {"filled", "submitted"} else 0.0,
            timestamp=now,
            notes=notes,
        )
        self._executions.append(execution)

        if status in {"submitted", "filled"}:
            if status == "filled":
                self.risk.record_trade(
                    opp.canonical_id,
                    opp.suggested_qty * (opp.yes_price + opp.no_price),
                    execution.realized_pnl,
                    yes_platform=opp.yes_platform,
                    no_platform=opp.no_platform,
                    yes_exposure=opp.suggested_qty * opp.yes_price,
                    no_exposure=opp.suggested_qty * opp.no_price,
                )

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
    ) -> Order:
        """Dispatch a leg through self.adapters[platform], wrapped in asyncio.wait_for (EXEC-05).

        On local timeout, best-effort cancel through the same adapter.
        Every state transition is persisted via self.store.upsert_order (EXEC-02 / D-16).
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

        try:
            order = await asyncio.wait_for(
                adapter.place_fok(arb_id, market_id, canonical_id, side, price, qty),
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

    async def _recover_one_leg_risk(self, arb_id: str, opp: ArbitrageOpportunity, leg_yes: Order, leg_no: Order) -> List[str]:
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
        """
        self._recovery_count += 1
        notes: List[str] = []

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
                cancelled = await self._cancel_order(leg)
                notes.append(f"cancel-{leg.side}:{'ok' if cancelled else 'failed'}")
        return notes

    async def _cancel_order(self, order: Order) -> bool:
        """Dispatch cancel through self.adapters[order.platform]. Platform-agnostic."""
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
            try:
                await self.store.upsert_order(
                    order, arb_id=self._derive_arb_id_from_order(order),
                )
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
        if not self.config.polymarket.private_key:
            return None
        try:
            from py_clob_client.client import ClobClient

            self._poly_clob_client = ClobClient(
                host=self.config.polymarket.clob_url,
                key=self.config.polymarket.private_key,
                chain_id=self.config.polymarket.chain_id,
                signature_type=self.config.polymarket.signature_type,
                funder=self.config.polymarket.funder,
            )
            if hasattr(self._poly_clob_client, "create_or_derive_api_creds"):
                creds = self._poly_clob_client.create_or_derive_api_creds()
                if hasattr(self._poly_clob_client, "set_api_creds"):
                    self._poly_clob_client.set_api_creds(creds)
            logger.info("Polymarket ClobClient initialized (sig_type=%d, funder=%s)",
                        self.config.polymarket.signature_type,
                        self.config.polymarket.funder[:8] + "..." if self.config.polymarket.funder else "none")
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
        }

    async def run(self, arb_queue: asyncio.Queue):
        self._running = True
        logger.info("Execution engine started (dry_run=%s)", self.scanner_config.dry_run)

        while self._running:
            try:
                opp = await asyncio.wait_for(arb_queue.get(), timeout=5.0)
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
