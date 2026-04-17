"""SAFE-05 shutdown-ordering integration tests (plan 03-05, Task 0).

Wave-0 red tests for the graceful-shutdown restructure. These verify the
observable truths from the plan:

1. Call order: cancel_all BEFORE task.cancel (via run_shutdown_sequence).
2. Timeout bounded: a hanging adapter must not freeze shutdown indefinitely.
3. prepare_shutdown broadcasts shutdown_state BEFORE trip_kill runs so the
   dashboard sees it before cancel happens.

Task 1 turns these green by:
- Adding SafetySupervisor.prepare_shutdown()
- Implementing Kalshi/Polymarket cancel_all()
- Extracting run_shutdown_sequence helper in main.py
- Restructuring the handle_shutdown block in main.py
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from arbiter.config.settings import SafetyConfig
    from arbiter.safety.supervisor import SafetySupervisor
except Exception:  # pragma: no cover
    SafetyConfig = None  # type: ignore
    SafetySupervisor = None  # type: ignore


def _spy_adapter(call_order, name: str, cancelled_ids=None):
    """Async-mocked adapter whose cancel_all records into call_order."""
    ids = list(cancelled_ids or [])

    class _SpyAdapter:
        platform = name
        rate_limiter = MagicMock()

        async def cancel_all(self):
            call_order.append("cancel_all")
            return list(ids)

    return _SpyAdapter()


def _hang_adapter(name: str):
    class _HangAdapter:
        platform = name
        rate_limiter = MagicMock()

        async def cancel_all(self):
            await asyncio.sleep(30)
            return []

    return _HangAdapter()


def _build_supervisor(adapters):
    return SafetySupervisor(
        config=SafetyConfig(),
        engine=SimpleNamespace(),
        adapters=adapters,
        notifier=AsyncMock(),
        redis=None,
        store=None,
        safety_store=AsyncMock(),
    )


async def test_graceful_shutdown_cancels_orders_before_tasks():
    """Spy adapter + spy background task record order; cancel_all must precede
    task_cancelled.

    Task 1 adds ``run_shutdown_sequence`` to ``arbiter.main``; until then this
    test is RED (ImportError).
    """
    call_order: list = []

    adapters = {
        "kalshi": _spy_adapter(call_order, "kalshi", ["orderA"]),
        "polymarket": _spy_adapter(call_order, "polymarket", ["orderB"]),
    }

    async def dummy_task():
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            call_order.append("task_cancelled")
            raise

    from arbiter.main import run_shutdown_sequence  # provided by Task 1

    supervisor = _build_supervisor(adapters)
    tasks = [asyncio.create_task(dummy_task()) for _ in range(2)]
    await run_shutdown_sequence(supervisor, tasks, timeout=5.0)

    assert "cancel_all" in call_order, f"cancel_all never recorded; log={call_order}"
    assert "task_cancelled" in call_order, (
        f"task_cancelled never recorded; log={call_order}"
    )
    cancel_idx = call_order.index("cancel_all")
    task_idx = call_order.index("task_cancelled")
    assert cancel_idx < task_idx, (
        f"cancel_all must come before task_cancelled "
        f"(cancel_idx={cancel_idx}, task_idx={task_idx}, log={call_order})"
    )


async def test_shutdown_timeout_escalates(caplog):
    """A hanging adapter must not freeze shutdown. prepare_shutdown has a 5s
    budget; run_shutdown_sequence must log a timeout warning and continue to
    task.cancel so the process can exit.
    """
    caplog.set_level(logging.WARNING, logger="arbiter.main")
    adapters = {"kalshi": _hang_adapter("kalshi")}

    from arbiter.main import run_shutdown_sequence

    supervisor = _build_supervisor(adapters)
    tasks = [asyncio.create_task(asyncio.sleep(99))]
    start = time.monotonic()
    await run_shutdown_sequence(supervisor, tasks, timeout=5.0)
    elapsed = time.monotonic() - start
    # Should NOT hang indefinitely; 5s wait_for + small wrap-up budget.
    assert elapsed < 7.5, f"shutdown took {elapsed:.1f}s (expected <7.5s)"
    # Log message mentions either 'exceeded' or 'timeout' so operators can grep.
    assert any(
        "exceeded" in record.message.lower() or "timeout" in record.message.lower()
        for record in caplog.records
    ), f"no timeout warning logged; records={[r.message for r in caplog.records]}"
    # All tasks cancelled so the loop can exit.
    for task in tasks:
        assert task.done() or task.cancelled()


async def test_prepare_shutdown_broadcasts_before_trip():
    """prepare_shutdown must publish shutdown_state BEFORE trip_kill runs so
    the dashboard learns of the shutdown before adapters start cancelling.
    """
    order: list = []

    class SpyAdapter:
        platform = "kalshi"
        rate_limiter = MagicMock()

        async def cancel_all(self):
            order.append("cancel_all")
            return []

    supervisor = _build_supervisor({"k": SpyAdapter()})
    queue = supervisor.subscribe()
    await supervisor.prepare_shutdown()

    # First published event MUST be shutdown_state with phase="shutting_down".
    first = queue.get_nowait()
    assert first["type"] == "shutdown_state", (
        f"expected first event type 'shutdown_state', got {first}"
    )
    assert first["payload"]["phase"] == "shutting_down", (
        f"expected phase='shutting_down', got {first['payload']}"
    )
    # cancel_all must have happened after the broadcast (i.e., as part of trip_kill).
    assert "cancel_all" in order, (
        f"cancel_all never recorded; order={order}"
    )
