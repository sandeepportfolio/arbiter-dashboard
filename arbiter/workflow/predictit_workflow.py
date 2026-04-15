"""
PredictIt Manual Workflow Manager.

Handles the full manual PredictIt-assisted arbitrage lifecycle:
- Monitors stale positions and sends reminder Telegram alerts
- Generates unwind instructions when a leg fails
- Records P&L from operator inputs on close
- Exit monitoring for open positions near resolution
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

from ..config.settings import AlertConfig
from ..execution.engine import ManualPosition
from ..monitor.balance import TelegramNotifier

logger = logging.getLogger("arbiter.workflow")


# How long before a reminder is sent for a stuck position (in hours)
_STALE_ENTRY_HOURS = 1.0     # Still "awaiting-entry" after 1h
_STALE_HEDGE_HOURS = 2.0     # Still "entered" (no hedge) after 2h
_STALE_CLOSE_HOURS = 24.0    # Still "hedged" (not closed) after 24h


class UnwindReason(str, Enum):
    ONE_LEG_REJECTED = "one_leg_rejected"       # One order rejected/cancelled
    ONE_LEG_PARTIAL = "one_leg_partial"        # One leg partially filled
    ONE_LEG_TIMEOUT = "one_leg_timeout"        # One leg timed out
    VENUE_OUTAGE = "venue_outage"               # Venue went down mid-arb
    PRICE_MOVED = "price_moved"                # Price moved beyond slippage tolerance
    BALANCE_LOW = "balance_low"                 # Balance too low to hedge


@dataclass
class UnwindInstruction:
    reason: UnwindReason
    position_id: str
    canonical_id: str
    yes_platform: str
    no_platform: str
    yes_order_id: str
    no_order_id: str
    yes_fill_qty: int
    no_fill_qty: int
    yes_avg_price: float
    no_avg_price: float
    exposure_at_risk: float
    recommended_action: str
    close_yes_first: bool  # True = close YES position first
    estimated_cost: float    # Estimated cost to unwind in $
    notes: List[str] = field(default_factory=list)

    def to_telegram_message(self) -> str:
        """Format unwind instructions as a clear Telegram message."""
        lines = [
            "🚨 <b>UNWIND REQUIRED</b>",
            "",
            f"<b>Reason:</b> {self.reason.value.replace('_', ' ').title()}",
            f"<b>Position:</b> {self.position_id}",
            f"<b>Market:</b> {self.canonical_id}",
            "",
            "📊 <b>Current State</b>",
            f"  YES ({self.yes_platform}): {self.yes_fill_qty} contracts @ ${self.yes_avg_price:.4f}",
            f"  NO  ({self.no_platform}): {self.no_fill_qty} contracts @ ${self.no_avg_price:.4f}",
            f"  Exposure at risk: <b>${self.exposure_at_risk:.2f}</b>",
            "",
            "🛑 <b>Recommended Unwind</b>",
            f"  {'Close YES first' if self.close_yes_first else 'Close NO first'} — to minimize fees",
            f"  Est. unwind cost: <b>${self.estimated_cost:.2f}</b>",
        ]
        if self.notes:
            lines.append("")
            lines.append("📝 <b>Notes</b>")
            for note in self.notes:
                lines.append(f"  • {note}")
        lines.extend([
            "",
            "Reply with: /unwind " + self.position_id,
        ])
        return "\n".join(lines)


@dataclass
class ReminderAlert:
    position_id: str
    canonical_id: str
    current_status: str
    stuck_duration_hours: float
    yes_platform: str
    no_platform: str
    quantity: int
    yes_price: float
    no_price: float
    notes: str

    def to_telegram_message(self) -> str:
        status_emoji = {
            "awaiting-entry": "⏳",
            "entered": "📋",
            "hedged": "🛡️",
        }.get(self.current_status, "❓")

        lines = [
            f"{status_emoji} <b>PredictIt Position Reminder</b>",
            f"<b>Position:</b> {self.position_id}",
            f"<b>Market:</b> {self.canonical_id}",
            f"<b>Status:</b> {self.current_status} (stuck {self.stuck_duration_hours:.1f}h)",
            "",
            f"YES ({self.yes_platform}): {self.quantity} contracts @ ${self.yes_price:.4f}",
            f"NO  ({self.no_platform}): {self.quantity} contracts @ ${self.no_price:.4f}",
            "",
            self.notes,
        ]
        return "\n".join(lines)


@dataclass
class CloseResult:
    position_id: str
    realized_pnl: float
    fees_paid: float
    net_pnl: float
    closed_at: datetime
    operator_note: str


class PredictItWorkflowManager:
    """
    Manages the PredictIt-assisted arbitrage lifecycle:
    - Sends Telegram reminders for stale positions
    - Generates unwind instructions when needed
    - Records operator-confirmed P&L on close
    - Monitors hedged positions for resolution
    """

    def __init__(
        self,
        alert_config: AlertConfig,
        reminder_interval_seconds: float = 1800.0,  # 30 min default
    ):
        self.alert_config = alert_config
        self.telegram = TelegramNotifier(
            bot_token=alert_config.telegram_bot_token,
            chat_id=alert_config.telegram_chat_id,
        )
        self.reminder_interval = reminder_interval_seconds
        self._running = False
        self._closed_results: Deque[CloseResult] = Deque(maxlen=500)

    async def start(self) -> None:
        self._running = True
        logger.info("PredictItWorkflowManager: started")

    async def stop(self) -> None:
        self._running = False
        logger.info("PredictItWorkflowManager: stopped")

    # ─── Stale Position Monitoring ─────────────────────────────────────────

    async def check_stale_positions(
        self,
        positions: List[ManualPosition],
    ) -> List[ReminderAlert]:
        """
        Check a list of manual positions for stale states.
        Returns a list of ReminderAlert objects to send.
        """
        alerts = []
        now = time.time()

        for pos in positions:
            if pos.status == "awaiting-entry":
                stuck_s = now - pos.timestamp
                if stuck_s > _STALE_ENTRY_HOURS * 3600:
                    alerts.append(ReminderAlert(
                        position_id=pos.position_id,
                        canonical_id=pos.canonical_id,
                        current_status=pos.status,
                        stuck_duration_hours=stuck_s / 3600,
                        yes_platform=pos.yes_platform,
                        no_platform=pos.no_platform,
                        quantity=pos.quantity,
                        yes_price=pos.yes_price,
                        no_price=pos.no_price,
                        notes=(
                            "⏳ Entry not confirmed yet. Did you place the order? "
                            "Reply with /entered " + pos.position_id + " when done."
                        ),
                    ))

            elif pos.status == "entered":
                stuck_s = now - pos.entry_confirmed_at
                if stuck_s > _STALE_HEDGE_HOURS * 3600:
                    alerts.append(ReminderAlert(
                        position_id=pos.position_id,
                        canonical_id=pos.canonical_id,
                        current_status=pos.status,
                        stuck_duration_hours=stuck_s / 3600,
                        yes_platform=pos.yes_platform,
                        no_platform=pos.no_platform,
                        quantity=pos.quantity,
                        yes_price=pos.yes_price,
                        no_price=pos.no_price,
                        notes=(
                            "🛡️ Hedge not placed yet. Please place NO hedge order "
                            "and reply with /hedged " + pos.position_id
                        ),
                    ))

            elif pos.status == "hedged":
                stuck_s = now - pos.closed_at if pos.closed_at else now
                if pos.closed_at == 0.0:
                    stuck_s = now - pos.updated_at
                if stuck_s > _STALE_CLOSE_HOURS * 3600:
                    alerts.append(ReminderAlert(
                        position_id=pos.position_id,
                        canonical_id=pos.canonical_id,
                        current_status=pos.status,
                        stuck_duration_hours=stuck_s / 3600,
                        yes_platform=pos.yes_platform,
                        no_platform=pos.no_platform,
                        quantity=pos.quantity,
                        yes_price=pos.yes_price,
                        no_price=pos.no_price,
                        notes=(
                            "⚠️ Hedged but not closed. Market may be near resolution. "
                            "Reply with /close " + pos.position_id + " when settled."
                        ),
                    ))

        return alerts

    async def send_stale_reminders(
        self,
        positions: List[ManualPosition],
    ) -> int:
        """Check stale positions and send Telegram reminders. Returns count sent."""
        alerts = await self.check_stale_positions(positions)
        sent = 0
        for alert in alerts:
            success = await self.telegram.send(alert.to_telegram_message())
            if success:
                sent += 1
                logger.info(
                    f"[Workflow] Sent stale reminder for {alert.position_id} "
                    f"(status={alert.current_status}, stuck={alert.stuck_duration_hours:.1f}h)"
                )
        return sent

    # ─── Unwind Instructions ───────────────────────────────────────────────

    def generate_unwind_instruction(
        self,
        position: ManualPosition,
        reason: UnwindReason,
        yes_fill_qty: int,
        no_fill_qty: int,
        yes_avg_price: float,
        no_avg_price: float,
        yes_order_id: str = "",
        no_order_id: str = "",
        price_after: Tuple[float, float] = None,
        notes: List[str] = None,
    ) -> UnwindInstruction:
        """
        Generate unwind instructions for a position that needs to be closed.
        Called when one leg fails and recovery is needed.
        """
        has_yes_fill = yes_fill_qty > 0
        has_no_fill = no_fill_qty > 0

        # Filled leg should be closed first to minimize further exposure
        close_yes_first = False
        if has_yes_fill and not has_no_fill:
            close_yes_first = True
        elif has_no_fill and not has_yes_fill:
            close_yes_first = False
        else:
            close_yes_first = yes_avg_price >= no_avg_price

        # Estimate unwind cost
        est_yes_close = yes_fill_qty * yes_avg_price * 0.02 if has_yes_fill else 0.0
        est_no_close = (
            no_fill_qty * max(no_avg_price - yes_avg_price, 0) * 0.10
            if has_no_fill
            else 0.0
        )
        estimated_cost = est_yes_close + est_no_close

        action_map = {
            UnwindReason.ONE_LEG_REJECTED: "Place missing leg immediately to lock in profit",
            UnwindReason.ONE_LEG_PARTIAL: "Complete partial fill or cancel and restart",
            UnwindReason.ONE_LEG_TIMEOUT: "Re-check prices — market may have moved",
            UnwindReason.VENUE_OUTAGE: "Wait for venue to recover, then complete",
            UnwindReason.PRICE_MOVED: "Recalculate — original arb may be gone",
            UnwindReason.BALANCE_LOW: "Top up account before continuing",
        }

        note_list = list(notes) if notes else []
        if price_after:
            note_list.append(
                f"Price moved: was ${yes_avg_price:.4f}/${no_avg_price:.4f}, "
                f"now ${price_after[0]:.4f}/${price_after[1]:.4f}"
            )

        exposure = yes_fill_qty * yes_avg_price + no_fill_qty * no_avg_price

        return UnwindInstruction(
            reason=reason,
            position_id=position.position_id,
            canonical_id=position.canonical_id,
            yes_platform=position.yes_platform,
            no_platform=position.no_platform,
            yes_order_id=yes_order_id,
            no_order_id=no_order_id,
            yes_fill_qty=yes_fill_qty,
            no_fill_qty=no_fill_qty,
            yes_avg_price=yes_avg_price,
            no_avg_price=no_avg_price,
            exposure_at_risk=exposure,
            recommended_action=action_map.get(reason, "Assess and close"),
            close_yes_first=close_yes_first,
            estimated_cost=estimated_cost,
            notes=note_list,
        )

    async def send_unwind_alert(
        self,
        instruction: UnwindInstruction,
    ) -> bool:
        """Send unwind alert via Telegram and return success status."""
        logger.warning(
            f"[Workflow] UNWIND REQUIRED: {instruction.position_id} "
            f"reason={instruction.reason.value}"
        )
        return await self.telegram.send(instruction.to_telegram_message())

    # ─── P&L Recording ───────────────────────────────────────────────────

    async def record_close(
        self,
        position_id: str,
        realized_pnl: float,
        fees_paid: float,
        operator_note: str = "",
    ) -> CloseResult:
        """
        Record the confirmed close of a PredictIt-assisted position.
        Call this when the operator confirms the position is closed with /close.
        """
        result = CloseResult(
            position_id=position_id,
            realized_pnl=realized_pnl,
            fees_paid=fees_paid,
            net_pnl=realized_pnl - fees_paid,
            closed_at=datetime.utcnow(),
            operator_note=operator_note,
        )
        self._closed_results.appendleft(result)
        logger.info(
            f"[Workflow] Close recorded: {position_id} "
            f"pnl=${result.realized_pnl:.2f} fees=${result.fees_paid:.2f} "
            f"net=${result.net_pnl:.2f}"
        )
        return result

    def get_recent_closes(self, limit: int = 20) -> List[CloseResult]:
        """Return recent close results."""
        return list(self._closed_results)[:limit]

    def get_performance_summary(self) -> Dict[str, float]:
        """Aggregate P&L stats from closed positions."""
        if not self._closed_results:
            return {
                "total_trades": 0,
                "total_pnl": 0.0,
                "total_fees": 0.0,
                "net_pnl": 0.0,
                "win_rate": 0.0,
            }

        trades = list(self._closed_results)
        wins = sum(1 for t in trades if t.net_pnl > 0)
        return {
            "total_trades": len(trades),
            "total_pnl": sum(t.realized_pnl for t in trades),
            "total_fees": sum(t.fees_paid for t in trades),
            "net_pnl": sum(t.net_pnl for t in trades),
            "win_rate": wins / len(trades) if trades else 0.0,
        }
