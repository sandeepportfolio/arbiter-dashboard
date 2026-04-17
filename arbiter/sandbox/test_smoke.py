"""Smoke tests: validates that @pytest.mark.live + custom async dispatch interoperate (A6)."""
from __future__ import annotations

import pytest


@pytest.mark.live
async def test_live_marker_runs():
    """Runs only under `pytest -m live` or `pytest --live`. Proves root conftest async dispatch works with marker."""
    assert True


async def test_non_live_runs():
    """Runs unconditionally. Proves non-live async tests still work under the root conftest dispatcher."""
    assert True
