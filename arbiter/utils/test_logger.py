"""Tests for arbiter.utils.logger structlog + JSON output."""
import io
import json
import logging

import pytest
import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from arbiter.utils.logger import setup_logging


def _capture_root_to(buf: io.StringIO) -> logging.Handler:
    """Install a fresh capture handler on the root logger using the same JSON formatter.

    Avoids handler.setStream(buf) (experimental in older Python releases). Constructs
    a new StreamHandler bound to the buffer, copies the formatter from the existing
    setup_logging-installed handler, removes the old handlers, and installs the new
    capture handler. Tests that need teardown should reset structlog/logging state
    (see test fixtures in conftest.py / arbiter.utils.test_logger pytest fixtures).
    """
    root = logging.getLogger()
    # The setup_logging-installed handler is at root.handlers[0]; reuse its JSON formatter
    formatter = root.handlers[0].formatter if root.handlers else None
    capture = logging.StreamHandler(buf)
    if formatter is not None:
        capture.setFormatter(formatter)
    # Replace handlers entirely so output goes ONLY to the capture buffer for the test
    root.handlers.clear()
    root.addHandler(capture)
    return capture


def test_output_is_json_parseable():
    setup_logging(level="INFO")
    buf = io.StringIO()
    _capture_root_to(buf)
    logging.getLogger("arbiter.test").info("hello.event", extra={"k": "v"})
    line = buf.getvalue().strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["event"] == "hello.event"
    assert parsed["k"] == "v"
    assert parsed["level"] == "info"
    assert "timestamp" in parsed


def test_contextvars_propagate():
    setup_logging(level="INFO")
    buf = io.StringIO()
    _capture_root_to(buf)
    clear_contextvars()
    bind_contextvars(arb_id="ARB-000123", canonical_id="DEM_HOUSE")
    try:
        logging.getLogger("arbiter.exec").info("order.submitted")
    finally:
        clear_contextvars()
    line = buf.getvalue().strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["arb_id"] == "ARB-000123"
    assert parsed["canonical_id"] == "DEM_HOUSE"


def test_secret_stripping():
    setup_logging(level="INFO")
    buf = io.StringIO()
    _capture_root_to(buf)
    logging.getLogger("arbiter.test").info(
        "secret.test", extra={"POLY_PRIVATE_KEY": "0xdeadbeef", "safe_field": "ok"}
    )
    line = buf.getvalue().strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["POLY_PRIVATE_KEY"] == "***REDACTED***"
    assert parsed["safe_field"] == "ok"


def test_existing_call_signature_preserved():
    """arbiter.main calls setup_logging(level=...) and expects the arbiter logger back."""
    result = setup_logging(level="DEBUG")
    assert result.name == "arbiter"
    assert result.level == logging.DEBUG
