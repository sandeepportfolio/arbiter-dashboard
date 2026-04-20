"""Shared fixtures and opt-in wiring for arbiter.live tests (Phase 5 live-fire).

Mirror of arbiter/sandbox/conftest.py with two critical differences:

1. ``evidence_dir`` writes to ``evidence/05/<scenario>_<ts>/`` (NOT evidence/04).
2. ``--live`` flag registration is guarded — arbiter/sandbox/conftest.py already
   owns the option; registering it twice raises ValueError on collection. If
   arbiter/live/ is invoked standalone (without arbiter/sandbox/ also being
   collected) pytest walks this conftest alone and the try/except registers the
   flag; if both conftests are walked, the sandbox one registers first and the
   try/except here silently absorbs the ValueError.

Root conftest.py owns async dispatch via pytest_pyfunc_call; DO NOT redefine
that here. Live scenarios are plain ``async def`` + @pytest.mark.live.
"""
from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timezone

import pytest
import structlog
from structlog.stdlib import ProcessorFormatter

from arbiter.utils.logger import SHARED_PROCESSORS


# Re-export fixtures from fixtures/ submodule so scenario tests can consume them.
pytest_plugins = [
    "arbiter.live.fixtures.production_db",
    "arbiter.live.fixtures.kalshi_production",
    "arbiter.live.fixtures.polymarket_production",
]


def pytest_addoption(parser):
    """Register ``--live`` if arbiter/sandbox/conftest.py has not already done so.

    Pytest only walks conftest.py files in the path ancestry of the collected
    items, so ``pytest arbiter/live/`` alone does NOT load
    arbiter/sandbox/conftest.py. The live conftest must register the flag to
    remain usable standalone; when both sandbox and live are collected
    together, the live conftest's pytest_addoption runs first (alphabetical
    discovery order) and the sandbox conftest raises ValueError on the second
    registration — this is why Plan 05-01 Task 2 called out putting the
    try/except HERE (and why sandbox's conftest.py intentionally does NOT
    carry the try/except: its pytest_addoption is the historical owner of
    this flag and any future removal would fold back to a single registration
    here). Threat T-5-01-10 (double-registration) mitigation.
    """
    try:
        parser.addoption(
            "--live",
            action="store_true",
            default=False,
            help="Run Phase 4 sandbox + Phase 5 live-fire scenarios "
                 "(real API calls; real $ on Polymarket production).",
        )
    except ValueError:
        # Already registered — this path fires only if sandbox's conftest
        # happens to load first (unusual given alphabetical order, but
        # defensive). No-op is safe because the flag is usable either way.
        pass


def pytest_configure(config):
    # Pytest deduplicates marker registrations by name, so declaring `live`
    # here is safe even if arbiter/sandbox/conftest.py declared it first.
    config.addinivalue_line(
        "markers",
        "live: Phase 4 sandbox or Phase 5 live-fire scenario — "
        "requires real API creds + --live flag or -m live",
    )


def pytest_collection_modifyitems(config, items):
    # Opt-in gate: if user passed --live OR -m live, do not skip.
    if config.getoption("--live"):
        return
    markexpr = config.getoption("-m", default="") or ""
    if "live" in markexpr:
        return
    skip_live = pytest.mark.skip(reason="Use -m live or --live to run Phase 5 scenarios")
    for item in items:
        # NB: ``"live" in item.keywords`` returns True when the path contains
        # "live" (e.g. ``arbiter/live/test_reconcile.py``), which would skip
        # every non-live unit test under arbiter/live/. Use ``get_closest_marker``
        # to detect only tests that explicitly carry ``@pytest.mark.live``.
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)


@pytest.fixture
def evidence_dir(request):
    """Per-scenario Phase 5 evidence directory + structlog JSONL file handler.

    Creates ``evidence/05/<scenario>_<UTC timestamp>/`` and installs a
    ``logging.FileHandler`` that writes structured JSON to ``run.log.jsonl``.
    Captures every structlog/stdlib record under the ``arbiter`` namespace
    for the test's lifetime; removed on teardown so leakage into later tests
    is impossible.

    Direct mirror of arbiter.sandbox.conftest.evidence_dir with the path
    rewritten from ``evidence/04`` -> ``evidence/05``.
    """
    scenario = request.node.name
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = pathlib.Path("evidence/05") / f"{scenario}_{timestamp}"
    directory.mkdir(parents=True, exist_ok=True)

    jsonl_path = directory / "run.log.jsonl"
    formatter = ProcessorFormatter(
        foreign_pre_chain=SHARED_PROCESSORS,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    file_handler = logging.FileHandler(jsonl_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    arbiter_logger = logging.getLogger("arbiter")
    prior_level = arbiter_logger.level
    arbiter_logger.addHandler(file_handler)
    if prior_level == logging.NOTSET or prior_level > logging.DEBUG:
        arbiter_logger.setLevel(logging.DEBUG)

    try:
        yield directory
    finally:
        arbiter_logger.removeHandler(file_handler)
        file_handler.close()
        arbiter_logger.setLevel(prior_level)
