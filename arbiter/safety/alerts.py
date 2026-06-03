"""SafetyAlertTemplates — HTML message composition for kill-switch alerts.

These are pure-function templates; message egress is delegated to the shared
``TelegramNotifier`` (arbiter/monitor/balance.py:28) — we never construct a
new notifier. Callers wrap ``notifier.send(template.kill_armed(...))`` in a
try/except so Telegram outages never abort a kill-switch trip.
"""
from __future__ import annotations

import logging
from typing import Dict

from ..notifiers.fmt import DIVIDER, h, usd

logger = logging.getLogger("arbiter.safety.alerts")


class SafetyAlertTemplates:
    """Static HTML templates for Telegram alerts."""

    @staticmethod
    def kill_armed(
        by: str, reason: str, cancelled_counts: Dict[str, int]
    ) -> str:
        cancel_lines = []
        for plat, n in (cancelled_counts or {}).items():
            cancel_lines.append(f"  {h(plat.title())}: <code>{n}</code> orders")
        cancel_section = "\n".join(cancel_lines) if cancel_lines else "  <i>none</i>"

        return (
            f"\U0001f6d1 <b>KILL SWITCH ARMED</b>\n"
            f"{DIVIDER}\n"
            f"\U0001f464 By: <code>{h(by)}</code>\n"
            f"\U0001f4dd Reason: {h(reason)}\n"
            f"\n"
            f"\U0001f6ab <b>Cancelled Orders</b>\n"
            f"{cancel_section}\n"
            f"{DIVIDER}\n"
            f"⚠️ <i>Manual reset required via dashboard.</i>"
        )

    @staticmethod
    def kill_reset(by: str, note: str) -> str:
        return (
            f"\U0001f7e2 <b>KILL SWITCH RESET</b>\n"
            f"{DIVIDER}\n"
            f"\U0001f464 By: <code>{h(by)}</code>\n"
            f"\U0001f4dd Note: {h(note or 'operator reset')}\n"
            f"{DIVIDER}\n"
            f"✅ <i>Execution gate re-opened. Trading may resume.</i>"
        )

    @staticmethod
    def kill_restored_from_redis(prior_reason: str) -> str:
        """Sent on startup when ``restore_from_redis`` finds the switch armed.

        The previous arm's Telegram alert is days/weeks in the past — without
        this restart-time ping a SIGTERM-induced auto-arm silently keeps the
        engine muted until an operator notices. See 2026-05-22 audit notes.
        """
        return (
            f"\U0001f6d1 <b>KILL SWITCH RESTORED ON STARTUP</b>\n"
            f"{DIVIDER}\n"
            f"\U0001f504 Previous instance left the switch armed in Redis.\n"
            f"\U0001f4cb Prior reason: <code>{h(prior_reason or 'unknown')}</code>\n"
            f"{DIVIDER}\n"
            f"\U0001f6ab <i>Engine refuses trades until operator resets via dashboard.</i>"
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
        return (
            f"\U0001f6a8 <b>NAKED POSITION</b>\n"
            f"{DIVIDER}\n"
            f"\U0001f4cd Market: <code>{h(canonical_id)}</code>\n"
            f"⚡ Filled: <b>{fill_qty} {h(filled_side.upper())}</b> on <b>{h(filled_platform.upper())}</b>\n"
            f"\U0001f4b8 Exposure: <code>{usd(exposure_usd)}</code>\n"
            f"{DIVIDER}\n"
            f"\U0001f527 <b>Unwind:</b> {h(unwind_instruction)}\n"
            f"⚠️ <i>Check position on venue and close manually if needed.</i>"
        )
