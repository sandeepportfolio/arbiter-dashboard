"""
Tests for TelegramNotifier.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbiter.monitor.balance import TelegramNotifier


@pytest.fixture
def notifier():
    return TelegramNotifier(
        bot_token="test_bot_token",
        chat_id="123456789",
    )


class TestTelegramNotifier:
    def test_initialization(self, notifier):
        assert notifier.bot_token == "test_bot_token"
        assert notifier.chat_id == "123456789"
        assert notifier._enabled is True

    @pytest.mark.asyncio
    async def test_send_message_success(self, notifier):
        """send_message should return True when HTTP POST succeeds."""
        mock_response = AsyncMock()
        mock_response.status = 200

        with patch.object(notifier, "_get_session", new_callable=AsyncMock) as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            session_instance.post.return_value.__aenter__.return_value = mock_response

            result = await notifier.send("Test message")

            assert result is True
            session_instance.post.assert_called_once()
            call_url = session_instance.post.call_args
            assert "api.telegram.org" in str(call_url)

    @pytest.mark.asyncio
    async def test_send_message_failure(self, notifier):
        """send_message should return False when HTTP POST fails."""
        mock_response = AsyncMock()
        mock_response.status = 400
        mock_response.text = AsyncMock(return_value="Bad Request")

        with patch.object(notifier, "_get_session", new_callable=AsyncMock) as mock_session:
            session_instance = MagicMock()
            mock_session.return_value = session_instance
            session_instance.post.return_value.__aenter__.return_value = mock_response

            result = await notifier.send("Test message")
            assert result is False

    @pytest.mark.asyncio
    async def test_disabled_notifier_returns_false(self):
        """Notifier with empty bot_token should return False without network call."""
        disabled = TelegramNotifier(bot_token="", chat_id="")
        result = await disabled.send("Should not send")
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
