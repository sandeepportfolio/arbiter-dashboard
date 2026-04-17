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
