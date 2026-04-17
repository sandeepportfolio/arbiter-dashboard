"""Wave-0 test stubs for POST /api/kill-switch + GET /api/safety/status.

Task 0 ships these skipped; Task 3 un-skips them with an aiohttp test-client
harness copied from arbiter/test_api_integration.py.
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="implementation pending (Task 3)")
async def test_kill_switch_requires_auth(aiohttp_client):
    # POST /api/kill-switch without a session cookie → 401
    pass


@pytest.mark.skip(reason="implementation pending (Task 3)")
async def test_kill_switch_arm_with_auth(aiohttp_client):
    # POST with valid operator session + body {action:"arm", reason:"manual"}
    # → 200 with armed=True state body.
    pass


@pytest.mark.skip(reason="implementation pending (Task 3)")
async def test_kill_switch_reset_cooldown_denies(aiohttp_client):
    # Arm → immediate reset → 400 with error mentioning cooldown.
    pass


@pytest.mark.skip(reason="implementation pending (Task 3)")
async def test_kill_switch_unknown_action_rejected(aiohttp_client):
    # {action:"frobnicate"} → 400 Unsupported kill-switch action.
    pass
