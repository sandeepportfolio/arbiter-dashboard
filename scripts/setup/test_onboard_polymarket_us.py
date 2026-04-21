"""Unit tests for onboard_polymarket_us.py.

Tests are isolated — no real browser or network required.

Coverage:
    - rewrite_env_file() with synthetic .env buffers
    - Secret value is never present in stdout when running the rewrite
"""
from __future__ import annotations

import io
import sys
import contextlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.setup.onboard_polymarket_us import rewrite_env_file, _scrub_env_file

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_KEY_ID = "test-key-id-abc123"
FAKE_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # 44-char base64


# ---------------------------------------------------------------------------
# rewrite_env_file tests
# ---------------------------------------------------------------------------


class TestRewriteEnvFile:
    def test_sets_new_values_when_lines_present(self):
        """Existing key/secret lines are replaced in-place."""
        original = (
            "FOO=bar\n"
            "POLYMARKET_US_API_KEY_ID=old-id\n"
            "POLYMARKET_US_API_SECRET=old-secret\n"
            "BAZ=qux\n"
        )
        result = rewrite_env_file(original, FAKE_KEY_ID, FAKE_SECRET)
        assert f"POLYMARKET_US_API_KEY_ID={FAKE_KEY_ID}" in result
        assert f"POLYMARKET_US_API_SECRET={FAKE_SECRET}" in result
        # Old values gone
        assert "old-id" not in result
        assert "old-secret" not in result
        # Other lines preserved
        assert "FOO=bar" in result
        assert "BAZ=qux" in result

    def test_appends_when_keys_absent(self):
        """If the keys don't exist in the file, they are appended at the end."""
        original = "EXISTING=value\n"
        result = rewrite_env_file(original, FAKE_KEY_ID, FAKE_SECRET)
        assert f"POLYMARKET_US_API_KEY_ID={FAKE_KEY_ID}" in result
        assert f"POLYMARKET_US_API_SECRET={FAKE_SECRET}" in result
        assert "EXISTING=value" in result

    def test_handles_empty_file(self):
        """An empty .env file produces two appended lines."""
        result = rewrite_env_file("", FAKE_KEY_ID, FAKE_SECRET)
        assert f"POLYMARKET_US_API_KEY_ID={FAKE_KEY_ID}" in result
        assert f"POLYMARKET_US_API_SECRET={FAKE_SECRET}" in result

    def test_preserves_comments_and_blank_lines(self):
        """Comments and blank lines are preserved verbatim."""
        original = (
            "# Top comment\n"
            "\n"
            "FOO=bar\n"
            "# Another comment\n"
            "POLYMARKET_US_API_KEY_ID=old\n"
            "POLYMARKET_US_API_SECRET=oldsecret\n"
        )
        result = rewrite_env_file(original, FAKE_KEY_ID, FAKE_SECRET)
        assert "# Top comment" in result
        assert "# Another comment" in result
        # Blank line preserved
        lines = result.splitlines()
        assert "" in lines

    def test_handles_key_id_without_secret(self):
        """Only one key present — the other is appended."""
        original = "POLYMARKET_US_API_KEY_ID=old-id\n"
        result = rewrite_env_file(original, FAKE_KEY_ID, FAKE_SECRET)
        assert f"POLYMARKET_US_API_KEY_ID={FAKE_KEY_ID}" in result
        assert f"POLYMARKET_US_API_SECRET={FAKE_SECRET}" in result
        assert "old-id" not in result

    def test_handles_secret_without_key_id(self):
        """Only secret present — key_id is appended."""
        original = "POLYMARKET_US_API_SECRET=old-secret\n"
        result = rewrite_env_file(original, FAKE_KEY_ID, FAKE_SECRET)
        assert f"POLYMARKET_US_API_KEY_ID={FAKE_KEY_ID}" in result
        assert f"POLYMARKET_US_API_SECRET={FAKE_SECRET}" in result
        assert "old-secret" not in result

    def test_does_not_duplicate_keys_on_multiple_rewrites(self):
        """Calling rewrite twice does not create duplicate entries."""
        original = "OTHER=1\n"
        first = rewrite_env_file(original, FAKE_KEY_ID, FAKE_SECRET)
        second = rewrite_env_file(first, FAKE_KEY_ID, FAKE_SECRET)
        # Each key appears exactly once
        assert second.count("POLYMARKET_US_API_KEY_ID=") == 1
        assert second.count("POLYMARKET_US_API_SECRET=") == 1

    def test_result_contains_no_old_placeholder(self):
        """Placeholders from the template file are replaced."""
        original = (
            "POLYMARKET_US_API_KEY_ID=<paste Key ID from polymarket.us/developer>\n"
            "POLYMARKET_US_API_SECRET=<paste base64 Ed25519 secret, shown once on key creation>\n"
        )
        result = rewrite_env_file(original, FAKE_KEY_ID, FAKE_SECRET)
        assert "<paste" not in result


# ---------------------------------------------------------------------------
# Secret-never-in-stdout tests
# ---------------------------------------------------------------------------


class TestSecretNotLeakedToStdout:
    """Verify the secret value never appears in stdout when running the rewrite."""

    def test_rewrite_does_not_print_secret(self, capsys):
        """rewrite_env_file() must not print the secret to stdout."""
        original = "OTHER=value\n"
        rewrite_env_file(original, FAKE_KEY_ID, FAKE_SECRET)
        captured = capsys.readouterr()
        assert FAKE_SECRET not in captured.out, (
            "Secret must not appear in stdout output of rewrite_env_file()"
        )
        assert FAKE_SECRET not in captured.err, (
            "Secret must not appear in stderr output of rewrite_env_file()"
        )

    def test_rewrite_does_not_echo_secret_to_captured_output(self):
        """Explicit stdout/stderr capture: secret not in either stream."""
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            rewrite_env_file("POLYMARKET_US_API_SECRET=old\n", FAKE_KEY_ID, FAKE_SECRET)
        assert FAKE_SECRET not in stdout_buf.getvalue()
        assert FAKE_SECRET not in stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# _scrub_env_file tests
# ---------------------------------------------------------------------------


class TestScrubEnvFile:
    def test_scrub_removes_partial_credentials(self, tmp_path):
        """_scrub_env_file removes any POLYMARKET_US_API_KEY_ID/SECRET lines."""
        env_file = tmp_path / ".env.production"
        env_file.write_text(
            "EXISTING=value\n"
            "POLYMARKET_US_API_KEY_ID=partial-id\n"
            "POLYMARKET_US_API_SECRET=partial-secret\n"
            "OTHER=keep\n",
            encoding="utf-8",
        )
        _scrub_env_file(env_file)
        result = env_file.read_text(encoding="utf-8")
        assert "POLYMARKET_US_API_KEY_ID" not in result
        assert "POLYMARKET_US_API_SECRET" not in result
        assert "EXISTING=value" in result
        assert "OTHER=keep" in result

    def test_scrub_is_noop_when_file_absent(self, tmp_path):
        """_scrub_env_file does not raise when the file doesn't exist."""
        missing = tmp_path / "nonexistent.env"
        _scrub_env_file(missing)  # should not raise
