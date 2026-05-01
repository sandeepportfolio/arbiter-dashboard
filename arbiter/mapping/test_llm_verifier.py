"""
Tests for llm_verifier.py — Layer 2 LLM-based market equivalence verifier.

TDD: tests written before implementation.
All tests mock the Anthropic SDK — no real API calls.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from arbiter.mapping.llm_verifier import verify, verify_batch


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
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
        result = await verify(
            "Will the Fed cut rates in May 2026?",
            "Federal Reserve rate cut May 2026?",
        )
    assert result == "YES"


@pytest.mark.asyncio
async def test_no_response_returns_NO():
    """A clear NO response should return 'NO'."""
    mock_client = _make_mock_client("NO - these markets have different resolution dates.")
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
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
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
        result = await verify(
            "Will Bitcoin hit 100K?",
            "Will BTC exceed $100,000?",
        )
    assert result == "MAYBE"


@pytest.mark.asyncio
async def test_maybe_explicit_returns_MAYBE():
    """An explicit MAYBE response should return 'MAYBE'."""
    mock_client = _make_mock_client("MAYBE - the resolution criteria are unclear.")
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
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
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
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
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
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
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
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
    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
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

    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
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


@pytest.mark.asyncio
async def test_verify_batch_uses_single_api_call_and_populates_cache():
    """Batch verification should amortize API calls and feed the pair cache."""
    import arbiter.mapping.llm_verifier as lv
    lv._cache.clear()

    response = """
    [
      {"index": 0, "answer": "YES", "reason": "same event"},
      {"index": 1, "answer": "NO", "reason": "different threshold"}
    ]
    """
    mock_client = _make_mock_client(response)
    pairs = [
        ("Will A happen?", "Will A happen on the same date?"),
        ("BTC above 85000?", "BTC above 100000?"),
    ]

    with patch("arbiter.mapping.llm_verifier._get_client", return_value=mock_client), \
         patch("arbiter.mapping.llm_verifier._BACKEND", "api"):
        results = await verify_batch(pairs)
        cached = await verify(*pairs[0])

    assert results == ["YES", "NO"]
    assert cached == "YES"
    assert mock_client.messages.create.call_count == 1
