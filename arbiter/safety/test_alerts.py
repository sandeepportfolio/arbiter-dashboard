"""Wave-0 test stubs for SafetyAlertTemplates (SAFE-01)."""
from __future__ import annotations

import pytest

try:
    from arbiter.safety.alerts import SafetyAlertTemplates  # type: ignore
except Exception:  # pragma: no cover
    SafetyAlertTemplates = None  # type: ignore


def test_kill_armed_template_html():
    message = SafetyAlertTemplates.kill_armed(
        by="operator:x",
        reason="manual",
        cancelled_counts={"kalshi": 3, "polymarket": 2},
    )
    assert "KILL SWITCH ARMED" in message
    assert "kalshi:3" in message
    assert "polymarket:2" in message


def test_kill_reset_template():
    message = SafetyAlertTemplates.kill_reset(by="operator:x", note="recovered")
    assert "Kill switch RESET" in message
    assert "operator:x" in message


def test_one_leg_template_contains_required_parts():
    """SafetyAlertTemplates.one_leg_exposure formats the Telegram body with
    all fields plan 03-03's handler passes in. Assertions mirror the
    ``must_haves.truths`` from the plan frontmatter."""
    message = SafetyAlertTemplates.one_leg_exposure(
        canonical_id="DEM_PRES_2028",
        filled_platform="kalshi",
        filled_side="yes",
        fill_qty=100,
        exposure_usd=56.0,
        unwind_instruction="Sell 100 YES on KALSHI at market",
    )
    assert "NAKED POSITION" in message
    assert "DEM_PRES_2028" in message
    # Case-insensitive platform check (template upper-cases for display).
    assert "kalshi" in message.lower()
    assert "100" in message
    assert "$56.00" in message
    assert "Sell 100 YES" in message
