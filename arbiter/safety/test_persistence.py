"""Wave-0 test stubs for SafetyEventStore + optional Redis shim."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

try:
    from arbiter.safety.persistence import RedisStateShim, SafetyEventStore  # type: ignore
except Exception:  # pragma: no cover
    RedisStateShim = None  # type: ignore
    SafetyEventStore = None  # type: ignore


@pytest.mark.skip(reason="requires live Postgres fixture or pool monkeypatch")
async def test_insert_safety_event_writes_row():
    # Integration-style — exercises asyncpg pool. Keep skipped until
    # a mocking harness is wired (Task 1 monkeypatches _pool in its own suite).
    pass


async def test_redis_optional_no_op_when_client_none():
    shim = RedisStateShim(redis_client=None)
    # No exception even without a client:
    await shim.set_armed(True)
    armed = await shim.is_armed()
    assert armed is False


async def test_safety_event_store_none_pool_is_noop():
    store = SafetyEventStore(pool=None)
    # Should not raise — return early with a warning
    await store.insert_safety_event(
        event_type="arm",
        actor="operator:test",
        reason="unit-test",
        state={"armed": True},
        cancelled_counts={},
    )
