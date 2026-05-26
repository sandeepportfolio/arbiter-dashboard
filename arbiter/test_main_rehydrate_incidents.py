"""Tests for ``arbiter.main.rehydrate_open_incidents``.

engine._incidents is a bounded deque that starts empty on every container
boot. Without explicit rehydration, an open critical incident raised by a
prior session disappears from the readiness gate on restart and the trade
gate would re-arm despite the blocking exposure. These tests verify the
rehydration helper invariants:

  - open incidents from the store are appended to the engine deque
  - duplicates already present in the deque are skipped (idempotent)
  - a store failure is logged but never raised (startup must not abort)
"""
from __future__ import annotations

import logging
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.engine import ExecutionIncident
from arbiter.main import rehydrate_open_incidents


def _make_engine() -> SimpleNamespace:
    return SimpleNamespace(_incidents=deque(maxlen=200))


def _make_incident(incident_id: str, severity: str = "critical") -> ExecutionIncident:
    return ExecutionIncident(
        incident_id=incident_id,
        arb_id="ARB-X",
        canonical_id="MKT_X",
        severity=severity,
        message=f"open incident {incident_id}",
        timestamp=0.0,
        metadata={"event_type": "one_leg_exposure"},
        status="open",
    )


@pytest.mark.asyncio
async def test_rehydrates_open_incidents_into_engine_deque():
    store = MagicMock()
    incidents = [_make_incident("INC-1"), _make_incident("INC-2")]
    store.list_incidents = AsyncMock(return_value=incidents)
    engine = _make_engine()
    logger = logging.getLogger("test")

    count = await rehydrate_open_incidents(store, engine, logger)

    assert count == 2
    assert {i.incident_id for i in engine._incidents} == {"INC-1", "INC-2"}
    store.list_incidents.assert_awaited_once_with(status="open", limit=200)


@pytest.mark.asyncio
async def test_rehydrate_skips_already_present_incidents():
    """A second invocation must not double-insert the same incident — the
    deque is bounded, so dedup matters for both correctness and capacity."""
    store = MagicMock()
    incidents = [_make_incident("INC-1"), _make_incident("INC-2")]
    store.list_incidents = AsyncMock(return_value=incidents)
    engine = _make_engine()
    logger = logging.getLogger("test")

    first = await rehydrate_open_incidents(store, engine, logger)
    second = await rehydrate_open_incidents(store, engine, logger)

    assert first == 2
    assert second == 0
    assert len(engine._incidents) == 2


@pytest.mark.asyncio
async def test_rehydrate_swallows_store_errors():
    """A DB failure during rehydration must NOT block startup. The helper
    returns 0 and logs a warning instead of propagating."""
    store = MagicMock()
    store.list_incidents = AsyncMock(side_effect=RuntimeError("DB down"))
    engine = _make_engine()
    logger = logging.getLogger("test")

    count = await rehydrate_open_incidents(store, engine, logger)

    assert count == 0
    assert len(engine._incidents) == 0


@pytest.mark.asyncio
async def test_rehydrate_returns_zero_when_no_open_incidents():
    store = MagicMock()
    store.list_incidents = AsyncMock(return_value=[])
    engine = _make_engine()
    logger = logging.getLogger("test")

    count = await rehydrate_open_incidents(store, engine, logger)

    assert count == 0
    assert len(engine._incidents) == 0
