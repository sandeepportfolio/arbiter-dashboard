"""
ARBITER — Balance Monitor + Telegram Alerts
Tracks balances across all platforms, sends alerts when low.
Also sends alerts for profitable arbitrage opportunities.
"""
import asyncio
import html
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

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

# BUG #5: defensive fee rates used by ``_estimated_net_edge_after_fees``
# to double-check alert profitability at the alert-gate, independent of
# what the scanner attached to ``opp.total_fees``. If the scanner
# attaches a stale or under-counted fee number, this gate still rejects
# unprofitable alerts. Operator-tunable via env so we can ratchet up
# when a venue raises fees without a code redeploy.
ALERT_FEE_RATE_FALLBACK = {
    "kalshi": float(os.getenv("ALERT_FEE_RATE_KALSHI", "0.07")),     # 7%
    "polymarket": float(os.getenv("ALERT_FEE_RATE_POLYMARKET", "0.05")),  # 5%
    "forecastex": float(os.getenv("ALERT_FEE_RATE_FORECASTEX", "0.03")),  # 3%
}


def _venue_fee_rate(opp: ArbitrageOpportunity, side: str) -> float:
    """Return the higher of (scanner-attached rate, fallback) per venue.

    Defensive: we never trust the scanner-attached rate to go *below*
    the operator-set fallback. If the scanner under-reports the rate
    (e.g. stale fee config), the fallback floors the estimate so an
    unprofitable trade still gets caught here.
    """
    attached = float(getattr(opp, f"{side}_fee_rate", 0.0) or 0.0)
    platform = str(getattr(opp, f"{side}_platform", "") or "").lower()
    fallback = ALERT_FEE_RATE_FALLBACK.get(platform, 0.07)
    return max(attached, fallback)


def _estimated_net_edge_after_fees_usd(opp: ArbitrageOpportunity) -> float:
    """BUG #5: independent fee-aware profit estimate for the alert gate.

    Recomputes the per-contract net edge using ALERT_FEE_RATE_FALLBACK
    so the alert gate never trusts a stale ``total_fees`` value. Returns
    the estimated USD profit on the suggested quantity.

    Math (per contract):
      gross = 1 - yes_price - no_price          # cross-platform arb payoff
      fees  = yes_price * fee_rate_yes_venue
            + no_price  * fee_rate_no_venue
      net   = gross - fees
    """
    yes_price = float(opp.yes_price or 0.0)
    no_price = float(opp.no_price or 0.0)
    gross_per_contract = 1.0 - yes_price - no_price
    yes_fee = yes_price * _venue_fee_rate(opp, "yes")
    no_fee = no_price * _venue_fee_rate(opp, "no")
    net_per_contract = gross_per_contract - yes_fee - no_fee
    qty = max(int(opp.suggested_qty or 0), 0)
    return net_per_contract * qty
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
    if opp.max_profit_usd <= 0:
        logger.warning(
            "Alert suppressed [%s] expected_profit=$%.4f (not profitable after fees/size)",
            opp.canonical_id, opp.max_profit_usd,
        )
        return False
    # BUG #5: defense-in-depth fee gate. Recompute net edge with
    # ALERT_FEE_RATE_FALLBACK so a stale scanner fee number cannot push
    # an unprofitable alert. Floors at fallback so the gate is at least
    # as strict as the operator-set venue fee rates.
    estimated_net = _estimated_net_edge_after_fees_usd(opp)
    if estimated_net <= 0:
        logger.warning(
            "Alert suppressed [%s] estimated_net_after_fees=$%.4f "
            "(yes=%s@%.3f no=%s@%.3f qty=%d) — not profitable at "
            "fallback fee rates",
            opp.canonical_id, estimated_net,
            opp.yes_platform, opp.yes_price,
            opp.no_platform, opp.no_price,
            int(opp.suggested_qty or 0),
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


def _html(text: object) -> str:
    return html.escape(str(text or ""), quote=False)


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
    from ..notifiers.fmt import DIVIDER, h, price3, usd, age as fmt_age, truncate, short_id

    outcome_header = _pick_alert_outcome(opp)
    yes_age = opp.yes_quote_age_seconds or opp.quote_age_seconds
    no_age = opp.no_quote_age_seconds or opp.quote_age_seconds
    yes_id = short_id(opp.yes_market_id) if opp.yes_platform == "polymarket" else opp.yes_market_id
    no_id = short_id(opp.no_market_id) if opp.no_platform == "polymarket" else opp.no_market_id

    yes_bid_ask = (
        f"{price3(opp.yes_bid)}/{price3(opp.yes_ask)}"
        if (opp.yes_bid or opp.yes_ask)
        else "n/a"
    )
    no_bid_ask = (
        f"{price3(opp.no_bid)}/{price3(opp.no_ask)}"
        if (opp.no_bid or opp.no_ask)
        else "n/a"
    )

    yes_question_line = (
        f"\n  ❓ <i>{h(truncate(opp.yes_question, 120))}</i>" if opp.yes_question else ""
    )
    no_question_line = (
        f"\n  ❓ <i>{h(truncate(opp.no_question, 120))}</i>" if opp.no_question else ""
    )

    gross_cents = round(float(opp.gross_edge or 0) * 100, 1)
    fee_cents = round(float(opp.total_fees or 0) * 100, 1)
    net_cents = round(float(opp.net_edge_cents or 0), 1)

    return (
        f"\U0001f4b0 <b>ARBITRAGE FOUND</b>\n"
        f"{DIVIDER}\n"
        f"\U0001f3af {h(truncate(outcome_header, 80))}\n"
        f"<code>{h(opp.canonical_id)}</code>\n"
        f"\n"
        f"\U0001f7e2 <b>{opp.yes_platform.upper()}</b>: BUY <b>YES</b> @ <code>{price3(opp.yes_price)}</code> ({fmt_age(yes_age)} old)\n"
        f"  ├ Market: <code>{h(yes_id)}</code>\n"
        f"  └ Bid/Ask: {yes_bid_ask}"
        f"{yes_question_line}\n"
        f"\n"
        f"\U0001f535 <b>{opp.no_platform.upper()}</b>: BUY <b>NO</b> @ <code>{price3(opp.no_price)}</code> ({fmt_age(no_age)} old)\n"
        f"  ├ Market: <code>{h(no_id)}</code>\n"
        f"  └ Bid/Ask: {no_bid_ask}"
        f"{no_question_line}\n"
        f"\n"
        f"{DIVIDER}\n"
        f"\U0001f4c8 <b>Edge:</b> {gross_cents:.1f}¢ gross → <b>{net_cents:.1f}¢ net</b> (fees: {fee_cents:.1f}¢)\n"
        f"\U0001f4e6 <b>Qty:</b> <code>{opp.suggested_qty}</code>  |  \U0001f4b5 <b>Profit:</b> <code>{usd(opp.max_profit_usd)}</code>\n"
        f"\U0001f3af Confidence: <code>{round(float(opp.confidence or 0) * 100):.0f}%</code>  |  "
        f"Score: <code>{round(float(opp.mapping_score or 0), 2):.2f}</code>\n"
        f"{DIVIDER}\n"
        f"⚠️ <i>Verify both legs target the SAME outcome before trading.</i>"
    )


@dataclass
class BalanceSnapshot:
    platform: str
    balance: float
    timestamp: float
    is_low: bool = False
    # Optional source descriptor — e.g. "polymarket-us:/account/balances" or
    # "polymarket:funder-erc20" — so the dashboard can tell which surface the
    # number actually came from. Stays "" for legacy callers.
    source: str = ""
    # When True, this snapshot is a re-served *last-known-good* value because
    # the most recent live fetch failed (circuit open / gateway down). The
    # ``timestamp`` then refers to when the value was last KNOWN GOOD, not when
    # it was re-served, so the dashboard can age it honestly. ``error`` carries
    # the reason the live fetch failed so operators see "stale, last-known $X,
    # reason: <…>" instead of a bare null. Defaults keep legacy callers intact.
    stale: bool = False
    error: str = ""


@dataclass
class OpportunityAlertRecord:
    alert_id: str
    canonical_id: str
    state: str
    reason: str
    yes_platform: str
    no_platform: str
    net_edge_cents: float
    expected_profit_usd: float
    quantity: int
    timestamp: float
    execution_queue: str = ""

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "canonical_id": self.canonical_id,
            "state": self.state,
            "reason": self.reason,
            "yes_platform": self.yes_platform,
            "no_platform": self.no_platform,
            "net_edge_cents": round(self.net_edge_cents, 4),
            "expected_profit_usd": round(self.expected_profit_usd, 4),
            "quantity": self.quantity,
            "timestamp": self.timestamp,
            "execution_queue": self.execution_queue,
        }


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
        burst_window_sec: float = 10.0,
        burst_max: int = 5,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None
        self._enabled = bool(bot_token and chat_id)
        self._dedup_window_sec = max(0.0, float(dedup_window_sec))
        self._max_retries = max(1, int(max_retries))
        self._last_sent: Dict[str, float] = {}
        # Global burst guard — caps total sends in a rolling window so a
        # multi-source cascade (kill switch + stranded reconciler + critical
        # incidents firing in the same second) cannot flood the chat. The
        # per-key dedup above only protects against the SAME alert repeating;
        # this is the cross-key ceiling that backstops it.
        self._burst_window_sec = max(0.0, float(burst_window_sec))
        self._burst_max = max(1, int(burst_max))
        self._burst_history: list[float] = []
        self._burst_dropped: int = 0

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
        bypass_burst: bool = False,
    ) -> bool:
        """Send a Telegram message with retry + optional dedup.

        ``bypass_burst``: skip the global burst-guard cap for this send.
        Reserved for alert classes that are already deduplicated upstream
        (one row per distinct condition, never a repeat) where the burst
        guard's own purpose — protecting against a *repeat* alert storm —
        doesn't apply, and silently dropping them defeats their point.
        2026-07-15: a cold-start stranded-position sweep emitting all N
        distinct positions at once was 4-of-8 burst-dropped exactly when
        the operator most needed the full picture. See
        ``StrandedPositionReconciler`` / ``ExecutionEngine.record_incident``
        (``event_type == "stranded_position"``).

        Returns True on HTTP 200 from Telegram, False on any other outcome
        (disabled, deduped, retries exhausted, non-200 response).
        """
        if not self._enabled:
            logger.debug(f"Telegram disabled, would send: {message[:80]}...")
            return False

        if self._is_duplicate(dedup_key):
            logger.debug(f"Telegram deduped (key={dedup_key!r}): {message[:80]}...")
            return False

        # Global burst guard: drop the send (after dedup) when the rolling
        # window has already saturated. Prevents a cross-source storm from
        # turning the channel into noise during an incident cascade.
        if self._burst_window_sec > 0 and not bypass_burst:
            now_burst = time.time()
            cutoff = now_burst - self._burst_window_sec
            self._burst_history = [t for t in self._burst_history if t >= cutoff]
            if len(self._burst_history) >= self._burst_max:
                self._burst_dropped += 1
                logger.warning(
                    "Telegram burst-dropped (%d in %.0fs, total dropped=%d): %s",
                    len(self._burst_history), self._burst_window_sec,
                    self._burst_dropped, message[:80],
                )
                return False
            self._burst_history.append(now_burst)

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
                    if (
                        resp.status == 400
                        and parse_mode
                        and "can't parse entities" in text.lower()
                    ):
                        fallback_payload = {
                            "chat_id": self.chat_id,
                            "text": message,
                        }
                        logger.warning("Telegram HTML parse failed; retrying without parse_mode")
                        async with session.post(url, json=fallback_payload) as fallback_resp:
                            if fallback_resp.status == 200:
                                logger.debug("Telegram plain-text fallback sent")
                                return True
                            fallback_text = await fallback_resp.text()
                            logger.warning(
                                "Telegram plain-text fallback failed %s: %s",
                                fallback_resp.status, fallback_text[:200],
                            )
                            return False
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
            "forecastex": getattr(config, "forecastex_low", 50.0),
        }
        # Manual balance overrides (for platforms without balance API)
        self._manual_balances: Dict[str, float] = {}
        # Per-platform last-error message + timestamp so the dashboard can tell
        # operators *why* a balance number looks stale. None = no recent error.
        self._last_errors: Dict[str, dict] = {}
        # Per-platform last *successful* (known-good) snapshot. When a live
        # fetch fails (e.g. ForecastEx IBKR gateway SSO expired → circuit open),
        # we re-serve this value flagged stale + with the failure reason rather
        # than collapsing the platform to a bare null. A null balance makes the
        # platform vanish from current_balances entirely, which trips readiness
        # ("no fresh quotes") and blinds operators. Survives only in-memory —
        # cleared on restart, which is the conservative default (no balance is
        # better than a balance we can't vouch for after a cold start).
        self._last_good: Dict[str, BalanceSnapshot] = {}
        # Serialize check_balances() calls so concurrent /api/balances requests
        # with force_refresh=1 don't hammer the platform APIs.
        self._refresh_lock = asyncio.Lock()
        # 2026-05-23 audit finding #5: global burst guard on execution-result
        # alerts. A cascade (e.g. the DEM_HOUSE_2026 incident: 22 trades in
        # 15 minutes) would otherwise spam Telegram with one alert per leg,
        # making the real signal unreadable. Keep the rolling window of recent
        # send timestamps; reject when the count in the last EXEC_ALERT_WINDOW_S
        # exceeds EXEC_ALERT_MAX_BURST.
        self._exec_alert_window_s: float = 10.0
        self._exec_alert_max_burst: int = 5
        self._exec_alert_history: "list[float]" = []
        self._exec_alert_lock = asyncio.Lock()
        self._exec_alert_dropped: int = 0
        # Recent opportunity alert state. This gives the API a visible
        # alert-to-execution breadcrumb instead of "Telegram sent and vanished".
        self._opportunity_alerts: "deque[OpportunityAlertRecord]" = deque(maxlen=500)
        # Auto-execution must be downstream of approved alerts, not a sibling
        # subscriber of raw scanner output. Only alert_opportunity() writes here,
        # after the safety gate passes and Telegram send succeeds.
        self._approved_opportunities: asyncio.Queue = asyncio.Queue()

    def set_manual_balance(self, platform: str, balance: float):
        """Set balance manually for platforms without API."""
        self._manual_balances[platform] = balance
        logger.info(f"Manual balance set: {platform} = ${balance:.2f}")

    async def check_balances(self) -> Dict[str, BalanceSnapshot]:
        """Fetch balances from all platforms.

        Records the per-platform last error (with timestamp) on failure so
        operators can see *why* a displayed balance is stale instead of just
        watching the age tick up silently.
        """
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
                        source=getattr(collector, "balance_source", ""),
                    )
                    snapshots[platform] = snap
                    self._balances[platform] = snap
                    # Remember this as the last KNOWN-GOOD value so a future
                    # failed fetch can degrade to it instead of a bare null.
                    self._last_good[platform] = snap
                    # Successful fetch clears any prior error
                    self._last_errors.pop(platform, None)

                    # Send alert if low and cooldown elapsed
                    if is_low:
                        await self._maybe_alert_low_balance(platform, balance, threshold)
                else:
                    # fetch_balance() returned None — collector swallowed the
                    # error or has no credentials; surface that to the UI.
                    self._record_failure(
                        platform,
                        "balance unavailable (no credentials or fetch returned None)",
                    )

            except Exception as e:
                logger.error(f"Balance check error for {platform}: {e}")
                self._record_failure(platform, str(e))

        return snapshots

    def _record_failure(self, platform: str, message: str) -> None:
        """Record a failed balance fetch and, when possible, degrade to the
        last-known-good value instead of letting the platform collapse to a
        bare null.

        A null balance is the dangerous state: ``current_balances`` then has no
        entry for the platform, so it disappears from the dashboard, trips
        readiness ("no fresh quotes for X"), and gives operators no number at
        all to reason about. Re-serving the last-known-good value — explicitly
        flagged ``stale`` with the failure reason and the original timestamp —
        is both safer and more honest: the UI shows "stale, last-known $X as of
        <ts>, reason: <…>" and the value ages naturally.
        """
        now = time.time()
        self._last_errors[platform] = {"message": message, "timestamp": now}

        good = self._last_good.get(platform)
        if good is None:
            # Never had a good read this process lifetime — nothing to fall
            # back to. The platform stays absent from current_balances and the
            # error map (above) carries the reason for the UI's null block.
            self._balances.pop(platform, None)
            return

        # Re-serve the known-good value, flagged stale, preserving its original
        # timestamp so age_seconds reflects true data age (not re-serve time).
        threshold = self._thresholds.get(platform, 50.0)
        stale_snap = BalanceSnapshot(
            platform=platform,
            balance=good.balance,
            timestamp=good.timestamp,
            is_low=good.balance < threshold,
            source=good.source,
            stale=True,
            error=message,
        )
        self._balances[platform] = stale_snap

    async def refresh_balances(self) -> Dict[str, BalanceSnapshot]:
        """Force a balance refresh, serialised so concurrent callers share a
        single platform hit. Used by the /api/balances?force_refresh=1 path.
        """
        async with self._refresh_lock:
            return await self.check_balances()

    @property
    def last_errors(self) -> Dict[str, dict]:
        """Snapshot of the most recent error per platform (empty if none)."""
        return dict(self._last_errors)

    async def _maybe_alert_low_balance(self, platform: str, balance: float, threshold: float):
        """Send low balance alert if cooldown has elapsed."""
        from ..notifiers.fmt import DIVIDER, usd as fmt_usd

        now = time.time()
        last = self._last_alert_time.get(f"balance_{platform}", 0)
        if now - last < self.config.cooldown:
            return

        self._last_alert_time[f"balance_{platform}"] = now
        msg = (
            f"\U0001f534 <b>LOW BALANCE</b>\n"
            f"{DIVIDER}\n"
            f"\U0001f3e6 <b>{platform.upper()}</b>\n"
            f"  Balance: <code>{fmt_usd(balance)}</code>\n"
            f"  Threshold: <code>{fmt_usd(threshold)}</code>\n"
            f"{DIVIDER}\n"
            f"⚠️ <i>Fund this account to continue arbitrage operations.</i>"
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
            self._record_opportunity_alert(opp, state="suppressed", reason="alert_gate_rejected")
            return

        now = time.time()
        key = f"arb_{opp.canonical_id}_{opp.yes_platform}_{opp.no_platform}"
        last = self._last_alert_time.get(key, 0)
        if now - last < self.config.cooldown:
            self._record_opportunity_alert(opp, state="suppressed", reason="cooldown")
            return

        self._last_alert_time[key] = now
        msg = _format_arb_alert(opp)
        sent = await self.notifier.send(msg, dedup_key=key)
        if not sent:
            self._record_opportunity_alert(opp, state="send_failed", reason="telegram_send_failed_or_deduped")
            return
        state = "manual_workflow" if opp.requires_manual or opp.status == "manual" else "queued_for_execution"
        queue = "manual" if state == "manual_workflow" else "auto_executor"
        self._record_opportunity_alert(opp, state=state, reason="profitable_alert_sent", execution_queue=queue)
        if state == "queued_for_execution":
            self._approved_opportunities.put_nowait(opp)

    def _record_opportunity_alert(
        self,
        opp: ArbitrageOpportunity,
        *,
        state: str,
        reason: str,
        execution_queue: str = "",
    ) -> None:
        alert_id = (
            f"{opp.canonical_id}:{opp.yes_platform}:{opp.no_platform}:"
            f"{int(getattr(opp, 'timestamp', 0) or time.time())}"
        )
        self._opportunity_alerts.appendleft(
            OpportunityAlertRecord(
                alert_id=alert_id,
                canonical_id=opp.canonical_id,
                state=state,
                reason=reason,
                yes_platform=opp.yes_platform,
                no_platform=opp.no_platform,
                net_edge_cents=float(opp.net_edge_cents or 0.0),
                expected_profit_usd=float(opp.max_profit_usd or 0.0),
                quantity=int(opp.suggested_qty or 0),
                timestamp=time.time(),
                execution_queue=execution_queue,
            )
        )

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
        from ..notifiers.fmt import DIVIDER, h, price3, usd as fmt_usd

        status_config = {
            "filled":  ("\U0001f7e2", "TRADE FILLED"),
            "partial": ("\U0001f7e1", "PARTIAL FILL"),
            "failed":  ("\U0001f534", "TRADE FAILED"),
            "aborted": ("\U0001f534", "TRADE ABORTED"),
            "unwound": ("\U0001f504", "TRADE UNWOUND"),
        }
        emoji, header = status_config.get(status, ("\U0001f4cb", f"TRADE {status.upper()}"))

        pnl_emoji = "\U0001f4c8" if realized_pnl >= 0 else "\U0001f4c9"  # chart up/down

        yes_status = leg_yes.status.value if hasattr(leg_yes.status, "value") else str(leg_yes.status)
        no_status = leg_no.status.value if hasattr(leg_no.status, "value") else str(leg_no.status)

        desc = h((opp.description or "")[:80])
        net_cents = round(float(opp.net_edge_cents or 0), 1)

        msg = (
            f"{emoji} <b>{header}</b>\n"
            f"{DIVIDER}\n"
            f"\U0001f3af {desc}\n"
            f"<code>{h(arb_id)}</code>\n"
            f"\n"
            f"\U0001f7e2 <b>{opp.yes_platform.upper()} YES</b>\n"
            f"  Limit: <code>{price3(leg_yes.price)}</code> → "
            f"Fill: <code>{price3(leg_yes.fill_price)}</code> x{leg_yes.fill_qty}\n"
            f"  Status: <code>{h(yes_status)}</code>\n"
            f"\n"
            f"\U0001f535 <b>{opp.no_platform.upper()} NO</b>\n"
            f"  Limit: <code>{price3(leg_no.price)}</code> → "
            f"Fill: <code>{price3(leg_no.fill_price)}</code> x{leg_no.fill_qty}\n"
            f"  Status: <code>{h(no_status)}</code>\n"
            f"\n"
            f"{DIVIDER}\n"
            f"\U0001f4c8 Edge: <code>{net_cents:.1f}c</code> net  |  Qty: <code>{opp.suggested_qty}</code>\n"
            f"{pnl_emoji} <b>P&amp;L: <code>{fmt_usd(realized_pnl, signed=True)}</code></b>"
        )

        if leg_yes.error:
            msg += f"\n⚠️ YES error: <i>{h(str(leg_yes.error)[:100])}</i>"
        if leg_no.error:
            msg += f"\n⚠️ NO error: <i>{h(str(leg_no.error)[:100])}</i>"

        # Burst guard: drop alerts when more than _exec_alert_max_burst
        # fired in the last _exec_alert_window_s seconds. Critical alerts
        # (failed / unwound) still pass through so an operator never misses
        # the bad path even during a cascade.
        critical_statuses = {"failed", "aborted", "unwound", "recovering"}
        is_critical = status in critical_statuses or realized_pnl < 0
        async with self._exec_alert_lock:
            now_ts = time.time()
            cutoff = now_ts - self._exec_alert_window_s
            self._exec_alert_history = [
                t for t in self._exec_alert_history if t >= cutoff
            ]
            if (
                not is_critical
                and len(self._exec_alert_history) >= self._exec_alert_max_burst
            ):
                self._exec_alert_dropped += 1
                logger.info(
                    "Execution alert burst-dropped for %s (%d in window, total dropped=%d)",
                    arb_id, len(self._exec_alert_history),
                    self._exec_alert_dropped,
                )
                return
            self._exec_alert_history.append(now_ts)

        await self.notifier.send(msg, dedup_key=f"exec_{arb_id}")
        logger.info("Execution alert sent for %s: %s pnl=$%.2f", arb_id, status, realized_pnl)

    async def send_daily_summary(self):
        """Send daily summary of balances and activity."""
        from ..notifiers.fmt import DIVIDER, usd as fmt_usd

        lines = [
            f"\U0001f4ca <b>ARBITER DAILY SUMMARY</b>",
            DIVIDER,
            f"\U0001f3e6 <b>Balances</b>",
        ]

        total = 0.0
        for plat, snap in self._balances.items():
            icon = "\U0001f534" if snap.is_low else "\U0001f7e2"
            lines.append(f"  {icon} {plat.upper()}: <code>{fmt_usd(snap.balance)}</code>")
            total += snap.balance

        lines.append(f"\n\U0001f4b0 <b>Total:</b> <code>{fmt_usd(total)}</code>")
        lines.append(DIVIDER)
        await self.notifier.send("\n".join(lines))

    @property
    def current_balances(self) -> Dict[str, BalanceSnapshot]:
        return dict(self._balances)

    @property
    def opportunity_alerts(self) -> List[dict]:
        return [record.to_dict() for record in self._opportunity_alerts]

    @property
    def approved_opportunity_queue(self) -> asyncio.Queue:
        return self._approved_opportunities

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
