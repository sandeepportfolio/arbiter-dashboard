import asyncio, os, sys
sys.path.insert(0, '/app')

async def main():
    from arbiter.notifiers.telegram import TelegramNotifier
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
    if not bot_token or not chat_id:
        print('Telegram not configured, skipping')
        return
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
    msg = (
        "=== Auto-Validator Pipeline Deployed ===\n"
        "\n"
        "Mapping Stats:\n"
        "  Confirmed: 305 (all live)\n"
        "  Candidates: 137 (all live)\n"
        "  Review: 76 (all live)\n"
        "  Auto-expired: 74 dead mappings\n"
        "\n"
        "New background tasks:\n"
        "  Auto-validator: every 30min\n"
        "  Auto-discovery: every 2hr\n"
        "\n"
        "Pipeline is fully operational."
    )
    await notifier.send(msg)
    print('Telegram notification sent')

asyncio.run(main())
