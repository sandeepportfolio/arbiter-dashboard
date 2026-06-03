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
        Cumulative realized P&L in USD from the reconciled execution ledger.
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
    from .fmt import DIVIDER, usd, h

    extra = status.extra or {}

    # ── Balances section ──
    balances_str = str(extra.get("balances", ""))
    balance_lines = []
    if balances_str:
        for pair in balances_str.split(", "):
            pair = pair.strip()
            if "=" in pair:
                plat, val = pair.split("=", 1)
                balance_lines.append(f"  {h(plat.strip().title())}: <code>{h(val.strip())}</code>")

    # ── Auto-exec status ──
    auto_exec = str(extra.get("auto_execute", "false")).lower()
    ae_icon = "\U0001f7e2" if auto_exec == "true" else "\U0001f534"  # green/red circle
    ae_label = "ON" if auto_exec == "true" else "OFF"

    # ── Kill switch / verdict ──
    verdict = str(extra.get("verdict", "unknown"))
    verdict_icon = "\U0001f7e2" if verdict == "profitable" else "\U0001f7e1"  # green/yellow circle

    # ── Scanner stats ──
    scans = extra.get("scans", 0)
    active_opps = extra.get("active_opps", 0)
    published = extra.get("published", 0)
    best_edge = extra.get("best_edge_c", 0)

    # ── Executor stats ──
    executed = extra.get("executed", 0)
    considered = extra.get("considered", 0)

    # ── Naked legs ──
    naked = int(extra.get("naked_legs", 0) or 0)
    naked_icon = "\U0001f7e2" if naked == 0 else "\U0001f534"  # green/red circle

    lines = [
        f"\U0001f4ca <b>ARBITER HEARTBEAT</b>",
        DIVIDER,
        f"\U0001f4b0 <b>P&L / Orders</b>",
        f"  Realized: <code>{usd(status.realized_pnl, signed=True)}</code>",
        f"  Open orders: <code>{status.open_order_count}</code>",
    ]

    if balance_lines:
        lines.append("")
        lines.append(f"\U0001f3e6 <b>Balances</b>")
        lines.extend(balance_lines)

    lines.append("")
    lines.append(f"\U0001f50d <b>Scanner</b>")
    lines.append(f"  Scans: <code>{scans}</code>  |  Opps: <code>{active_opps}</code>  |  Published: <code>{published}</code>")
    if best_edge:
        lines.append(f"  Best edge: <code>{float(best_edge):.1f}c</code>")

    lines.append("")
    lines.append(f"{ae_icon} <b>Auto-Exec:</b> <code>{ae_label}</code>")
    if auto_exec == "true":
        lines.append(f"  Executed: <code>{executed}</code>  |  Considered: <code>{considered}</code>")

    lines.append(f"{verdict_icon} <b>Verdict:</b> <code>{h(verdict)}</code>")
    lines.append(f"{naked_icon} <b>Naked legs:</b> <code>{naked}</code>")

    lines.append(DIVIDER)

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

    # Fire one heartbeat shortly after startup so operators have immediate
    # proof the alert pipe is alive without waiting for the full interval.
    # Then settle into the periodic cadence.
    first_send_delay = min(30.0, float(interval_sec))
    fired_once = False
    logger.info(
        "Telegram heartbeat task started (first send in %ss, interval %ss)",
        int(first_send_delay), int(interval_sec),
    )
    while True:
        await asyncio.sleep(first_send_delay if not fired_once else interval_sec)
        is_first = not fired_once
        fired_once = True

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
            # First send logs at INFO so operators see proof-of-life in the
            # default log stream; subsequent periodic sends stay at DEBUG
            # so the steady-state log is uncluttered.
            success_log = logger.info if is_first else logger.debug
            if ok:
                success_log(
                    "Heartbeat sent (first=%s): pnl=$%.2f orders=%d",
                    is_first, status.realized_pnl, status.open_order_count,
                )
            else:
                logger.warning(
                    "Heartbeat send() returned False (first=%s) — notifier disabled or deduped",
                    is_first,
                )
        except Exception as exc:
            logger.error("Heartbeat error: %r", exc)
