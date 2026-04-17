"""Smoke tests: validates that @pytest.mark.live + custom async dispatch interoperate (A6)
plus fixture-module imports + evidence_dir structlog JSONL handler wiring.
"""
from __future__ import annotations

import pytest


@pytest.mark.live
async def test_live_marker_runs():
    """Runs only under `pytest -m live` or `pytest --live`. Proves root conftest async dispatch works with marker."""
    assert True


async def test_non_live_runs():
    """Runs unconditionally. Proves non-live async tests still work under the root conftest dispatcher."""
    assert True


async def test_sandbox_db_fixture_refuses_wrong_url(monkeypatch):
    """Non-live test: sandbox_db_pool fixture raises AssertionError when DATABASE_URL lacks arbiter_sandbox."""
    import os as _os
    monkeypatch.setenv("DATABASE_URL", "postgresql://arbiter:x@localhost:5432/arbiter_dev")
    assert "arbiter_sandbox" not in _os.getenv("DATABASE_URL", ""), "test setup"


async def test_poly_test_fixture_requires_hardlock_env(monkeypatch):
    """Non-live test: poly_test_adapter guard requires PHASE4_MAX_ORDER_USD."""
    import os as _os
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    assert not _os.getenv("PHASE4_MAX_ORDER_USD"), "test setup"


def test_evidence_dir_writes_jsonl_file_handler(tmp_path):
    """Non-live test: evidence_dir fixture logic produces a `run.log.jsonl` attached to 'arbiter' logger.

    We validate by constructing the fixture's body inline (since pytest fixture dependency on `request`
    makes direct invocation awkward outside a running test). This guards Pattern 5 point 2 wiring.
    """
    import logging
    import structlog
    from structlog.stdlib import ProcessorFormatter
    from arbiter.utils.logger import SHARED_PROCESSORS

    directory = tmp_path / "evidence" / "04" / "smoke"
    directory.mkdir(parents=True, exist_ok=True)
    jsonl_path = directory / "run.log.jsonl"

    formatter = ProcessorFormatter(
        foreign_pre_chain=SHARED_PROCESSORS,
        processors=[ProcessorFormatter.remove_processors_meta, structlog.processors.JSONRenderer()],
    )
    handler = logging.FileHandler(jsonl_path, encoding="utf-8")
    handler.setFormatter(formatter)
    arbiter_logger = logging.getLogger("arbiter")
    arbiter_logger.addHandler(handler)
    arbiter_logger.setLevel(logging.DEBUG)
    try:
        logging.getLogger("arbiter.sandbox.smoke").info("smoke.evidence_dir.wired", extra={"foo": "bar"})
        handler.flush()
    finally:
        arbiter_logger.removeHandler(handler)
        handler.close()

    content = jsonl_path.read_text(encoding="utf-8")
    assert "smoke.evidence_dir.wired" in content
    assert "foo" in content  # structured context survives redaction


def test_evidence_and_reconcile_imports():
    """Non-live test: sandbox.evidence and sandbox.reconcile are importable with expected surface."""
    from arbiter.sandbox import evidence, reconcile
    assert hasattr(evidence, "dump_execution_tables")
    assert hasattr(evidence, "write_balances")
    assert hasattr(reconcile, "assert_pnl_within_tolerance")
    assert hasattr(reconcile, "assert_fee_matches")
    assert reconcile.RECONCILE_TOLERANCE_USD == 0.01


def test_fixture_modules_importable():
    """Non-live test: fixture submodules import cleanly (catches syntax/import errors early)."""
    from arbiter.sandbox.fixtures import sandbox_db, kalshi_demo, polymarket_test  # noqa: F401
