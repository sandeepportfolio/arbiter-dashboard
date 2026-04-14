import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from arbiter.utils.retry import RateLimiter


def test_rate_limiter_penalty_stats():
    limiter = RateLimiter("test", max_requests=5, window_seconds=1.0)
    applied = limiter.penalize(0.25, reason="unit_test")
    stats = limiter.stats

    assert applied == 0.25
    assert stats["penalty_count"] == 1
    assert stats["last_penalty_reason"] == "unit_test"
    assert stats["remaining_penalty_seconds"] > 0.0


def test_rate_limiter_retry_after_parses_seconds_and_http_date():
    limiter = RateLimiter("test", max_requests=5, window_seconds=1.0)

    applied_seconds = limiter.apply_retry_after("2", fallback_delay=0.5, reason="seconds")
    assert applied_seconds == 2.0

    http_date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=3))
    applied_http = limiter.apply_retry_after(http_date, fallback_delay=0.5, reason="http_date")
    assert applied_http > 0.0


def test_rate_limiter_acquire_waits_for_penalty():
    async def runner():
        limiter = RateLimiter("test", max_requests=1, window_seconds=1.0)
        limiter.penalize(0.05, reason="wait_test")
        started = time.perf_counter()
        await limiter.acquire()
        elapsed = time.perf_counter() - started

        assert elapsed >= 0.04
        assert limiter.stats["total_acquires"] == 1
        assert limiter.stats["total_wait_time"] >= 0.04

    asyncio.run(runner())
