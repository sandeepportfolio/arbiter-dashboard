"""SafetySupervisor — kill-switch state machine and execution gate (SAFE-01).

The supervisor is the single authorized path that gates the execution engine.
Once armed, ``allow_execution`` denies every opportunity until an operator
resets via POST /api/kill-switch (subject to a cooldown).

Invariants:
- All state transitions are serialized through ``self._state_lock`` so a
  burst of concurrent arm/reset calls cannot double-cancel or double-publish.
- Telegram and Postgres failures are swallowed so they never abort a trip.
- Adapter ``cancel_all`` calls run in parallel under ``asyncio.gather`` with a
  per-adapter 5s timeout; exceptions are logged, not raised.

See .planning/phases/03-safety-layer/03-RESEARCH.md Pattern 1 and
03-PATTERNS.md §arbiter/safety/supervisor.py for analogs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from structlog.contextvars import bind_contextvars, clear_contextvars

from ..config.settings import SafetyConfig
from .alerts import SafetyAlertTemplates

if TYPE_CHECKING:  # pragma: no cover
    from ..execution.adapters.base import PlatformAdapter
    from ..execution.engine import ExecutionEngine
    from ..execution.store import ExecutionStore
    from ..monitor.balance import TelegramNotifier
    from .persistence import RedisStateShim, SafetyEventStore

logger = logging.getLogger("arbiter.safety.supervisor")


@dataclass
class SafetyState:
    """Serializable kill-switch state snapshot."""

    armed: bool = False
    armed_by: Optional[str] = None
    armed_at: float = 0.0
    armed_reason: str = ""
    cooldown_until: float = 0.0
    last_reset_at: float = 0.0
    last_reset_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "armed": self.armed,
            "armed_by": self.armed_by,
            "armed_at": self.armed_at,
            "armed_reason": self.armed_reason,
            "cooldown_until": self.cooldown_until,
            "cooldown_remaining": max(self.cooldown_until - now, 0.0),
            "last_reset_at": self.last_reset_at,
            "last_reset_by": self.last_reset_by,
        }


class SafetySupervisor:
    """Owns the kill-switch state machine and gates the ExecutionEngine."""

    def __init__(
        self,
        config: SafetyConfig,
        engine: "ExecutionEngine",
        adapters: Dict[str, "PlatformAdapter"],
        notifier: "TelegramNotifier",
        redis: Optional["RedisStateShim"] = None,
        store: Optional["ExecutionStore"] = None,
        safety_store: Optional["SafetyEventStore"] = None,
    ):
        self.config = config
        self.engine = engine
        self.adapters = dict(adapters or {})
        self.notifier = notifier
        self.redis = redis
        self.store = store
        self._safety_store = safety_store
        self._state = SafetyState()
        self._state_lock = asyncio.Lock()
        self._subscribers: List[asyncio.Queue] = []

    # ─── pub/sub fanout ─────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        return queue

    async def _publish(self, event: Dict[str, Any]) -> None:
        for subscriber in list(self._subscribers):
            try:
                subscriber.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("safety.supervisor: skipping slow subscriber")

    # ─── trade gate ─────────────────────────────────────────────────────

    async def allow_execution(
        self, opportunity: Any
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Engine trade-gate contract (matches ``ExecutionEngine._check_trade_gate``).

        Returns ``(allowed, reason, context_dict)``. When armed, denial reason
        is ``"Kill switch armed: <reason>"`` and context contains the full
        SafetyState.to_dict() for downstream logging/incidents.
        """
        if self._state.armed:
            return (
                False,
                f"Kill switch armed: {self._state.armed_reason or 'manual'}",
                self._state.to_dict(),
            )
        return True, "safety supervisor approved", {"kill_switch": False}

    # ─── trip_kill / reset_kill ─────────────────────────────────────────

    async def trip_kill(self, by: str, reason: str) -> SafetyState:
        """Arm the kill switch, cancel every open order, audit, publish.

        Idempotent: concurrent callers serialize on ``self._state_lock``; if
        the switch is already armed, this is a no-op (returns current state
        without re-cancelling or re-broadcasting).
        """
        clear_contextvars()
        bind_contextvars(event="safety.trip_kill", actor=by)
        try:
            async with self._state_lock:
                if self._state.armed:
                    logger.info(
                        "safety.supervisor: trip_kill no-op; already armed (by=%s)",
                        self._state.armed_by,
                    )
                    return self._state

                now = time.time()
                self._state = SafetyState(
                    armed=True,
                    armed_by=by,
                    armed_at=now,
                    armed_reason=reason,
                    cooldown_until=now + float(self.config.min_cooldown_seconds),
                )

                cancelled_counts = await self._cancel_all_adapters()

                # Telegram send: never propagate failures out of trip_kill.
                try:
                    message = SafetyAlertTemplates.kill_armed(
                        by=by, reason=reason, cancelled_counts=cancelled_counts,
                    )
                    await self.notifier.send(message)
                except Exception as exc:
                    logger.warning(
                        "safety.supervisor: telegram kill_armed send failed: %s", exc,
                    )

                # Optional Redis live state (no-op when disabled).
                if self.redis is not None:
                    try:
                        await self.redis.set_armed(True)
                    except Exception as exc:
                        logger.warning(
                            "safety.supervisor: redis set_armed failed: %s", exc,
                        )

                # Postgres audit INSERT (append-only).
                if self._safety_store is not None:
                    try:
                        await self._safety_store.insert_safety_event(
                            event_type="arm",
                            actor=by,
                            reason=reason,
                            state=self._state.to_dict(),
                            cancelled_counts=cancelled_counts,
                        )
                    except Exception as exc:
                        logger.warning(
                            "safety.supervisor: safety_events insert_arm failed: %s",
                            exc,
                        )

                logger.info(
                    "safety.supervisor: KILL SWITCH ARMED by=%s reason=%s cancelled=%s",
                    by, reason, cancelled_counts,
                )
                await self._publish(
                    {"type": "kill_switch", "payload": self._state.to_dict()}
                )
                return self._state
        finally:
            clear_contextvars()

    async def reset_kill(self, by: str, note: str = "") -> SafetyState:
        """Disarm the kill switch. Respects ``min_cooldown_seconds`` cooldown.

        Raises ``ValueError`` when the cooldown has not elapsed yet.
        """
        clear_contextvars()
        bind_contextvars(event="safety.reset_kill", actor=by)
        try:
            async with self._state_lock:
                now = time.time()
                if self._state.armed and now < self._state.cooldown_until:
                    remaining = self._state.cooldown_until - now
                    raise ValueError(
                        f"Kill switch cooldown: {remaining:.1f}s remaining"
                    )

                self._state = SafetyState(
                    armed=False,
                    armed_by=None,
                    armed_at=0.0,
                    armed_reason="",
                    cooldown_until=0.0,
                    last_reset_at=now,
                    last_reset_by=by,
                )

                try:
                    message = SafetyAlertTemplates.kill_reset(by=by, note=note)
                    await self.notifier.send(message)
                except Exception as exc:
                    logger.warning(
                        "safety.supervisor: telegram kill_reset send failed: %s", exc,
                    )

                if self.redis is not None:
                    try:
                        await self.redis.set_armed(False)
                    except Exception as exc:
                        logger.warning(
                            "safety.supervisor: redis clear_armed failed: %s", exc,
                        )

                if self._safety_store is not None:
                    try:
                        await self._safety_store.insert_safety_event(
                            event_type="reset",
                            actor=by,
                            reason=note or "operator reset",
                            state=self._state.to_dict(),
                            cancelled_counts=None,
                        )
                    except Exception as exc:
                        logger.warning(
                            "safety.supervisor: safety_events insert_reset failed: %s",
                            exc,
                        )

                logger.info(
                    "safety.supervisor: kill switch RESET by=%s note=%s", by, note,
                )
                await self._publish(
                    {"type": "kill_switch", "payload": self._state.to_dict()}
                )
                return self._state
        finally:
            clear_contextvars()

    # ─── internals ──────────────────────────────────────────────────────

    async def _cancel_all_adapters(self) -> Dict[str, int]:
        """Fan out adapter.cancel_all() in parallel with a 5s per-adapter budget.

        Returns ``{platform: cancelled_count}``. Individual adapter failures
        are logged and counted as zero-cancellations — never raised.
        """
        if not self.adapters:
            return {}

        async def _cancel_one(platform: str, adapter: Any) -> Tuple[str, List[str]]:
            try:
                ids = await asyncio.wait_for(adapter.cancel_all(), timeout=5.0)
                if ids is None:
                    ids = []
                return platform, list(ids)
            except asyncio.TimeoutError:
                logger.error(
                    "safety.supervisor: cancel_all timeout platform=%s", platform,
                )
                return platform, []
            except Exception as exc:
                logger.error(
                    "safety.supervisor: cancel_all failed platform=%s err=%s",
                    platform, exc,
                )
                return platform, []

        results = await asyncio.gather(
            *[_cancel_one(p, a) for p, a in self.adapters.items()],
            return_exceptions=False,
        )
        return {platform: len(ids) for platform, ids in results}
