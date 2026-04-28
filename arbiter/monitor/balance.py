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


# ── Alert validation gates ────────────────────────────────────────────
# An alert is only safe to send if ALL of these hold. These match the
# scanner's "tradable" status guarantees but are duplicated here so a
# regression in scanner gating cannot cause us to push misleading
# "ARBITRAGE FOUND" alerts.
ALERT_MIN_NET_EDGE_CENTS = 3.0  # buffer above break-even (covers slippage)
ALERT_MAX_QUOTE_AGE_SECONDS = 30.0
ALERT_MIN_CONFIDENCE = 0.5


def _alert_is_safe_to_send(opp: ArbitrageOpportunity) -> bool:
    """Return True only if every safety condition for alerting is met.

    Each failed check logs at WARNING so operators can audit why an alert
    was suppressed. Order is from cheapest/most-fundamental check first.
    """
    if opp.mapping_status != "confirmed":
        logger.warning(
            "Alert suppressed [%s] mapping_status=%s (must be 'confirmed')",
            opp.canonical_id, opp.mapping_status,
        )
        return False
    if opp.status not in {"tradable", "manual"}:
        logger.warning(
            "Alert suppressed [%s] status=%s (must be 'tradable' or 'manual')",
            opp.canonical_id, opp.status,
        )
        return False
    if opp.yes_price <= 0 or opp.no_price <= 0:
        logger.warning(
            "Alert suppressed [%s] non-positive price yes=%.3f no=%.3f",
            opp.canonical_id, opp.yes_price, opp.no_price,
        )
        return False
    if opp.yes_price + opp.no_price >= 1.0:
        logger.warning(
            "Alert suppressed [%s] yes+no=%.3f, no genuine cross-platform arb",
            opp.canonical_id, opp.yes_price + opp.no_price,
        )
        return False
    if opp.net_edge_cents < ALERT_MIN_NET_EDGE_CENTS:
        logger.warning(
            "Alert suppressed [%s] net_edge=%.2f¢ below %.1f¢ profitability buffer",
            opp.canonical_id, opp.net_edge_cents, ALERT_MIN_NET_EDGE_CENTS,
        )
        return False
    if opp.quote_age_seconds > ALERT_MAX_QUOTE_AGE_SECONDS:
        logger.warning(
            "Alert suppressed [%s] quote_age=%.1fs > %.0fs (stale)",
            opp.canonical_id, opp.quote_age_seconds, ALERT_MAX_QUOTE_AGE_SECONDS,
        )
        return False
    if opp.confidence < ALERT_MIN_CONFIDENCE:
        logger.warning(
            "Alert suppressed [%s] confidence=%.2f below %.2f threshold",
            opp.canonical_id, opp.confidence, ALERT_MIN_CONFIDENCE,
        )
        return False
    if opp.suggested_qty <= 0:
        logger.warning(
            "Alert suppressed [%s] suggested_qty=%d (no executable size)",
            opp.canonical_id, opp.suggested_qty,
        )
        return False
    return True


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class BalanceSnapshot:
    platform: str
    balance: float
    timestamp: float
    is_low: bool = False


class TelegramNotifier:
    """Send alerts via Telegram bot.

    Phase 6 Plan 06-03 adds:
      - Retry on transient aiohttp failures (3 attempts with 0.5/1/2s backoff).
      - Dedup within a sliding window (default 60s) keyed by ``dedup_key`` so
        repeat alerts (e.g., rate-limit crit bursts) don't spam Telegram.
      - Disabled-mode is a true no-op: ``send()`` returns False quickly with
        no HTTP call.

    Backwards-compatible: the previous ``send(message)`` signature still works;
    ``dedup_key`` is optional.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        dedup_window_sec: float = 60.0,
        max_retries: int = 3,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None
        self._enabled = bool(bot_token and chat_id)
        self._dedup_window_sec = max(0.0, float(dedup_window_sec))
        self._max_retries = max(1, int(max_retries))
        self._last_sent: Dict[str, float] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _is_duplicate(self, dedup_key: Optional[str]) -> bool:
        if dedup_key is None or self._dedup_window_sec <= 0:
            return False
        now = time.time()
        prior = self._last_sent.get(dedup_key)
        if prior is not None and (now - prior) < self._dedup_window_sec:
            return True
        self._last_sent[dedup_key] = now
        # Opportunistic compaction (bounded memory growth).
        if len(self._last_sent) > 256:
            cutoff = now - self._dedup_window_sec * 4
            self._last_sent = {
                k: t for k, t in self._last_sent.items() if t >= cutoff
            }
        return False

    async def send(
        self,
        message: str,
        parse_mode: str = "HTML",
        *,
        dedup_key: Optional[str] = None,
    ) -> bool:
        """Send a Telegram message with retry + optional dedup.

        Returns True on HTTP 200 from Telegram, False on any other outcome
        (disabled, deduped, retries exhausted, non-200 response).
        """
        if not self._enabled:
            logger.debug(f"Telegram disabled, would send: {message[:80]}...")
            return False

        if self._is_duplicate(dedup_key):
            logger.debug(f"Telegram deduped (key={dedup_key!r}): {message[:80]}...")
            return False

        session = await self._get_session()
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
        }

        backoff = 0.5
        for attempt in range(1, self._max_retries + 1):
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        logger.debug("Telegram message sent")
                        return True
                    text = await resp.text()
                    logger.warning(
                        f"Telegram API error {resp.status} (attempt {attempt}/{self._max_retries}): {text[:200]}"
                    )
                    # 5xx → retry; 4xx (bad token, missing chat, rate-limit 429) → give up
                    if resp.status < 500 and resp.status != 429:
                        return False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    f"Telegram send transient error (attempt {attempt}/{self._max_retries}): {e}"
                )
            if attempt < self._max_retries:
                await asyncio.sleep(backoff)
                backoff *= 2
        logger.error("Telegram send failed after %d retries", self._max_retries)
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
        alerts_chat_id = getattr(config, "telegram_alerts_chat_id", "") or config.telegram_chat_id
        self.notifier = TelegramNotifier(config.telegram_bot_token, alerts_chat_id)
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
        """Send Telegram alert for a profitable arbitrage opportunity.

        Defense-in-depth: this re-validates the safety conditions the scanner
        already checks, so a regression in scanner gating cannot cause us to
        push misleading "ARBITRAGE FOUND" alerts to operators. Each suppression
        path logs at WARNING so operators can see why an alert was skipped.
        """
        if not _alert_is_safe_to_send(opp):
            return

        now = time.time()
        key = f"arb_{opp.canonical_id}_{opp.yes_platform}_{opp.no_platform}"
        last = self._last_alert_time.get(key, 0)
        if now - last < self.config.cooldown:
            return

        self._last_alert_time[key] = now
        total_cost = opp.yes_price + opp.no_price
        yes_q_line = f"\n  ❓ <i>{_truncate(opp.yes_question, 140)}</i>" if opp.yes_question else ""
        no_q_line = f"\n  ❓ <i>{_truncate(opp.no_question, 140)}</i>" if opp.no_question else ""

        msg = (
            f"💰 <b>ARBITRAGE FOUND</b>\n"
            f"\n"
            f"🎯 <b>Outcome:</b> {opp.description}\n"
            f"🆔 <code>{opp.canonical_id}</code>\n"
            f"\n"
            f"📈 <b>BUY YES on {opp.yes_platform.upper()}</b> @ ${opp.yes_price:.3f}\n"
            f"  📍 Market: <code>{opp.yes_market_id}</code>"
            f"{yes_q_line}\n"
            f"\n"
            f"📉 <b>BUY NO on {opp.no_platform.upper()}</b> @ ${opp.no_price:.3f}\n"
            f"  📍 Market: <code>{opp.no_market_id}</code>"
            f"{no_q_line}\n"
            f"\n"
            f"💵 <b>Profit math (per contract):</b>\n"
            f"├ Cost: ${total_cost:.3f} (YES + NO)\n"
            f"├ Gross edge: {opp.gross_edge*100:.2f}¢\n"
            f"├ Fees: {opp.total_fees*100:.2f}¢\n"
            f"└ <b>Net profit: {opp.net_edge_cents:.2f}¢ after fees ✅</b>\n"
            f"\n"
            f"📊 Suggested qty: <b>{opp.suggested_qty}</b>\n"
            f"💎 Max profit: <b>${opp.max_profit_usd:.2f}</b>\n"
            f"\n"
            f"🔒 Mapping: {opp.mapping_status} (score {opp.mapping_score:.2f})\n"
            f"⏱ Quote age: {opp.quote_age_seconds:.1f}s\n"
            f"🎯 Confidence: {opp.confidence*100:.0f}%\n"
            f"\n"
            f"⚠️ <i>Verify both legs target the SAME outcome on the apps before trading.</i>"
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
        """Process arbitrage opportunities and send alerts for good ones.

        All gating is delegated to ``_alert_is_safe_to_send`` (defense-in-depth:
        the same checks run again inside ``alert_opportunity``).
        """
        while self._running:
            try:
                opp = await asyncio.wait_for(queue.get(), timeout=5.0)
                if _alert_is_safe_to_send(opp):
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
