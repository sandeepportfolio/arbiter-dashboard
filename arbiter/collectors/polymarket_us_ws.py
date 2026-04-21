"""
Polymarket US WebSocket multiplexer.

Connects ceil(len(slugs) / 100) WebSocket connections, each subscribed
to at most 100 market slugs. All messages are merged into a single
asyncio.Queue for the caller to consume.

Reconnect behaviour:
  - Exponential backoff: 1s, 2s, 4s, 8s … capped at 30s.
  - Unclean closes (exceptions) trigger a retry; clean CLOSED frames exit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import aiohttp

logger = logging.getLogger("arbiter.collector.polymarket_us_ws")

_CHUNK_SIZE = 100
_MAX_BACKOFF = 30.0


@dataclass
class PolymarketUSWebSocket:
    """Multiplexed WebSocket subscriber for Polymarket US market data.

    Parameters
    ----------
    ws_url:
        WebSocket endpoint, e.g. ``"wss://api.polymarket.us/v1/ws/markets"``.
    slugs:
        List of market slugs to subscribe to. Chunked into batches of ≤100.
    queue:
        Shared output queue. If *None*, one is created internally when
        :meth:`run` is called.
    """

    ws_url: str
    slugs: List[str]
    queue: Optional[asyncio.Queue] = field(default=None)

    # ------------------------------------------------------------------
    # Chunk helpers
    # ------------------------------------------------------------------

    def _chunk_slugs(self) -> List[List[str]]:
        """Return *slugs* split into sublists of at most ``_CHUNK_SIZE``."""
        result: List[List[str]] = []
        for i in range(0, len(self.slugs), _CHUNK_SIZE):
            result.append(self.slugs[i : i + _CHUNK_SIZE])
        return result

    # ------------------------------------------------------------------
    # Low-level WebSocket helpers
    # ------------------------------------------------------------------

    async def _open_ws(self, url: str) -> aiohttp.ClientWebSocketResponse:
        """Open a WebSocket connection to *url*. Separated for testability."""
        session = aiohttp.ClientSession()
        return await session.ws_connect(url)

    async def _subscribe(
        self, ws: aiohttp.ClientWebSocketResponse, slugs: List[str]
    ) -> None:
        """Send the subscription message on *ws* for *slugs*."""
        payload = {
            "subscribe": {
                "requestId": str(uuid.uuid4()),
                "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                "marketSlugs": slugs,
            }
        }
        await ws.send_str(json.dumps(payload))
        logger.debug(
            "polymarket-us-ws subscribed to %d slugs (first: %s)",
            len(slugs),
            slugs[0] if slugs else "",
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def _run_connection_with_retry(
        self,
        chunk: List[str],
        queue: asyncio.Queue,
        max_retries: int = 16,
        base_delay: float = 1.0,
    ) -> None:
        """Run one connection for *chunk* with exponential-backoff retries.

        A clean WebSocket close (CLOSED frame) is treated as a normal end and
        does **not** trigger a retry. Any exception triggers a retry.

        Parameters
        ----------
        chunk:
            The slug subset for this connection.
        queue:
            Shared output queue — parsed JSON dicts are put here.
        max_retries:
            Maximum number of reconnect attempts before giving up.
        base_delay:
            Initial backoff delay (seconds). Each retry doubles it, capped at
            ``_MAX_BACKOFF``.
        """
        attempt = 0
        while attempt <= max_retries:
            try:
                ws = await self._open_ws(self.ws_url)
                try:
                    await self._subscribe(ws, chunk)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                parsed = json.loads(msg.data)
                                await queue.put(parsed)
                            except json.JSONDecodeError:
                                logger.warning(
                                    "polymarket-us-ws: invalid JSON in message"
                                )
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                        ):
                            # Clean close — exit without retry
                            logger.info(
                                "polymarket-us-ws: clean close for chunk[%s…]",
                                chunk[0] if chunk else "",
                            )
                            return
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError("WebSocket error frame received")
                    # Iteration exhausted without CLOSED frame → treat as clean
                    return
                finally:
                    if not ws.closed:
                        await ws.close()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                if attempt > max_retries:
                    logger.error(
                        "polymarket-us-ws: max retries exhausted for chunk [%s…]: %s",
                        chunk[0] if chunk else "",
                        exc,
                    )
                    return

                delay = min(base_delay * (2 ** (attempt - 1)), _MAX_BACKOFF)
                logger.warning(
                    "polymarket-us-ws: reconnect attempt %d/%d in %.1fs: %s",
                    attempt,
                    max_retries,
                    delay,
                    exc,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> asyncio.Queue:
        """Start all connections and return the shared output queue.

        Launches one :meth:`_run_connection_with_retry` task per slug chunk.
        All tasks write into the same ``asyncio.Queue``.

        Returns
        -------
        asyncio.Queue
            The merged message queue. Caller must drain it.
        """
        if self.queue is None:
            self.queue = asyncio.Queue()

        chunks = self._chunk_slugs()
        logger.info(
            "polymarket-us-ws: starting %d connections for %d slugs",
            len(chunks),
            len(self.slugs),
        )

        tasks = [
            asyncio.create_task(
                self._run_connection_with_retry(chunk, self.queue)
            )
            for chunk in chunks
        ]
        # Store tasks so the caller can cancel them
        self._tasks = tasks
        return self.queue

    async def stop(self) -> None:
        """Cancel all running connection tasks."""
        for task in getattr(self, "_tasks", []):
            task.cancel()
        await asyncio.gather(*getattr(self, "_tasks", []), return_exceptions=True)
        self._tasks = []
