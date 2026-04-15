"""
Tests for API auth helpers.
"""
import asyncio
import hashlib
import hmac
import os
import sys
import time
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbiter import api as api_module


class TestPasswordHashing:
    def test_hash_password_is_sha256(self):
        h = api_module._hash_password("testpassword")
        expected = hashlib.sha256(b"testpassword").hexdigest()
        assert h == expected

    def test_same_password_same_hash(self):
        h1 = api_module._hash_password("abc123")
        h2 = api_module._hash_password("abc123")
        assert h1 == h2

    def test_different_passwords_different_hash(self):
        h1 = api_module._hash_password("password1")
        h2 = api_module._hash_password("password2")
        assert h1 != h2


class TestTokenGeneration:
    def _secret(self):
        return "test-secret-12345"

    def test_generate_token_format(self):
        secret = self._secret()
        with patch.object(api_module, "_get_secret", return_value=secret):
            token = api_module._generate_token("test@example.com")
        parts = token.split(":")
        assert len(parts) == 3
        assert parts[0] == "test@example.com"
        assert int(parts[1]) > 0  # timestamp

    def test_verify_valid_token(self):
        secret = self._secret()
        email = "user@test.com"
        with patch.object(api_module, "_get_secret", return_value=secret):
            token = api_module._generate_token(email)
            result = api_module._verify_token(token)
        assert result == email

    def test_verify_tampered_token(self):
        secret = self._secret()
        with patch.object(api_module, "_get_secret", return_value=secret):
            token = api_module._generate_token("user@test.com")
        tampered = token[:-5] + "XXXXX"
        with patch.object(api_module, "_get_secret", return_value=secret):
            result = api_module._verify_token(tampered)
        assert result is None

    def test_verify_expired_token(self):
        """Token older than 7 days should be rejected."""
        secret = self._secret()
        old_ts = str(int(time.time()) - 8 * 86400)  # 8 days ago
        fake_token = f"user@test.com:{old_ts}:badsig"
        with patch.object(api_module, "_get_secret", return_value=secret):
            result = api_module._verify_token(fake_token)
        assert result is None

    def test_verify_empty_token(self):
        secret = self._secret()
        with patch.object(api_module, "_get_secret", return_value=secret):
            assert api_module._verify_token("") is None
            assert api_module._verify_token("not-a-valid-token") is None

    def test_verify_wrong_secret(self):
        with patch.object(api_module, "_get_secret", return_value="test-secret-12345"):
            token = api_module._generate_token("user@test.com")
        with patch.object(api_module, "_get_secret", return_value="different-secret"):
            result = api_module._verify_token(token)
        assert result is None


class TestAllowedUsers:
    def test_default_user_configured(self):
        """Default user should be configured from env or fallback."""
        # Should always have at least the fallback user
        assert len(api_module.UI_ALLOWED_USERS) >= 1

    def test_password_must_be_hashed(self):
        """Stored passwords should be hashed, not plaintext."""
        for email, hashed in api_module.UI_ALLOWED_USERS.items():
            # SHA-256 hex digest is 64 characters
            assert len(hashed) == 64
            assert all(c in "0123456789abcdef" for c in hashed)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
