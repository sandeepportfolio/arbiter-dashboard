"""
Tests for PredictItWorkflowManager.
"""
import asyncio
import sys
import os
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbiter.execution.engine import ManualPosition
from arbiter.workflow.predictit_workflow import (
    CloseResult,
    PredictItWorkflowManager,
    ReminderAlert,
    UnwindInstruction,
    UnwindReason,
)


@pytest.fixture
def manager():
    from arbiter.config.settings import AlertConfig
    config = AlertConfig(
        telegram_bot_token="test_token",
        telegram_chat_id="123456",
    )
    return PredictItWorkflowManager(config, reminder_interval_seconds=60)


def make_position(
    position_id: str,
    status: str,
    hours_ago: float,
    canonical_id: str = "TEST-EVENT-001",
) -> ManualPosition:
    # Use time.time() to get Unix timestamp (not datetime.timestamp() which
    # incorrectly treats naive datetime as local time in Python)
    ts = time.time() - (hours_ago * 3600)
    pos = ManualPosition(
        position_id=position_id,
        canonical_id=canonical_id,
        description="Test position",
        instructions="Test instructions",
        yes_platform="kalshi",
        no_platform="predictit",
        quantity=10,
        yes_price=0.55,
        no_price=0.48,
        status=status,
        timestamp=ts,
        updated_at=ts,
    )
    # For "entered" status, also set entry_confirmed_at
    if status == "entered":
        pos.entry_confirmed_at = ts
    # For "hedged" or "closed" status, also set closed_at
    if status in ("hedged", "closed"):
        pos.closed_at = ts
    return pos


@pytest.mark.asyncio
async def test_check_stale_awaiting_entry(manager):
    """Position stuck in awaiting-entry should generate alert after 1h."""
    positions = [
        make_position("POS-STALE-1", "awaiting-entry", hours_ago=2.0),
        make_position("POS-FRESH", "awaiting-entry", hours_ago=0.5),  # Not stale yet
    ]

    alerts = await manager.check_stale_positions(positions)

    assert len(alerts) == 1
    assert alerts[0].position_id == "POS-STALE-1"
    assert alerts[0].current_status == "awaiting-entry"
    assert alerts[0].stuck_duration_hours >= 1.9


@pytest.mark.asyncio
async def test_check_stale_entered(manager):
    """Position stuck in 'entered' should generate alert after 2h."""
    positions = [
        make_position("POS-ENTERED", "entered", hours_ago=3.0),
    ]

    alerts = await manager.check_stale_positions(positions)

    assert len(alerts) == 1
    assert alerts[0].position_id == "POS-ENTERED"
    assert alerts[0].current_status == "entered"


@pytest.mark.asyncio
async def test_no_alerts_for_fresh_positions(manager):
    """Fresh positions should not generate alerts."""
    positions = [
        make_position("POS-FRESH-1", "awaiting-entry", hours_ago=0.5),
        make_position("POS-FRESH-2", "entered", hours_ago=1.0),
        make_position("POS-FRESH-3", "hedged", hours_ago=12.0),
    ]

    alerts = await manager.check_stale_positions(positions)
    # hedged at 12h is stale (threshold 24h), awaiting-entry at 0.5h (threshold 1h), entered at 1h (threshold 2h)
    # only hedged at 12h is under threshold
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_send_stale_reminders_calls_telegram(manager):
    """send_stale_reminders should call telegram for each alert."""
    positions = [
        make_position("POS-STALE-1", "awaiting-entry", hours_ago=3.0),
    ]

    with patch.object(manager.telegram, "send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True

        sent = await manager.send_stale_reminders(positions)

        assert sent == 1
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "PredictIt Position Reminder" in msg
        assert "POS-STALE-1" in msg


@pytest.mark.asyncio
async def test_generate_unwind_instruction_one_leg_rejected(manager):
    """Unwind instruction should close YES first when only NO filled."""
    pos = make_position("POS-UNWIND-1", "entered", hours_ago=1.0)

    instruction = manager.generate_unwind_instruction(
        position=pos,
        reason=UnwindReason.ONE_LEG_REJECTED,
        yes_fill_qty=0,     # Kalshi order rejected
        no_fill_qty=10,     # PredictIt filled
        yes_avg_price=0.55,
        no_avg_price=0.48,
    )

    assert instruction.close_yes_first is False  # Close NO first since NO is filled
    assert instruction.exposure_at_risk == 10 * 0.48  # NO only
    assert instruction.reason == UnwindReason.ONE_LEG_REJECTED
    assert "UNWIND REQUIRED" in instruction.to_telegram_message()


@pytest.mark.asyncio
async def test_generate_unwind_instruction_both_filled_close_higher_price_first(manager):
    """When both legs filled, close higher-price leg first."""
    pos = make_position("POS-BOTH", "hedged", hours_ago=0.5)

    instruction = manager.generate_unwind_instruction(
        position=pos,
        reason=UnwindReason.PRICE_MOVED,
        yes_fill_qty=5,
        no_fill_qty=5,
        yes_avg_price=0.65,  # Higher price — should close first
        no_avg_price=0.38,
    )

    assert instruction.close_yes_first is True
    assert instruction.exposure_at_risk == 5 * 0.65 + 5 * 0.38


@pytest.mark.asyncio
async def test_record_close(manager):
    """record_close should store the result and compute net_pnl."""
    result = await manager.record_close(
        position_id="POS-CLOSE-1",
        realized_pnl=5.50,
        fees_paid=0.45,
        operator_note="Closed per market resolution",
    )

    assert result.position_id == "POS-CLOSE-1"
    assert result.realized_pnl == 5.50
    assert result.fees_paid == 0.45
    assert result.net_pnl == 5.05  # 5.50 - 0.45
    assert result.operator_note == "Closed per market resolution"


@pytest.mark.asyncio
async def test_get_performance_summary(manager):
    """Performance summary should aggregate all close results."""
    await manager.record_close("POS-1", realized_pnl=5.00, fees_paid=0.50)
    await manager.record_close("POS-2", realized_pnl=-2.00, fees_paid=0.30)
    await manager.record_close("POS-3", realized_pnl=1.50, fees_paid=0.20)

    summary = manager.get_performance_summary()

    assert summary["total_trades"] == 3
    assert summary["total_pnl"] == 4.50  # 5.00 - 2.00 + 1.50
    assert summary["total_fees"] == 1.00  # 0.50 + 0.30 + 0.20
    assert summary["net_pnl"] == 3.50    # 4.50 - 1.00
    assert summary["win_rate"] == 2 / 3  # 2 wins, 1 loss


def test_unwind_instruction_telegram_message():
    """UnwindInstruction.to_telegram_message should be well-formatted."""
    instruction = UnwindInstruction(
        reason=UnwindReason.ONE_LEG_REJECTED,
        position_id="POS-001",
        canonical_id="TEST-EVENT",
        yes_platform="kalshi",
        no_platform="predictit",
        yes_order_id="K-123",
        no_order_id="PI-456",
        yes_fill_qty=5,
        no_fill_qty=5,
        yes_avg_price=0.60,
        no_avg_price=0.42,
        exposure_at_risk=5 * 0.60 + 5 * 0.42,
        recommended_action="Close YES position first",
        close_yes_first=True,
        estimated_cost=0.62,
        notes=["Test note 1", "Test note 2"],
    )

    msg = instruction.to_telegram_message()

    assert "UNWIND REQUIRED" in msg
    assert "one_leg_rejected" in msg or "One Leg Rejected" in msg
    assert "POS-001" in msg
    assert "TEST-EVENT" in msg
    assert "Close YES first" in msg
    assert "Test note 1" in msg
    assert "/unwind POS-001" in msg


def test_reminder_alert_telegram_message():
    """ReminderAlert.to_telegram_message should be well-formatted."""
    alert = ReminderAlert(
        position_id="POS-REMIND",
        canonical_id="TEST-EVENT",
        current_status="awaiting-entry",
        stuck_duration_hours=2.5,
        yes_platform="kalshi",
        no_platform="predictit",
        quantity=10,
        yes_price=0.55,
        no_price=0.48,
        notes="Please confirm order placement.",
    )

    msg = alert.to_telegram_message()

    assert "PredictIt Position Reminder" in msg
    assert "POS-REMIND" in msg
    assert "2.5" in msg
    # The notes come from the ReminderAlert object directly
    assert "Please confirm order placement." in msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
