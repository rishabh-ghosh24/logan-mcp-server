from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.config import Settings
from oci_logan_mcp.report_delivery import (
    ReportDeliveryError,
    ReportDeliveryService,
    redact_recipient,
)


REPORT = {
    "title": "Incident Report",
    "markdown": """# Incident Report

## Executive Summary
One source showed elevated errors.

## Top Findings
- Apache access logs increased by 300%.
- No parser failures were found.

## Evidence
Query: `* | stats count`
""",
}


def make_service(tmp_path, notification_service=None, audit_logger=None):
    settings = Settings()
    settings.report_delivery.artifact_dir = tmp_path
    settings.notifications.telegram.default_chat_id = "-100999"
    settings.notifications.ons.default_topic_ocid = "ocid1.onstopic.oc1..abc"
    notification_service = notification_service or MagicMock()
    notification_service.send_telegram_document = AsyncMock(
        return_value={"status": "sent", "message_id": "tg-1"}
    )
    notification_service.send_to_telegram = AsyncMock(
        return_value={"status": "sent", "destination": "telegram", "message_id": "tg-msg"}
    )
    notification_service.send_to_slack = AsyncMock(
        return_value={"status": "sent", "destination": "slack"}
    )
    notification_service.send_to_ons_email = AsyncMock(
        return_value={"status": "sent", "destination": "email", "message_id": "ons-1"}
    )
    return ReportDeliveryService(
        settings=settings,
        notification_service=notification_service,
        audit_logger=audit_logger,
        user_id="test-user",
    ), notification_service


@pytest.mark.asyncio
async def test_delivers_pdf_to_telegram(tmp_path):
    svc, notifications = make_service(tmp_path)

    result = await svc.deliver(
        report=REPORT,
        channels=["telegram"],
        recipients={},
        output_format="pdf",
        title=None,
    )

    assert result["status"] == "sent"
    assert result["pdf_path"].endswith(".pdf")
    assert Path(result["pdf_path"]).read_bytes().startswith(b"%PDF")
    assert result["delivered"][0]["channel"] == "telegram"
    assert result["delivered"][0]["artifact"] == "pdf"
    notifications.send_telegram_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_markdown_format_sends_telegram_message_without_pdf(tmp_path):
    svc, notifications = make_service(tmp_path)

    result = await svc.deliver(
        report=REPORT,
        channels=["telegram"],
        recipients={},
        output_format="markdown",
        title=None,
    )

    assert result["status"] == "sent"
    assert result["pdf_path"] is None
    notifications.send_to_telegram.assert_awaited_once()
    notifications.send_telegram_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_and_slack_receive_inline_summary(tmp_path):
    svc, notifications = make_service(tmp_path)

    result = await svc.deliver(
        report=REPORT,
        channels=["email", "slack"],
        recipients={"email_topic_ocid": "ocid1.onstopic.oc1..override"},
        output_format="pdf",
        title="Override Title",
    )

    assert result["status"] == "sent"
    assert [row["channel"] for row in result["delivered"]] == ["email", "slack"]
    notifications.send_to_ons_email.assert_awaited_once()
    email_kwargs = notifications.send_to_ons_email.await_args.kwargs
    assert email_kwargs["topic_id"] == "ocid1.onstopic.oc1..override"
    assert "Executive Summary" in email_kwargs["body"]
    assert "Top Findings" in email_kwargs["body"]
    assert "Object Storage" not in email_kwargs["body"]
    notifications.send_to_slack.assert_awaited_once()


@pytest.mark.asyncio
async def test_both_format_sends_pdf_to_telegram_and_summaries_elsewhere(tmp_path):
    svc, notifications = make_service(tmp_path)

    result = await svc.deliver(
        report=REPORT,
        channels=["telegram", "email", "slack"],
        recipients={},
        output_format="both",
        title=None,
    )

    assert result["status"] == "sent"
    assert Path(result["pdf_path"]).read_bytes().startswith(b"%PDF")
    artifacts = {row["channel"]: row["artifact"] for row in result["delivered"]}
    assert artifacts == {
        "telegram": "pdf",
        "email": "summary",
        "slack": "summary",
    }
    notifications.send_telegram_document.assert_awaited_once()
    notifications.send_to_ons_email.assert_awaited_once()
    notifications.send_to_slack.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_failure_keeps_channel_results(tmp_path):
    svc, notifications = make_service(tmp_path)
    notifications.send_to_ons_email.side_effect = RuntimeError("ONS down")

    result = await svc.deliver(
        report=REPORT,
        channels=["telegram", "email"],
        recipients={},
        output_format="pdf",
        title=None,
    )

    assert result["status"] == "partial"
    statuses = {row["channel"]: row["status"] for row in result["delivered"]}
    assert statuses == {"telegram": "sent", "email": "failed"}
    assert "ONS down" in result["delivered"][1]["error"]


@pytest.mark.asyncio
async def test_telegram_size_cap_blocks_upload(tmp_path, monkeypatch):
    svc, notifications = make_service(tmp_path)
    pdf = tmp_path / "too-large.pdf"
    pdf.write_bytes(b"x")

    monkeypatch.setattr(
        "oci_logan_mcp.report_delivery.render_markdown_pdf",
        lambda markdown, title, output_path: pdf,
    )
    monkeypatch.setattr(
        "oci_logan_mcp.report_delivery.TELEGRAM_DOCUMENT_MAX_BYTES",
        0,
    )

    result = await svc.deliver(
        report=REPORT,
        channels=["telegram"],
        recipients={},
        output_format="pdf",
        title=None,
    )

    assert result["status"] == "failed"
    assert result["delivered"][0]["status"] == "failed"
    assert "0 MB" in result["delivered"][0]["error"]
    notifications.send_telegram_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_markdown_failure_reports_summary_artifact(tmp_path):
    svc, notifications = make_service(tmp_path)
    notifications.send_to_telegram.side_effect = RuntimeError("Telegram down")

    result = await svc.deliver(
        report=REPORT,
        channels=["telegram"],
        recipients={},
        output_format="markdown",
        title=None,
    )

    assert result["status"] == "failed"
    assert result["delivered"][0]["artifact"] == "summary"
    assert "Telegram down" in result["delivered"][0]["error"]


@pytest.mark.asyncio
async def test_delivery_attempts_are_audited(tmp_path):
    audit = MagicMock()
    svc, notifications = make_service(tmp_path, audit_logger=audit)
    notifications.send_to_ons_email.side_effect = RuntimeError("ONS down")

    await svc.deliver(
        report=REPORT,
        channels=["telegram", "email"],
        recipients={"telegram_chat_id": "-100999"},
        output_format="pdf",
        title=None,
    )

    outcomes = [call.kwargs["outcome"] for call in audit.log.call_args_list]
    assert outcomes == ["delivery_sent", "delivery_failed"]
    first_args = audit.log.call_args_list[0].kwargs["args"]
    assert first_args["channel"] == "telegram"
    assert first_args["recipient"] == "telegram:...0999"
    assert "-100999" not in str(first_args)


def test_rejects_report_without_markdown(tmp_path):
    svc, _ = make_service(tmp_path)
    with pytest.raises(ReportDeliveryError, match="markdown"):
        svc._validate_report({"title": "No body"})


def test_report_id_is_not_accepted_in_p0(tmp_path):
    svc, _ = make_service(tmp_path)
    with pytest.raises(ReportDeliveryError, match="markdown"):
        svc._validate_report({"report_id": "r-123"})


def test_redact_recipient_masks_values():
    assert redact_recipient("telegram", "-100999") == "telegram:...0999"
    assert redact_recipient("email", "ocid1.onstopic.oc1..abcdef") == "ons:...cdef"
    assert redact_recipient("slack", "") == "slack:configured"
