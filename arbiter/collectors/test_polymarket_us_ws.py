"""
Tests for PolymarketUSWebSocket (Task 6).
Uses a fake WebSocket class to avoid live network connections.
pytest-asyncio / custom conftest asyncio runner handles async tests.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbiter.collectors.polymarket_us_ws import PolymarketUSWebSocket


# ---------------------------------------------------------------------------
# Fake WebSocket helpers
# ---------------------------------------------------------------------------

class FakeWSMessage:
    """Mimics aiohttp.WSMessage."""

    def __init__(self, data: str, type_=None):
        import aiohttp
        self.data = data
        self.type = type_ if type_ is not None else aiohttp.WSMsgType.TEXT


class FakeWS:
    """Fake async-iterable WebSocket response.

    Emits *messages* one by one, then raises StopAsyncIteration (clean close).
    Set *close_cleanly=False* to raise a RuntimeError instead (unclean close).
    """

    def __init__(
        self,
        messages: List[str],
        close_cleanly: bool = True,
        sent: Optional[List[Any]] = None,
    ):
        self._messages = list(messages)
        self._close_cleanly = close_cleanly
        self._idx = 0
        self.sent: List[Any] = sent if sent is not None else []
        self.closed = False

    async def send_str(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        import aiohttp

        if self._idx < len(self._messages):
            msg = self._messages[self._idx]
            self._idx += 1
            return FakeWSMessage(msg, type_=aiohttp.WSMsgType.TEXT)
        if not self._close_cleanly:
            raise RuntimeError("unclean ws close")
        # Emit a CLOSED message to signal end
        return FakeWSMessage("", type_=aiohttp.WSMsgType.CLOSED)


class _AlwaysFailWS(FakeWS):
    """First iteration immediately raises an error (simulates conn failure)."""

    async def __anext__(self):
        raise RuntimeError("connection refused")


# ---------------------------------------------------------------------------
# test_chunks_250_slugs_into_3_conns
# ---------------------------------------------------------------------------
async def test_chunks_250_slugs_into_3_conns():
    """250 slugs must be split into ceil(250/100) = 3 connections."""
    slugs = [f"slug-{i}" for i in range(250)]
    ws = PolymarketUSWebSocket(ws_url="wss://fake", slugs=slugs)
    chunks = ws._chunk_slugs()
    assert len(chunks) == 3
    assert len(chunks[0]) == 100
    assert len(chunks[1]) == 100
    assert len(chunks[2]) == 50
    # No slug should be lost or duplicated
    assert sorted(sum(chunks, [])) == sorted(slugs)


# ---------------------------------------------------------------------------
# test_subscribe_payload_shape
# ---------------------------------------------------------------------------
async def test_subscribe_payload_shape():
    """On connection open, the client must send the correct subscribe JSON."""
    slugs = ["alpha", "beta"]
    sent: List[Any] = []
    fake_ws = FakeWS(messages=[], sent=sent)

    ws = PolymarketUSWebSocket(ws_url="wss://fake", slugs=slugs)

    # Patch _connect_one to use our fake WS (runs subscribe + drains messages)
    async def fake_connect_one(chunk, queue):
        await ws._subscribe(fake_ws, chunk)
        async for msg in fake_ws:
            import aiohttp
            if msg.type == aiohttp.WSMsgType.CLOSED:
                break
            await queue.put(json.loads(msg.data))

    ws._connect_one = fake_connect_one
    q = asyncio.Queue()
    await ws._connect_one(slugs, q)

    assert len(sent) == 1
    payload = sent[0]
    assert "subscribe" in payload
    sub = payload["subscribe"]
    assert sub["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA"
    assert sub["marketSlugs"] == slugs
    assert "requestId" in sub


# ---------------------------------------------------------------------------
# test_reconnect_on_unclean_close
# ---------------------------------------------------------------------------
async def test_reconnect_on_unclean_close():
    """One unclean close must trigger exactly one reconnect attempt."""
    slugs = ["m1"]
    ws = PolymarketUSWebSocket(ws_url="wss://fake", slugs=slugs)

    call_count = 0
    results = [
        RuntimeError("unclean close"),  # first attempt fails
        None,                            # second attempt succeeds (returns nothing / clean)
    ]

    async def fake_open(url):
        nonlocal call_count
        call_count += 1
        err = results[call_count - 1]
        if isinstance(err, Exception):
            raise err
        # Return a minimal clean-close WS
        return FakeWS(messages=[])

    q: asyncio.Queue = asyncio.Queue()

    # Patch _open_ws inside the method being tested
    with patch.object(ws, "_open_ws", side_effect=fake_open):
        # _run_connection_with_retry retries once then gives up
        await ws._run_connection_with_retry(slugs, q, max_retries=2, base_delay=0)

    assert call_count == 2, f"Expected 2 connection attempts, got {call_count}"


# ---------------------------------------------------------------------------
# test_merge_stream_from_multiple_conns
# ---------------------------------------------------------------------------
async def test_merge_stream_from_multiple_conns():
    """Two connections emitting messages must all appear in the shared queue.

    We force two separate connections by calling _run_connection_with_retry
    directly with two distinct slug chunks and two distinct fake WebSockets.
    """
    chunk_a = ["a1", "a2"]
    chunk_b = ["b1", "b2"]
    all_slugs = chunk_a + chunk_b

    messages_a = [json.dumps({"slug": "a1", "price": 0.5}), json.dumps({"slug": "a2", "price": 0.6})]
    messages_b = [json.dumps({"slug": "b1", "price": 0.7}), json.dumps({"slug": "b2", "price": 0.8})]

    ws = PolymarketUSWebSocket(ws_url="wss://fake", slugs=all_slugs)

    # Map each chunk's first slug to the appropriate FakeWS so each connection
    # gets its own independent message stream.
    fake_ws_map = {
        "a1": FakeWS(messages=messages_a),
        "b1": FakeWS(messages=messages_b),
    }

    async def fake_open(url):
        # We'll figure out which FakeWS to return from context — not possible
        # here, so instead we patch at the task level using a closure.
        raise RuntimeError("should not be called directly in this test")

    q: asyncio.Queue = asyncio.Queue()

    async def run_chunk(chunk):
        """Run _run_connection_with_retry with the matching FakeWS."""
        first = chunk[0]
        fake = fake_ws_map[first]

        async def _open(url):
            return fake

        original_open = ws._open_ws
        ws._open_ws = _open
        try:
            await ws._run_connection_with_retry(chunk, q, max_retries=1, base_delay=0)
        finally:
            ws._open_ws = original_open

    # Run both connections concurrently
    await asyncio.gather(
        run_chunk(chunk_a),
        run_chunk(chunk_b),
        return_exceptions=True,
    )

    # Drain queue
    received = []
    while not q.empty():
        received.append(await q.get())

    slugs_received = {msg["slug"] for msg in received}
    assert "a1" in slugs_received
    assert "a2" in slugs_received
    assert "b1" in slugs_received
    assert "b2" in slugs_received
