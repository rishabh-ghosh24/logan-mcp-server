"""Tests for NotificationService."""
import json
import pytest
from unittest.mock import patch, MagicMock
from oci_logan_mcp.notification_service import NotificationService
from oci_logan_mcp.config import Settings, SlackConfig, TelegramConfig, NotificationsConfig


def make_settings(slack_url="", telegram_token="", telegram_chat=""):
    s = Settings()
    s.notifications.slack.webhook_url = slack_url
    s.notifications.telegram.bot_token = telegram_token
    s.notifications.telegram.default_chat_id = telegram_chat
    return s


class TestSendToSlack:
    @pytest.mark.asyncio
    async def test_raises_if_not_configured(self):
        svc = NotificationService(make_settings())
        with pytest.raises(ValueError, match="Slack not configured"):
            await svc.send_to_slack(message="hello")

    @pytest.mark.asyncio
    async def test_sends_message_only(self):
        svc = NotificationService(make_settings(slack_url="https://hooks.slack.com/test"))
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value.status = 200
            result = await svc.send_to_slack(message="hello world")
        assert result["status"] == "sent"
        assert mock_urlopen.called

    @pytest.mark.asyncio
    async def test_sends_with_query_result(self):
        svc = NotificationService(make_settings(slack_url="https://hooks.slack.com/test"))
        query_result = {
            "columns": [{"name": "count"}],
            "rows": [[42]],
            "total_count": 1,
        }
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value.status = 200
            result = await svc.send_to_slack(message="results:", query_result=query_result)
        assert result["status"] == "sent"

    @pytest.mark.asyncio
    async def test_raises_if_no_message_or_query(self):
        svc = NotificationService(make_settings(slack_url="https://hooks.slack.com/test"))
        with pytest.raises(ValueError, match="message or query"):
            await svc.send_to_slack()

    @pytest.mark.asyncio
    async def test_raises_on_non_200(self):
        svc = NotificationService(make_settings(slack_url="https://hooks.slack.com/test"))
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value.status = 400
            mock_urlopen.return_value.read.return_value = b"invalid_payload"
            with pytest.raises(RuntimeError, match="Slack delivery failed"):
                await svc.send_to_slack(message="test")


class TestSendToTelegram:
    @pytest.mark.asyncio
    async def test_raises_if_not_configured(self):
        svc = NotificationService(make_settings())
        with pytest.raises(ValueError, match="Telegram not configured"):
            await svc.send_to_telegram(message="hello")

    @pytest.mark.asyncio
    async def test_sends_message(self):
        svc = NotificationService(make_settings(
            telegram_token="123:ABC", telegram_chat="-100999"
        ))
        with patch("urllib.request.urlopen") as mock_urlopen:
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({"ok": True}).encode()
            mock_urlopen.return_value = resp
            result = await svc.send_to_telegram(message="hello")
        assert result["status"] == "sent"

    @pytest.mark.asyncio
    async def test_uses_override_chat_id(self):
        svc = NotificationService(make_settings(
            telegram_token="123:ABC", telegram_chat="-100999"
        ))
        captured = {}
        def fake_urlopen(req, timeout=None):
            import io
            body = req.data.decode()
            captured["body"] = json.loads(body)
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            resp.read.return_value = json.dumps({"ok": True}).encode()
            return resp
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await svc.send_to_telegram(message="hi", chat_id="-111")
        assert captured["body"]["chat_id"] == "-111"


class TestTruncation:
    def test_truncates_long_result(self):
        svc = NotificationService(make_settings())
        long_text = "x" * 5000
        result = svc._truncate_results(long_text)
        assert len(result) <= 3100
        assert "truncated" in result.lower()
