"""Report delivery orchestration for Telegram, Slack, and ONS email."""
from __future__ import annotations

import html
import re
import textwrap
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
        is_partial = self._is_partial_report(report, markdown)
        summary = self._summary_markdown(markdown)
        email_body = self._truncate_email_body(
            self._append_object_storage_links(
                self._render_ons_plaintext(summary, is_partial=is_partial),
                report.get("object_storage_links") or [],
            )
        )

        pdf_path: Optional[Path] = None
        if output_format in {"pdf", "both"}:
            pdf_path = self._pdf_path(effective_title)
            pdf_path = render_markdown_pdf(markdown, effective_title, pdf_path)

        delivered: List[Dict[str, Any]] = []
        for channel in channels:
            row = await self._deliver_one(
                channel=channel,
                title=effective_title,
                is_partial=is_partial,
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
        is_partial: bool,
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
                    title=self._email_title(title, is_partial=is_partial),
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

    def _is_partial_report(self, report: Dict[str, Any], markdown: str) -> bool:
        metadata = report.get("metadata") if isinstance(report, dict) else None
        if isinstance(metadata, dict):
            if metadata.get("partial") is True:
                return True
            completeness = metadata.get("completeness")
            if isinstance(completeness, dict) and completeness.get("status") == "partial":
                return True
        return "partial investigation" in markdown.lower()

    def _email_title(self, title: str, *, is_partial: bool) -> str:
        if not is_partial or title.startswith("[PARTIAL]"):
            return title
        return f"[PARTIAL] {title}"

    def _render_ons_plaintext(self, markdown: str, *, is_partial: bool) -> str:
        lines = markdown.splitlines()
        out: List[str] = []
        if is_partial:
            out.extend([
                "PARTIAL INVESTIGATION",
                "This report was generated from an incomplete investigation.",
                "",
            ])

        i = 0
        item_no = 1
        while i < len(lines):
            line = lines[i].rstrip()
            if _is_table_line(line):
                table_lines = []
                while i < len(lines) and _is_table_line(lines[i]):
                    table_lines.append(lines[i])
                    i += 1
                rendered = _render_ascii_table(table_lines)
                out.extend(rendered)
                if rendered:
                    out.append("")
                continue
            if not line.strip():
                if out and out[-1] != "":
                    out.append("")
                i += 1
                continue
            if line.startswith("## "):
                title = _strip_markdown(line[3:]).upper()
                out.extend(textwrap.wrap(title, width=80) or [title])
                item_no = 1
            elif line.startswith("# "):
                title = _strip_markdown(line[2:]).upper()
                out.extend(textwrap.wrap(title, width=80) or [title])
                item_no = 1
            elif line.lstrip().startswith("- "):
                text = _strip_markdown(line.lstrip()[2:])
                out.extend(_wrap_numbered(item_no, text))
                item_no += 1
            else:
                out.extend(textwrap.wrap(_strip_markdown(line), width=80) or [""])
            i += 1

        return "\n".join(_limit_line(line, 80) for line in out).strip()

    def _append_object_storage_links(
        self,
        body: str,
        links: List[Dict[str, Any]],
    ) -> str:
        if not links:
            return body
        out = [body.rstrip(), "", "Full Report Links"]
        for link in links:
            name = link.get("name", "report")
            url = link.get("url", "")
            expires_at = link.get("expires_at", "")
            out.append(f"- {name}: {url}")
            if expires_at:
                out.append(f"  Expires: {expires_at}")
        return "\n".join(out).strip()

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


def _strip_markdown(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"__([^_]*)__", r"\1", text)
    return text.strip()


def _wrap_numbered(number: int, text: str) -> List[str]:
    prefix = f"{number}. "
    wrapped = textwrap.wrap(text, width=80 - len(prefix)) or [""]
    return [prefix + wrapped[0]] + [
        " " * len(prefix) + line for line in wrapped[1:]
    ]


def _limit_line(line: str, width: int) -> str:
    if len(line) <= width:
        return line
    return line[: max(0, width - 3)].rstrip() + "..."


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and "|" in stripped[1:-1]


def _split_table_row(line: str) -> List[str]:
    return [_strip_markdown(cell.strip()) for cell in line.strip().strip("|").split("|")]


def _is_separator_row(cells: List[str]) -> bool:
    return bool(cells) and all(set(cell.replace(" ", "")) <= set("-:") for cell in cells)


def _render_ascii_table(table_lines: List[str]) -> List[str]:
    rows = [_split_table_row(line) for line in table_lines]
    rows = [row for row in rows if not _is_separator_row(row)]
    if len(rows) < 2:
        return [" ".join(rows[0])] if rows else []

    column_count = max(len(row) for row in rows)
    normalized = [row + [""] * (column_count - len(row)) for row in rows]
    caps = [38, 28, 12]
    widths = []
    for idx in range(column_count):
        max_len = max(len(row[idx]) for row in normalized)
        if idx == 0 and len(normalized) > 1:
            max_len = max(max_len, max(len(row[idx]) + 3 for row in normalized[1:]))
        widths.append(min(caps[idx] if idx < len(caps) else 20, max_len))
    while sum(widths) + (2 * (column_count - 1)) > 80 and max(widths) > 8:
        idx = max(range(column_count), key=lambda i: widths[i])
        widths[idx] -= 1

    def format_row(row: List[str]) -> str:
        return "  ".join(
            _limit_line(cell, widths[idx]).ljust(widths[idx])
            for idx, cell in enumerate(row)
        ).rstrip()

    output = [format_row(normalized[0])]
    output.append("  ".join("-" * width for width in widths).rstrip())
    for row_no, row in enumerate(normalized[1:], start=1):
        row = list(row)
        if row and row[0]:
            row[0] = f"{row_no}. {row[0]}"
        output.append(format_row(row))
    return output


def _extract_sections(markdown: str, names: set[str]) -> List[str]:
    sections: List[str] = []
    current_title: Optional[str] = None
    current_matched_title: Optional[str] = None
    current_lines: List[str] = []
    wanted = {_normalize_heading(name) for name in names}

    def flush() -> None:
        if current_matched_title is not None:
            body = "\n".join(current_lines).strip()
            if body:
                sections.append(f"## {current_title}\n{body}")

    for line in markdown.splitlines():
        if line.startswith("## "):
            flush()
            current_title = line[3:].strip()
            normalized = _normalize_heading(current_title)
            current_matched_title = current_title if normalized in wanted else None
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    return sections


def _normalize_heading(title: str) -> str:
    lowered = re.sub(r"\s+", " ", title.strip().lower())
    aliases = {
        "summary": "executive summary",
        "findings": "top findings",
        "key findings": "top findings",
    }
    return aliases.get(lowered, lowered)
