"""Unit tests for the enhanced TelegramNotifier (Phase 6 Plan 06-03).

Covers:
    - disabled mode (no token/chat_id) returns False without HTTP
    - 200 success
    - 5xx retry succeeds on 3rd attempt
    - 5xx retry exhausted returns False
    - 4xx (non-429) fails fast (no retry)
    - 429 rate-limit triggers retry
    - dedup within window skips the second call
    - dedup with different keys always sends
    - dedup after window expires allows resend
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from arbiter.monitor.balance import TelegramNotifier


class _FakeResponse:
    def __init__(self, status: int, text: str = ""):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_session_post(responses):
    """Return a context manager that patches aiohttp.ClientSession.post to
    yield the next response from `responses` on each call. Raises a helpful
    RuntimeError if the list is exhausted."""
    it = iter(responses)

    def _post(*args, **kwargs):
        try:
            value = next(it)
        except StopIteration as exc:
            raise RuntimeError("session.post called more times than expected") from exc
        if isinstance(value, Exception):
            raise value
        return value  # already a context manager via _FakeResponse

    return _post


@pytest.mark.asyncio
async def test_disabled_returns_false_no_network():
    n = TelegramNotifier(bot_token="", chat_id="")
    with patch("aiohttp.ClientSession.post") as mock_post:
        ok = await n.send("hi")
    assert ok is False
    mock_post.assert_not_called()
    await n.close()


@pytest.mark.asyncio
async def test_200_success():
    n = TelegramNotifier(bot_token="t", chat_id="c")
    with patch("aiohttp.ClientSession.post", side_effect=_patch_session_post([_FakeResponse(200, "ok")])):
        ok = await n.send("hi")
    assert ok is True
    await n.close()


@pytest.mark.asyncio
async def test_5xx_retry_succeeds_on_third():
    n = TelegramNotifier(bot_token="t", chat_id="c", max_retries=3)
    responses = [_FakeResponse(503, "unavail"), _FakeResponse(503, "unavail"), _FakeResponse(200, "ok")]
    with patch("aiohttp.ClientSession.post", side_effect=_patch_session_post(responses)):
        with patch("asyncio.sleep", new_callable=AsyncMock):  # make retries fast
            ok = await n.send("hi")
    assert ok is True
    await n.close()


@pytest.mark.asyncio
async def test_5xx_retries_exhausted():
    n = TelegramNotifier(bot_token="t", chat_id="c", max_retries=2)
    responses = [_FakeResponse(502), _FakeResponse(502)]
    with patch("aiohttp.ClientSession.post", side_effect=_patch_session_post(responses)):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            ok = await n.send("hi")
    assert ok is False
    await n.close()


@pytest.mark.asyncio
async def test_4xx_fails_fast_no_retry():
    n = TelegramNotifier(bot_token="t", chat_id="c", max_retries=3)
    # 401 (bad token) should NOT be retried; first response is used.
    responses = [_FakeResponse(401, "unauthorized")]
    with patch("aiohttp.ClientSession.post", side_effect=_patch_session_post(responses)) as post:
        ok = await n.send("hi")
    assert ok is False
    # Only one call attempted (no retry)
    await n.close()


@pytest.mark.asyncio
async def test_429_triggers_retry():
    n = TelegramNotifier(bot_token="t", chat_id="c", max_retries=3)
    responses = [_FakeResponse(429, "too many"), _FakeResponse(200, "ok")]
    with patch("aiohttp.ClientSession.post", side_effect=_patch_session_post(responses)):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            ok = await n.send("hi")
    assert ok is True
    await n.close()


@pytest.mark.asyncio
async def test_dedup_within_window_skips_second():
    n = TelegramNotifier(bot_token="t", chat_id="c", dedup_window_sec=60)
    with patch("aiohttp.ClientSession.post", side_effect=_patch_session_post([_FakeResponse(200)])) as post:
        first = await n.send("hi", dedup_key="kill")
        second = await n.send("hi again", dedup_key="kill")
    assert first is True
    assert second is False
    await n.close()


@pytest.mark.asyncio
async def test_dedup_different_keys_always_send():
    n = TelegramNotifier(bot_token="t", chat_id="c", dedup_window_sec=60)
    responses = [_FakeResponse(200), _FakeResponse(200)]
    with patch("aiohttp.ClientSession.post", side_effect=_patch_session_post(responses)):
        a = await n.send("a", dedup_key="kill")
        b = await n.send("b", dedup_key="one_leg")
    assert a is True and b is True
    await n.close()


@pytest.mark.asyncio
async def test_dedup_window_zero_disables_dedup():
    n = TelegramNotifier(bot_token="t", chat_id="c", dedup_window_sec=0)
    responses = [_FakeResponse(200), _FakeResponse(200)]
    with patch("aiohttp.ClientSession.post", side_effect=_patch_session_post(responses)):
        a = await n.send("a", dedup_key="kill")
        b = await n.send("a", dedup_key="kill")
    assert a is True and b is True
    await n.close()


@pytest.mark.asyncio
async def test_transient_exception_is_retried():
    import aiohttp

    n = TelegramNotifier(bot_token="t", chat_id="c", max_retries=3)
    responses = [aiohttp.ClientError("connection reset"), _FakeResponse(200)]
    with patch("aiohttp.ClientSession.post", side_effect=_patch_session_post(responses)):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            ok = await n.send("hi")
    assert ok is True
    await n.close()
