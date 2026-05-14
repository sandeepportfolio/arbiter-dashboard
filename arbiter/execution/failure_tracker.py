"""Track per-(market, side, price-range) failure patterns and back off the
hot ones so a stuck book doesn't get hammered with rejections.

The retry-scheduler already has a (market, side, price_cents) cooldown that
suppresses *immediate* re-submission of the exact same losing tuple. This
module is the coarser, longer-horizon companion: any tuple that fails 3+
times within the trailing hour gets blocked from new submissions until the
clock-out (default 30 min), regardless of which path produced the failure.

Keys:
    (canonical_id, side, price_bucket_cents)
where ``price_bucket_cents = round(price * 100 / 5) * 5`` — 5-cent buckets
so 0.42 and 0.43 share a record but 0.42 and 0.48 don't.

The tracker is purely in-memory. It is NOT a substitute for the durable
RetryScheduler cooldown or for SafetySupervisor — it's a soft de-prioritizer
on top of those.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from .engine import ArbExecution, OrderStatus

logger = logging.getLogger("arbiter.execution.failure_tracker")


FailureKey = Tuple[str, str, int]


def _price_bucket_cents(price: float, *, bucket_size: int = 5) -> int:
    """Bucket a price (probability in [0,1]) to the nearest ``bucket_size`` cents.

    ``bucket_size=5`` collapses 0.40–0.44 into one bucket, 0.45–0.49 into
    the next, etc. Negative or zero bucket sizes are clamped to 1 so the
    function never divides by zero.
    """
    bucket = max(int(bucket_size), 1)
    cents = int(round(float(price or 0.0) * 100.0))
    return (cents // bucket) * bucket


@dataclass
class FailurePatternStats:
    recorded: int = 0
    queries: int = 0
    backoff_blocks: int = 0
    last_recorded_ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recorded": self.recorded,
            "queries": self.queries,
            "backoff_blocks": self.backoff_blocks,
            "last_recorded_ts": self.last_recorded_ts,
        }


@dataclass
class FailureTrackerConfig:
    window_seconds: float = 3600.0  # 1 hour
    backoff_threshold: int = 3  # 3+ failures in the window → block
    backoff_seconds: float = 1800.0  # 30-minute cool-off after triggering
    bucket_size_cents: int = 5
    # Keep at most this many timestamps per key; older entries are dropped.
    max_samples_per_key: int = 64


class FailureTracker:
    """Sliding-window counter of failures keyed by (market, side, price-bucket).

    Subscribe to ``ExecutionEngine`` via ``engine.subscribe()`` and feed
    every emitted ``ArbExecution`` through ``record_execution``. Auto- and
    manual-execution paths can then call ``should_skip(canonical_id, side,
    price)`` before submitting a new attempt.
    """

    def __init__(
        self,
        *,
        engine=None,
        config: Optional[FailureTrackerConfig] = None,
    ) -> None:
        self._engine = engine
        self._config = config or FailureTrackerConfig()
        self._history: Dict[FailureKey, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self._config.max_samples_per_key)
        )
        # When set, the key is blocked from new submissions until the
        # mapped timestamp. Cleared lazily inside ``should_skip``.
        self._backoff_until: Dict[FailureKey, float] = {}
        self._queue = None
        self._task = None
        self._running = False
        self.stats = FailurePatternStats()

    # ─── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        import asyncio

        if self._running:
            return
        if self._engine is None:
            logger.info("failure_tracker.start: no engine wired, running passive")
            self._running = True
            return
        self._queue = self._engine.subscribe()
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="failure_tracker")
        logger.info(
            "failure_tracker.started window_s=%.0f threshold=%d backoff_s=%.0f",
            self._config.window_seconds,
            self._config.backoff_threshold,
            self._config.backoff_seconds,
        )

    async def stop(self) -> None:
        import asyncio

        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        import asyncio

        while self._running:
            try:
                execution = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                self.record_execution(execution)
            except Exception as exc:  # noqa: BLE001 — loop must not die
                logger.error("failure_tracker.loop.unexpected_error: %s", exc)

    # ─── recording ────────────────────────────────────────────────

    def record_execution(self, execution: ArbExecution) -> None:
        """Record a failure if the execution failed; no-op otherwise.

        ``recovering`` is treated as a failure for tracking purposes (one
        leg filled, the other unwound — that book is unhealthy even if the
        unwind happened to land profitable).
        """
        status = (execution.status or "").lower()
        if status not in {"failed", "recovering"}:
            return

        canonical = execution.opportunity.canonical_id if execution.opportunity else ""
        if not canonical:
            return

        now = time.time()
        # Record for whichever leg actually failed (or both, if the engine
        # marked both terminal-failed).
        terminal_failed = {OrderStatus.FAILED, OrderStatus.CANCELLED, OrderStatus.ABORTED}
        for leg, side in (
            (execution.leg_yes, "yes"),
            (execution.leg_no, "no"),
        ):
            if leg is None:
                continue
            if leg.status not in terminal_failed:
                # Only the filled leg in a "recovering" arb shouldn't count
                # — its presence is part of the signal, not the failure.
                continue
            self._record_failure(canonical, side, float(leg.price), now)

    def _record_failure(
        self, canonical_id: str, side: str, price: float, now: Optional[float] = None,
    ) -> None:
        side_norm = (side or "").lower()
        if side_norm not in {"yes", "no"}:
            return
        ts = float(now if now is not None else time.time())
        bucket = _price_bucket_cents(price, bucket_size=self._config.bucket_size_cents)
        key: FailureKey = (canonical_id, side_norm, bucket)
        history = self._history[key]
        history.append(ts)
        self.stats.recorded += 1
        self.stats.last_recorded_ts = ts
        # If we've crossed the threshold inside the trailing window, trip
        # the backoff cool-off. Otherwise leave it alone — should_skip
        # recomputes on each query so a clock-out always resolves.
        if self._failures_in_window(history, ts) >= self._config.backoff_threshold:
            self._backoff_until[key] = ts + self._config.backoff_seconds
            logger.info(
                "failure_tracker.backoff.armed canonical=%s side=%s bucket=%dc until=%s",
                canonical_id, side_norm, bucket,
                int(self._backoff_until[key]),
            )

    def _failures_in_window(self, history: Deque[float], now: float) -> int:
        cutoff = now - self._config.window_seconds
        # Prune from the left (deque is append-only oldest-first).
        while history and history[0] < cutoff:
            history.popleft()
        return len(history)

    # ─── querying ─────────────────────────────────────────────────

    def should_skip(
        self, canonical_id: str, side: str, price: float,
    ) -> Tuple[bool, str]:
        """Return (skip, reason). ``skip=True`` means the caller should
        suppress this submission; ``reason`` is operator-readable.
        """
        self.stats.queries += 1
        side_norm = (side or "").lower()
        if not canonical_id or side_norm not in {"yes", "no"}:
            return False, ""
        bucket = _price_bucket_cents(price, bucket_size=self._config.bucket_size_cents)
        key: FailureKey = (canonical_id, side_norm, bucket)
        now = time.time()

        # Active cool-off short-circuits.
        until = self._backoff_until.get(key)
        if until is not None:
            if until <= now:
                self._backoff_until.pop(key, None)
            else:
                self.stats.backoff_blocks += 1
                return True, (
                    f"failure-pattern backoff active "
                    f"({int(until - now)}s remaining; "
                    f"{self._config.backoff_threshold}+ failures in last "
                    f"{int(self._config.window_seconds / 60)}m)"
                )

        # Live threshold check — covers the case where ``record_execution``
        # crossed threshold but the cool-off entry expired between calls.
        history = self._history.get(key)
        if history is None:
            return False, ""
        n = self._failures_in_window(history, now)
        if n >= self._config.backoff_threshold:
            self._backoff_until[key] = now + self._config.backoff_seconds
            self.stats.backoff_blocks += 1
            return True, (
                f"failure-pattern backoff: {n} failures in trailing "
                f"{int(self._config.window_seconds / 60)}m"
            )
        return False, ""

    # ─── reporting ────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serialisable summary of currently-tracked patterns
        for /api/metrics and dashboards. Only keys with at least one failure
        in the trailing window are included so the payload stays bounded.
        """
        now = time.time()
        patterns: List[Dict[str, Any]] = []
        for key, history in self._history.items():
            n = self._failures_in_window(history, now)
            if n == 0:
                continue
            until = self._backoff_until.get(key, 0.0)
            patterns.append(
                {
                    "canonical_id": key[0],
                    "side": key[1],
                    "price_bucket_cents": key[2],
                    "failures_in_window": n,
                    "last_failure_ts": history[-1] if history else 0.0,
                    "backoff_until_ts": until if until > now else 0.0,
                }
            )
        patterns.sort(key=lambda p: p["failures_in_window"], reverse=True)
        return {
            "config": {
                "window_seconds": self._config.window_seconds,
                "backoff_threshold": self._config.backoff_threshold,
                "backoff_seconds": self._config.backoff_seconds,
                "bucket_size_cents": self._config.bucket_size_cents,
            },
            "stats": self.stats.to_dict(),
            "patterns": patterns,
        }


def make_failure_tracker_from_env(
    *, engine=None, config_env: Optional[Dict[str, str]] = None,
) -> FailureTracker:
    """Read ``FAILURE_TRACKER_*`` env vars; sensible defaults for the rest."""
    env = config_env or {}

    def _float(name: str, default: float) -> float:
        raw = env.get(name)
        try:
            return float(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    def _int(name: str, default: int) -> int:
        raw = env.get(name)
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    cfg = FailureTrackerConfig(
        window_seconds=_float("FAILURE_TRACKER_WINDOW_SECONDS", 3600.0),
        backoff_threshold=_int("FAILURE_TRACKER_BACKOFF_THRESHOLD", 3),
        backoff_seconds=_float("FAILURE_TRACKER_BACKOFF_SECONDS", 1800.0),
        bucket_size_cents=_int("FAILURE_TRACKER_BUCKET_CENTS", 5),
    )
    return FailureTracker(engine=engine, config=cfg)
