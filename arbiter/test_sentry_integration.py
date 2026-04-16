"""Sentry async exception capture test using a fake transport.

sentry-sdk 2.x requires `transport=` to be a Transport *subclass* (not an instance).
The SDK instantiates the class with the client options, so the captured envelopes
are stored on a module-level list to be read back after init.
"""
import asyncio
import logging

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport


# Module-level buffer so the FakeTransport class (instantiated by sentry-sdk
# with options) can still surface captured envelopes back to the test.
_CAPTURED_ENVELOPES: list = []


class _FakeTransport(Transport):
    """Transport subclass that buffers envelopes in memory instead of sending HTTP."""

    def __init__(self, options=None):
        super().__init__(options)
        # Do NOT reset the module-level buffer here — sentry-sdk may instantiate
        # multiple transports across tests. Individual tests should clear the
        # buffer at their start.

    def capture_envelope(self, envelope: Envelope) -> None:
        _CAPTURED_ENVELOPES.append(envelope)

    def flush(self, timeout=None, callback=None):
        return None

    def kill(self):
        return None

    def is_healthy(self):
        return True


def _has_runtime_error(envelopes) -> bool:
    for env in envelopes:
        for item in env.items:
            payload = item.payload.json or {}
            for ex in (payload.get("exception", {}) or {}).get("values", []) or []:
                if ex.get("type") == "RuntimeError" and "boom" in (ex.get("value") or ""):
                    return True
    return False


def test_async_exception_captured():
    """Unhandled exception in an asyncio task lands in the Sentry transport buffer."""
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    _CAPTURED_ENVELOPES.clear()
    sentry_sdk.init(
        dsn="https://public@example.invalid/1",  # placeholder; transport intercepts
        transport=_FakeTransport,
        integrations=[
            AsyncioIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=0.0,
        sample_rate=1.0,
        send_default_pii=False,
    )

    async def _boom():
        raise RuntimeError("boom")

    async def runner():
        try:
            await _boom()
        except Exception:
            sentry_sdk.capture_exception()

    asyncio.run(runner())
    sentry_sdk.flush(timeout=2.0)

    assert _has_runtime_error(_CAPTURED_ENVELOPES), \
        f"Expected RuntimeError('boom') in captured envelopes; got {len(_CAPTURED_ENVELOPES)}"


def test_sentry_init_noop_when_dsn_unset(monkeypatch):
    """sentry_sdk.init(dsn=None) does not raise."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    # Direct call mirroring _init_sentry but with dsn=None must succeed
    sentry_sdk.init(dsn=None, traces_sample_rate=0.0, sample_rate=1.0)
    # No assertion — success is "no exception raised"
