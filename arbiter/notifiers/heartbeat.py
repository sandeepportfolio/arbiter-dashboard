"""Telegram heartbeat notifier — Task 18.

Sends a status digest to the ops Telegram chat every ``interval_sec`` seconds
(default 900 = 15 min) while ``AUTO_EXECUTE_ENABLED=true``.

Silent in dev mode (``AUTO_EXECUTE_ENABLED`` unset or any value other than
``"true"``).

Usage::

    notifier = TelegramNotifier(token, chat_id)
    await run_heartbeat(notifier, interval_sec=900, get_status=my_status_fn)
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Callable

from ..monitor.balance import TelegramNotifier

logger = logging.getLogger("arbiter.notifiers.heartbeat")


@dataclass
class HeartbeatStatus:
    """Current snapshot of system health for the heartbeat message.

    Attributes
    ----------
    realized_pnl:
        Cumulative realized P&L in USD since startup.
    open_order_count:
        Number of currently open orders.
    extra:
        Optional extra key/value pairs to include in the message.
    """

    realized_pnl: float = 0.0
    open_order_count: int = 0
    extra: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}


def _auto_execute_enabled() -> bool:
    """Return True when AUTO_EXECUTE_ENABLED is set to the string ``"true"``."""
    return os.getenv("AUTO_EXECUTE_ENABLED", "").lower() == "true"


def _format_message(status: HeartbeatStatus) -> str:
    """Format a Telegram HTML heartbeat message from the status snapshot."""
    lines = [
        "<b>Arbiter Heartbeat</b>",
        f"realized_pnl: <code>${status.realized_pnl:.2f}</code>",
        f"open_orders:  <code>{status.open_order_count}</code>",
    ]
    for key, value in status.extra.items():
        lines.append(f"{key}: <code>{value}</code>")
    return "\n".join(lines)


async def run_heartbeat(
    notifier: TelegramNotifier,
    interval_sec: int = 900,
    get_status: Callable[[], "HeartbeatStatus"] = None,  # type: ignore[assignment]
) -> None:
    """Run the heartbeat loop.

    Every ``interval_sec`` seconds, if ``AUTO_EXECUTE_ENABLED=true``, calls
    ``get_status()`` and sends the result as a Telegram message.

    Silent (no-op sleep) when ``AUTO_EXECUTE_ENABLED`` is not set or not ``"true"``.

    Parameters
    ----------
    notifier:
        A :class:`~arbiter.monitor.balance.TelegramNotifier` instance.
    interval_sec:
        Seconds between heartbeat posts. Default 900 (15 minutes).
    get_status:
        Callable with no arguments that returns a :class:`HeartbeatStatus`.
        If ``None`` a zero-value status is used.
    """
    if get_status is None:
        def get_status() -> HeartbeatStatus:
            return HeartbeatStatus()

    while True:
        await asyncio.sleep(interval_sec)

        if not _auto_execute_enabled():
            logger.debug("Heartbeat: AUTO_EXECUTE_ENABLED not true — skipping")
            continue

        try:
            status = get_status()
            message = _format_message(status)
            ok = await notifier.send(
                message,
                parse_mode="HTML",
                dedup_key=f"arbiter.heartbeat.{int(asyncio.get_event_loop().time())}",
            )
            if ok:
                logger.debug("Heartbeat sent: pnl=%.2f orders=%d", status.realized_pnl, status.open_order_count)
            else:
                logger.warning("Heartbeat send() returned False (notifier disabled or deduped)")
        except Exception as exc:
            logger.error("Heartbeat error: %r", exc)
