# Report Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `deliver_report` so an N3 Markdown report can be rendered to a local PDF and delivered through Telegram, Slack, and OCI Notifications email-topic delivery with reproducible content, stable PDF metadata, and per-channel status.

**Architecture:** Keep `NotificationService` as the outbound transport boundary and extend it instead of creating a parallel delivery path. Add a pure `report_pdf.py` renderer for Markdown-to-PDF, a `report_delivery.py` orchestrator for channel decisions and partial-failure handling, and a thin handler/tool-schema layer. Email means OCI Notifications topic publish to subscribers, not SMTP or OCI Email Delivery.

**Tech Stack:** Python stdlib, existing `matplotlib.backends.backend_pdf.PdfPages`, existing `urllib.request` Telegram/Slack transport, OCI Python SDK `oci.ons.NotificationDataPlaneClient.publish_message`, pytest, AsyncMock.

---

## Scope Decisions

- P0 schema accepts only `report: {"markdown": str, "title": str | None}`. Do not include `report_id` in the tool schema until N3-F4 report persistence / lookup lands.
- Channels are `telegram`, `email`, and optional `slack`.
- Telegram is the current/default IM path for P0.
- Telegram can receive the full PDF via Bot API `sendDocument` when `format` is `pdf` or `both`.
- Slack is summary-only and explicit opt-in in P0 because the current integration is webhook-based. It can be validated with a private/free Slack registration before any Oracle Slack workspace integration. Full Slack file upload and Oracle Slack rollout are deferred.
- Email is summary-only in P0 via OCI Notifications topic publish. No attachment and no Object Storage/PAR link.
- `format="pdf"` still produces a PDF artifact. Non-document channels receive the inline summary and their result row says `artifact: "summary"`.
- `format="markdown"` skips PDF generation and delivers inline summaries/messages only.
- `format="both"` generates a PDF, sends it to Telegram, and sends inline summaries to Slack/email. Telegram uses the document caption for the summary rather than sending a second message.
- Delivery is mutating because it sends outbound notifications. Add `deliver_report` to read-only blocking.
- Every per-channel attempt must be audited with channel, status, and a redacted recipient target. The generic invoked audit entry for `deliver_report` must not persist raw recipient values.

## Response Contract

Successful or partially successful calls return this shape:

```json
{
  "status": "sent",
  "delivered": [
    {
      "channel": "telegram",
      "status": "sent",
      "message_id": "123",
      "artifact": "pdf",
      "recipient": "telegram:...0999"
    },
    {
      "channel": "email",
      "status": "failed",
      "message_id": null,
      "artifact": "summary",
      "recipient": "ons:...abcd",
      "error": "ONS delivery failed: ..."
    }
  ],
  "pdf_path": "/Users/rishabh/.oci-logan-mcp/reports/report-20260424T120000Z.pdf"
}
```

Overall `status` values:

- `sent`: every requested channel was sent.
- `partial`: at least one channel sent and at least one failed.
- `failed`: every requested channel failed.
- `error`: validation or PDF generation failed before channel attempts.

---

## File Map

- Create: `src/oci_logan_mcp/report_pdf.py`
  - Owns Markdown-to-PDF rendering with reproducible content and stable metadata.
  - No network, no config loading, no delivery side effects.
- Create: `src/oci_logan_mcp/report_delivery.py`
  - Owns channel orchestration, summary extraction, Telegram size preflight, per-channel status, and delivery-attempt audit logging.
- Modify: `src/oci_logan_mcp/notification_service.py`
  - Add Telegram `sendDocument`.
  - Add ONS topic publish transport through the existing OCI client.
  - Keep Slack and Telegram message behavior backward compatible.
- Modify: `src/oci_logan_mcp/client.py`
  - Add lazy `NotificationDataPlaneClient`.
  - Add `publish_notification(topic_id, title, body)`.
- Modify: `src/oci_logan_mcp/config.py`
  - Add `ONSConfig`.
  - Add `ReportDeliveryConfig`.
  - Parse YAML and env overrides.
- Modify: `src/oci_logan_mcp/handlers.py`
  - Instantiate `ReportDeliveryService`.
  - Register `_deliver_report`.
  - Redact deliver-report recipients in audit args.
- Modify: `src/oci_logan_mcp/tools.py`
  - Add MCP tool schema for `deliver_report`.
- Modify: `src/oci_logan_mcp/read_only_guard.py`
  - Add `deliver_report` to mutating tools.
- Modify: `tests/test_config.py`
- Modify: `tests/test_notification_service.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_read_only_guard.py`
- Modify: `tests/test_tools.py`
- Create: `tests/test_report_pdf.py`
- Create: `tests/test_report_delivery.py`
- Modify: `docs/phase-2/specs/reports-and-playbooks.md`
  - Align P0 interface with no `report_id` and optional Slack summary delivery.
- Modify: `docs/phase-2/backlog.md`
  - Track explicit P1 deferrals.

---

### Task 1: Config And OCI Notification Publish

**Files:**
- Modify: `src/oci_logan_mcp/config.py`
- Modify: `src/oci_logan_mcp/client.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Add these imports in `tests/test_config.py`:

```python
from oci_logan_mcp.config import ONSConfig, ReportDeliveryConfig
```

Add these tests under `TestNotificationsConfig`:

```python
def test_defaults_include_ons_and_report_delivery(self):
    s = Settings()
    assert s.notifications.ons.default_topic_ocid == ""
    assert s.report_delivery.max_email_body_chars == 8000
    assert s.report_delivery.artifact_dir == Path.home() / ".oci-logan-mcp" / "reports"

def test_parse_config_ons(self):
    data = {"notifications": {"ons": {"default_topic_ocid": "ocid1.onstopic.oc1..abc"}}}
    s = _parse_config(data)
    assert s.notifications.ons.default_topic_ocid == "ocid1.onstopic.oc1..abc"

def test_parse_config_report_delivery(self):
    data = {
        "report_delivery": {
            "artifact_dir": "/tmp/logan-reports",
            "max_email_body_chars": 1200,
        }
    }
    s = _parse_config(data)
    assert s.report_delivery.artifact_dir == Path("/tmp/logan-reports")
    assert s.report_delivery.max_email_body_chars == 1200

def test_env_override_ons_topic(self, monkeypatch):
    monkeypatch.setenv("OCI_LOGAN_ONS_TOPIC_OCID", "ocid1.onstopic.oc1..env")
    s = _apply_env_overrides(Settings())
    assert s.notifications.ons.default_topic_ocid == "ocid1.onstopic.oc1..env"

def test_env_override_report_delivery(self, monkeypatch):
    monkeypatch.setenv("OCI_LOGAN_REPORT_ARTIFACT_DIR", "/tmp/reports-env")
    monkeypatch.setenv("OCI_LOGAN_REPORT_MAX_EMAIL_CHARS", "4096")
    s = _apply_env_overrides(Settings())
    assert s.report_delivery.artifact_dir == Path("/tmp/reports-env")
    assert s.report_delivery.max_email_body_chars == 4096

def test_to_dict_includes_ons_and_report_delivery(self):
    s = Settings()
    s.notifications.ons.default_topic_ocid = "ocid1.onstopic.oc1..abc"
    s.report_delivery.max_email_body_chars = 1234
    d = s.to_dict()
    assert d["notifications"]["ons"]["default_topic_ocid"] == "ocid1.onstopic.oc1..abc"
    assert d["report_delivery"]["max_email_body_chars"] == 1234
```

- [ ] **Step 2: Run config tests and verify failure**

Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py::TestNotificationsConfig -q
```

Expected: FAIL because `ONSConfig`, `ReportDeliveryConfig`, `notifications.ons`, and `settings.report_delivery` do not exist.

- [ ] **Step 3: Implement config dataclasses and parsing**

In `src/oci_logan_mcp/config.py`, add:

```python
@dataclass
class ONSConfig:
    default_topic_ocid: str = ""


@dataclass
class ReportDeliveryConfig:
    artifact_dir: Path = field(
        default_factory=lambda: Path.home() / ".oci-logan-mcp" / "reports"
    )
    max_email_body_chars: int = 8000
```

Update `NotificationsConfig`:

```python
@dataclass
class NotificationsConfig:
    slack: SlackConfig = field(default_factory=SlackConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    ons: ONSConfig = field(default_factory=ONSConfig)
```

Update `Settings`:

```python
report_delivery: ReportDeliveryConfig = field(default_factory=ReportDeliveryConfig)
```

Update `Settings.to_dict()`:

```python
"notifications": {
    "slack": {
        "webhook_url": self.notifications.slack.webhook_url,
    },
    "telegram": {
        "bot_token": self.notifications.telegram.bot_token,
        "default_chat_id": self.notifications.telegram.default_chat_id,
    },
    "ons": {
        "default_topic_ocid": self.notifications.ons.default_topic_ocid,
    },
},
"report_delivery": {
    "artifact_dir": str(self.report_delivery.artifact_dir),
    "max_email_body_chars": self.report_delivery.max_email_body_chars,
},
```

Update `_parse_config()`:

```python
if notif_data := data.get("notifications"):
    if slack_data := notif_data.get("slack"):
        settings.notifications.slack = SlackConfig(
            webhook_url=slack_data.get("webhook_url", ""),
        )
    if tg_data := notif_data.get("telegram"):
        settings.notifications.telegram = TelegramConfig(
            bot_token=tg_data.get("bot_token", ""),
            default_chat_id=tg_data.get("default_chat_id", ""),
        )
    if ons_data := notif_data.get("ons"):
        settings.notifications.ons = ONSConfig(
            default_topic_ocid=ons_data.get("default_topic_ocid", ""),
        )

if rd_data := data.get("report_delivery"):
    settings.report_delivery = ReportDeliveryConfig(
        artifact_dir=Path(
            rd_data.get("artifact_dir", settings.report_delivery.artifact_dir)
        ),
        max_email_body_chars=rd_data.get(
            "max_email_body_chars",
            settings.report_delivery.max_email_body_chars,
        ),
    )
```

Update `_apply_env_overrides()`:

```python
if v := os.environ.get("OCI_LOGAN_ONS_TOPIC_OCID"):
    settings.notifications.ons.default_topic_ocid = v
if v := os.environ.get("OCI_LOGAN_REPORT_ARTIFACT_DIR"):
    settings.report_delivery.artifact_dir = Path(v)
if v := os.environ.get("OCI_LOGAN_REPORT_MAX_EMAIL_CHARS"):
    settings.report_delivery.max_email_body_chars = int(v)
```

- [ ] **Step 4: Add OCI data-plane publish support**

In `src/oci_logan_mcp/client.py`, add a lazy data-plane client property near the existing `ons_client`:

Add the import:

```python
import asyncio
```

```python
@property
def ons_data_client(self):
    """Lazy accessor for OCI Notification data-plane client."""
    if not hasattr(self, "_ons_data_client") or self._ons_data_client is None:
        self._ons_data_client = oci.ons.NotificationDataPlaneClient(
            config=self._config, signer=self._signer
        )
    return self._ons_data_client
```

Add an async publish method after `get_topic()`:

```python
async def publish_notification(
    self,
    topic_id: str,
    title: str,
    body: str,
) -> Dict[str, Any]:
    """Publish a message to an OCI Notifications topic."""
    await self._rate_limiter.acquire()
    try:
        details = oci.ons.models.MessageDetails(title=title, body=body)
        response = await asyncio.to_thread(
            self.ons_data_client.publish_message,
            topic_id=topic_id,
            message_details=details,
        )
        self._rate_limiter.reset()
        data = response.data
        return {
            "message_id": getattr(data, "message_id", None),
            "status": "sent",
            "topic_id": topic_id,
        }
    except oci.exceptions.ServiceError as e:
        if e.status == 429:
            await self._rate_limiter.handle_rate_limit()
            return await self.publish_notification(topic_id, title, body)
        raise
```

- [ ] **Step 5: Run config tests**

Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py::TestNotificationsConfig -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/config.py src/oci_logan_mcp/client.py tests/test_config.py
git commit -m "feat(report-delivery): add notification delivery config"
```

---

### Task 2: Deterministic Markdown To PDF Renderer

**Files:**
- Create: `src/oci_logan_mcp/report_pdf.py`
- Create: `tests/test_report_pdf.py`

- [ ] **Step 1: Write failing PDF tests**

Create `tests/test_report_pdf.py`:

```python
from pathlib import Path

import pytest

from oci_logan_mcp.report_pdf import ReportPdfError, render_markdown_pdf


SAMPLE_REPORT = """# Incident Report

## Executive Summary
The investigation found one anomalous source.

## Top Findings
- Apache logs increased by 300%.
- Parser failures were not detected.

## Recommended Next Steps
1. Run trace_request_id for the affected request.
"""


def test_render_markdown_pdf_creates_valid_pdf(tmp_path):
    out = tmp_path / "incident.pdf"
    result = render_markdown_pdf(SAMPLE_REPORT, title="Incident Report", output_path=out)

    assert result == out
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF")
    assert out.stat().st_size > 500


def test_render_markdown_pdf_rejects_empty_markdown(tmp_path):
    with pytest.raises(ReportPdfError, match="markdown"):
        render_markdown_pdf("", title="Empty", output_path=tmp_path / "empty.pdf")


def test_render_markdown_pdf_creates_parent_dir(tmp_path):
    out = tmp_path / "nested" / "incident.pdf"
    render_markdown_pdf(SAMPLE_REPORT, title="Incident Report", output_path=out)
    assert out.exists()


def test_render_markdown_pdf_has_no_creation_date_metadata(tmp_path):
    out = tmp_path / "incident.pdf"
    render_markdown_pdf(SAMPLE_REPORT, title="Incident Report", output_path=out)
    data = out.read_bytes()
    assert b"/CreationDate" not in data
    assert b"/ModDate" not in data
```

- [ ] **Step 2: Run PDF tests and verify failure**

Run:

```bash
MPLCONFIGDIR=/tmp/logan-mcp-mpl PYTHONPATH=src python3 -m pytest tests/test_report_pdf.py -q
```

Expected: FAIL because `oci_logan_mcp.report_pdf` does not exist.

- [ ] **Step 3: Implement renderer**

Create `src/oci_logan_mcp/report_pdf.py`:

```python
"""Deterministic Markdown-to-PDF rendering for incident reports."""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Iterable, List, Tuple

import matplotlib

matplotlib.use("Agg")

from matplotlib import pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages


class ReportPdfError(ValueError):
    """Raised when a report cannot be rendered to PDF."""


def render_markdown_pdf(markdown: str, title: str | None, output_path: Path) -> Path:
    """Render Markdown-ish report text to a valid local PDF.

    The renderer intentionally supports the stable subset emitted by
    ReportGenerator: headings, bullets, numbered lines, fenced code, and plain
    paragraphs. It is not a general Markdown engine. The output content and
    metadata are stable, but byte-for-byte PDF equality is not guaranteed by
    matplotlib.
    """
    if not isinstance(markdown, str) or not markdown.strip():
        raise ReportPdfError("markdown is required and must be a non-empty string")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = _layout_lines(markdown, title=title)
    pages = _paginate(lines, lines_per_page=52)

    metadata = {
        "Creator": "logan-mcp-server",
        "Producer": "matplotlib",
        "CreationDate": None,
        "ModDate": None,
    }
    with PdfPages(output_path, metadata=metadata) as pdf:
        for page in pages:
            fig = plt.figure(figsize=(8.27, 11.69))
            fig.patch.set_facecolor("white")
            ax = fig.add_axes([0, 0, 1, 1])
            ax.axis("off")

            y = 0.96
            for text, style in page:
                font_size = 10
                weight = "normal"
                family = "DejaVu Sans"
                if style == "title":
                    font_size = 18
                    weight = "bold"
                elif style == "h1":
                    font_size = 15
                    weight = "bold"
                elif style == "h2":
                    font_size = 12
                    weight = "bold"
                elif style == "code":
                    font_size = 8
                    family = "DejaVu Sans Mono"

                ax.text(
                    0.08,
                    y,
                    text,
                    fontsize=font_size,
                    fontweight=weight,
                    fontfamily=family,
                    va="top",
                    wrap=False,
                )
                y -= 0.027 if style == "code" else 0.032
            pdf.savefig(fig, bbox_inches="tight")
            plt.close(fig)

    return output_path


def _layout_lines(markdown: str, title: str | None) -> List[Tuple[str, str]]:
    laid_out: List[Tuple[str, str]] = []
    if title:
        laid_out.append((title.strip(), "title"))
        laid_out.append(("", "body"))

    in_code = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            laid_out.extend((chunk, "code") for chunk in _wrap(line, width=92))
            continue
        if not line.strip():
            laid_out.append(("", "body"))
            continue
        if line.startswith("# "):
            laid_out.append((line[2:].strip(), "h1"))
            continue
        if line.startswith("## "):
            laid_out.append((line[3:].strip(), "h2"))
            continue

        indent = ""
        content = line
        if line.lstrip().startswith(("- ", "* ")):
            indent = "  "
        wrapped = _wrap(content, width=92)
        if not wrapped:
            laid_out.append(("", "body"))
        for idx, chunk in enumerate(wrapped):
            laid_out.append(((indent if idx else "") + chunk, "body"))
    return laid_out


def _wrap(text: str, width: int) -> List[str]:
    return textwrap.wrap(
        text,
        width=width,
        replace_whitespace=False,
        drop_whitespace=False,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [""]


def _paginate(
    lines: Iterable[Tuple[str, str]],
    lines_per_page: int,
) -> List[List[Tuple[str, str]]]:
    pages: List[List[Tuple[str, str]]] = []
    current: List[Tuple[str, str]] = []
    for line in lines:
        current.append(line)
        if len(current) >= lines_per_page:
            pages.append(current)
            current = []
    if current:
        pages.append(current)
    return pages or [[("No report content.", "body")]]
```

- [ ] **Step 4: Run PDF tests**

Run:

```bash
MPLCONFIGDIR=/tmp/logan-mcp-mpl PYTHONPATH=src python3 -m pytest tests/test_report_pdf.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/report_pdf.py tests/test_report_pdf.py
git commit -m "feat(report-delivery): render reports as pdf"
```

---

### Task 3: Extend NotificationService Transports

**Files:**
- Modify: `src/oci_logan_mcp/notification_service.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_notification_service.py`

- [ ] **Step 1: Write failing Telegram document tests**

Add imports in `tests/test_notification_service.py`:

```python
from pathlib import Path
from unittest.mock import AsyncMock
```

Add tests under `TestSendToTelegram`:

```python
@pytest.mark.asyncio
async def test_sends_telegram_document(self, tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    svc = NotificationService(make_settings(
        telegram_token="123:ABC", telegram_chat="-100999"
    ))

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["content_type"] = req.headers["Content-type"]
        captured["body"] = req.data
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = json.dumps({
            "ok": True,
            "result": {"message_id": 44},
        }).encode()
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = await svc.send_telegram_document(
            file_path=pdf,
            caption="Incident summary",
        )

    assert result["status"] == "sent"
    assert result["message_id"] == "44"
    assert captured["url"].endswith("/sendDocument")
    assert "multipart/form-data" in captured["content_type"]
    assert b'name="chat_id"' in captured["body"]
    assert b'name="document"; filename="report.pdf"' in captured["body"]
    assert b"%PDF-1.4 test" in captured["body"]


@pytest.mark.asyncio
async def test_sends_telegram_document_requires_existing_file(tmp_path):
    svc = NotificationService(make_settings(
        telegram_token="123:ABC", telegram_chat="-100999"
    ))
    with pytest.raises(ValueError, match="file"):
        await svc.send_telegram_document(file_path=tmp_path / "missing.pdf")
```

- [ ] **Step 2: Write failing ONS tests**

Add:

```python
def make_settings(slack_url="", telegram_token="", telegram_chat="", ons_topic=""):
    s = Settings()
    s.notifications.slack.webhook_url = slack_url
    s.notifications.telegram.bot_token = telegram_token
    s.notifications.telegram.default_chat_id = telegram_chat
    s.notifications.ons.default_topic_ocid = ons_topic
    return s
```

Add:

```python
class TestSendToOnsEmail:
    @pytest.mark.asyncio
    async def test_raises_if_ons_client_missing(self):
        svc = NotificationService(make_settings(ons_topic="ocid1.onstopic.oc1..abc"))
        with pytest.raises(ValueError, match="OCI client"):
            await svc.send_to_ons_email(title="Report", body="Body")

    @pytest.mark.asyncio
    async def test_raises_if_topic_missing(self):
        oci_client = MagicMock()
        svc = NotificationService(make_settings(), oci_client=oci_client)
        with pytest.raises(ValueError, match="ONS topic"):
            await svc.send_to_ons_email(title="Report", body="Body")

    @pytest.mark.asyncio
    async def test_publishes_to_ons_topic(self):
        oci_client = MagicMock()
        oci_client.publish_notification = AsyncMock(
            return_value={"status": "sent", "message_id": "mid-1"}
        )
        svc = NotificationService(
            make_settings(ons_topic="ocid1.onstopic.oc1..abc"),
            oci_client=oci_client,
        )

        result = await svc.send_to_ons_email(title="Report", body="Body")

        assert result["status"] == "sent"
        assert result["destination"] == "email"
        assert result["message_id"] == "mid-1"
        oci_client.publish_notification.assert_awaited_once_with(
            topic_id="ocid1.onstopic.oc1..abc",
            title="Report",
            body="Body",
        )
```

- [ ] **Step 3: Run notification tests and verify failure**

Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_notification_service.py -q
```

Expected: FAIL because `NotificationService.__init__` does not accept `oci_client`, `send_telegram_document` does not exist, and `send_to_ons_email` does not exist.

- [ ] **Step 4: Implement transport methods**

Update `NotificationService.__init__`:

```python
def __init__(self, settings: Settings, oci_client: Any = None):
    self.slack_config = settings.notifications.slack
    self.telegram_config = settings.notifications.telegram
    self.ons_config = settings.notifications.ons
    self.oci_client = oci_client
```

Add:

```python
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
    data = await asyncio.to_thread(self._post_telegram_document, url, body, content_type)
    return {
        "status": "sent",
        "destination": "telegram",
        "message_id": str((data.get("result") or {}).get("message_id", "")) or None,
    }
```

Add:

```python
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
```

Add helpers:

```python
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
```

Update `MCPHandlers.__init__`:

```python
self.notification_service = NotificationService(settings, oci_client=oci_client)
```

- [ ] **Step 5: Run notification tests**

Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_notification_service.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/notification_service.py src/oci_logan_mcp/handlers.py tests/test_notification_service.py
git commit -m "feat(report-delivery): add telegram document and ons transports"
```

---

### Task 4: ReportDeliveryService Orchestration

**Files:**
- Create: `src/oci_logan_mcp/report_delivery.py`
- Create: `tests/test_report_delivery.py`

- [ ] **Step 1: Write failing orchestration tests**

Create `tests/test_report_delivery.py`:

```python
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.config import Settings
from oci_logan_mcp.report_delivery import (
    TELEGRAM_DOCUMENT_MAX_BYTES,
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
```

- [ ] **Step 2: Write failing audit test**

Add:

```python
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
```

- [ ] **Step 3: Run report delivery tests and verify failure**

Run:

```bash
MPLCONFIGDIR=/tmp/logan-mcp-mpl PYTHONPATH=src python3 -m pytest tests/test_report_delivery.py -q
```

Expected: FAIL because `report_delivery.py` does not exist.

- [ ] **Step 4: Implement `report_delivery.py`**

Create `src/oci_logan_mcp/report_delivery.py`:

```python
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
                markdown=markdown,
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
        markdown: str,
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
                    title, summary, pdf_path, output_format, recipients
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
        title: str,
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
        return self.settings.report_delivery.artifact_dir / f"{slug}-{stamp}-{uuid4().hex[:8]}.pdf"

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
```

Add module helpers:

```python
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
```

- [ ] **Step 5: Run report delivery tests**

Run:

```bash
MPLCONFIGDIR=/tmp/logan-mcp-mpl PYTHONPATH=src python3 -m pytest tests/test_report_delivery.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/report_delivery.py tests/test_report_delivery.py
git commit -m "feat(report-delivery): orchestrate report delivery"
```

---

### Task 5: Tool Schema, Handler, And Read-Only Classification

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/read_only_guard.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_read_only_guard.py`

- [ ] **Step 1: Write failing tool schema tests**

In `tests/test_tools.py`, add:

```python
def test_deliver_report_schema_is_markdown_first():
    tools = {tool["name"]: tool for tool in get_tool_definitions()}
    schema = tools["deliver_report"]["inputSchema"]
    props = schema["properties"]

    assert "report" in props
    report_schema = props["report"]
    assert report_schema["required"] == ["markdown"]
    assert "report_id" not in report_schema["properties"]
    assert props["channels"]["items"]["enum"] == ["telegram", "email", "slack"]
    assert props["format"]["enum"] == ["pdf", "markdown", "both"]
```

- [ ] **Step 2: Write failing handler tests**

In `tests/test_handlers.py`, add:

```python
class TestDeliverReportHandler:
    @pytest.mark.asyncio
    async def test_deliver_report_routes_to_service(self, handlers):
        handlers.report_delivery_service.deliver = AsyncMock(
            return_value={"status": "sent", "delivered": [], "pdf_path": "/tmp/r.pdf"}
        )

        result = await handlers.handle_tool_call(
            "deliver_report",
            {
                "report": {"markdown": "# Report", "title": "Report"},
                "channels": ["telegram"],
                "format": "pdf",
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "sent"
        handlers.report_delivery_service.deliver.assert_awaited_once_with(
            report={"markdown": "# Report", "title": "Report"},
            channels=["telegram"],
            recipients={},
            output_format="pdf",
            title=None,
        )

    @pytest.mark.asyncio
    async def test_deliver_report_rejects_missing_markdown(self, handlers):
        result = await handlers.handle_tool_call(
            "deliver_report",
            {"report": {"report_id": "r-123"}},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_report_markdown"

    @pytest.mark.asyncio
    async def test_deliver_report_returns_delivery_option_errors(self, handlers):
        handlers.report_delivery_service.deliver = AsyncMock(
            side_effect=ReportDeliveryError("unsupported channels: ['sms']")
        )

        result = await handlers.handle_tool_call(
            "deliver_report",
            {
                "report": {"markdown": "# Report"},
                "channels": ["sms"],
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_delivery_options"
        assert "sms" in payload["error"]
```

Add imports:

```python
from oci_logan_mcp.report_delivery import ReportDeliveryError
```

- [ ] **Step 3: Write failing read-only test updates**

In `tests/test_read_only_guard.py`, add `deliver_report` to `expected_subset`.

Do not add a redundant parametrized read-only handler test. `test_all_registered_tools_are_classified` and `test_read_only_blocks_mutating_tool` already cover the invariant.

- [ ] **Step 4: Run focused tests and verify failure**

Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_tools.py::test_deliver_report_schema_is_markdown_first tests/test_handlers.py::TestDeliverReportHandler tests/test_read_only_guard.py -q
```

Expected: FAIL because the tool, handler, and classification do not exist.

- [ ] **Step 5: Implement handler wiring**

In `src/oci_logan_mcp/handlers.py`, import:

```python
from .report_delivery import ReportDeliveryError, ReportDeliveryService
```

In `MCPHandlers.__init__`, after `self.notification_service`:

```python
self.report_delivery_service = ReportDeliveryService(
    settings=settings,
    notification_service=self.notification_service,
    audit_logger=audit_logger,
    user_id=user_store.user_id,
)
```

In `handle_tool_call`, add to the `handlers` dict:

```python
"deliver_report": self._deliver_report,
```

Add:

```python
async def _deliver_report(self, args: Dict) -> List[Dict]:
    report = args.get("report")
    if not isinstance(report, dict) or not isinstance(report.get("markdown"), str):
        return [{"type": "text", "text": json.dumps({
            "status": "error",
            "error_code": "missing_report_markdown",
            "error": "report.markdown is required in P0; report_id lookup is deferred",
        }, indent=2)}]

    try:
        result = await self.report_delivery_service.deliver(
            report=report,
            channels=args.get("channels", ["telegram"]),
            recipients=args.get("recipients") or {},
            output_format=args.get("format", "pdf"),
            title=args.get("title"),
        )
    except ReportDeliveryError as e:
        return [{"type": "text", "text": json.dumps({
            "status": "error",
            "error_code": "invalid_delivery_options",
            "error": str(e),
        }, indent=2)}]

    return [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
```

- [ ] **Step 6: Redact deliver-report args in generic audit logs**

Add a helper near `_build_confirmation_unavailable_response`:

```python
def _clean_args_for_audit(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    clean_args = {
        k: v for k, v in arguments.items()
        if k not in (
            "confirmation_token",
            "confirmation_secret",
            "confirmation_secret_confirm",
        )
    }
    if tool_name == "deliver_report":
        clean_args = dict(clean_args)
        if "recipients" in clean_args:
            clean_args["recipients"] = "<redacted>"
    return clean_args
```

Use it in the invoked and guarded-call clean-args paths:

```python
clean_args_for_invoked = self._clean_args_for_audit(name, arguments)
```

and:

```python
clean_args = self._clean_args_for_audit(name, arguments)
```

In the read-only-blocked audit path, log `args=self._clean_args_for_audit(name, arguments)`.

- [ ] **Step 7: Implement tool schema**

In `src/oci_logan_mcp/tools.py`, add:

```python
{
    "name": "deliver_report",
    "description": (
        "Deliver a generated incident report via Telegram, Slack, or OCI Notifications "
        "email-topic delivery. P0 accepts inline markdown report content only; "
        "report_id lookup is deferred until report persistence exists."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["report"],
        "properties": {
            "report": {
                "type": "object",
                "required": ["markdown"],
                "properties": {
                    "markdown": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
            "channels": {
                "type": "array",
                "items": {"type": "string", "enum": ["telegram", "email", "slack"]},
                "default": ["telegram"],
            },
            "recipients": {
                "type": "object",
                "properties": {
                    "telegram_chat_id": {"type": "string"},
                    "email_topic_ocid": {"type": "string"},
                },
            },
            "format": {
                "type": "string",
                "enum": ["pdf", "markdown", "both"],
                "default": "pdf",
            },
            "title": {"type": "string"},
        },
    },
},
```

- [ ] **Step 8: Implement read-only classification**

Add `"deliver_report"` to `MUTATING_TOOLS` in `src/oci_logan_mcp/read_only_guard.py`.

Add `"deliver_report"` to `expected_subset` in `tests/test_read_only_guard.py`.

- [ ] **Step 9: Run focused tests**

Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_tools.py::test_deliver_report_schema_is_markdown_first tests/test_handlers.py::TestDeliverReportHandler tests/test_read_only_guard.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/oci_logan_mcp/handlers.py src/oci_logan_mcp/tools.py src/oci_logan_mcp/read_only_guard.py tests/test_handlers.py tests/test_tools.py tests/test_read_only_guard.py
git commit -m "feat(report-delivery): expose deliver_report tool"
```

---

### Task 6: Handler Audit Coverage For Redacted Recipients

**Files:**
- Modify: `tests/test_handlers.py`
- Modify: `src/oci_logan_mcp/handlers.py`

- [ ] **Step 1: Write focused audit-redaction test**

In `tests/test_handlers.py`, add:

```python
@pytest.mark.asyncio
async def test_deliver_report_invoked_audit_redacts_recipients(self, handlers):
    handlers.report_delivery_service.deliver = AsyncMock(
        return_value={"status": "sent", "delivered": [], "pdf_path": None}
    )

    await handlers.handle_tool_call(
        "deliver_report",
        {
            "report": {"markdown": "# Report"},
            "channels": ["telegram"],
            "recipients": {
                "telegram_chat_id": "-100999",
                "email_topic_ocid": "ocid1.onstopic.oc1..secret",
            },
        },
    )

    invoked = [
        call.kwargs for call in handlers.audit_logger.log.call_args_list
        if call.kwargs["outcome"] == "invoked"
    ][-1]
    assert invoked["tool"] == "deliver_report"
    assert invoked["args"]["recipients"] == "<redacted>"
    assert "-100999" not in str(invoked["args"])
    assert "secret" not in str(invoked["args"])
```

If the existing `handlers` fixture uses a concrete `AuditLogger` instead of a mock, use `mock_audit_logger` or add a local handler construction that passes a `MagicMock()` audit logger. Do not weaken the assertion to string search over a file.

- [ ] **Step 2: Write non-delivery audit no-op regression test**

Add:

```python
@pytest.mark.asyncio
async def test_non_delivery_invoked_audit_args_are_unchanged(self, handlers):
    handlers.investigate_tool.run = AsyncMock(return_value={
        "summary": "ok",
        "partial": False,
        "partial_reasons": [],
    })

    await handlers.handle_tool_call(
        "investigate_incident",
        {
            "query": "'Severity' = 'ERROR'",
            "time_range": "last_1_hour",
            "top_k": 2,
            "compartment_id": "ocid1.compartment.oc1..abc",
        },
    )

    invoked = [
        call.kwargs for call in handlers.audit_logger.log.call_args_list
        if call.kwargs["outcome"] == "invoked"
    ][-1]
    assert invoked["tool"] == "investigate_incident"
    assert invoked["args"] == {
        "query": "'Severity' = 'ERROR'",
        "time_range": "last_1_hour",
        "top_k": 2,
        "compartment_id": "ocid1.compartment.oc1..abc",
    }
```

This pins `_clean_args_for_audit` as a no-op for unrelated tools except existing confirmation-secret/token stripping.

- [ ] **Step 3: Run audit tests and verify failure**

Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_handlers.py::test_deliver_report_invoked_audit_redacts_recipients tests/test_handlers.py::test_non_delivery_invoked_audit_args_are_unchanged -q
```

Expected before Task 5 Step 6: FAIL because recipients are not redacted. If Task 5 Step 6 was already implemented, expected PASS.

- [ ] **Step 4: Implement or confirm `_clean_args_for_audit` coverage**

Make sure every audit log in `handle_tool_call` that uses caller arguments goes through `_clean_args_for_audit(name, arguments)`:

- invoked
- read_only_blocked
- confirmation_unavailable
- confirmation_requested
- confirmation_failed
- confirmed
- executed
- execution_failed

For non-`deliver_report` tools, the helper must preserve current argument shape except confirmation secret/token stripping.

- [ ] **Step 5: Run audit and confirmation regression tests**

Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_handlers.py::test_deliver_report_invoked_audit_redacts_recipients tests/test_handlers.py::test_non_delivery_invoked_audit_args_are_unchanged tests/test_confirmation.py tests/test_secret_store.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/handlers.py tests/test_handlers.py
git commit -m "fix(report-delivery): redact delivery recipients in audit logs"
```

---

### Task 7: Documentation And Backlog Alignment

**Files:**
- Modify: `docs/phase-2/specs/reports-and-playbooks.md`
- Modify: `docs/phase-2/backlog.md`

- [ ] **Step 1: Update Report Delivery spec P0 interface**

In `docs/phase-2/specs/reports-and-playbooks.md`, update the Report Delivery tool interface to:

```text
deliver_report(
  report: {markdown: str, title: str | None},
  channels: list["telegram" | "email" | "slack"] = ["telegram"],
  recipients: {
    telegram_chat_id: str | None,     # default: from config
    email_topic_ocid: str | None,     # default: from config
  } = {},
  format: "pdf" | "markdown" | "both" = "pdf",
  title: str | None = None,
) -> {
  status: "sent" | "partial" | "failed" | "error",
  delivered: list[{channel, status, message_id, artifact, recipient, error?}],
  pdf_path: str | None,
}
```

Add a note immediately below:

```markdown
> P0 intentionally does not accept `report_id`. N3 P0 returns `report_id` as a
> correlation id only; report persistence / lookup is tracked as `N3-F4`.
> `deliver_report(report_id=...)` should land with that persistence feature.
```

- [ ] **Step 2: Update delivery notes**

In the same spec section:

- Add Slack to P0 as "optional inline summary via existing webhook only"; Telegram remains the default IM path.
- Clarify email as "OCI Notifications topic publish to email subscribers".
- Keep Object Storage/PAR and branding as P1 deferrals.

- [ ] **Step 3: Update backlog**

Add a `Report Delivery` subsection under the N3 section in `docs/phase-2/backlog.md`:

```markdown
#### Report Delivery
Source: [reports-and-playbooks.md](specs/reports-and-playbooks.md) and [2026-04-24-report-delivery.md](plans/2026-04-24-report-delivery.md)

- `RD-F1` — `deliver_report` support for `{report_id}` once N3-F4 report persistence / lookup exists.
- `RD-F2` — Object Storage bucket + PAR URL for full PDF access from email notifications.
- `RD-F3` — Branding, custom CSS, and custom PDF templates.
- `RD-F4` — Full PDF delivery to Slack via Slack Web API file upload. P0 Slack delivery uses the existing webhook and sends an inline summary only.
- `RD-F5` — Oracle Slack workspace/app rollout after validating the Slack path with a private/free Slack registration.
```

- [ ] **Step 4: Commit**

```bash
git add docs/phase-2/specs/reports-and-playbooks.md docs/phase-2/backlog.md
git commit -m "docs(report-delivery): document p0 delivery boundaries"
```

---

### Task 8: Full Verification

**Files:**
- All changed files.

- [ ] **Step 1: Run focused delivery tests**

Run:

```bash
MPLCONFIGDIR=/tmp/logan-mcp-mpl PYTHONPATH=src python3 -m pytest tests/test_report_pdf.py tests/test_report_delivery.py tests/test_notification_service.py -q
```

Expected: PASS.

- [ ] **Step 2: Run handler/schema/guard tests**

Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_handlers.py::TestDeliverReportHandler tests/test_tools.py tests/test_read_only_guard.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full suite with supported interpreter**

Run:

```bash
MPLCONFIGDIR=/tmp/logan-mcp-mpl PYTHONPATH=src python3 -m pytest -q
```

Expected: PASS. Do not use bare `pytest` on this machine; it resolves to Apple Python 3.9 and fails because `hashlib.scrypt` is unavailable. The project requires Python `>=3.10`.

- [ ] **Step 4: Manual smoke shape**

Use a local handler test or REPL snippet with mocked notification methods to call:

```python
await handlers.handle_tool_call(
    "deliver_report",
    {
        "report": {"markdown": "# Incident Report\n\n## Executive Summary\nOK"},
        "channels": ["telegram", "email", "slack"],
        "format": "both",
    },
)
```

Expected response:

- `status` is `sent` when all mocks succeed.
- `delivered` has three rows.
- Telegram row has `artifact: "pdf"`.
- Email and Slack rows have `artifact: "summary"`.
- `pdf_path` points to an existing PDF.

- [ ] **Step 5: Review diff for scope**

Run:

```bash
git diff --stat main...HEAD
git diff --check
```

Expected:

- No whitespace errors.
- Diff scope limited to report delivery, notifications, config, docs, and tests.

---

## Self-Review Checklist

- Spec coverage:
  - Valid PDF for Markdown: Task 2.
  - Telegram 50 MB cap: Task 4.
  - Telegram `sendDocument`: Task 3.
  - ONS email inline summary only: Tasks 3 and 4.
  - Slack inline summary: Task 4.
  - Partial failure response shape: Task 4.
  - Audit logging with redacted recipients: Tasks 4 and 6.
  - Mutating/read-only classification: Task 5.
  - Deferrals in backlog: Task 7.
- No `report_id` P0 schema stub: Task 5 schema test and Task 7 docs.
- No internal LLM, no SMTP, no Object Storage, no Slack OAuth/file upload in P0.
- Type consistency:
  - Tool arg is `format`; service arg is `output_format`.
  - Recipient keys are `telegram_chat_id` and `email_topic_ocid`.
  - Channel enum order is `["telegram", "email", "slack"]`.
