"""Exercise notification HTTP payloads without contacting Slack or Telegram."""

from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.error
import urllib.request

import pytest

from oci_logan_mcp.config import Settings
from oci_logan_mcp.notification_service import NotificationService


class TelegramMarkup(HTMLParser):
    def handle_starttag(self, tag, attrs):
        if tag not in {"b", "pre", "i"}:
            raise ValueError("Unsupported HTML tag in log data: " + tag)


@pytest.fixture
def endpoint():
    state = {"requests": [], "reject": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "payload": payload,
                                      "content_type": self.headers.get("Content-Type")})
            status, body = 200, b"ok"
            if self.path == "/telegram":
                try:
                    TelegramMarkup().feed(payload["text"])
                    body = json.dumps({"ok": not state["reject"], "description": "synthetic backend rejection"}).encode()
                except ValueError as error:
                    body = json.dumps({"ok": False, "description": str(error)}).encode()
            elif state["reject"]:
                status, body = 503, b"synthetic backend rejection"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(server.server_port), state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def service_for(channel, url, monkeypatch):
    settings = Settings()
    if channel == "slack":
        settings.notifications.slack.webhook_url = url + "/slack"
    else:
        settings.notifications.telegram.bot_token = "qualification-token"
        settings.notifications.telegram.default_chat_id = "qualification-chat"
        original = urllib.request.urlopen

        def redirect_test_token(request, timeout):
            assert request.full_url == "https://api.telegram.org/botqualification-token/sendMessage"
            routed = urllib.request.Request(url + "/telegram", data=request.data,
                                            headers=dict(request.headers), method=request.get_method())
            return original(routed, timeout=timeout)

        monkeypatch.setattr(urllib.request, "urlopen", redirect_test_token)
    return NotificationService(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["slack", "telegram"])
async def test_real_http_payload_keeps_log_markup_literal(channel, endpoint, monkeypatch):
    url, state = endpoint
    service = service_for(channel, url, monkeypatch)
    query_result = {"columns": [{"name": "col<1>&"}], "rows": [["<xml> & status"]], "total_count": 1}

    result = await getattr(service, "send_to_" + channel)(message="QA <notice> & result", query_result=query_result)

    assert result == {"status": "sent", "destination": channel}
    assert len(state["requests"]) == 1
    captured = state["requests"][0]
    assert captured["content_type"] == "application/json"
    if channel == "telegram":
        assert captured["payload"]["chat_id"] == "qualification-chat"
        assert captured["payload"]["parse_mode"] == "HTML"
        assert "&lt;xml&gt; &amp; status" in captured["payload"]["text"]
        assert "col&lt;1&gt;&amp;" in captured["payload"]["text"]
    else:
        assert captured["payload"]["blocks"][0]["text"]["text"] == "QA <notice> & result"
        assert "<xml> & status" in captured["payload"]["blocks"][1]["text"]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["slack", "telegram"])
async def test_backend_rejection_never_reports_sent(channel, endpoint, monkeypatch):
    url, state = endpoint
    service = service_for(channel, url, monkeypatch)
    delivered = await getattr(service, "send_to_" + channel)(message="Positive control")
    assert delivered["status"] == "sent"
    state["reject"] = True

    with pytest.raises((RuntimeError, urllib.error.HTTPError), match="synthetic backend rejection|503"):
        await getattr(service, "send_to_" + channel)(message="Negative control")

    assert len(state["requests"]) == 2
