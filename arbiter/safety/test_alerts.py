"""Wave-0 test stubs for SafetyAlertTemplates (SAFE-01)."""
from __future__ import annotations

import pytest

try:
    from arbiter.safety.alerts import SafetyAlertTemplates  # type: ignore
except Exception:  # pragma: no cover
    SafetyAlertTemplates = None  # type: ignore


@pytest.mark.skip(reason="implementation pending (Task 1)")
def test_kill_armed_template_html():
    message = SafetyAlertTemplates.kill_armed(
        by="operator:x",
        reason="manual",
        cancelled_counts={"kalshi": 3, "polymarket": 2},
    )
    assert "KILL SWITCH ARMED" in message
    assert "kalshi:3" in message
    assert "polymarket:2" in message


@pytest.mark.skip(reason="implementation pending (Task 1)")
def test_kill_reset_template():
    message = SafetyAlertTemplates.kill_reset(by="operator:x", note="recovered")
    assert "Kill switch RESET" in message
    assert "operator:x" in message


@pytest.mark.skip(reason="plan 03-03 fills this")
def test_one_leg_template():
    # Placeholder: implemented in plan 03-03.
    pass
