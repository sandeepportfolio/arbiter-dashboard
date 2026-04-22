"""Telegram notifier re-export + CLI dry-test entry point.

  $ python -m arbiter.notifiers.telegram

Reads TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (pairing DM), and optional
TELEGRAM_ALERTS_CHAT_ID (dedicated arbitrage-alerts channel) from the
environment. Sends a dry-test to EACH configured chat so live-trading
operators can confirm both routes before flipping AUTO_EXECUTE_ENABLED.

Exit codes:
  0 — every configured chat received the message
  1 — disabled mode (missing token/chat ids) or any send returned False
  2 — unexpected exception
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

from ..monitor.balance import TelegramNotifier

__all__ = ["TelegramNotifier"]


async def _send_to(token: str, chat_id: str, label: str, ts: str) -> bool:
    notifier = TelegramNotifier(token, chat_id)
    try:
        ok = await notifier.send(
            f"🧪 <b>Arbiter Telegram dry-test — {label}</b>\n"
            f"If you are reading this, {label} routing is wired.\n"
            f"Timestamp: {ts}",
            dedup_key=f"arbiter.dry_test.{label}",
        )
        if ok:
            print(f"Telegram dry-test OK [{label}] — message delivered to chat {chat_id}.")
        else:
            print(
                f"Telegram dry-test FAILED [{label}] — send() returned False for chat {chat_id}. "
                f"Check TELEGRAM_BOT_TOKEN and that the bot is a member/admin of the chat.",
                file=sys.stderr,
            )
        return ok
    finally:
        await notifier.close()


async def _dry_test() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    alerts_chat_id = os.getenv("TELEGRAM_ALERTS_CHAT_ID", "").strip()

    if not token or not chat_id:
        print(
            "Telegram disabled — TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID "
            "is unset. Fill .env.production and re-run.",
            file=sys.stderr,
        )
        return 1

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        pairing_ok = await _send_to(token, chat_id, "pairing DM", ts)

        if alerts_chat_id and alerts_chat_id != chat_id:
            alerts_ok = await _send_to(token, alerts_chat_id, "arbitrage alerts channel", ts)
        else:
            if not alerts_chat_id:
                print(
                    "WARNING: TELEGRAM_ALERTS_CHAT_ID not set — live alerts will fall "
                    "back to TELEGRAM_CHAT_ID. For live trading, create a dedicated "
                    "channel (see scripts/setup/setup_telegram_alerts_channel.py).",
                    file=sys.stderr,
                )
            alerts_ok = True

        return 0 if (pairing_ok and alerts_ok) else 1
    except Exception as exc:  # noqa: BLE001 — CLI wants a readable error
        print(f"Telegram dry-test ERROR: {exc!r}", file=sys.stderr)
        return 2


def main() -> int:
    return asyncio.run(_dry_test())


if __name__ == "__main__":
    sys.exit(main())
