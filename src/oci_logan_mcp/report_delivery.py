"""Report delivery orchestration for Telegram, Slack, and ONS email."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .config import Settings
from .report_pdf import render_markdown_pdf


TELEGRAM_DOCUMENT_MAX_BYTES = 50 * 1024 * 1024
SUPPORTED_CHANNELS = frozenset({"telegram", "email", "slack"})
SUPPORTED_FORMATS = frozenset({"pdf", "markdown", "both"})


class ReportDeliveryError(ValueError):
    """Raised when report delivery input is invalid."""


class ReportDeliveryService:
    def __init__(
        self,
        settings: Settings,
        notification_service: Any,
        audit_logger: Any = None,
        user_id: str = "unknown",
    ) -> None:
        self.settings = settings
        self.notification_service = notification_service
        self.audit_logger = audit_logger
        self.user_id = user_id

    async def deliver(
        self,
        report: Dict[str, Any],
        channels: Optional[List[str]] = None,
        recipients: Optional[Dict[str, str]] = None,
        output_format: str = "pdf",
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        markdown, effective_title = self._validate_report(report, title=title)
        channels = self._validate_channels(channels)
        if output_format not in SUPPORTED_FORMATS:
            raise ReportDeliveryError(
                f"format must be one of {sorted(SUPPORTED_FORMATS)}"
            )
        recipients = recipients or {}
        summary = self._summary_markdown(markdown)
        email_body = self._truncate_email_body(summary)

        pdf_path: Optional[Path] = None
        if output_format in {"pdf", "both"}:
            pdf_path = self._pdf_path(effective_title)
            pdf_path = render_markdown_pdf(markdown, effective_title, pdf_path)

        delivered: List[Dict[str, Any]] = []
        for channel in channels:
            row = await self._deliver_one(
                channel=channel,
                title=effective_title,
                summary=summary,
                email_body=email_body,
                pdf_path=pdf_path,
                output_format=output_format,
                recipients=recipients,
            )
            delivered.append(row)
            self._audit_attempt(row)

        return {
            "status": _overall_status(delivered),
            "delivered": delivered,
            "pdf_path": str(pdf_path) if pdf_path else None,
        }

    def _validate_report(
        self,
        report: Dict[str, Any],
        title: Optional[str] = None,
    ) -> tuple[str, str]:
        if not isinstance(report, dict):
            raise ReportDeliveryError("report is required and must be an object")
        markdown = report.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ReportDeliveryError(
                "report.markdown is required in P0; report_id lookup is deferred"
            )
        effective_title = title or report.get("title") or "Incident Report"
        return markdown, str(effective_title)

    def _validate_channels(self, channels: Optional[List[str]]) -> List[str]:
        if channels is None:
            return ["telegram"]
        if not isinstance(channels, list) or not channels:
            raise ReportDeliveryError("channels must be a non-empty list")
        unknown = [c for c in channels if c not in SUPPORTED_CHANNELS]
        if unknown:
            raise ReportDeliveryError(f"unsupported channels: {unknown}")
        return channels

    async def _deliver_one(
        self,
        channel: str,
        title: str,
        summary: str,
        email_body: str,
        pdf_path: Optional[Path],
        output_format: str,
        recipients: Dict[str, str],
    ) -> Dict[str, Any]:
        recipient = _recipient_for(channel, recipients)
        try:
            if channel == "telegram":
                return await self._deliver_telegram(
                    summary, pdf_path, output_format, recipients
                )
            if channel == "email":
                result = await self.notification_service.send_to_ons_email(
                    title=title,
                    body=email_body,
                    topic_id=recipients.get("email_topic_ocid"),
                )
                return _sent(channel, "summary", recipient, result.get("message_id"))
            if channel == "slack":
                result = await self.notification_service.send_to_slack(message=summary)
                return _sent(channel, "summary", recipient, result.get("message_id"))
        except Exception as e:
            artifact = _artifact_for(channel, output_format)
            return _failed(channel, artifact, recipient, str(e))
        return _failed(channel, "summary", recipient, "unsupported channel")

    async def _deliver_telegram(
        self,
        summary: str,
        pdf_path: Optional[Path],
        output_format: str,
        recipients: Dict[str, str],
    ) -> Dict[str, Any]:
        recipient = _recipient_for("telegram", recipients)
        chat_id = recipients.get("telegram_chat_id")
        if output_format == "markdown":
            result = await self.notification_service.send_to_telegram(
                message=summary,
                chat_id=chat_id,
            )
            return _sent("telegram", "summary", recipient, result.get("message_id"))
        if pdf_path is None:
            return _failed("telegram", "pdf", recipient, "PDF was not generated")
        size = pdf_path.stat().st_size
        if size > TELEGRAM_DOCUMENT_MAX_BYTES:
            return _failed(
                "telegram",
                "pdf",
                recipient,
                f"Telegram document exceeds {_telegram_limit_label()} limit",
            )
        caption = html.escape(summary[:1000])
        result = await self.notification_service.send_telegram_document(
            file_path=pdf_path,
            caption=caption,
            chat_id=chat_id,
        )
        return _sent("telegram", "pdf", recipient, result.get("message_id"))

    def _pdf_path(self, title: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in title).strip("-")
        slug = "-".join(part for part in slug.split("-") if part)[:48] or "report"
        return (
            self.settings.report_delivery.artifact_dir
            / f"{slug}-{stamp}-{uuid4().hex[:8]}.pdf"
        )

    def _summary_markdown(self, markdown: str) -> str:
        sections = _extract_sections(markdown, {"Executive Summary", "Top Findings"})
        if sections:
            return "\n\n".join(sections)
        return markdown.strip()[:4000]

    def _truncate_email_body(self, body: str) -> str:
        limit = self.settings.report_delivery.max_email_body_chars
        if len(body) <= limit:
            return body
        marker = "\n...(truncated)"
        return body[: max(0, limit - len(marker))] + marker

    def _audit_attempt(self, row: Dict[str, Any]) -> None:
        if not self.audit_logger:
            return
        self.audit_logger.log(
            user=self.user_id,
            tool="deliver_report",
            args={
                "channel": row["channel"],
                "recipient": row["recipient"],
                "artifact": row["artifact"],
            },
            outcome=f"delivery_{row['status']}",
            error=row.get("error", ""),
        )


def _recipient_for(channel: str, recipients: Dict[str, str]) -> str:
    if channel == "telegram":
        return redact_recipient("telegram", recipients.get("telegram_chat_id", ""))
    if channel == "email":
        return redact_recipient("email", recipients.get("email_topic_ocid", ""))
    return redact_recipient("slack", "")


def _artifact_for(channel: str, output_format: str) -> str:
    if channel == "telegram" and output_format in {"pdf", "both"}:
        return "pdf"
    return "summary"


def _telegram_limit_label() -> str:
    mb = TELEGRAM_DOCUMENT_MAX_BYTES // (1024 * 1024)
    return f"{mb} MB"


def redact_recipient(channel: str, value: str) -> str:
    if channel == "slack":
        return "slack:configured"
    prefix = "ons" if channel == "email" else channel
    if not value:
        return f"{prefix}:default"
    return f"{prefix}:...{value[-4:]}"


def _sent(channel: str, artifact: str, recipient: str, message_id: Any = None) -> Dict[str, Any]:
    return {
        "channel": channel,
        "status": "sent",
        "message_id": str(message_id) if message_id is not None else None,
        "artifact": artifact,
        "recipient": recipient,
    }


def _failed(channel: str, artifact: str, recipient: str, error: str) -> Dict[str, Any]:
    return {
        "channel": channel,
        "status": "failed",
        "message_id": None,
        "artifact": artifact,
        "recipient": recipient,
        "error": error,
    }


def _overall_status(rows: List[Dict[str, Any]]) -> str:
    sent = sum(1 for row in rows if row["status"] == "sent")
    if sent == len(rows):
        return "sent"
    if sent:
        return "partial"
    return "failed"


def _extract_sections(markdown: str, names: set[str]) -> List[str]:
    sections: List[str] = []
    current_title: Optional[str] = None
    current_lines: List[str] = []

    def flush() -> None:
        if current_title in names:
            body = "\n".join(current_lines).strip()
            if body:
                sections.append(f"## {current_title}\n{body}")

    for line in markdown.splitlines():
        if line.startswith("## "):
            flush()
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    return sections
