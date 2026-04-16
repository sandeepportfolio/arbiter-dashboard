"""Tests for arbiter.execution.adapters.retry_policy.transient_retry."""
from __future__ import annotations

import asyncio

import aiohttp
import pytest

from arbiter.execution.adapters.retry_policy import (
    TRANSIENT_EXCEPTIONS,
    transient_retry,
)


# ─── Tuple shape ──────────────────────────────────────────────────────────

def test_transient_exceptions_tuple_shape():
    assert aiohttp.ClientConnectionError in TRANSIENT_EXCEPTIONS
    assert aiohttp.ServerTimeoutError in TRANSIENT_EXCEPTIONS
    assert asyncio.TimeoutError in TRANSIENT_EXCEPTIONS
    assert ValueError not in TRANSIENT_EXCEPTIONS
    assert RuntimeError not in TRANSIENT_EXCEPTIONS


# ─── Behavior ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transient_retries_then_succeeds():
    """Two ServerTimeoutErrors then success -> returns value, 3 calls total."""
    calls = {"n": 0}

    @transient_retry(max_attempts=5)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise aiohttp.ServerTimeoutError("transient")
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_transient_retries_exhaust_and_reraise():
    """Always-failing transient -> after max_attempts, original exception re-raised."""
    calls = {"n": 0}

    @transient_retry(max_attempts=3)
    async def always_timeout():
        calls["n"] += 1
        raise aiohttp.ServerTimeoutError("never recovers")

    with pytest.raises(aiohttp.ServerTimeoutError, match="never recovers"):
        await always_timeout()
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_permanent_error_not_retried_value_error():
    """ValueError is not in TRANSIENT_EXCEPTIONS -> raised immediately, no retry."""
    calls = {"n": 0}

    @transient_retry(max_attempts=3)
    async def permanent_failure():
        calls["n"] += 1
        raise ValueError("400 Bad Request")

    with pytest.raises(ValueError, match="400 Bad Request"):
        await permanent_failure()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_permanent_error_not_retried_runtime_error():
    calls = {"n": 0}

    @transient_retry(max_attempts=3)
    async def permanent_failure():
        calls["n"] += 1
        raise RuntimeError("internal")

    with pytest.raises(RuntimeError, match="internal"):
        await permanent_failure()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_asyncio_timeout_treated_as_transient():
    """asyncio.TimeoutError -> retried (it's in the transient tuple)."""
    calls = {"n": 0}

    @transient_retry(max_attempts=3)
    async def times_out_then_succeeds():
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError("deadline")
        return "recovered"

    result = await times_out_then_succeeds()
    assert result == "recovered"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_no_retry_when_decorator_uses_max_attempts_1():
    calls = {"n": 0}

    @transient_retry(max_attempts=1)
    async def fails_once():
        calls["n"] += 1
        raise aiohttp.ServerTimeoutError("only one try")

    with pytest.raises(aiohttp.ServerTimeoutError):
        await fails_once()
    assert calls["n"] == 1
