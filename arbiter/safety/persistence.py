"""SafetyEventStore + RedisStateShim — append-only audit trail for SAFE-01.

Mirrors ``arbiter/execution/store.py:ExecutionStore.insert_incident`` for the
asyncpg pool contract. The ``safety_events`` table is INSERT-only — no UPDATE
or DELETE methods are exposed (per Phase 3 threat-model T-3-01-D).

Redis is optional; the shim no-ops when ``redis_client`` is ``None`` so a dev
environment without Redis does not error.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("arbiter.safety.persistence")


class SafetyEventStore:
    """Postgres-backed append-only audit store for kill-switch events."""

    def __init__(self, pool: Optional[Any] = None):
        # ``asyncpg.Pool`` — typed as Any to avoid a hard dependency import
        # at module load time.
        self._pool = pool

    async def insert_safety_event(
        self,
        *,
        event_type: str,
        actor: str,
        reason: str,
        state: Dict[str, Any],
        cancelled_counts: Optional[Dict[str, int]] = None,
    ) -> None:
        """Append a single safety event row (INSERT only; never UPDATE/DELETE)."""
        if self._pool is None:
            logger.warning(
                "safety.persistence: no pool configured; skipping insert (type=%s actor=%s)",
                event_type, actor,
            )
            return

        state_json = json.dumps(state or {}, default=str)
        counts_json = json.dumps(cancelled_counts or {}, default=str)
        event_id = f"SE-{uuid.uuid4().hex[:8]}"
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO safety_events (
                    event_id, event_type, actor, reason,
                    state_json, cancelled_counts_json, created_at
                ) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, NOW())
                """,
                event_id,
                event_type,
                actor,
                reason,
                state_json,
                counts_json,
            )

    async def list_events(
        self, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Paginated history for GET /api/safety/events."""
        if self._pool is None:
            return []

        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, event_type, actor, reason,
                       state_json, cancelled_counts_json, created_at
                FROM safety_events
                ORDER BY created_at DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        result: List[Dict[str, Any]] = []
        for row in rows:
            state_raw = row["state_json"]
            counts_raw = row["cancelled_counts_json"]
            try:
                state = (
                    state_raw
                    if isinstance(state_raw, dict)
                    else json.loads(state_raw) if state_raw else {}
                )
            except Exception:
                state = {}
            try:
                counts = (
                    counts_raw
                    if isinstance(counts_raw, dict)
                    else json.loads(counts_raw) if counts_raw else {}
                )
            except Exception:
                counts = {}
            result.append(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "actor": row["actor"],
                    "reason": row["reason"],
                    "state": state,
                    "cancelled_counts": counts,
                    "created_at": (
                        row["created_at"].isoformat()
                        if row["created_at"] is not None
                        else None
                    ),
                }
            )
        return result


class RedisStateShim:
    """Optional Redis mirror of the kill-switch boolean. No-ops when disabled."""

    KEY = "arbiter:kill_switch"

    def __init__(self, redis_client: Optional[Any] = None):
        self._client = redis_client

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def set_armed(self, armed: bool) -> None:
        if self._client is None:
            return
        try:
            if armed:
                await self._client.set(self.KEY, "armed")
            else:
                await self._client.delete(self.KEY)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("safety.persistence: redis set_armed failed: %s", exc)

    async def is_armed(self) -> bool:
        if self._client is None:
            return False
        try:
            value = await self._client.get(self.KEY)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("safety.persistence: redis is_armed failed: %s", exc)
            return False
        if value is None:
            return False
        if isinstance(value, bytes):
            value = value.decode("utf-8", "ignore")
        return str(value).lower() == "armed"
