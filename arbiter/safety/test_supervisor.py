"""Wave-0 test stubs for SafetySupervisor (SAFE-01).

Task 0 ships these skipped; Task 1 un-skips them as the implementation lands.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

try:  # Imports may fail until Task 1 lands — keep collection stable.
    from arbiter.config.settings import SafetyConfig  # type: ignore
    from arbiter.safety.supervisor import SafetyState, SafetySupervisor  # type: ignore
except Exception:  # pragma: no cover - bootstrap path only
    SafetyConfig = None  # type: ignore
    SafetyState = None  # type: ignore
    SafetySupervisor = None  # type: ignore


def _fake_opp(canonical_id: str = "TEST", suggested_qty: int = 10):
    return SimpleNamespace(
        canonical_id=canonical_id,
        yes_platform="kalshi",
        no_platform="polymarket",
        yes_price=0.55,
        no_price=0.40,
        suggested_qty=suggested_qty,
    )


def _build_supervisor(adapters, notifier, config=None):
    cfg = config or SafetyConfig()
    engine = SimpleNamespace()
    return SafetySupervisor(
        config=cfg,
        engine=engine,
        adapters=adapters,
        notifier=notifier,
        redis=None,
        store=None,
        safety_store=None,
    )


async def test_trip_kill_cancels_all(fake_notifier, fake_adapter_factory):
    adapters = {
        "kalshi": fake_adapter_factory("kalshi", ["k1", "k2", "k3"]),
        "polymarket": fake_adapter_factory("polymarket", ["p1", "p2"]),
    }
    supervisor = _build_supervisor(adapters, fake_notifier)
    state = await asyncio.wait_for(
        supervisor.trip_kill(by="operator:test", reason="manual"),
        timeout=5.0,
    )
    assert state.armed is True
    adapters["kalshi"].cancel_all.assert_awaited_once()
    adapters["polymarket"].cancel_all.assert_awaited_once()


async def test_allow_execution_armed(fake_notifier, fake_adapter_factory):
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    supervisor = _build_supervisor(adapters, fake_notifier)
    await supervisor.trip_kill(by="operator:test", reason="manual")
    allowed, reason, ctx = await supervisor.allow_execution(_fake_opp())
    assert allowed is False
    assert "Kill switch armed" in reason
    assert isinstance(ctx, dict)
    assert ctx.get("armed") is True


async def test_reset_respects_cooldown(fake_notifier, fake_adapter_factory, monkeypatch):
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    cfg = SafetyConfig()
    cfg.min_cooldown_seconds = 30.0
    supervisor = _build_supervisor(adapters, fake_notifier, config=cfg)

    base = time.time()
    monkeypatch.setattr("arbiter.safety.supervisor.time.time", lambda: base)
    await supervisor.trip_kill(by="operator:test", reason="manual")

    with pytest.raises(ValueError) as excinfo:
        await supervisor.reset_kill(by="operator:test", note="too soon")
    assert str(excinfo.value).startswith("Kill switch cooldown")

    monkeypatch.setattr("arbiter.safety.supervisor.time.time", lambda: base + 120.0)
    state = await supervisor.reset_kill(by="operator:test", note="ok")
    assert state.armed is False


async def test_trip_kill_publishes_event(fake_notifier, fake_adapter_factory):
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    supervisor = _build_supervisor(adapters, fake_notifier)
    queue = supervisor.subscribe()
    await supervisor.trip_kill(by="operator:test", reason="manual")
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert isinstance(event, dict)
    assert event.get("type") == "kill_switch"
    assert event.get("payload", {}).get("armed") is True


async def test_concurrent_arm_serializes(fake_notifier, fake_adapter_factory):
    adapters = {"kalshi": fake_adapter_factory("kalshi", ["k1"])}
    supervisor = _build_supervisor(adapters, fake_notifier)
    calls = []

    original = adapters["kalshi"].cancel_all

    async def track_call():
        calls.append("kalshi")
        return await original()

    adapters["kalshi"].cancel_all = track_call

    await asyncio.gather(
        *[supervisor.trip_kill(by=f"op:{i}", reason="concurrent") for i in range(10)]
    )
    assert len(calls) == 1
    # All subsequent trips become no-ops because state is already armed
    assert supervisor._state.armed is True


async def test_telegram_failure_does_not_abort_trip(fake_adapter_factory):
    broken_notifier = AsyncMock()
    broken_notifier.send = AsyncMock(side_effect=RuntimeError("telegram down"))
    adapters = {"kalshi": fake_adapter_factory("kalshi", ["k1"])}
    supervisor = _build_supervisor(adapters, broken_notifier)
    state = await supervisor.trip_kill(by="operator:test", reason="telegram_fails")
    assert state.armed is True


async def test_subscribe_delivers_kill_switch_event(fake_notifier, fake_adapter_factory):
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    supervisor = _build_supervisor(adapters, fake_notifier)
    queue = supervisor.subscribe()
    await supervisor.trip_kill(by="operator:test", reason="subscribe_test")
    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert event["type"] == "kill_switch"


async def test_handle_one_leg_exposure_sends_telegram_and_publishes(
    fake_notifier, fake_adapter_factory,
):
    """Plan 03-03: supervisor.handle_one_leg_exposure must fire the
    NAKED POSITION Telegram template AND publish a dedicated
    one_leg_exposure subscriber event (payload carries canonical_id)."""
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    supervisor = _build_supervisor(adapters, fake_notifier)
    queue = supervisor.subscribe()

    filled_leg = SimpleNamespace(
        platform="kalshi",
        side="yes",
        fill_qty=100,
        fill_price=0.56,
    )
    failed_leg = SimpleNamespace(
        platform="polymarket",
        side="no",
        error="rate_limited",
    )
    incident = SimpleNamespace(
        incident_id="INC-abcd1234",
        metadata={
            "event_type": "one_leg_exposure",
            "filled_platform": "kalshi",
            "filled_side": "yes",
            "filled_qty": 100,
            "filled_price": 0.56,
            "exposure_usd": 56.0,
            "failed_platform": "polymarket",
            "failed_reason": "rate_limited",
            "recommended_unwind": "Sell 100 YES on KALSHI at market",
        },
    )
    opp = SimpleNamespace(canonical_id="MKT1")

    await supervisor.handle_one_leg_exposure(incident, filled_leg, failed_leg, opp)

    # Telegram was sent exactly once with the NAKED POSITION template.
    fake_notifier.send.assert_awaited_once()
    sent_message = fake_notifier.send.await_args.args[0]
    assert "NAKED POSITION" in sent_message

    # Subscribers received a one_leg_exposure event whose payload carries
    # the canonical_id (dashboard uses this for the hero banner in plan 03-07).
    event = queue.get_nowait()
    assert event["type"] == "one_leg_exposure"
    payload = event.get("payload", {})
    assert payload.get("canonical_id") == "MKT1"


async def test_restore_from_redis_arms_when_persisted(fake_notifier, fake_adapter_factory):
    """Kill-switch state must survive container restart: if Redis says
    the switch was armed before the crash, the supervisor must come back
    up armed so no trades execute until an operator resets it.
    """
    from arbiter.safety.persistence import RedisStateShim

    class _StubRedis:
        def __init__(self, value):
            self._value = value

        async def get(self, key):
            return self._value

        async def set(self, key, value):
            self._value = value

        async def delete(self, key):
            self._value = None

    shim = RedisStateShim(redis_client=_StubRedis("armed"))
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    supervisor = SafetySupervisor(
        config=SafetyConfig(),
        engine=SimpleNamespace(),
        adapters=adapters,
        notifier=fake_notifier,
        redis=shim,
        store=None,
        safety_store=None,
    )
    await supervisor.restore_from_redis()
    assert supervisor.is_armed is True
    assert supervisor.armed_by == "system:redis_restore"


async def test_restore_from_redis_noop_when_not_armed(fake_notifier, fake_adapter_factory):
    """If Redis says not armed (or no Redis at all), supervisor stays disarmed."""
    from arbiter.safety.persistence import RedisStateShim

    class _StubRedis:
        async def get(self, key):
            return None

    shim = RedisStateShim(redis_client=_StubRedis())
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    supervisor = SafetySupervisor(
        config=SafetyConfig(),
        engine=SimpleNamespace(),
        adapters=adapters,
        notifier=fake_notifier,
        redis=shim,
        store=None,
        safety_store=None,
    )
    await supervisor.restore_from_redis()
    assert supervisor.is_armed is False


async def test_restore_from_redis_no_shim_is_safe(fake_notifier, fake_adapter_factory):
    """Calling restore_from_redis with no Redis configured must not raise."""
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    supervisor = SafetySupervisor(
        config=SafetyConfig(),
        engine=SimpleNamespace(),
        adapters=adapters,
        notifier=fake_notifier,
        redis=None,
        store=None,
        safety_store=None,
    )
    await supervisor.restore_from_redis()
    assert supervisor.is_armed is False


async def test_reset_kill_clears_engine_db_write_failed(
    fake_notifier, fake_adapter_factory, monkeypatch,
):
    """An ExecutionStore failure during live trading sets
    ``engine._db_write_failed = True`` and trips the kill switch via
    ``trip_kill``. Once the operator fixes the DB and resets the switch,
    the engine must come back online — but it stays blocked forever
    unless ``reset_kill`` also clears the flag. This regression test
    pins that contract."""
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    engine = SimpleNamespace(_db_write_failed=True)

    cfg = SafetyConfig()
    cfg.min_cooldown_seconds = 10.0
    supervisor = SafetySupervisor(
        config=cfg,
        engine=engine,
        adapters=adapters,
        notifier=fake_notifier,
        redis=None,
        store=None,
        safety_store=None,
    )

    base = time.time()
    monkeypatch.setattr("arbiter.safety.supervisor.time.time", lambda: base)
    await supervisor.trip_kill(by="execution_engine:db_failure", reason="pg down")
    assert engine._db_write_failed is True  # trip leaves the flag alone

    monkeypatch.setattr("arbiter.safety.supervisor.time.time", lambda: base + 60.0)
    await supervisor.reset_kill(by="operator:test", note="db recovered")
    assert engine._db_write_failed is False


async def test_reset_kill_safe_when_engine_lacks_flag(
    fake_notifier, fake_adapter_factory, monkeypatch,
):
    """Some test scenarios construct a stub engine without
    ``_db_write_failed`` (e.g. plain SimpleNamespace). ``reset_kill``
    must not raise AttributeError in that case."""
    adapters = {"kalshi": fake_adapter_factory("kalshi", [])}
    engine = SimpleNamespace()  # no _db_write_failed attribute

    cfg = SafetyConfig()
    cfg.min_cooldown_seconds = 10.0
    supervisor = SafetySupervisor(
        config=cfg,
        engine=engine,
        adapters=adapters,
        notifier=fake_notifier,
        redis=None,
        store=None,
        safety_store=None,
    )
    base = time.time()
    monkeypatch.setattr("arbiter.safety.supervisor.time.time", lambda: base)
    await supervisor.trip_kill(by="operator:test", reason="manual")
    monkeypatch.setattr("arbiter.safety.supervisor.time.time", lambda: base + 60.0)
    # Must not raise.
    await supervisor.reset_kill(by="operator:test", note="ok")
