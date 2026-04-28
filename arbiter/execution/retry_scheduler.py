"""RetryScheduler — investigate and retry failed arbitrage executions.

When ExecutionEngine emits an ArbExecution with status="failed" (both legs
terminal — the safe-to-retry case with no naked exposure), this scheduler:

    1. Classifies the failure (FOK rejected, second-leg skipped, timeout, API
       error, price moved, …) for operator visibility.
    2. Sleeps ``retry_delay_s`` (default 30s) then re-fetches fresh quotes for
       the same canonical market on both platforms via PriceStore.
    3. Recomputes net_edge with venue-aware fees (mirrors the math in
       engine._pre_trade_requote).
    4. If net_edge_cents ≥ ``min_edge_cents_retry`` (default 3¢) it routes the
       requoted opportunity through ``engine.execute_opportunity``.
    5. Up to ``max_retries`` (default 2) attempts. After the final attempt the
       scheduler annotates the *original* ArbExecution.failure_details and
       appends a FailedTradeRecord to ``self.failed_trades`` so /api/executions
       can surface the full retry history to the dashboard "Failed Trades"
       section.

Status filter — IMPORTANT:
    We only retry status == "failed" (both legs reached terminal failed/cancel
    states, no exposure on the books). status == "recovering" (one leg filled,
    naked position) is INTENTIONALLY skipped — that path already triggers
    SafetySupervisor.handle_one_leg_exposure + operator unwind via
    engine._recover_one_leg_risk. Doubling down with another order on a naked
    position would compound exposure rather than capture spread.

Kill-switch:
    SafetySupervisor.is_armed is checked before every retry attempt. An armed
    supervisor immediately marks the chain "retry_exhausted" with reason
    "kill switch armed" — no orders are submitted while the system is tripped.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Deque, Dict, List, Optional, Tuple

import structlog

from ..scanner.arbitrage import ArbitrageOpportunity, compute_fee
from .engine import ArbExecution, OrderStatus

log = structlog.get_logger("arbiter.retry_scheduler")


# ─── Failure classification ──────────────────────────────────────────


def classify_failure(execution: ArbExecution) -> Tuple[str, str, str]:
    """Inspect a failed ArbExecution and return (side, platform, reason).

    The "primary failed leg" is whichever leg reached a terminal failed state
    first (or whichever has the more informative error). The reason is one of
    a small set of operator-friendly strings; falls back to "Platform error"
    when no specific signal is found.
    """
    leg_yes = execution.leg_yes
    leg_no = execution.leg_no

    terminal_failed = {OrderStatus.FAILED, OrderStatus.CANCELLED, OrderStatus.ABORTED}

    # Prefer the leg that actually failed (status terminal_failed) and whose
    # error message is non-empty. If both failed, the YES leg wins as primary
    # for tie-breaking; "Second leg skipped" wording in the NO leg is the
    # signal that the YES leg was the upstream cause.
    if leg_yes.status in terminal_failed and (
        leg_no.status not in terminal_failed
        or "second leg skipped" in (leg_no.error or "").lower()
    ):
        primary = leg_yes
    elif leg_no.status in terminal_failed and (
        leg_yes.status not in terminal_failed
        or "second leg skipped" in (leg_yes.error or "").lower()
    ):
        primary = leg_no
    elif leg_yes.error:
        primary = leg_yes
    else:
        primary = leg_no

    err = (primary.error or "").lower()

    if "fok" in err and ("reject" in err or "insufficient" in err):
        reason = "Insufficient liquidity (FOK rejected)"
    elif "second leg skipped" in err:
        reason = "Second leg skipped (primary leg failed)"
    elif "slippage" in err:
        reason = "Price moved beyond threshold"
    elif "timeout" in err:
        reason = "Local timeout"
    elif (
        "http" in err
        or "connection" in err
        or "503" in err
        or "502" in err
        or "504" in err
        or "reset by peer" in err
    ):
        reason = "Platform API error"
    elif "no adapter" in err:
        reason = "No platform adapter configured"
    else:
        reason = "Platform error"

    return primary.side, primary.platform, reason


# ─── Data model ──────────────────────────────────────────────────────


@dataclass
class FailedTradeRecord:
    """Final disposition of a failed arb after the retry loop completes."""

    arb_id: str
    canonical_id: str
    description: str
    yes_platform: str
    no_platform: str
    failed_leg_side: str  # "yes" | "no"
    failed_leg_platform: str
    failure_reason: str  # classified
    failure_raw: str  # underlying leg.error string
    original_yes_price: float
    original_no_price: float
    original_net_edge_cents: float
    retry_attempts: int = 0  # 0..max_retries
    retry_arb_ids: List[str] = field(default_factory=list)
    retry_edges_cents: List[float] = field(default_factory=list)
    final_disposition: str = "pending"  # retried_success | retry_exhausted | spread_closed
    final_reason: str = ""
    retried_arb_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arb_id": self.arb_id,
            "canonical_id": self.canonical_id,
            "description": self.description,
            "yes_platform": self.yes_platform,
            "no_platform": self.no_platform,
            "failed_leg_side": self.failed_leg_side,
            "failed_leg_platform": self.failed_leg_platform,
            "failure_reason": self.failure_reason,
            "failure_raw": self.failure_raw,
            "original_yes_price": round(self.original_yes_price, 4),
            "original_no_price": round(self.original_no_price, 4),
            "original_net_edge_cents": round(self.original_net_edge_cents, 2),
            "retry_attempts": self.retry_attempts,
            "retry_arb_ids": list(self.retry_arb_ids),
            "retry_edges_cents": [round(e, 2) for e in self.retry_edges_cents],
            "final_disposition": self.final_disposition,
            "final_reason": self.final_reason,
            "retried_arb_id": self.retried_arb_id,
            "timestamp": self.timestamp,
        }


@dataclass
class RetrySchedulerConfig:
    max_retries: int = 2
    retry_delay_s: float = 30.0
    min_edge_cents_retry: float = 3.0
    max_quote_age_seconds: float = 30.0


@dataclass
class RetrySchedulerStats:
    considered: int = 0
    skipped_not_failed: int = 0
    retries_attempted: int = 0
    retried_success: int = 0
    retry_exhausted: int = 0
    spread_closed: int = 0
    last_record_ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "considered": self.considered,
            "skipped_not_failed": self.skipped_not_failed,
            "retries_attempted": self.retries_attempted,
            "retried_success": self.retried_success,
            "retry_exhausted": self.retry_exhausted,
            "spread_closed": self.spread_closed,
            "last_record_ts": self.last_record_ts,
        }


# ─── Scheduler ───────────────────────────────────────────────────────


class RetryScheduler:
    """Subscribe to ExecutionEngine and retry failed arbs with fresh quotes.

    The engine, price_store, and supervisor are duck-typed:
        engine.subscribe()  -> asyncio.Queue[ArbExecution]
        engine.execute_opportunity(opp) -> Awaitable[Optional[ArbExecution]]
        price_store.get(platform, canonical_id) -> Awaitable[Optional[PricePoint]]
        supervisor.is_armed -> bool
    """

    def __init__(
        self,
        *,
        engine,
        price_store,
        supervisor,
        config: Optional[RetrySchedulerConfig] = None,
    ):
        self._engine = engine
        self._price_store = price_store
        self._supervisor = supervisor
        self._config = config or RetrySchedulerConfig()
        # Subscribe up front so we don't miss executions emitted while
        # start() is awaited (mirrors AutoExecutor's pattern).
        self._queue: asyncio.Queue = engine.subscribe()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._failed_trades: Deque[FailedTradeRecord] = deque(maxlen=200)
        self.stats = RetrySchedulerStats()

    # ─── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="retry_scheduler")
        log.info(
            "retry_scheduler.started",
            max_retries=self._config.max_retries,
            retry_delay_s=self._config.retry_delay_s,
            min_edge_cents=self._config.min_edge_cents_retry,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("retry_scheduler.stopped", stats=self.stats.to_dict())

    async def _run_loop(self) -> None:
        while self._running:
            try:
                execution = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._handle_execution(execution)
            except Exception as exc:  # noqa: BLE001 — loop must not die
                log.error("retry_scheduler.loop.unexpected_error", err=str(exc))

    @property
    def failed_trades(self) -> List[FailedTradeRecord]:
        return list(self._failed_trades)

    # ─── core logic ───────────────────────────────────────────────

    async def _handle_execution(
        self, execution: ArbExecution
    ) -> Optional[FailedTradeRecord]:
        self.stats.considered += 1

        # Only "failed" (both legs terminal, no exposure) is retry-safe.
        # "recovering" means one leg filled — a naked position which the
        # engine's recovery path / SafetySupervisor handles separately.
        # All other statuses (filled, submitted, simulated, manual_*) are
        # not failures.
        if execution.status != "failed":
            self.stats.skipped_not_failed += 1
            return None

        opp = execution.opportunity
        side, platform, reason = classify_failure(execution)
        primary_leg = execution.leg_yes if side == "yes" else execution.leg_no
        record = FailedTradeRecord(
            arb_id=execution.arb_id,
            canonical_id=opp.canonical_id,
            description=opp.description,
            yes_platform=opp.yes_platform,
            no_platform=opp.no_platform,
            failed_leg_side=side,
            failed_leg_platform=platform,
            failure_reason=reason,
            failure_raw=primary_leg.error or "",
            original_yes_price=opp.yes_price,
            original_no_price=opp.no_price,
            original_net_edge_cents=opp.net_edge_cents,
        )

        log.info(
            "retry_scheduler.queued",
            arb_id=execution.arb_id,
            canonical_id=opp.canonical_id,
            failure_reason=reason,
            failed_leg=f"{side}:{platform}",
        )

        for attempt in range(1, self._config.max_retries + 1):
            # Kill-switch check before sleep + before submission.
            if self._supervisor_armed():
                record.final_disposition = "retry_exhausted"
                record.final_reason = "kill switch armed — retries aborted"
                self.stats.retry_exhausted += 1
                break

            await asyncio.sleep(self._config.retry_delay_s)

            if self._supervisor_armed():
                record.final_disposition = "retry_exhausted"
                record.final_reason = "kill switch armed — retries aborted"
                self.stats.retry_exhausted += 1
                break

            requoted, requote_reason = await self._requote(opp)
            if requoted is None:
                record.final_disposition = "spread_closed"
                record.final_reason = requote_reason
                self.stats.spread_closed += 1
                break

            edge_cents = float(requoted.net_edge_cents)
            record.retry_edges_cents.append(edge_cents)

            if edge_cents < self._config.min_edge_cents_retry:
                record.final_disposition = "spread_closed"
                record.final_reason = (
                    f"Edge collapsed to {edge_cents:.2f}¢ "
                    f"(retry floor {self._config.min_edge_cents_retry:.2f}¢)"
                )
                self.stats.spread_closed += 1
                break

            self.stats.retries_attempted += 1
            try:
                result = await self._engine.execute_opportunity(requoted)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "retry_scheduler.execute.raised",
                    arb_id=execution.arb_id,
                    attempt=attempt,
                    err=str(exc),
                )
                record.retry_attempts = attempt
                continue

            record.retry_attempts = attempt
            if result is not None:
                retry_arb_id = getattr(result, "arb_id", None)
                if retry_arb_id:
                    record.retry_arb_ids.append(retry_arb_id)
                if getattr(result, "status", "failed") in {
                    "filled",
                    "submitted",
                    "simulated",
                }:
                    record.final_disposition = "retried_success"
                    record.final_reason = (
                        f"Retry {attempt} succeeded with {edge_cents:.2f}¢ edge"
                    )
                    record.retried_arb_id = retry_arb_id
                    self.stats.retried_success += 1
                    break
            # else: result was None (rejected by risk manager / re-quote
            # collapse inside the engine) or status was "failed" — keep trying.
        else:
            # Loop exhausted without break.
            record.final_disposition = "retry_exhausted"
            record.final_reason = (
                f"All {self._config.max_retries} retries failed"
            )
            self.stats.retry_exhausted += 1

        record.timestamp = time.time()
        self.stats.last_record_ts = record.timestamp
        self._failed_trades.appendleft(record)
        # Annotate the original execution so /api/executions surfaces details.
        execution.failure_details = record.to_dict()

        log.info(
            "retry_scheduler.complete",
            arb_id=execution.arb_id,
            disposition=record.final_disposition,
            reason=record.final_reason,
            attempts=record.retry_attempts,
        )
        return record

    # ─── helpers ──────────────────────────────────────────────────

    def _supervisor_armed(self) -> bool:
        try:
            return bool(getattr(self._supervisor, "is_armed", False))
        except Exception:
            return False

    async def _requote(
        self, opp: ArbitrageOpportunity
    ) -> Tuple[Optional[ArbitrageOpportunity], str]:
        """Re-fetch fresh quotes and return a recomputed opportunity.

        Returns (None, reason) if quotes are missing or stale; otherwise
        (new_opp, ""). Mirrors the math in engine._pre_trade_requote so the
        retry path uses the same edge accounting as the live path.
        """
        if self._price_store is None:
            return None, "price store unavailable"

        try:
            current_yes = await self._price_store.get(
                opp.yes_platform, opp.canonical_id
            )
            current_no = await self._price_store.get(
                opp.no_platform, opp.canonical_id
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"price store error: {exc}"

        if not current_yes or not current_no:
            return None, "no fresh quotes available for either leg"

        age = max(
            getattr(current_yes, "age_seconds", 0.0),
            getattr(current_no, "age_seconds", 0.0),
        )
        if age > self._config.max_quote_age_seconds:
            return None, f"quotes too stale ({age:.1f}s)"

        yes_price = float(current_yes.yes_price)
        no_price = float(current_no.no_price)
        gross_edge = 1.0 - yes_price - no_price
        qty = max(int(opp.suggested_qty or 1), 1)
        total_fees = (
            compute_fee(opp.yes_platform, yes_price, qty, getattr(current_yes, "fee_rate", 0.0))
            + compute_fee(opp.no_platform, no_price, qty, getattr(current_no, "fee_rate", 0.0))
        ) / qty
        net_edge = gross_edge - total_fees
        net_edge_cents = net_edge * 100.0

        return (
            replace(
                opp,
                yes_price=yes_price,
                no_price=no_price,
                yes_market_id=getattr(current_yes, "yes_market_id", "") or opp.yes_market_id,
                no_market_id=getattr(current_no, "no_market_id", "") or opp.no_market_id,
                gross_edge=gross_edge,
                total_fees=total_fees,
                net_edge=net_edge,
                net_edge_cents=net_edge_cents,
                quote_age_seconds=age,
                timestamp=time.time(),
                yes_fee_rate=getattr(current_yes, "fee_rate", 0.0),
                no_fee_rate=getattr(current_no, "fee_rate", 0.0),
            ),
            "",
        )


# ─── Factory ─────────────────────────────────────────────────────────


def make_retry_scheduler_from_env(
    *,
    engine,
    price_store,
    supervisor,
    config_env: Dict[str, str],
) -> RetryScheduler:
    """Read RETRY_* env vars and build a RetryScheduler with sensible defaults."""

    def _float(value: Optional[str], default: float) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def _int(value: Optional[str], default: int) -> int:
        try:
            return int(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    cfg = RetrySchedulerConfig(
        max_retries=_int(config_env.get("RETRY_MAX_ATTEMPTS"), 2),
        retry_delay_s=_float(config_env.get("RETRY_DELAY_SECONDS"), 30.0),
        min_edge_cents_retry=_float(config_env.get("RETRY_MIN_EDGE_CENTS"), 3.0),
        max_quote_age_seconds=_float(config_env.get("RETRY_MAX_QUOTE_AGE_SECONDS"), 30.0),
    )
    return RetryScheduler(
        engine=engine,
        price_store=price_store,
        supervisor=supervisor,
        config=cfg,
    )
