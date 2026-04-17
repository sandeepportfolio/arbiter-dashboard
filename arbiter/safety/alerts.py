"""SafetyAlertTemplates — HTML message composition for kill-switch alerts.

These are pure-function templates; message egress is delegated to the shared
``TelegramNotifier`` (arbiter/monitor/balance.py:28) — we never construct a
new notifier. Callers wrap ``notifier.send(template.kill_armed(...))`` in a
try/except so Telegram outages never abort a kill-switch trip.
"""
from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger("arbiter.safety.alerts")


class SafetyAlertTemplates:
    """Static HTML templates for Telegram alerts."""

    @staticmethod
    def kill_armed(
        by: str, reason: str, cancelled_counts: Dict[str, int]
    ) -> str:
        counts = (
            " | ".join(f"{p}:{n}" for p, n in cancelled_counts.items())
            or "none"
        )
        return (
            "🛑 <b>KILL SWITCH ARMED</b>\n"
            f"By: {by}\n"
            f"Reason: {reason}\n"
            f"Cancelled: {counts}\n"
            "Manual reset required."
        )

    @staticmethod
    def kill_reset(by: str, note: str) -> str:
        return (
            "🟢 <b>Kill switch RESET</b>\n"
            f"By: {by}\n"
            f"Note: {note}"
        )

    @staticmethod
    def one_leg_exposure(
        canonical_id: str,
        filled_platform: str,
        filled_side: str,
        fill_qty: int,
        exposure_usd: float,
        unwind_instruction: str,
    ) -> str:
        # Placeholder — plan 03-03 wires this into the incident pipeline.
        return (
            "🚨 <b>NAKED POSITION</b>\n"
            f"Market: {canonical_id}\n"
            f"Filled: {fill_qty} {filled_side.upper()} on {filled_platform.upper()}\n"
            f"Exposure: ${exposure_usd:.2f}\n"
            f"Unwind: {unwind_instruction}"
        )
