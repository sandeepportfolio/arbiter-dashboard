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
# "ARBITRAGE FOUND" alerts. Past incident: a stale Kalshi last_price of
# $0.04 paired with a real Polymarket ask paged the operator with a
# fake 49¢ edge — hence the explicit price floor below.
ALERT_MIN_NET_EDGE_CENTS = 3.0  # buffer above break-even (covers slippage)
ALERT_MAX_QUOTE_AGE_SECONDS = 30.0
ALERT_MIN_CONFIDENCE = 0.5
# Below this, a "price" is almost certainly a stale last_price or phantom
# quote (real bids/asks on tradeable political markets sit well above 5¢
# until the very last moments of resolution). Reject before notifying so
# we don't alert on illusory edge.
ALERT_MIN_PRICE = 0.05


def _normalize_for_compare(text: str) -> str:
    """Lower + collapse whitespace + strip punctuation noise for comparing
    an outcome name to a canonical description. Generous on purpose: we
    want any reasonable equivalence to count, since a vague-only alert
    is the failure mode."""
    import re as _re
    return _re.sub(r"[\s\W_]+", " ", (text or "").lower()).strip()


def _alert_outcome_is_specific(opp: ArbitrageOpportunity) -> bool:
    """Return True if at least one side carries an outcome name that
    differs from the canonical mapping description.

    Past incident: the alert displayed "U.S Senate Midterm Winner" — the
    market category — instead of "Democrats" or "Republicans". Without
    a specific outcome the operator has no way to tell which side is
    being traded, so we suppress."""
    description_norm = _normalize_for_compare(opp.description)
    yes_norm = _normalize_for_compare(opp.yes_outcome_name)
    no_norm = _normalize_for_compare(opp.no_outcome_name)
    yes_specific = bool(yes_norm) and yes_norm != description_norm
    no_specific = bool(no_norm) and no_norm != description_norm
    return yes_specific or no_specific


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
    if opp.yes_price < ALERT_MIN_PRICE or opp.no_price < ALERT_MIN_PRICE:
        logger.warning(
            "Alert suppressed [%s] price below $%.2f floor (yes=$%.3f no=$%.3f) — likely stale/phantom quote",
            opp.canonical_id, ALERT_MIN_PRICE, opp.yes_price, opp.no_price,
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
    # Per-side age check is stricter than the legacy max(yes,no) because
    # both legs must be fresh — a stale leg means the displayed price
    # isn't actionable. yes_quote_age_seconds / no_quote_age_seconds may
    # be 0.0 on legacy opportunities; fall back to the aggregate.
    yes_age = opp.yes_quote_age_seconds or opp.quote_age_seconds
    no_age = opp.no_quote_age_seconds or opp.quote_age_seconds
    if yes_age > ALERT_MAX_QUOTE_AGE_SECONDS or no_age > ALERT_MAX_QUOTE_AGE_SECONDS:
        logger.warning(
            "Alert suppressed [%s] quote_age yes=%.1fs no=%.1fs (>%.0fs limit, stale)",
            opp.canonical_id, yes_age, no_age, ALERT_MAX_QUOTE_AGE_SECONDS,
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
    if not _alert_outcome_is_specific(opp):
        logger.warning(
            "Alert suppressed [%s] outcome name not specific (yes=%r no=%r matches canonical %r)",
            opp.canonical_id, opp.yes_outcome_name, opp.no_outcome_name, opp.description,
        )
        return False
    return True


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _short_market_id(value: str, head: int = 8, tail: int = 4) -> str:
    """Shorten long Polymarket token IDs for display, keep Kalshi tickers intact."""
    value = (value or "").strip()
    if len(value) <= head + tail + 1:
        return value
    return f"{value[:head]}…{value[-tail:]}"


def _pick_alert_outcome(opp: ArbitrageOpportunity) -> str:
    """Pick the most specific outcome name for the alert header.

    Prefer whichever side has a name that differs from the canonical
    description. Falls back to canonical description only if both sides
    are blank (gate should have rejected such an alert already)."""
    description_norm = _normalize_for_compare(opp.description)
    for candidate in (opp.yes_outcome_name, opp.no_outcome_name):
        if candidate and _normalize_for_compare(candidate) != description_norm:
            return candidate
    # Both blank or both equal canonical — fall back, gate normally rejects.
    return opp.yes_outcome_name or opp.no_outcome_name or opp.description


def _format_arb_alert(opp: ArbitrageOpportunity) -> str:
    """Render the user-facing arbitrage alert.

    Output is HTML (Telegram parse_mode=HTML). Includes per-side outcome,
    market id, executable bid/ask, quote age, and the math summary so the
    operator can verify the trade on each platform before submitting."""
    outcome_header = _pick_alert_outcome(opp)
    yes_age = opp.yes_quote_age_seconds or opp.quote_age_seconds
    no_age = opp.no_quote_age_seconds or opp.quote_age_seconds
    yes_id = _short_market_id(opp.yes_market_id) if opp.yes_platform == "polymarket" else opp.yes_market_id
    no_id = _short_market_id(opp.no_market_id) if opp.no_platform == "polymarket" else opp.no_market_id

    yes_bid_ask = (
        f"${opp.yes_bid:.3f}/${opp.yes_ask:.3f}"
        if (opp.yes_bid or opp.yes_ask)
        else "n/a"
    )
    no_bid_ask = (
        f"${opp.no_bid:.3f}/${opp.no_ask:.3f}"
        if (opp.no_bid or opp.no_ask)
        else "n/a"
    )

    yes_question_line = (
        f"\n  ❓ <i>{_truncate(opp.yes_question, 140)}</i>" if opp.yes_question else ""
    )
    no_question_line = (
        f"\n  ❓ <i>{_truncate(opp.no_question, 140)}</i>" if opp.no_question else ""
    )

    return (
        f"💰 <b>ARBITRAGE: {_truncate(outcome_header, 80)}</b>\n"
        f"<code>{opp.canonical_id}</code>\n"
        f"\n"
        f"<b>{opp.yes_platform.upper()}</b>: BUY <b>YES</b> @ ${opp.yes_price:.3f} "
        f"(ask, {yes_age:.0f}s old)\n"
        f"  ├ Market: <code>{yes_id}</code>\n"
        f"  └ Bid/Ask: {yes_bid_ask}"
        f"{yes_question_line}\n"
        f"<b>{opp.no_platform.upper()}</b>: BUY <b>NO</b> @ ${opp.no_price:.3f} "
        f"(ask, {no_age:.0f}s old)\n"
        f"  ├ Market: <code>{no_id}</code>\n"
        f"  └ Bid/Ask: {no_bid_ask}"
        f"{no_question_line}\n"
        f"\n"
        f"Edge: {opp.gross_edge*100:.1f}¢ gross → "
        f"<b>{opp.net_edge_cents:.1f}¢ net</b> (after {opp.total_fees*100:.1f}¢ fees)\n"
        f"Qty: <b>{opp.suggested_qty}</b> | Max profit: <b>${opp.max_profit_usd:.2f}</b>\n"
        f"Confidence: {opp.confidence*100:.0f}% | Mapping: {opp.mapping_status} "
        f"(score {opp.mapping_score:.2f})\n"
        f"\n"
        f"⚠️ <i>Verify both legs target the SAME outcome on the apps before trading.</i>"
    )


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
        msg = _format_arb_alert(opp)
        await self.notifier.send(msg, dedup_key=key)

    async def alert_execution_result(
        self,
        arb_id: str,
        opp: ArbitrageOpportunity,
        status: str,
        leg_yes: "Order",
        leg_no: "Order",
        realized_pnl: float = 0.0,
    ):
        """Send Telegram alert with trade execution result."""
        if status == "filled":
            emoji = "✅"
            header = "TRADE FILLED"
        elif status == "partial":
            emoji = "⚠️"
            header = "PARTIAL FILL"
        elif status in ("failed", "aborted"):
            emoji = "❌"
            header = f"TRADE {status.upper()}"
        elif status == "unwound":
            emoji = "🔄"
            header = "TRADE UNWOUND"
        else:
            emoji = "📋"
            header = f"TRADE {status.upper()}"

        pnl_emoji = "📈" if realized_pnl >= 0 else "📉"

        yes_status = leg_yes.status.value if hasattr(leg_yes.status, "value") else str(leg_yes.status)
        no_status = leg_no.status.value if hasattr(leg_no.status, "value") else str(leg_no.status)

        msg = (
            f"{emoji} <b>{header}</b>\n"
            f"<code>{arb_id}</code> — {opp.description[:80]}\n"
            f"\n"
            f"<b>{opp.yes_platform.upper()}</b> YES: "
            f"limit ${leg_yes.price:.3f} → fill ${leg_yes.fill_price:.3f} "
            f"x{leg_yes.fill_qty} [{yes_status}]\n"
            f"<b>{opp.no_platform.upper()}</b> NO: "
            f"limit ${leg_no.price:.3f} → fill ${leg_no.fill_price:.3f} "
            f"x{leg_no.fill_qty} [{no_status}]\n"
            f"\n"
            f"Edge: {opp.net_edge_cents:.1f}¢ net | Qty: {opp.suggested_qty}\n"
            f"{pnl_emoji} Realized P&L: <b>${realized_pnl:+.2f}</b>"
        )

        if leg_yes.error:
            msg += f"\n⚠️ YES error: {leg_yes.error[:100]}"
        if leg_no.error:
            msg += f"\n⚠️ NO error: {leg_no.error[:100]}"

        await self.notifier.send(msg, dedup_key=f"exec_{arb_id}")
        logger.info("Execution alert sent for %s: %s pnl=$%.2f", arb_id, status, realized_pnl)

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
