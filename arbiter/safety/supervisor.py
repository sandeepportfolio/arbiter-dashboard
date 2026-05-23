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

    # ─── public accessors (W-5: Plan 05-01) ─────────────────────────────
    #
    # These properties expose the existing private ``_state`` attribute as
    # read-only public surface so callers (dashboard API, live-fire tests)
    # no longer reach into ``supervisor._state.armed`` / ``._state.armed_by``
    # directly. No behavior change; mutation still goes through
    # ``trip_kill`` and ``reset_kill``.

    @property
    def is_armed(self) -> bool:
        """Read-only: True when the kill-switch is armed."""
        return bool(self._state.armed)

    @property
    def armed_by(self) -> Optional[str]:
        """Read-only: actor that armed the kill-switch, or None if not armed."""
        return self._state.armed_by

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

    async def trip_kill(
        self,
        by: str,
        reason: str,
        *,
        persist: bool = True,
    ) -> SafetyState:
        """Arm the kill switch, cancel every open order, audit, publish.

        Idempotent: concurrent callers serialize on ``self._state_lock``; if
        the switch is already armed, this is a no-op (returns current state
        without re-cancelling or re-broadcasting).

        ``persist`` controls whether the armed boolean is written to Redis.
        Pass ``persist=False`` for transient arms that should NOT survive a
        container restart — most notably the SIGTERM-induced
        ``prepare_shutdown`` arm. Persisting that arm caused the 2026-05-21
        audit bug where every deploy came back armed because the previous
        container's graceful-shutdown drain left the Redis key set. Operator
        and incident-driven arms keep the default ``persist=True`` so a real
        kill-switch state survives across restarts as the original design
        intended.
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
                # Phase 6 Plan 06-03: dedup_key prevents a fan-out of kill_armed
                # messages when multiple actors attempt to arm within the dedup
                # window; only the first one sends a Telegram alert.
                try:
                    message = SafetyAlertTemplates.kill_armed(
                        by=by, reason=reason, cancelled_counts=cancelled_counts,
                    )
                    await self.notifier.send(message, dedup_key=f"kill_armed:{by}")
                except Exception as exc:
                    logger.warning(
                        "safety.supervisor: telegram kill_armed send failed: %s", exc,
                    )

                # Optional Redis live state (no-op when disabled).
                # ``persist=False`` skips the write so a transient SIGTERM
                # arm does not leave the next container instance armed.
                if self.redis is not None and persist:
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

                # CRITICAL so the arm event is never buried in INFO noise
                # — operators must be able to find this in container logs
                # without grepping (2026-05-22 audit follow-up).
                logger.critical(
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
                    await self.notifier.send(message, dedup_key=f"kill_reset:{by}")
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

                # Clear the engine's db-write-failed block (C4.2). The flag
                # is set by ExecutionEngine._handle_db_failure on a Postgres
                # write failure and refuses every subsequent order
                # placement; without clearing it here a successful operator
                # reset leaves the engine permanently muted even after the
                # DB recovers. Best-effort: stub engines used in tests may
                # lack the attribute, and a getattr/setattr dance keeps us
                # from coupling the supervisor to ExecutionEngine internals.
                if getattr(self.engine, "_db_write_failed", False):
                    try:
                        self.engine._db_write_failed = False
                        logger.info(
                            "safety.supervisor: cleared engine._db_write_failed on reset",
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning(
                            "safety.supervisor: clearing _db_write_failed failed: %s",
                            exc,
                        )

                logger.warning(
                    "safety.supervisor: KILL SWITCH RESET by=%s note=%s",
                    by, note,
                )
                await self._publish(
                    {"type": "kill_switch", "payload": self._state.to_dict()}
                )
                return self._state
        finally:
            clear_contextvars()

    # ─── restore_from_redis (post-restart kill-switch persistence) ──────

    async def restore_from_redis(self) -> None:
        """Restore armed state from Redis on startup.

        Why: the supervisor boots disarmed; without this, a kill-switch
        armed before a container restart silently re-enables trading on
        the next deploy. ``trip_kill`` writes the boolean to Redis on
        arm, so a True reading here means the previous instance was
        armed and we must come back up armed too.

        Cancels are intentionally skipped (the orders that triggered the
        trip have long since been cancelled or settled); we only need
        ``allow_execution`` to keep refusing trades until an operator
        resets via the dashboard.

        2026-05-22 audit: the previous behavior emitted only a
        ``logger.warning`` and no Telegram alert ("the original arm alert
        already fired" — but that ping is days old by the time anyone reads
        it). When the kill switch was armed by ``system:shutdown`` (every
        SIGTERM does so) the engine silently came back muted on every
        deploy. We now (a) escalate the log to CRITICAL, (b) read the prior
        arm reason from ``safety_events`` so the operator knows whether the
        restore is from a real incident or a stale SIGTERM, (c) send a
        Telegram restart-time alert, and (d) append a ``restore`` row to
        ``safety_events`` so the audit trail does not silently skip the
        restart-time re-arming.
        """
        if self.redis is None:
            return
        try:
            armed = await self.redis.is_armed()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "safety.supervisor: redis is_armed read failed on restore: %s",
                exc,
            )
            return
        if not armed:
            return

        prior_reason = await self._lookup_last_arm_reason()

        async with self._state_lock:
            if self._state.armed:
                return
            now = time.time()
            self._state = SafetyState(
                armed=True,
                armed_by="system:redis_restore",
                armed_at=now,
                armed_reason=(
                    f"kill switch restored from persisted state "
                    f"(prior: {prior_reason or 'unknown'})"
                ),
                cooldown_until=now + float(self.config.min_cooldown_seconds),
            )
            # CRITICAL because a silently-restored kill switch will keep
            # the engine muted across a deploy until an operator notices.
            logger.critical(
                "safety.supervisor: KILL SWITCH RESTORED from Redis on startup "
                "(prior arm reason: %s) — engine refuses trades until reset.",
                prior_reason or "unknown",
            )

            from .alerts import SafetyAlertTemplates  # local import: avoid cycle
            try:
                await self.notifier.send(
                    SafetyAlertTemplates.kill_restored_from_redis(
                        prior_reason or "unknown",
                    ),
                    dedup_key="kill_restored_from_redis",
                )
            except Exception as exc:
                logger.warning(
                    "safety.supervisor: telegram kill_restored send failed: %s",
                    exc,
                )

            if self._safety_store is not None:
                try:
                    await self._safety_store.insert_safety_event(
                        event_type="restore",
                        actor="system:redis_restore",
                        reason=(
                            f"Restored armed state on startup; prior arm: "
                            f"{prior_reason or 'unknown'}"
                        ),
                        state=self._state.to_dict(),
                        cancelled_counts=None,
                    )
                except Exception as exc:
                    logger.warning(
                        "safety.supervisor: safety_events insert_restore failed: %s",
                        exc,
                    )

            await self._publish(
                {"type": "kill_switch", "payload": self._state.to_dict()}
            )

    async def _lookup_last_arm_reason(self) -> Optional[str]:
        """Best-effort lookup of the most recent ``arm`` event reason.

        Used by ``restore_from_redis`` to tell the operator *why* the prior
        instance left the switch armed. Returns ``None`` when the safety
        event store is unavailable or the lookup fails — never raises so
        startup recovery cannot be aborted by a Postgres hiccup.
        """
        if self._safety_store is None:
            return None
        try:
            events = await self._safety_store.list_events(limit=10, offset=0)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "safety.supervisor: lookup_last_arm_reason failed: %s", exc,
            )
            return None
        for event in events or []:
            if event.get("event_type") == "arm":
                return (
                    f"{event.get('reason', '') or 'unknown'} "
                    f"(armed_by={event.get('actor', '') or 'unknown'})"
                ).strip()
        return None

    # ─── prepare_shutdown (SAFE-05, plan 03-05) ─────────────────────────

    async def prepare_shutdown(self) -> None:
        """Broadcast ``shutdown_state`` BEFORE trip_kill so the dashboard
        learns of the impending shutdown before adapters start cancelling.

        Sequence:
          1. publish ``{"type":"shutdown_state","payload":{"phase":"shutting_down",...}}``
          2. ``trip_kill(by="system:shutdown", ...)`` — fans out cancel_all
          3. publish ``{"type":"shutdown_state","payload":{"phase":"complete",...}}``
             inside a ``finally`` block so the dashboard always sees the
             completion event even if trip_kill raises.

        Idempotent by delegation: ``trip_kill`` serialises on ``_state_lock``
        so multiple concurrent ``prepare_shutdown`` callers (rare — only the
        main-loop signal handler calls this) do not double-cancel.
        """
        await self._publish(
            {
                "type": "shutdown_state",
                "payload": {
                    "phase": "shutting_down",
                    "started_at": time.time(),
                    "reason": "Process shutdown signal",
                },
            }
        )
        try:
            # ``persist=False``: SIGTERM-induced arms are a transient drain
            # mechanism, not a durable kill-switch state. Without this every
            # ``docker restart`` / image redeploy left the next container
            # armed because the previous instance's graceful shutdown wrote
            # ``armed=true`` to Redis on its way out (see 2026-05-22 audit).
            await self.trip_kill(
                by="system:shutdown",
                reason="Process shutdown signal",
                persist=False,
            )
        finally:
            await self._publish(
                {
                    "type": "shutdown_state",
                    "payload": {
                        "phase": "complete",
                        "completed_at": time.time(),
                    },
                }
            )

    # ─── one-leg exposure (SAFE-03, plan 03-03) ─────────────────────────

    async def handle_one_leg_exposure(
        self,
        incident: Any,
        filled_leg: Any,
        failed_leg: Any,
        opp: Any,
    ) -> None:
        """Operator-facing fanout for a naked-position incident.

        Called by ``ExecutionEngine._recover_one_leg_risk`` after it records
        the structured ``one_leg_exposure`` incident. The three notification
        channels are independent:

        1. **Incident queue** — already delivered by the engine via
           ``record_incident``; this method does NOT re-emit to that queue.
        2. **Telegram** — the ``NAKED POSITION`` HTML template, wrapped in a
           try/except so a Telegram outage cannot abort recovery.
        3. **WebSocket** — dedicated ``one_leg_exposure`` pub/sub event that
           ``arbiter/api.py::_broadcast_loop`` consumes; this is separate
           from the generic ``incident`` event so the dashboard can render a
           hero-level banner (plan 03-07 UI).

        Threat model T-3-03-C (DoS): Telegram ``notifier.send`` hangs/raises
        are caught here; the caller (the engine) continues to the cancel-leg
        loop regardless.
        """
        clear_contextvars()
        bind_contextvars(event="safety.one_leg_exposure", actor="engine")
        try:
            meta = getattr(incident, "metadata", {}) or {}
            canonical_id = getattr(opp, "canonical_id", "") or ""
            exposure_usd = float(
                meta.get(
                    "exposure_usd",
                    float(getattr(filled_leg, "fill_qty", 0) or 0)
                    * float(getattr(filled_leg, "fill_price", 0.0) or 0.0),
                )
            )
            unwind_instruction = str(
                meta.get("recommended_unwind", "Close exposure manually")
            )

            # Telegram egress — swallow every failure mode; naked-position
            # recovery MUST NOT abort on notifier problems.
            try:
                message = SafetyAlertTemplates.one_leg_exposure(
                    canonical_id=canonical_id,
                    filled_platform=str(getattr(filled_leg, "platform", "")),
                    filled_side=str(getattr(filled_leg, "side", "")),
                    fill_qty=int(getattr(filled_leg, "fill_qty", 0) or 0),
                    exposure_usd=exposure_usd,
                    unwind_instruction=unwind_instruction,
                )
                await self.notifier.send(
                    message,
                    dedup_key=f"one_leg:{canonical_id}",
                )
            except Exception as exc:
                logger.warning(
                    "safety.supervisor: telegram one_leg_exposure send failed: %s",
                    exc,
                )

            # Build payload for the dedicated WS event. Prefer the incident's
            # to_dict() serialization when available so the dashboard sees the
            # same shape as the generic `incident` event.
            if hasattr(incident, "to_dict") and callable(incident.to_dict):
                try:
                    payload = incident.to_dict()
                except Exception:
                    payload = {
                        "canonical_id": canonical_id,
                        "metadata": dict(meta),
                        "incident_id": getattr(incident, "incident_id", None),
                    }
            else:
                payload = {
                    "canonical_id": canonical_id,
                    "metadata": dict(meta),
                    "incident_id": getattr(incident, "incident_id", None),
                }

            # Ensure canonical_id is populated even if an incident.to_dict()
            # implementation omits it (subscribers — e.g. the plan 03-07
            # hero banner — key off this field).
            if "canonical_id" not in payload or not payload.get("canonical_id"):
                payload["canonical_id"] = canonical_id

            logger.warning(
                "safety.supervisor: one_leg_exposure canonical_id=%s exposure=$%.2f",
                canonical_id,
                exposure_usd,
            )
            await self._publish(
                {"type": "one_leg_exposure", "payload": payload}
            )
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
