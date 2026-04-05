"""Slack and Telegram on-demand notification delivery."""
import asyncio
import json
import urllib.request
from typing import Any, Dict, List, Optional

from .config import Settings


class NotificationService:
    def __init__(self, settings: Settings):
        self.slack_config = settings.notifications.slack
        self.telegram_config = settings.notifications.telegram

    async def send_to_slack(
        self,
        message: Optional[str] = None,
        query_result: Optional[Dict[str, Any]] = None,
        format_type: str = "summary",
    ) -> Dict[str, Any]:
        if not message and query_result is None:
            raise ValueError("Provide at least one of message or query result")
        if not self.slack_config.webhook_url:
            raise ValueError(
                "Slack not configured. Set webhook_url in config.yaml or SLACK_WEBHOOK_URL env var."
            )
        blocks = self._format_slack_blocks(message, query_result, format_type)
        payload = json.dumps({"blocks": blocks}).encode("utf-8")
        await asyncio.to_thread(self._post_slack, self.slack_config.webhook_url, payload)
        return {"status": "sent", "destination": "slack"}

    def _post_slack(self, webhook_url: str, payload: bytes) -> None:
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                body = resp.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Slack delivery failed (HTTP {resp.status}): {body}")

    async def send_to_telegram(
        self,
        message: Optional[str] = None,
        query_result: Optional[Dict[str, Any]] = None,
        format_type: str = "summary",
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not message and query_result is None:
            raise ValueError("Provide at least one of message or query result")
        if not self.telegram_config.bot_token:
            raise ValueError(
                "Telegram not configured. Set bot_token in config.yaml or TELEGRAM_BOT_TOKEN env var."
            )
        effective_chat_id = chat_id or self.telegram_config.default_chat_id
        text = self._format_telegram_html(message, query_result, format_type)
        payload = json.dumps({
            "chat_id": effective_chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{self.telegram_config.bot_token}/sendMessage"
        await asyncio.to_thread(self._post_telegram, url, payload)
        return {"status": "sent", "destination": "telegram"}

    def _post_telegram(self, url: str, payload: bytes) -> None:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            if not data.get("ok"):
                raise RuntimeError(f"Telegram delivery failed: {data.get('description', body)}")

    def _format_slack_blocks(
        self,
        message: Optional[str],
        query_result: Optional[Dict[str, Any]],
        format_type: str,
    ) -> List[Dict]:
        blocks = []
        if message:
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": message[:150]}})
        if query_result:
            table_text = self._render_table(query_result, format_type)
            table_text = self._truncate_results(table_text)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{table_text}```"},
            })
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn",
                               "text": f"Rows: {query_result.get('total_count', '?')}"}],
            })
        return blocks

    def _format_telegram_html(
        self,
        message: Optional[str],
        query_result: Optional[Dict[str, Any]],
        format_type: str,
    ) -> str:
        parts = []
        if message:
            parts.append(f"<b>{message}</b>")
        if query_result:
            table_text = self._render_table(query_result, format_type)
            table_text = self._truncate_results(table_text)
            parts.append(f"<pre>{table_text}</pre>")
            parts.append(f"<i>Rows: {query_result.get('total_count', '?')}</i>")
        return "\n\n".join(parts)

    def _render_table(self, query_result: Dict[str, Any], format_type: str) -> str:
        columns = [c["name"] for c in query_result.get("columns", [])]
        rows = query_result.get("rows", [])
        if format_type == "summary":
            rows = rows[:5]
        if not columns:
            return str(rows)
        header = " | ".join(columns)
        sep = "-+-".join("-" * len(c) for c in columns)
        lines = [header, sep]
        for row in rows:
            lines.append(" | ".join(str(v) for v in row))
        return "\n".join(lines)

    def _truncate_results(self, text: str, max_chars: int = 3000) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n... [truncated, showing first {max_chars} chars]"
