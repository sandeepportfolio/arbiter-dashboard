"""setup_telegram_alerts_channel.py — walk through wiring a dedicated
Telegram channel for Arbiter arbitrage alerts.

Why this script exists:
    The live trading runtime posts sensitive alerts — kill-switch, fills,
    low balances, heartbeat, opportunities — to a single Telegram chat.
    Routing those to the operator's personal DM (or worse, an unrelated
    bot channel like OpenClaw) mingles signals and has already caused
    false calm / missed pages in practice. This helper creates a clean
    separation: one channel, one purpose, one bot-owner.

    Telegram's Bot API cannot create a channel for you — channels are
    owned by a human account, not a bot. So this script is a guided
    checklist + channel-id auto-discovery helper, not a fully automated
    provisioner.

Usage:
    set -a; source .env.production; set +a
    python scripts/setup/setup_telegram_alerts_channel.py

What it does:
    1. Confirms TELEGRAM_BOT_TOKEN is present and bot identity is valid
       (getMe).
    2. Prints step-by-step instructions to create a private channel,
       add the bot as an administrator with "Post Messages" permission,
       and send a seed message so getUpdates has something to return.
    3. Polls getUpdates and auto-detects new channel_post chat ids that
       differ from the existing TELEGRAM_CHAT_ID, and prints the id.
    4. Sends a confirmation message to the detected channel.
    5. Prints the exact .env.production line to add.

Exit codes:
    0 — channel detected and confirmation message delivered
    1 — missing token, bot not reachable, no channel detected, or send failed
    2 — unexpected exception

The script is idempotent — rerunning it just re-verifies the channel.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import aiohttp  # noqa: E402

POLL_ROUNDS = 24  # 24 * 5s = 2 minutes
POLL_INTERVAL_SEC = 5.0


async def _get_me(session: aiohttp.ClientSession, token: str) -> Optional[dict]:
    async with session.get(f"https://api.telegram.org/bot{token}/getMe") as resp:
        data = await resp.json()
        if resp.status != 200 or not data.get("ok"):
            print(f"getMe failed (status={resp.status}): {data}", file=sys.stderr)
            return None
        return data["result"]


async def _get_updates(session: aiohttp.ClientSession, token: str) -> list[dict]:
    async with session.get(f"https://api.telegram.org/bot{token}/getUpdates") as resp:
        data = await resp.json()
        if resp.status != 200 or not data.get("ok"):
            print(f"getUpdates failed (status={resp.status}): {data}", file=sys.stderr)
            return []
        return data.get("result", [])


def _extract_channel_chat_ids(updates: list[dict], exclude: set[str]) -> list[tuple[int, str]]:
    """Return (chat_id, title) pairs for channel_post updates not in exclude."""
    seen: dict[int, str] = {}
    for u in updates:
        post = u.get("channel_post") or u.get("edited_channel_post")
        if not post:
            continue
        chat = post.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type")
        if chat_type != "channel" or chat_id is None:
            continue
        if str(chat_id) in exclude:
            continue
        seen[chat_id] = chat.get("title", "<no title>")
    return sorted(seen.items())


async def _send(session: aiohttp.ClientSession, token: str, chat_id: int, text: str) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with session.post(
        f"https://api.telegram.org/bot{token}/sendMessage", json=payload
    ) as resp:
        if resp.status == 200:
            return True
        body = await resp.text()
        print(f"sendMessage failed (status={resp.status}): {body[:200]}", file=sys.stderr)
        return False


def _print_instructions(bot_username: str) -> None:
    print()
    print("━" * 72)
    print("  Arbiter — set up a dedicated Telegram alerts channel")
    print("━" * 72)
    print()
    print(f"  Bot: @{bot_username}")
    print()
    print("  Follow these steps in the Telegram app (mobile or desktop):")
    print()
    print("    1. Tap ✎ / New Message → 'New Channel'.")
    print("    2. Name it something clear, e.g. 'Arbiter Alerts'.")
    print("       Make it PRIVATE (alerts may include balance/PnL detail).")
    print("    3. Skip 'Add Members' — bots are added as admins, not members.")
    print("    4. Open the channel → (⋯) → 'Manage channel' → 'Administrators'")
    print("       → 'Add Administrator' → search and select your bot:")
    print(f"           @{bot_username}")
    print("       Grant 'Post Messages' (the other perms can stay off).")
    print("       Save.")
    print("    5. Post ANY message in the channel (e.g. 'hello'). The bot")
    print("       cannot see channel history before it was added.")
    print()
    print("  This script will now poll Telegram for new channel activity for")
    print(f"  up to {int(POLL_ROUNDS * POLL_INTERVAL_SEC)} seconds...")
    print()


async def _run() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    legacy_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    existing_alerts = os.getenv("TELEGRAM_ALERTS_CHAT_ID", "").strip()

    if not token:
        print(
            "TELEGRAM_BOT_TOKEN is not set. Source your .env.production first:\n"
            "    set -a; source .env.production; set +a",
            file=sys.stderr,
        )
        return 1

    exclude = {cid for cid in (legacy_chat_id, existing_alerts) if cid}

    async with aiohttp.ClientSession() as session:
        me = await _get_me(session, token)
        if not me:
            return 1
        bot_username = me.get("username") or "<unknown>"
        print(f"Bot identity confirmed: @{bot_username} (id={me.get('id')})")

        if existing_alerts:
            print()
            print(f"TELEGRAM_ALERTS_CHAT_ID is already set to {existing_alerts}.")
            print("Sending a confirmation message to verify the wiring...")
            try:
                chat_id_int = int(existing_alerts)
            except ValueError:
                print(
                    f"  existing value {existing_alerts!r} is not an integer — fix it manually",
                    file=sys.stderr,
                )
                return 1
            ok = await _send(
                session,
                token,
                chat_id_int,
                "✅ <b>Arbiter alerts channel verified</b>\n"
                f"Bot @{bot_username} can post here.\n"
                f"Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            )
            return 0 if ok else 1

        _print_instructions(bot_username)

        detected: Optional[tuple[int, str]] = None
        for round_ix in range(1, POLL_ROUNDS + 1):
            updates = await _get_updates(session, token)
            candidates = _extract_channel_chat_ids(updates, exclude)
            if candidates:
                # If multiple, take the most recent (last in sorted list).
                # Sorted by chat_id not recency, but it's rare to have many.
                detected = candidates[-1]
                break
            print(
                f"  [{round_ix:>2}/{POLL_ROUNDS}] no new channel activity yet… "
                f"(sleeping {POLL_INTERVAL_SEC:.0f}s — send a message in the channel)",
            )
            await asyncio.sleep(POLL_INTERVAL_SEC)

        if not detected:
            print()
            print(
                "No new channel_post detected within the window. Common causes:",
                file=sys.stderr,
            )
            print(
                "  - The bot was not promoted to admin in the new channel.\n"
                "  - No message has been posted in the channel since the bot\n"
                "    was added (required — Bot API cannot see prior history).\n"
                "  - Only a group (not a channel) was created — groups use the\n"
                "    same var but require different steps. Rerun after fixing.",
                file=sys.stderr,
            )
            return 1

        chat_id, title = detected
        print()
        print(f"✓ Detected channel: {title!r} (id={chat_id})")
        print()
        print("Sending confirmation message...")
        ok = await _send(
            session,
            token,
            chat_id,
            "✅ <b>Arbiter alerts channel wired</b>\n"
            f"Bot @{bot_username} can post here.\n"
            "This channel will receive all live trading alerts: opportunities, "
            "executions, balance warnings, kill-switch events, and heartbeats.",
        )
        if not ok:
            print(
                "Confirmation send failed. The channel was detected but the bot "
                "could not post — double-check the 'Post Messages' admin permission.",
                file=sys.stderr,
            )
            return 1

        print()
        print("━" * 72)
        print("  Next: add this line to .env.production")
        print("━" * 72)
        print()
        print(f"    TELEGRAM_ALERTS_CHAT_ID={chat_id}")
        print()
        print("Then rerun:")
        print("    python scripts/setup/check_telegram.py")
        return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI wants a readable error
        print(f"setup_telegram_alerts_channel ERROR: {exc!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
