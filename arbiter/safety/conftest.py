"""Shared fixtures for arbiter.safety tests."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def fake_notifier():
    """AsyncMock replacing arbiter.monitor.balance.TelegramNotifier."""
    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=True)
    return notifier


@pytest.fixture
def fake_adapter_factory():
    """Returns a factory producing AsyncMock adapters with a cancel_all method."""

    def make(platform: str, cancelled_ids: list[str] | None = None):
        adapter = AsyncMock()
        adapter.platform = platform
        adapter.cancel_all = AsyncMock(return_value=list(cancelled_ids or []))
        return adapter

    return make
