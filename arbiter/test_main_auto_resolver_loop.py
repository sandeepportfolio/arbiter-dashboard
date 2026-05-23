"""Tests for ``arbiter.main.run_auto_resolver_loop``.

The AutoResolver itself is exhaustively covered by
``arbiter.recovery.test_auto_resolver``. These tests verify only the
loop helper's contract: invoke ``run_once`` on a cadence, log meaningful
passes, survive per-iteration exceptions, and shut down cleanly on
cancellation.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from arbiter.main import run_auto_resolver_loop


@pytest.mark.asyncio
async def test_loop_calls_run_once_repeatedly_and_logs_actions(caplog):
    """Each tick invokes AutoResolver.run_once and a pass with actions is logged."""
    resolver = AsyncMock()
    resolver.run_once.side_effect = [
        # First tick — applied + expired: should log
        {
            "reconciled": [
                {"arb_id": "ARB-1", "outcome": "applied", "reason": "ok"}
            ],
            "kill_switch": {"verdict": "not_armed", "recommend_manual_disarm": False, "blocking_reasons": []},
            "expired_incidents": [
                {"incident_id": "INC-A", "outcome": "expired", "severity": "warning", "age_seconds": 99999.0}
            ],
        },
        # Second tick — fully quiet: should NOT log a "pass" line
        {
            "reconciled": [],
            "kill_switch": {"verdict": "not_armed", "recommend_manual_disarm": False, "blocking_reasons": []},
            "expired_incidents": [],
        },
        # Third tick — used to break out
        asyncio.CancelledError(),
    ]

    with caplog.at_level(logging.WARNING, logger="arbiter.main.auto_resolver"):
        loop_task = asyncio.create_task(
            run_auto_resolver_loop(resolver, interval_s=0.0)
        )
        # Give the loop time to run the first two side effects then receive
        # the CancelledError on the third call.
        await asyncio.sleep(0.05)
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert resolver.run_once.await_count >= 2
    msgs = [r.message for r in caplog.records]
    assert any("reconciled=1" in m and "expired=1" in m for m in msgs), msgs
    # Quiet pass MUST NOT emit a "pass:" line — verify only the actionable
    # pass produced log output.
    assert sum("auto_resolver pass:" in m for m in msgs) == 1


@pytest.mark.asyncio
async def test_loop_logs_when_kill_switch_classified_non_clear(caplog):
    """A BENIGN classification should be visible in the loop's log output."""
    resolver = AsyncMock()
    resolver.run_once.return_value = {
        "reconciled": [],
        "kill_switch": {
            "verdict": "benign",
            "recommend_manual_disarm": True,
            "blocking_reasons": [],
        },
        "expired_incidents": [],
    }

    with caplog.at_level(logging.WARNING, logger="arbiter.main.auto_resolver"):
        loop_task = asyncio.create_task(
            run_auto_resolver_loop(resolver, interval_s=0.0)
        )
        await asyncio.sleep(0.02)
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert any(
        "kill_switch_verdict=benign" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_loop_survives_run_once_raising(caplog):
    """An exception on one tick MUST NOT kill the loop."""
    resolver = AsyncMock()
    resolver.run_once.side_effect = [
        RuntimeError("boom"),
        {
            "reconciled": [],
            "kill_switch": {"verdict": "not_armed", "recommend_manual_disarm": False, "blocking_reasons": []},
            "expired_incidents": [],
        },
        {
            "reconciled": [],
            "kill_switch": {"verdict": "not_armed", "recommend_manual_disarm": False, "blocking_reasons": []},
            "expired_incidents": [],
        },
    ]

    with caplog.at_level(logging.ERROR, logger="arbiter.main.auto_resolver"):
        loop_task = asyncio.create_task(
            run_auto_resolver_loop(resolver, interval_s=0.0)
        )
        await asyncio.sleep(0.05)
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert resolver.run_once.await_count >= 2, "loop must continue past raise"
    assert any(
        "auto_resolver loop error" in r.message and "boom" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_loop_exits_cleanly_on_cancellation():
    """Cancellation must return quickly and not raise to the caller."""
    resolver = AsyncMock()
    resolver.run_once.return_value = {
        "reconciled": [],
        "kill_switch": {"verdict": "not_armed", "recommend_manual_disarm": False, "blocking_reasons": []},
        "expired_incidents": [],
    }

    loop_task = asyncio.create_task(
        run_auto_resolver_loop(resolver, interval_s=60.0)
    )
    await asyncio.sleep(0.01)
    loop_task.cancel()
    # gather with return_exceptions=False; the loop swallows CancelledError
    # internally, so the task should resolve to None (not raise).
    results = await asyncio.gather(loop_task, return_exceptions=True)
    assert results == [None]
