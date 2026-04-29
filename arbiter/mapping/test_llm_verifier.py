"""
Tests for llm_verifier.py — Layer 2 LLM-based market equivalence verifier.

TDD: tests written before implementation.
All tests mock the Anthropic SDK — no real API calls.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from arbiter.mapping.llm_verifier import verify


@pytest.fixture(autouse=True)
def _force_api_backend_and_clear_cache(monkeypatch, tmp_path):
    """Each test: force the API backend (so _get_client is exercised) and
    isolate the persistent cache to a tmp file so prior runs don't leak."""
    monkeypatch.setenv("LLM_VERIFIER_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr("arbiter.mapping.llm_verifier._CACHE_PATH", cache_path)
    monkeypatch.setattr("arbiter.mapping.llm_verifier._persistent_cache", {})
    import arbiter.mapping.llm_verifier as lv
    lv._cache.clear()
    yield


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_mock_response(text: str):
    """Create a mock Anthropic response object with text content."""
    content_block = MagicMock()
    content_block.text = text
    resp = MagicMock()
    resp.content = [content_block]
    return resp


def _make_mock_client(response_text: str):
    """Return a mock AsyncAnthropic client that yields a fixed response."""
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(
        return_value=_make_mock_response(response_text)
    )
    return mock_client


# ─── Response parsing tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_yes_response_returns_YES():
    """A clear YES response should return 'YES'."""
    mock_client = _make_mock_client("YES - these two markets resolve to the same outcome.")
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        result = await verify(
            "Will the Fed cut rates in May 2026?",
            "Federal Reserve rate cut May 2026?",
        )
    assert result == "YES"


@pytest.mark.asyncio
async def test_no_response_returns_NO():
    """A clear NO response should return 'NO'."""
    mock_client = _make_mock_client("NO - these markets have different resolution dates.")
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        result = await verify(
            "Will the Fed cut rates in May 2026?",
            "Will the Fed cut rates by July 2026?",
        )
    assert result == "NO"


@pytest.mark.asyncio
async def test_ambiguous_response_returns_MAYBE():
    """A response that has neither YES nor NO at a word boundary → MAYBE."""
    mock_client = _make_mock_client(
        "I cannot determine from the text whether these markets resolve identically."
    )
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        result = await verify(
            "Will Bitcoin hit 100K?",
            "Will BTC exceed $100,000?",
        )
    assert result == "MAYBE"


@pytest.mark.asyncio
async def test_maybe_explicit_returns_MAYBE():
    """An explicit MAYBE response should return 'MAYBE'."""
    mock_client = _make_mock_client("MAYBE - the resolution criteria are unclear.")
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        result = await verify(
            "Will Trump win the 2028 election?",
            "Will Trump be elected president in 2028?",
        )
    assert result == "MAYBE"


# ─── Fail-safe invariant ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_exception_returns_MAYBE():
    """On any API exception, verify() must return MAYBE (fail-safe invariant)."""
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("connection refused"))
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        result = await verify(
            "Will BTC hit 100K in 2026?",
            "Bitcoin above 100K 2026?",
        )
    assert result == "MAYBE", "API exception MUST return MAYBE (fail-safe)"


@pytest.mark.asyncio
async def test_api_timeout_returns_MAYBE():
    """API timeout also returns MAYBE."""
    import asyncio as _asyncio
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(
        side_effect=_asyncio.TimeoutError("timeout")
    )
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        result = await verify(
            "Will the EU raise tariffs in 2026?",
            "European Union tariff increase 2026?",
        )
    assert result == "MAYBE"


# ─── Cache tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_pair_hits_cache_no_second_api_call():
    """Calling verify() twice with the same pair should only call the API once."""
    # Import and clear cache before test
    import arbiter.mapping.llm_verifier as lv
    lv._cache.clear()

    mock_client = _make_mock_client("YES - same outcome.")
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        r1 = await verify("Will the Fed cut rates in May 2026?", "Fed rate cut May 2026?")
        r2 = await verify("Will the Fed cut rates in May 2026?", "Fed rate cut May 2026?")

    assert r1 == "YES"
    assert r2 == "YES"
    assert mock_client.messages.create.call_count == 1, (
        "Second call should hit in-memory cache, not the API"
    )


@pytest.mark.asyncio
async def test_reversed_pair_hits_cache():
    """Cache key is a frozenset so (a, b) and (b, a) are the same cache entry."""
    import arbiter.mapping.llm_verifier as lv
    lv._cache.clear()

    mock_client = _make_mock_client("YES")
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        r1 = await verify("question A", "question B")
        r2 = await verify("question B", "question A")

    assert mock_client.messages.create.call_count == 1
    assert r1 == r2 == "YES"


# ─── Prompt caching test ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_system_prompt_has_cache_control():
    """The system prompt block must include cache_control for prompt caching."""
    import arbiter.mapping.llm_verifier as lv
    lv._cache.clear()

    captured_calls = []

    async def _capture_create(**kwargs):
        captured_calls.append(kwargs)
        return _make_mock_response("YES")

    mock_client = AsyncMock()
    mock_client.messages.create = _capture_create

    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        await verify("Will the Fed cut rates?", "Fed rate cut decision?")

    assert len(captured_calls) == 1
    call_kwargs = captured_calls[0]
    system_blocks = call_kwargs.get("system", [])
    # system must be a list of content blocks with cache_control
    assert isinstance(system_blocks, list), "system must be a list of content blocks"
    has_cache_control = any(
        getattr(b, "cache_control", None) is not None
        or (isinstance(b, dict) and b.get("cache_control") is not None)
        for b in system_blocks
    )
    assert has_cache_control, (
        "At least one system block must have cache_control set for prompt caching"
    )


# ─── Persistent cache tests ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_persistent_cache_survives_in_memory_clear(tmp_path, monkeypatch):
    """A verdict written by one verify() call should still hit on the next
    call even after the in-memory cache has been cleared (simulating a
    container restart that re-loads the persistent cache)."""
    import arbiter.mapping.llm_verifier as lv

    mock_client = _make_mock_client("YES - identical resolution.")
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        await verify("Q1 persistent", "Q2 persistent")
        # Clear in-memory cache; the persistent cache should still answer.
        lv._cache.clear()
        await verify("Q1 persistent", "Q2 persistent")

    assert mock_client.messages.create.call_count == 1, (
        "Persistent cache must short-circuit the second call"
    )


@pytest.mark.asyncio
async def test_persistent_cache_is_order_independent():
    """Cache key normalizes pair order — (A,B) and (B,A) collide."""
    import arbiter.mapping.llm_verifier as lv

    mock_client = _make_mock_client("YES")
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        await verify("alpha", "beta")
        lv._cache.clear()
        # Reverse the order — should still hit the persistent cache.
        await verify("beta", "alpha")

    assert mock_client.messages.create.call_count == 1


# ─── Category-aware prompt test ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_category_hint_appears_in_user_prompt():
    captured = []

    async def _capture_create(**kwargs):
        captured.append(kwargs)
        return _make_mock_response("YES")

    mock_client = AsyncMock()
    mock_client.messages.create = _capture_create

    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        await verify("Q1", "Q2", category="sports")

    assert captured, "expected one API call"
    msgs = captured[0].get("messages") or []
    user_text = msgs[0]["content"] if msgs else ""
    assert "sports markets" in user_text.lower(), user_text


@pytest.mark.asyncio
async def test_no_category_omits_hint():
    captured = []

    async def _capture_create(**kwargs):
        captured.append(kwargs)
        return _make_mock_response("YES")

    mock_client = AsyncMock()
    mock_client.messages.create = _capture_create

    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client):
        await verify("Q1 nohint", "Q2 nohint")

    user_text = captured[0]["messages"][0]["content"]
    assert "sports markets" not in user_text.lower()
    assert "political markets" not in user_text.lower()
