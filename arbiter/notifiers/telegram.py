"""Telegram notifier re-export + CLI dry-test entry point.

  $ python -m arbiter.notifiers.telegram

Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from the environment, sends a
single test message, and prints pass/fail. Used by Plan 06-06's GOLIVE.md as
the "Telegram dry test" gate before running the first live trade.

Exit codes:
  0 — message sent OK
  1 — disabled mode (missing token/chat_id) or send returned False
  2 — unexpected exception
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

from ..monitor.balance import TelegramNotifier

__all__ = ["TelegramNotifier"]


async def _dry_test() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print(
            "Telegram disabled — TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID "
            "is unset. Fill .env.production and re-run.",
            file=sys.stderr,
        )
        return 1

    notifier = TelegramNotifier(token, chat_id)
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ok = await notifier.send(
            f"🧪 <b>Arbiter Telegram dry-test</b>\n"
            f"If you are reading this, Telegram alerting is wired.\n"
            f"Timestamp: {ts}",
            dedup_key="arbiter.dry_test",
        )
        if ok:
            print("Telegram dry-test OK — message delivered.")
            return 0
        print(
            "Telegram dry-test FAILED — send() returned False. "
            "Check TELEGRAM_BOT_TOKEN (valid?) and TELEGRAM_CHAT_ID (bot is "
            "in the chat? messaged the bot at least once?).",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI wants a readable error
        print(f"Telegram dry-test ERROR: {exc!r}", file=sys.stderr)
        return 2
    finally:
        await notifier.close()


def main() -> int:
    return asyncio.run(_dry_test())


if __name__ == "__main__":
    sys.exit(main())
