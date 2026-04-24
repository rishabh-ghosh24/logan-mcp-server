"""Slack and Telegram on-demand notification delivery."""
import asyncio
import html
import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Settings


class NotificationService:
    def __init__(self, settings: Settings, oci_client: Any = None):
        self.slack_config = settings.notifications.slack
        self.telegram_config = settings.notifications.telegram
        self.ons_config = settings.notifications.ons
        self.oci_client = oci_client

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

    async def send_telegram_document(
        self,
        file_path: Path,
        caption: str = "",
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.telegram_config.bot_token:
            raise ValueError(
                "Telegram not configured. Set bot_token in config.yaml or TELEGRAM_BOT_TOKEN env var."
            )
        effective_chat_id = chat_id or self.telegram_config.default_chat_id
        if not effective_chat_id:
            raise ValueError("Telegram chat_id is required")
        path = Path(file_path)
        if not path.is_file():
            raise ValueError(f"Telegram document file does not exist: {path}")

        fields = {
            "chat_id": effective_chat_id,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        }
        body, content_type = self._multipart_form_data(
            fields=fields,
            file_field="document",
            file_path=path,
        )
        url = f"https://api.telegram.org/bot{self.telegram_config.bot_token}/sendDocument"
        data = await asyncio.to_thread(
            self._post_telegram_document, url, body, content_type
        )
        return {
            "status": "sent",
            "destination": "telegram",
            "message_id": str((data.get("result") or {}).get("message_id", "")) or None,
        }

    async def send_to_ons_email(
        self,
        title: str,
        body: str,
        topic_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.oci_client is None:
            raise ValueError("OCI client is required for ONS email delivery")
        effective_topic = topic_id or self.ons_config.default_topic_ocid
        if not effective_topic:
            raise ValueError(
                "ONS topic not configured. Set notifications.ons.default_topic_ocid "
                "or OCI_LOGAN_ONS_TOPIC_OCID."
            )
        result = await self.oci_client.publish_notification(
            topic_id=effective_topic,
            title=title,
            body=body,
        )
        return {
            "status": "sent",
            "destination": "email",
            "message_id": result.get("message_id"),
        }

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

    def _multipart_form_data(
        self,
        fields: Dict[str, str],
        file_field: str,
        file_path: Path,
    ) -> tuple[bytes, str]:
        boundary = "logan-mcp-boundary"
        chunks: List[bytes] = []
        for name, value in fields.items():
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
            chunks.append(str(value).encode("utf-8"))
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
                "Content-Type: application/pdf\r\n\r\n"
            ).encode()
        )
        chunks.append(file_path.read_bytes())
        chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def _post_telegram_document(
        self,
        url: str,
        payload: bytes,
        content_type: str,
    ) -> Dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": content_type},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            if not data.get("ok"):
                raise RuntimeError(
                    f"Telegram delivery failed: {data.get('description', body)}"
                )
            return data

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
            parts.append(f"<b>{html.escape(message)}</b>")
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
