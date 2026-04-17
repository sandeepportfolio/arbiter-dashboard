"""
ARBITER — Retry & Circuit Breaker utilities.
Used by all collectors for resilient API calls.
"""
import asyncio
import logging
import time
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Any

logger = logging.getLogger("arbiter.retry")


class CircuitState(Enum):
    CLOSED = "closed"       # normal operation
    OPEN = "open"           # failing, reject calls
    HALF_OPEN = "half_open" # testing recovery


@dataclass
class CircuitBreaker:
    """
    Circuit breaker pattern for API calls.
    Opens after `failure_threshold` consecutive failures.
    Tries again after `recovery_timeout` seconds.
    """
    name: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max: int = 2

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _half_open_attempts: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _total_calls: int = field(default=0, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_attempts = 0
                logger.info(f"Circuit [{self.name}] → HALF_OPEN (testing recovery)")
        return self._state

    def record_success(self):
        self._total_calls += 1
        self._success_count += 1
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_attempts += 1
            if self._half_open_attempts >= self.half_open_max:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                logger.info(f"Circuit [{self.name}] → CLOSED (recovered)")
        elif self._state == CircuitState.CLOSED:
            self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self):
        self._total_calls += 1
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning(f"Circuit [{self.name}] → OPEN (recovery failed)")
        elif self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.warning(f"Circuit [{self.name}] → OPEN ({self._failure_count} consecutive failures)")

    def can_execute(self) -> bool:
        s = self.state  # triggers state transition check
        return s != CircuitState.OPEN

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "state": self._state.value,
            "failures": self._failure_count,
            "total_calls": self._total_calls,
            "success_count": self._success_count,
            "success_rate": round(self._success_count / max(self._total_calls, 1), 3),
        }


async def retry_with_backoff(
    fn: Callable,
    retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    circuit: Optional[CircuitBreaker] = None,
    on_retry: Optional[Callable] = None,
) -> Any:
    """
    Execute an async function with exponential backoff retry.
    Integrates with CircuitBreaker if provided.
    """
    if circuit and not circuit.can_execute():
        raise CircuitOpenError(f"Circuit [{circuit.name}] is OPEN, call rejected")

    last_error = None
    for attempt in range(retries + 1):
        try:
            result = await fn()
            if circuit:
                circuit.record_success()
            return result
        except Exception as e:
            last_error = e
            if circuit:
                circuit.record_failure()

            if attempt < retries:
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                # Add jitter: 0.5x to 1.5x
                import random
                delay *= 0.5 + random.random()

                logger.warning(
                    f"Retry {attempt + 1}/{retries} after {delay:.1f}s: {e}"
                )
                if on_retry:
                    on_retry(attempt, delay, e)
                await asyncio.sleep(delay)
            else:
                logger.error(f"All {retries + 1} attempts failed: {e}")

    raise last_error


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open."""
    pass


@dataclass
class SessionManager:
    """
    Manages API sessions with automatic re-authentication.
    Tracks session expiry, handles token refresh, and manages cookies.
    """
    name: str
    session_ttl: float = 3600.0  # 1 hour default
    max_session_age: float = 86400.0  # 24 hours max

    _session_token: Optional[str] = field(default=None, init=False)
    _session_created: float = field(default=0.0, init=False)
    _last_activity: float = field(default=0.0, init=False)
    _refresh_count: int = field(default=0, init=False)
    _auth_fn: Optional[Callable] = field(default=None, init=False)

    def set_auth_fn(self, fn: Callable):
        """Set the async function that performs authentication."""
        self._auth_fn = fn

    @property
    def is_expired(self) -> bool:
        if not self._session_token:
            return True
        now = time.time()
        if now - self._session_created > self.max_session_age:
            return True
        if now - self._last_activity > self.session_ttl:
            return True
        return False

    @property
    def time_until_expiry(self) -> float:
        if not self._session_token:
            return 0.0
        activity_remaining = self.session_ttl - (time.time() - self._last_activity)
        age_remaining = self.max_session_age - (time.time() - self._session_created)
        return max(0.0, min(activity_remaining, age_remaining))

    async def get_token(self) -> Optional[str]:
        """Get a valid session token, refreshing if needed."""
        if self.is_expired:
            await self.refresh()
        self._last_activity = time.time()
        return self._session_token

    async def refresh(self) -> bool:
        """Re-authenticate and get a fresh session."""
        if not self._auth_fn:
            logger.warning(f"Session [{self.name}]: no auth function configured")
            return False
        try:
            token = await self._auth_fn()
            if token:
                self._session_token = token
                self._session_created = time.time()
                self._last_activity = time.time()
                self._refresh_count += 1
                logger.info(f"Session [{self.name}]: refreshed (#{self._refresh_count})")
                return True
            else:
                logger.warning(f"Session [{self.name}]: auth returned no token")
                return False
        except Exception as e:
            logger.error(f"Session [{self.name}]: refresh failed: {e}")
            return False

    def invalidate(self):
        """Force session expiry (e.g., on 401 response)."""
        self._session_token = None
        logger.info(f"Session [{self.name}]: invalidated")

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "has_token": self._session_token is not None,
            "expired": self.is_expired,
            "ttl_remaining": round(self.time_until_expiry, 1),
            "refresh_count": self._refresh_count,
            "age": round(time.time() - self._session_created, 1) if self._session_created > 0 else 0,
        }


@dataclass
class RateLimiter:
    """
    Token bucket rate limiter for API calls.
    Prevents hitting platform rate limits.
    """
    name: str
    max_requests: int = 10      # per window
    window_seconds: float = 1.0  # window size

    _tokens: float = field(default=0, init=False)
    _last_refill: float = field(default=0, init=False)
    _penalty_until: float = field(default=0.0, init=False)
    _penalty_count: int = field(default=0, init=False)
    _total_wait_time: float = field(default=0.0, init=False)
    _last_wait_seconds: float = field(default=0.0, init=False)
    _last_penalty_reason: str = field(default="", init=False)
    _total_acquires: int = field(default=0, init=False)

    def __post_init__(self):
        self._tokens = float(self.max_requests)
        self._last_refill = time.time()

    async def acquire(self):
        """Wait until a request token is available."""
        while True:
            wait = self.remaining_penalty_seconds
            if wait > 0:
                self._last_wait_seconds = wait
                self._total_wait_time += wait
                await asyncio.sleep(wait)
                continue
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                self._total_acquires += 1
                return
            # Wait for next token
            wait = self.window_seconds / self.max_requests
            self._last_wait_seconds = wait
            self._total_wait_time += wait
            await asyncio.sleep(wait)

    def _refill(self):
        now = time.time()
        elapsed = now - self._last_refill
        refill = elapsed * (self.max_requests / self.window_seconds)
        self._tokens = min(float(self.max_requests), self._tokens + refill)
        self._last_refill = now

    @property
    def available_tokens(self) -> int:
        self._refill()
        return int(self._tokens)

    @property
    def remaining_penalty_seconds(self) -> float:
        return max(self._penalty_until - time.time(), 0.0)

    def penalize(self, delay_seconds: float, reason: str = "rate_limited") -> float:
        delay_seconds = max(float(delay_seconds or 0.0), 0.0)
        if delay_seconds <= 0:
            return 0.0
        now = time.time()
        self._penalty_until = max(self._penalty_until, now + delay_seconds)
        self._penalty_count += 1
        self._last_penalty_reason = reason
        self._tokens = min(self._tokens, 0.0)
        return delay_seconds

    def apply_retry_after(self, retry_after: Any, fallback_delay: float, reason: str = "rate_limited") -> float:
        delay = self._parse_retry_after(retry_after)
        if delay is None:
            delay = max(float(fallback_delay or 0.0), 0.0)
        return self.penalize(delay, reason=reason)

    @staticmethod
    def _parse_retry_after(retry_after: Any) -> Optional[float]:
        if retry_after in (None, ""):
            return None
        if isinstance(retry_after, (int, float)):
            return max(float(retry_after), 0.0)
        raw = str(retry_after).strip()
        if not raw:
            return None
        try:
            return max(float(raw), 0.0)
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                return None
            return max(target.timestamp() - time.time(), 0.0)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "available_tokens": self.available_tokens,
            # SAFE-04: max_requests + time_window let the dashboard render the
            # "tokens available / cap" pill (plan 03-07) without hardcoding the
            # per-platform config.
            "max_requests": float(self.max_requests),
            "time_window": float(self.window_seconds),
            "remaining_penalty_seconds": round(self.remaining_penalty_seconds, 3),
            "penalty_count": self._penalty_count,
            "last_wait_seconds": round(self._last_wait_seconds, 3),
            "total_wait_time": round(self._total_wait_time, 3),
            "total_acquires": self._total_acquires,
            "last_penalty_reason": self._last_penalty_reason,
        }
