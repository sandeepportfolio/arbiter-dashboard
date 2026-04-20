"""
ARBITER — Balance Monitor + Telegram Alerts
Tracks balances across all platforms, sends alerts when low.
Also sends alerts for profitable arbitrage opportunities.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

import aiohttp

from ..config.settings import AlertConfig
from ..scanner.arbitrage import ArbitrageOpportunity

logger = logging.getLogger("arbiter.monitor")


@dataclass
class BalanceSnapshot:
    platform: str
    balance: float
    timestamp: float
    is_low: bool = False


class TelegramNotifier:
    """Send alerts via Telegram bot."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None
        self._enabled = bool(bot_token and chat_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a Telegram message."""
        if not self._enabled:
            logger.debug(f"Telegram disabled, would send: {message[:80]}...")
            return False

        session = await self._get_session()
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
        }

        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    logger.debug("Telegram message sent")
                    return True
                else:
                    text = await resp.text()
                    logger.warning(f"Telegram API error {resp.status}: {text[:200]}")
                    return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class BalanceMonitor:
    """
    Monitors balances across all platforms.
    Sends Telegram alerts when balance drops below threshold.
    Also forwards high-value arbitrage opportunities.
    """

    def __init__(self, config: AlertConfig, collectors: dict):
        """
        collectors: {"kalshi": KalshiCollector, "polymarket": PolymarketCollector, ...}
        """
        self.config = config
        self.collectors = collectors
        self.notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
        self._running = False
        self._balances: Dict[str, BalanceSnapshot] = {}
        self._last_alert_time: Dict[str, float] = {}
        self._thresholds = {
            "kalshi": config.kalshi_low,
            "polymarket": config.polymarket_low,
        }
        # Manual balance overrides (for platforms without balance API)
        self._manual_balances: Dict[str, float] = {}

    def set_manual_balance(self, platform: str, balance: float):
        """Set balance manually for platforms without API."""
        self._manual_balances[platform] = balance
        logger.info(f"Manual balance set: {platform} = ${balance:.2f}")

    async def check_balances(self) -> Dict[str, BalanceSnapshot]:
        """Fetch balances from all platforms."""
        snapshots = {}

        for platform, collector in self.collectors.items():
            try:
                balance = await collector.fetch_balance()

                # Fall back to manual balance
                if balance is None and platform in self._manual_balances:
                    balance = self._manual_balances[platform]

                if balance is not None:
                    threshold = self._thresholds.get(platform, 50.0)
                    is_low = balance < threshold
                    snap = BalanceSnapshot(
                        platform=platform,
                        balance=balance,
                        timestamp=time.time(),
                        is_low=is_low,
                    )
                    snapshots[platform] = snap
                    self._balances[platform] = snap

                    # Send alert if low and cooldown elapsed
                    if is_low:
                        await self._maybe_alert_low_balance(platform, balance, threshold)

            except Exception as e:
                logger.error(f"Balance check error for {platform}: {e}")

        return snapshots

    async def _maybe_alert_low_balance(self, platform: str, balance: float, threshold: float):
        """Send low balance alert if cooldown has elapsed."""
        now = time.time()
        last = self._last_alert_time.get(f"balance_{platform}", 0)
        if now - last < self.config.cooldown:
            return

        self._last_alert_time[f"balance_{platform}"] = now
        msg = (
            f"🔴 <b>LOW BALANCE ALERT</b>\n\n"
            f"<b>{platform.upper()}</b>: ${balance:.2f}\n"
            f"Threshold: ${threshold:.2f}\n"
            f"⚠️ Fund this account to continue arbitrage operations."
        )
        await self.notifier.send(msg)
        logger.warning(f"Low balance alert sent: {platform} ${balance:.2f} < ${threshold:.2f}")

    async def alert_opportunity(self, opp: ArbitrageOpportunity):
        """Send Telegram alert for a profitable arbitrage opportunity."""
        now = time.time()
        key = f"arb_{opp.canonical_id}_{opp.yes_platform}_{opp.no_platform}"
        last = self._last_alert_time.get(key, 0)
        if now - last < self.config.cooldown:
            return

        self._last_alert_time[key] = now
        msg = (
            f"💰 <b>ARBITRAGE FOUND</b>\n\n"
            f"<b>{opp.description}</b>\n"
            f"├ BUY YES @ {opp.yes_platform.upper()}: ${opp.yes_price:.2f}\n"
            f"├ BUY NO  @ {opp.no_platform.upper()}: ${opp.no_price:.2f}\n"
            f"├ Gross edge: {opp.gross_edge*100:.1f}¢\n"
            f"├ Fees: {opp.total_fees*100:.1f}¢\n"
            f"├ <b>Net profit: {opp.net_edge_cents:.1f}¢/contract</b>\n"
            f"├ Suggested qty: {opp.suggested_qty}\n"
            f"└ <b>Max profit: ${opp.max_profit_usd:.2f}</b>\n\n"
            f"Confidence: {opp.confidence*100:.0f}%"
        )
        await self.notifier.send(msg)

    async def send_daily_summary(self):
        """Send daily summary of balances and activity."""
        lines = ["📊 <b>ARBITER DAILY SUMMARY</b>\n"]

        total = 0.0
        for platform, snap in self._balances.items():
            emoji = "🔴" if snap.is_low else "🟢"
            lines.append(f"{emoji} {platform.upper()}: ${snap.balance:.2f}")
            total += snap.balance

        lines.append(f"\n💰 Total across platforms: ${total:.2f}")
        await self.notifier.send("\n".join(lines))

    @property
    def current_balances(self) -> Dict[str, BalanceSnapshot]:
        return dict(self._balances)

    @property
    def total_balance(self) -> float:
        return sum(s.balance for s in self._balances.values())

    async def run(self, arb_queue: Optional[asyncio.Queue] = None):
        """
        Main monitoring loop.
        Checks balances every 30s and processes arb opportunity alerts.
        """
        self._running = True
        logger.info("Balance monitor started")

        balance_task = asyncio.create_task(self._balance_loop())
        arb_task = asyncio.create_task(self._arb_alert_loop(arb_queue)) if arb_queue else None

        try:
            tasks = [balance_task]
            if arb_task:
                tasks.append(arb_task)
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            balance_task.cancel()
            if arb_task:
                arb_task.cancel()

    async def _balance_loop(self):
        """Check balances periodically."""
        while self._running:
            try:
                await self.check_balances()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Balance monitor error: {e}")
                await asyncio.sleep(10)

    async def _arb_alert_loop(self, queue: asyncio.Queue):
        """Process arbitrage opportunities and send alerts for good ones."""
        while self._running:
            try:
                opp = await asyncio.wait_for(queue.get(), timeout=5.0)
                if opp.net_edge_cents >= 3.0 and opp.confidence >= 0.5:
                    await self.alert_opportunity(opp)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Arb alert error: {e}")

    async def stop(self):
        self._running = False
        await self.notifier.close()
