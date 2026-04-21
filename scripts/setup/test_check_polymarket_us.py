"""Tests for scripts/setup/check_polymarket_us.py — Task 17.

M4 invariant: the secret value MUST NEVER appear in stdout or stderr.

Tests use either aioresponses (for async mock) or subprocess.run (for
stdout/stderr capture of the full script).
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest
from aioresponses import aioresponses

# Add repo root to path so we can import the script's async function.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, _REPO_ROOT)

from scripts.setup.check_polymarket_us import _check  # noqa: E402

# ── Test credentials ────────────────────────────────────────────────────────
# 32 zero bytes base64-encoded — a structurally valid Ed25519 seed.
VALID_SECRET_B64 = base64.b64encode(bytes(32)).decode()  # 44 chars
VALID_KEY_ID = "test-key-id-unit-abc"
API_URL = "https://api.polymarket.us"


# ─── Happy path ─────────────────────────────────────────────────────────────

async def test_happy_path_exits_0(monkeypatch):
    """Script exits 0 when balance response returns $100."""
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", VALID_SECRET_B64)
    monkeypatch.setenv("POLYMARKET_US_API_URL", API_URL)

    with aioresponses() as m:
        m.get(
            f"{API_URL}/v1/account/balances",
            payload={"currentBalance": 100.0},
            status=200,
        )
        result = await _check()

    assert result == 0


# ─── Auth failure ────────────────────────────────────────────────────────────

async def test_bad_auth_exits_1(monkeypatch, capsys):
    """Script exits 1 on HTTP 401 and prints the auth-failure message."""
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", VALID_SECRET_B64)
    monkeypatch.setenv("POLYMARKET_US_API_URL", API_URL)

    with aioresponses() as m:
        m.get(
            f"{API_URL}/v1/account/balances",
            status=401,
            payload={"detail": "unauthorized"},
        )
        result = await _check()

    assert result == 1
    captured = capsys.readouterr()
    assert "Auth failed" in captured.err


# ─── Missing env ─────────────────────────────────────────────────────────────

async def test_missing_env_exits_1(monkeypatch):
    """Script exits 1 immediately when env vars are not set."""
    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_API_SECRET", raising=False)

    result = await _check()

    assert result == 1


# ─── Secret-leak invariants (M4) ─────────────────────────────────────────────

# The fake secret we'll inject — any 32-byte seed that's *different* from
# the all-zeros default, so it stands out clearly in output.
_LEAK_SECRET_B64 = base64.b64encode(bytes(range(32))).decode()
_LEAK_SECRET_RAW = _LEAK_SECRET_B64  # The string we must NOT see in output


def _run_script(extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run check_polymarket_us.py as a subprocess with the given env overlay."""
    env = {**os.environ, **extra_env}
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "check_polymarket_us.py")],
        capture_output=True,
        text=True,
        env=env,
    )


def test_secret_never_in_stdout():
    """M4: the secret string must not appear in stdout under any exit path.

    We inject a fake but structurally valid secret and check that the exact
    base64 string never leaks to stdout, even on error paths.
    """
    result = _run_script({
        "POLYMARKET_US_API_KEY_ID": VALID_KEY_ID,
        "POLYMARKET_US_API_SECRET": _LEAK_SECRET_B64,
        # No POLYMARKET_US_API_URL set — will fail with network error, which
        # exercises the error path without needing a live connection.
        "POLYMARKET_US_API_URL": "http://127.0.0.1:1",  # unreachable
    })
    # The script will exit 1 (network error) — we only care about stdout.
    assert _LEAK_SECRET_RAW not in result.stdout, (
        f"SECRET LEAKED to stdout!\nstdout: {result.stdout[:500]}"
    )


def test_secret_never_in_stderr():
    """M4: the secret string must not appear in stderr under any exit path."""
    result = _run_script({
        "POLYMARKET_US_API_KEY_ID": VALID_KEY_ID,
        "POLYMARKET_US_API_SECRET": _LEAK_SECRET_B64,
        "POLYMARKET_US_API_URL": "http://127.0.0.1:1",  # unreachable
    })
    assert _LEAK_SECRET_RAW not in result.stderr, (
        f"SECRET LEAKED to stderr!\nstderr: {result.stderr[:500]}"
    )
