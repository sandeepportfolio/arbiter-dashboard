"""Shared fixtures and opt-in wiring for arbiter.sandbox tests (Phase 4 live-fire).

Root conftest.py owns async test dispatch via pytest_pyfunc_call; DO NOT redefine it here.
Sandbox scenarios are plain `async def` + @pytest.mark.live.
"""
from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run Phase 4 sandbox live-fire scenarios (real API calls; real $ on Polymarket).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: Phase 4 sandbox live-fire scenario - requires real API creds + --live flag or -m live",
    )


def pytest_collection_modifyitems(config, items):
    # Opt-in: if user passed --live OR -m live, do not skip.
    if config.getoption("--live"):
        return
    markexpr = config.getoption("-m", default="") or ""
    if "live" in markexpr:
        return
    skip_live = pytest.mark.skip(reason="Use -m live or --live to run Phase 4 scenarios")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
