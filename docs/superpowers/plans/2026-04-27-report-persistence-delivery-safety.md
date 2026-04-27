# Report Persistence, Delivery, and Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make generated incident reports durable and manually retrievable, allow `deliver_report` to send a stored report by `report_id`, keep delivery explicitly opt-in, and harden destructive local actions behind the existing confirmation-secret flow.

**Architecture:** Add a small filesystem-backed `ReportStore` under `settings.report_delivery.artifact_dir / "store"` while leaving delivery PDFs flat in `artifact_dir`. `generate_incident_report` continues to render the report, then persists it through the store and returns paths. New read/list tools use the same store. `deliver_report` accepts either inline markdown or a stored `report_id`, rejects ambiguous inputs, and sends only after an explicit tool call. Destructive-tool safety is enforced through named mutation classifications next to `GUARDED_TOOLS` and drift tests.

**Tech Stack:** Python 3.11+, pytest, existing Logan MCP handler/tool schema patterns, existing `Settings`, `ReportGenerator`, `ReportDeliveryService`, `ConfirmationManager`, and read-only guard modules.

---

## Implementation Overview

The implementation has four workstreams:

1. Durable report storage.
2. Tool and handler wiring for generate/get/list/deliver report flows.
3. Destructive-action 2FA classification hardening.
4. Documentation and verification.

The approved behavior is:

- Reports are stored under `settings.report_delivery.artifact_dir / "store" / rpt_<32 hex> /`.
- Delivery PDFs remain directly under `settings.report_delivery.artifact_dir`.
- Stored report IDs must match `^rpt_[0-9a-f]{32}$` before any path joins.
- `ReportStore.save()` writes `report.md`, then optional `report.html`, then `metadata.json` last, each via temp-file plus rename.
- `generate_incident_report` accepts optional `title`, persists by default, and returns artifact paths.
- `get_incident_report` returns markdown plus local paths and metadata.
- `list_incident_reports` returns newest reports plus `warnings.corrupt_count`.
- `deliver_report` accepts either `report.markdown` or `report.report_id`; both together returns `conflicting_report_inputs`.
- `deliver_report` remains non-2FA because delivery is opt-in at assistant/client behavior, not automatically chained by the server.
- `delete_playbook` is 2FA guarded.
- Additive/session-state mutations remain explicitly exempted with named reasons.

---

## Task 1: Add the Report Store Contract Tests

- [ ] Create `tests/test_report_store.py`.

Add tests first for the storage invariants. Use `tmp_path` and instantiate the store with the delivery artifact directory, not the final `store` directory. The store should create the `store` subdirectory internally.

```python
from __future__ import annotations

import json

import pytest

from oci_logan_mcp.report_store import (
    InvalidReportIdError,
    ReportNotFoundError,
    ReportStore,
)


def _report(report_id: str = "rpt_0123456789abcdef0123456789abcdef") -> dict:
    return {
        "report_id": report_id,
        "markdown": "# Incident Report\n\nBody",
        "html": "<h1>Incident Report</h1>",
        "metadata": {
            "title": "24-hour failures and issues report",
            "generated_at": "2026-04-27T12:00:00Z",
            "time_range": "last_24_hours",
            "summary_length": "standard",
        },
    }


def test_save_writes_report_files_under_store(tmp_path):
    store = ReportStore(tmp_path)

    saved = store.save(_report())

    report_dir = tmp_path / "store" / "rpt_0123456789abcdef0123456789abcdef"
    assert saved["report_id"] == "rpt_0123456789abcdef0123456789abcdef"
    assert saved["markdown_path"] == str(report_dir / "report.md")
    assert saved["html_path"] == str(report_dir / "report.html")
    assert saved["metadata_path"] == str(report_dir / "metadata.json")
    assert (report_dir / "report.md").read_text(encoding="utf-8") == "# Incident Report\n\nBody"
    assert (report_dir / "report.html").read_text(encoding="utf-8") == "<h1>Incident Report</h1>"
    metadata = json.loads((report_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["title"] == "24-hour failures and issues report"
    assert metadata["markdown_path"] == str(report_dir / "report.md")
    assert metadata["html_path"] == str(report_dir / "report.html")


def test_save_writes_metadata_last(tmp_path, monkeypatch):
    store = ReportStore(tmp_path)
    calls: list[str] = []
    original = store._atomic_write_text

    def tracking_write(path, content):
        calls.append(path.name)
        original(path, content)

    monkeypatch.setattr(store, "_atomic_write_text", tracking_write)

    store.save(_report())

    assert calls == ["report.md", "report.html", "metadata.json"]


def test_get_returns_markdown_html_paths_and_metadata(tmp_path):
    store = ReportStore(tmp_path)
    store.save(_report())

    loaded = store.get("rpt_0123456789abcdef0123456789abcdef")

    assert loaded["markdown"] == "# Incident Report\n\nBody"
    assert loaded["html"] == "<h1>Incident Report</h1>"
    assert loaded["metadata"]["title"] == "24-hour failures and issues report"
    assert loaded["markdown_path"].endswith("/report.md")
    assert loaded["html_path"].endswith("/report.html")
    assert loaded["metadata_path"].endswith("/metadata.json")


@pytest.mark.parametrize(
    "report_id",
    [
        "../rpt_0123456789abcdef0123456789abcdef",
        "rpt_0123456789ABCDEF0123456789abcdef",
        "rpt_0123456789abcdef0123456789abcde",
        "not_a_report",
    ],
)
def test_invalid_report_ids_are_rejected_before_path_join(tmp_path, report_id):
    store = ReportStore(tmp_path)

    with pytest.raises(InvalidReportIdError):
        store.get(report_id)


def test_get_missing_report_raises_not_found(tmp_path):
    store = ReportStore(tmp_path)

    with pytest.raises(ReportNotFoundError):
        store.get("rpt_0123456789abcdef0123456789abcdef")


def test_list_reports_newest_first_and_counts_corrupt_entries(tmp_path):
    store = ReportStore(tmp_path)
    store.save(
        _report("rpt_11111111111111111111111111111111")
        | {"metadata": {"title": "Older", "generated_at": "2026-04-27T10:00:00Z"}}
    )
    store.save(
        _report("rpt_22222222222222222222222222222222")
        | {"metadata": {"title": "Newer", "generated_at": "2026-04-27T12:00:00Z"}}
    )

    corrupt_dir = tmp_path / "store" / "rpt_33333333333333333333333333333333"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "metadata.json").write_text("{not json", encoding="utf-8")

    listed = store.list(limit=10)

    assert [entry["report_id"] for entry in listed["reports"]] == [
        "rpt_22222222222222222222222222222222",
        "rpt_11111111111111111111111111111111",
    ]
    assert listed["warnings"] == {"corrupt_count": 1}


def test_list_limit_is_clamped_to_one_hundred(tmp_path):
    store = ReportStore(tmp_path)
    for index in range(105):
        report_id = f"rpt_{index:032x}"
        store.save(_report(report_id) | {"metadata": {"generated_at": f"2026-04-27T12:{index % 60:02d}:00Z"}})

    listed = store.list(limit=500)

    assert len(listed["reports"]) == 100
```

- [ ] Run the new test file and confirm it fails because `report_store.py` does not exist yet.

Command:

```bash
PYTHONPATH=src python3 -m pytest tests/test_report_store.py -q
```

Expected failure:

```text
ModuleNotFoundError: No module named 'oci_logan_mcp.report_store'
```

---

## Task 2: Implement `ReportStore`

- [ ] Create `src/oci_logan_mcp/report_store.py`.

Implement a small store with explicit exceptions and no dependency on MCP handler state.

Core behavior:

- Constructor receives `artifact_dir: Path | str`.
- Actual report root is `Path(artifact_dir).expanduser() / "store"`.
- `save(report: dict) -> dict` validates the report id, creates the report directory, writes files atomically, and returns paths.
- `get(report_id: str) -> dict` validates the ID, reads markdown/html/metadata, and raises typed exceptions.
- `list(limit: int = 20) -> dict` returns newest-first metadata summaries and `warnings.corrupt_count`.
- Validate IDs before computing `self.root / report_id`.

Use this shape:

```python
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

REPORT_ID_RE = re.compile(r"^rpt_[0-9a-f]{32}$")


class ReportStoreError(Exception):
    """Base class for report store failures."""


class InvalidReportIdError(ReportStoreError):
    """Raised when a report id is unsafe or malformed."""


class ReportNotFoundError(ReportStoreError):
    """Raised when a report id does not exist in the store."""


class ReportStoreCorruptError(ReportStoreError):
    """Raised when a stored report cannot be read safely."""


class ReportStore:
    def __init__(self, artifact_dir: Path | str) -> None:
        self.artifact_dir = Path(artifact_dir).expanduser()
        self.root = self.artifact_dir / "store"

    def save(self, report: dict[str, Any]) -> dict[str, Any]:
        report_id = self._validate_report_id(str(report.get("report_id", "")))
        markdown = report.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ReportStoreError("report.markdown is required")

        report_dir = self._report_dir(report_id)
        report_dir.mkdir(parents=True, exist_ok=True)

        markdown_path = report_dir / "report.md"
        html = report.get("html")
        html_path = report_dir / "report.html" if isinstance(html, str) and html.strip() else None
        metadata_path = report_dir / "metadata.json"

        metadata = dict(report.get("metadata") or {})
        metadata.setdefault("title", "Incident Report")
        metadata["report_id"] = report_id
        metadata["markdown_path"] = str(markdown_path)
        metadata["html_path"] = str(html_path) if html_path else None
        metadata["metadata_path"] = str(metadata_path)

        self._atomic_write_text(markdown_path, markdown)
        if html_path:
            self._atomic_write_text(html_path, html)
        self._atomic_write_text(metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n")

        return {
            "report_id": report_id,
            "markdown_path": str(markdown_path),
            "html_path": str(html_path) if html_path else None,
            "metadata_path": str(metadata_path),
            "metadata": metadata,
        }

    def get(self, report_id: str) -> dict[str, Any]:
        report_id = self._validate_report_id(report_id)
        report_dir = self._report_dir(report_id)
        metadata_path = report_dir / "metadata.json"
        markdown_path = report_dir / "report.md"
        html_path = report_dir / "report.html"

        if not metadata_path.exists() or not markdown_path.exists():
            raise ReportNotFoundError(f"Report not found: {report_id}")

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
            html = html_path.read_text(encoding="utf-8") if html_path.exists() else None
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportStoreCorruptError(f"Report is corrupt: {report_id}") from exc

        return {
            "report_id": report_id,
            "markdown": markdown,
            "markdown_path": str(markdown_path),
            "html": html,
            "html_path": str(html_path) if html_path.exists() else None,
            "metadata": metadata,
            "metadata_path": str(metadata_path),
        }

    def list(self, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        reports: list[dict[str, Any]] = []
        corrupt_count = 0

        if not self.root.exists():
            return {"reports": [], "warnings": {"corrupt_count": 0}}

        for report_dir in self.root.iterdir():
            if not report_dir.is_dir() or not REPORT_ID_RE.match(report_dir.name):
                continue
            metadata_path = report_dir / "metadata.json"
            if not metadata_path.exists():
                corrupt_count += 1
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                corrupt_count += 1
                continue

            reports.append(
                {
                    "report_id": report_dir.name,
                    "title": metadata.get("title", "Incident Report"),
                    "generated_at": metadata.get("generated_at"),
                    "time_range": metadata.get("time_range"),
                    "summary_length": metadata.get("summary_length"),
                    "markdown_path": metadata.get("markdown_path"),
                    "html_path": metadata.get("html_path"),
                    "metadata_path": str(metadata_path),
                }
            )

        reports.sort(key=lambda entry: entry.get("generated_at") or "", reverse=True)
        return {"reports": reports[:limit], "warnings": {"corrupt_count": corrupt_count}}

    def _validate_report_id(self, report_id: str) -> str:
        if not REPORT_ID_RE.match(report_id):
            raise InvalidReportIdError(f"Invalid report_id: {report_id}")
        return report_id

    def _report_dir(self, report_id: str) -> Path:
        return self.root / self._validate_report_id(report_id)

    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
```

- [ ] Run the report store tests.

Command:

```bash
PYTHONPATH=src python3 -m pytest tests/test_report_store.py -q
```

Expected output:

```text
..........
```

The exact dot count may differ if more cases are added, but all tests must pass.

---

## Task 3: Add Optional Report Title to Generator and Tool Schema

- [ ] Update `tests/test_report_generator.py`.

Add a test proving a custom title is reflected in metadata and rendered markdown. Keep existing tests for report content.

```python
def test_generate_uses_custom_title_when_provided():
    generator = ReportGenerator()
    investigation = {
        "incident_id": "inc_123",
        "summary": "Parser failures increased",
        "time_range": "last_24_hours",
        "findings": [],
        "query_results": [],
        "recommendations": [],
    }

    report = generator.generate(investigation, title="24-hour failures and issues report")

    assert report["metadata"]["title"] == "24-hour failures and issues report"
    assert report["markdown"].startswith("# 24-hour failures and issues report")
```

- [ ] Update `src/oci_logan_mcp/report_generator.py`.

Make `title` an optional keyword argument on `ReportGenerator.generate(...)`.

Expected implementation points:

- Add `title: str | None = None` to the signature.
- Normalize with `report_title = title.strip() if title and title.strip() else "Incident Report"`.
- Use `report_title` for the markdown heading and metadata title.
- Keep the generated `report_id = f"rpt_{uuid.uuid4().hex}"`.
- Leave persistence out of `ReportGenerator`; it remains responsible for rendering only.

Expected shape:

```python
def generate(
    self,
    investigation: Mapping[str, Any],
    *,
    format: str = "markdown",
    include_sections: Sequence[str] | None = None,
    summary_length: str = "standard",
    title: str | None = None,
) -> dict[str, Any]:
    report_id = f"rpt_{uuid.uuid4().hex}"
    report_title = title.strip() if title and title.strip() else "Incident Report"
    ...
    metadata = {
        "title": report_title,
        ...
    }
```

- [ ] Update `tests/test_tools.py`.

Extend the `generate_incident_report` schema test to assert an optional string `title` field exists.

Expected assertion:

```python
schema = next(tool for tool in get_tools() if tool["name"] == "generate_incident_report")["inputSchema"]
assert schema["properties"]["title"]["type"] == "string"
assert "title" not in schema.get("required", [])
```

- [ ] Update `src/oci_logan_mcp/tools.py`.

Add optional `title` to the `generate_incident_report` input schema description:

```python
"title": {
    "type": "string",
    "description": "Optional display title for the stored incident report.",
}
```

- [ ] Run focused generator and schema tests.

Command:

```bash
PYTHONPATH=src python3 -m pytest tests/test_report_generator.py tests/test_tools.py -q
```

Expected output: all tests pass.

---

## Task 4: Persist Reports from `generate_incident_report`

- [ ] Update `tests/test_handlers.py`.

In `TestIncidentReports`, add or update the generation test so it proves:

- The handler persists the generated report.
- The response includes `artifacts` with `markdown_path`, optional `html_path`, and `metadata_path`.
- The persisted `metadata.json` includes the custom title.

Use a temporary settings artifact directory if the existing handler fixture supports injected settings. If the fixture constructs `LoganHandlers` directly, create a temporary `Settings` object with `report_delivery.artifact_dir = tmp_path / "reports"`.

Expected test shape:

```python
async def test_generate_incident_report_persists_report(tmp_path):
    settings = Settings()
    settings.report_delivery.artifact_dir = tmp_path / "reports"
    handlers = LoganHandlers(settings=settings)
    investigation = {
        "incident_id": "inc_123",
        "summary": "Parser failures increased",
        "time_range": "last_24_hours",
        "findings": [],
        "query_results": [],
        "recommendations": [],
    }

    response = await handlers.handle_tool_call(
        "generate_incident_report",
        {
            "investigation": investigation,
            "title": "24-hour failures and issues report",
        },
    )
    payload = _json(response)

    assert payload["report_id"].startswith("rpt_")
    assert payload["artifacts"]["markdown_path"].endswith("/report.md")
    assert payload["artifacts"]["metadata_path"].endswith("/metadata.json")
    assert Path(payload["artifacts"]["markdown_path"]).exists()
    assert Path(payload["artifacts"]["metadata_path"]).exists()
    assert payload["metadata"]["title"] == "24-hour failures and issues report"
```

Use the local test helper already present in `tests/test_handlers.py` for converting MCP response content to JSON.

- [ ] Update `src/oci_logan_mcp/handlers.py`.

Wire a `ReportStore` instance into `LoganHandlers`.

Implementation points:

- Import `ReportStore` and `ReportStoreError`.
- In `__init__`, after settings are available:

```python
self.report_store = ReportStore(self.settings.report_delivery.artifact_dir)
```

- In `_generate_incident_report`, pass `title=args.get("title")` to the generator.
- Immediately save the generated report:

```python
try:
    stored = self.report_store.save(report)
except ReportStoreError as exc:
    return self._error("report_persistence_failed", str(exc))
```

- Add artifact paths to the returned report:

```python
report["artifacts"] = {
    "markdown_path": stored["markdown_path"],
    "html_path": stored["html_path"],
    "metadata_path": stored["metadata_path"],
}
report["metadata"] = stored["metadata"]
```

If the existing code represents `artifacts` as a list, change the tool behavior to a mapping with path names and update the existing tests accordingly. This is more useful for manual read/download and matches the approved spec.

- [ ] Run focused handler generation tests.

Command:

```bash
PYTHONPATH=src python3 -m pytest tests/test_handlers.py::TestIncidentReports -q
```

Expected output: all tests in `TestIncidentReports` pass.

---

## Task 5: Add `get_incident_report` and `list_incident_reports`

- [ ] Update `tests/test_tools.py`.

Add schema tests for both tools.

Expected schema properties:

`get_incident_report`:

```python
{
    "report_id": {"type": "string", "description": "..."}
}
```

Required: `["report_id"]`.

`list_incident_reports`:

```python
{
    "limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 20,
    }
}
```

Required: none.

- [ ] Update `tests/test_handlers.py`.

Add handler tests that:

- Generate a report, then call `get_incident_report` and verify markdown/path/metadata are returned.
- Generate two reports, then call `list_incident_reports` and verify newest-first report summaries and `warnings.corrupt_count`.
- Call `get_incident_report` with an invalid ID and expect a structured `invalid_report_id` error.
- Call `get_incident_report` with a valid missing ID and expect a structured `report_not_found` error.

Expected response checks:

```python
loaded = _json(await handlers.handle_tool_call("get_incident_report", {"report_id": report_id}))
assert loaded["report_id"] == report_id
assert loaded["markdown"].startswith("#")
assert loaded["markdown_path"].endswith("/report.md")
assert loaded["metadata"]["title"] == "24-hour failures and issues report"
```

```python
listed = _json(await handlers.handle_tool_call("list_incident_reports", {"limit": 20}))
assert listed["reports"][0]["title"] == "Newer report"
assert listed["warnings"] == {"corrupt_count": 0}
```

- [ ] Update `src/oci_logan_mcp/tools.py`.

Add two tool definitions following existing style:

```python
{
    "name": "get_incident_report",
    "description": "Read a stored incident report by report_id, including markdown, paths, and metadata.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "report_id": {
                "type": "string",
                "description": "Stored report id such as rpt_0123456789abcdef0123456789abcdef.",
            },
        },
        "required": ["report_id"],
    },
}
```

```python
{
    "name": "list_incident_reports",
    "description": "List stored incident reports with local artifact paths.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum reports to return, clamped to 100.",
                "minimum": 1,
                "maximum": 100,
                "default": 20,
            },
        },
    },
}
```

- [ ] Update `src/oci_logan_mcp/handlers.py`.

Register both tool handlers in the same dispatch table as the existing report tools:

```python
"get_incident_report": self._get_incident_report,
"list_incident_reports": self._list_incident_reports,
```

Add handler methods:

```python
async def _get_incident_report(self, args: dict[str, Any]) -> list[types.TextContent]:
    report_id = str(args.get("report_id", ""))
    try:
        report = self.report_store.get(report_id)
    except InvalidReportIdError as exc:
        return self._error("invalid_report_id", str(exc))
    except ReportNotFoundError as exc:
        return self._error("report_not_found", str(exc))
    except ReportStoreError as exc:
        return self._error("report_store_error", str(exc))
    return self._json(report)
```

```python
async def _list_incident_reports(self, args: dict[str, Any]) -> list[types.TextContent]:
    limit = int(args.get("limit", 20))
    return self._json(self.report_store.list(limit=limit))
```

Use the existing handler JSON/error helper names rather than creating duplicate helpers. If the helpers have different names, keep the behavior and adapt the snippet to the local conventions.

- [ ] Confirm audit behavior.

The existing handler-level audit should log tool invocation for both tools. Do not add report markdown to audit arguments. If audit tests already cover generic invocation, no additional audit write is needed.

- [ ] Run focused tool and handler tests.

Command:

```bash
PYTHONPATH=src python3 -m pytest tests/test_tools.py tests/test_handlers.py::TestIncidentReports -q
```

Expected output: all tests pass.

---

## Task 6: Allow `deliver_report` to Use a Stored `report_id`

- [ ] Update `tests/test_report_delivery.py`.

Replace the current P0 rejection test for `report_id` with tests for the approved input rules.

Add direct service validation tests for inline markdown only:

```python
def test_deliver_report_rejects_missing_report_content(service):
    with pytest.raises(ReportDeliveryError) as exc:
        service._validate_report({})

    assert exc.value.code == "missing_report"
```

If `ReportDeliveryError` currently does not expose `code`, add that in this task.

- [ ] Update `tests/test_handlers.py`.

In `TestDeliverReportHandler`, add tests for:

1. Delivering a stored report by `report_id`:

```python
async def test_deliver_report_resolves_stored_report_id(tmp_path, monkeypatch):
    settings = Settings()
    settings.report_delivery.artifact_dir = tmp_path / "reports"
    handlers = LoganHandlers(settings=settings)
    generated = _json(await handlers.handle_tool_call("generate_incident_report", {...}))

    delivered_reports = []

    async def fake_deliver(**kwargs):
        delivered_reports.append(kwargs["report"])
        return {"status": "sent", "channel": kwargs["channel"], "delivery_id": "test"}

    monkeypatch.setattr(handlers.report_delivery_service, "deliver", fake_deliver)

    response = await handlers.handle_tool_call(
        "deliver_report",
        {
            "report": {"report_id": generated["report_id"]},
            "channel": "email",
            "recipients": ["demo@example.com"],
        },
    )

    payload = _json(response)
    assert payload["status"] == "sent"
    assert delivered_reports[0]["markdown"].startswith("#")
    assert delivered_reports[0]["report_id"] == generated["report_id"]
```

2. Rejecting both inline markdown and `report_id`:

```python
response = await handlers.handle_tool_call(
    "deliver_report",
    {
        "report": {"report_id": report_id, "markdown": "# stale"},
        "channel": "email",
        "recipients": ["demo@example.com"],
    },
)
payload = _json(response)
assert payload["error"] == "conflicting_report_inputs"
```

3. Rejecting neither inline markdown nor `report_id`:

```python
payload = _json(await handlers.handle_tool_call("deliver_report", {"report": {}, ...}))
assert payload["error"] == "missing_report"
```

4. Rejecting invalid or missing `report_id` with `invalid_report_id` / `report_not_found`.

- [ ] Update `src/oci_logan_mcp/report_delivery.py`.

If the existing `ReportDeliveryError` is plain, make it carry a stable code:

```python
class ReportDeliveryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
```

Update raise sites from:

```python
raise ReportDeliveryError("report.markdown is required")
```

to:

```python
raise ReportDeliveryError("missing_report", "report.markdown or report.report_id is required")
```

Keep `ReportDeliveryService` focused on transport delivery. It should still validate that the final report passed to it contains markdown. `report_id` resolution belongs in the handler so delivery transport does not need to know the store.

- [ ] Update `src/oci_logan_mcp/handlers.py`.

Add a helper to resolve delivery input before calling the service:

```python
def _resolve_report_for_delivery(self, report: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    has_markdown = bool(str(report.get("markdown") or "").strip())
    has_report_id = bool(str(report.get("report_id") or "").strip())

    if has_markdown and has_report_id:
        return None, "conflicting_report_inputs", "Use either report.markdown or report.report_id, not both."
    if not has_markdown and not has_report_id:
        return None, "missing_report", "report.markdown or report.report_id is required."
    if has_markdown:
        return dict(report), None, None

    report_id = str(report.get("report_id"))
    try:
        stored = self.report_store.get(report_id)
    except InvalidReportIdError as exc:
        return None, "invalid_report_id", str(exc)
    except ReportNotFoundError as exc:
        return None, "report_not_found", str(exc)
    except ReportStoreError as exc:
        return None, "report_store_error", str(exc)

    resolved = dict(report)
    resolved["markdown"] = stored["markdown"]
    resolved["metadata"] = stored["metadata"]
    resolved["markdown_path"] = stored["markdown_path"]
    resolved["html_path"] = stored["html_path"]
    resolved["metadata_path"] = stored["metadata_path"]
    return resolved, None, None
```

In `_deliver_report`:

```python
report, error_code, message = self._resolve_report_for_delivery(args.get("report") or {})
if error_code:
    return self._error(error_code, message)
```

Then call `self.report_delivery_service.deliver(report=report, ...)`.

- [ ] Update `src/oci_logan_mcp/tools.py`.

Change `deliver_report` schema:

- `report.markdown` optional.
- Add `report.report_id` optional.
- Description says exactly one of `markdown` or `report_id` should be supplied.
- Remove text saying report_id lookup is deferred.

Expected schema fragment:

```python
"report": {
    "type": "object",
    "description": "Report content to deliver. Provide exactly one of markdown or report_id.",
    "properties": {
        "markdown": {"type": "string", "description": "Inline markdown report content."},
        "report_id": {"type": "string", "description": "Stored report id returned by generate_incident_report."},
        "metadata": {"type": "object"},
    },
}
```

- [ ] Run focused delivery tests.

Command:

```bash
PYTHONPATH=src python3 -m pytest tests/test_report_delivery.py tests/test_handlers.py::TestDeliverReportHandler tests/test_tools.py -q
```

Expected output: all tests pass.

---

## Task 7: Harden Destructive-Action 2FA Classification

- [ ] Update `tests/test_confirmation.py`.

Add or update tests proving `delete_playbook` is guarded.

Expected assertion:

```python
from oci_logan_mcp.confirmation import GUARDED_TOOLS


def test_delete_playbook_requires_confirmation():
    assert "delete_playbook" in GUARDED_TOOLS
```

Keep the existing `setup_confirmation_secret` overwrite test. If it currently lives in handler tests, leave it there and add a second drift-oriented test near confirmation tests that calls the handler twice:

```python
async def test_setup_confirmation_secret_refuses_overwrite(tmp_path):
    handlers = LoganHandlers(settings=_settings_with_secret_path(tmp_path))
    first = _json(await handlers.handle_tool_call("setup_confirmation_secret", {"secret": "123456"}))
    second = _json(await handlers.handle_tool_call("setup_confirmation_secret", {"secret": "654321"}))

    assert first["status"] == "configured"
    assert second["status"] == "already_configured"
```

Do not call `SecretStore.set_secret()` directly for this regression; the safety contract is the handler-level tool behavior.

- [ ] Update `tests/test_read_only_guard.py`.

Add drift tests for mutation classification. Import `MUTATING_TOOLS`, `GUARDED_TOOLS`, and the new exemption constant.

Expected tests:

```python
from oci_logan_mcp.confirmation import GUARDED_TOOLS, NON_DESTRUCTIVE_MUTATION_EXEMPTIONS
from oci_logan_mcp.read_only_guard import MUTATING_TOOLS
from oci_logan_mcp.tools import get_tools


def test_every_mutating_tool_is_guarded_or_named_exempt():
    unclassified = MUTATING_TOOLS - GUARDED_TOOLS - set(NON_DESTRUCTIVE_MUTATION_EXEMPTIONS)

    assert unclassified == set()


def test_every_registered_delete_tool_is_classified_mutating():
    registered_delete_tools = {tool["name"] for tool in get_tools() if tool["name"].startswith("delete_")}

    assert registered_delete_tools <= MUTATING_TOOLS


def test_every_registered_delete_tool_is_guarded_unless_explicitly_exempt():
    registered_delete_tools = {tool["name"] for tool in get_tools() if tool["name"].startswith("delete_")}
    unguarded = registered_delete_tools - GUARDED_TOOLS - set(NON_DESTRUCTIVE_MUTATION_EXEMPTIONS)

    assert unguarded == set()
```

Optional extra symmetric check for `update_*` tools:

```python
def test_every_registered_update_tool_is_classified_mutating_or_exempt():
    registered_update_tools = {tool["name"] for tool in get_tools() if tool["name"].startswith("update_")}

    assert registered_update_tools <= MUTATING_TOOLS | set(NON_DESTRUCTIVE_MUTATION_EXEMPTIONS)
```

Include the optional check if it matches current tool names without creating noise. `update_tenancy_context` should be exempt with a named reason.

- [ ] Update `src/oci_logan_mcp/confirmation.py`.

Add `delete_playbook` to `GUARDED_TOOLS`.

Add a named exemption dictionary close to `GUARDED_TOOLS`:

```python
NON_DESTRUCTIVE_MUTATION_EXEMPTIONS: dict[str, str] = {
    "save_learned_query": "Additive learned-query state; overwrite paths require explicit force/rename behavior.",
    "remember_preference": "Additive preference signal with no deletion of managed resources.",
    "record_investigation": "Creates a fresh pb_<uuid> playbook record through PlaybookRecorder.",
    "setup_confirmation_secret": "Bootstraps confirmation secret and refuses overwrite through the tool handler.",
    "set_compartment": "Updates current session context only.",
    "set_namespace": "Updates current session context only.",
    "update_tenancy_context": "Updates local tenancy metadata; no deletion of managed resources.",
    "deliver_report": "Outbound delivery is explicitly requested and does not mutate OCI/local persisted state destructively.",
    "send_to_slack": "Outbound notification only.",
    "send_to_telegram": "Outbound notification only.",
}
```

Adjust names to match the exact current `MUTATING_TOOLS` set. Do not add tools that are not in `MUTATING_TOOLS`.

- [ ] Update summary metadata for confirmation prompts if the module has a summary/action map.

If `confirmation.py` has a guarded-tool summary dictionary, add an entry for `delete_playbook`:

```python
"delete_playbook": "Delete local investigation playbook",
```

- [ ] Run focused safety tests.

Command:

```bash
PYTHONPATH=src python3 -m pytest tests/test_confirmation.py tests/test_read_only_guard.py -q
```

Expected output: all tests pass.

---

## Task 8: Update Public Docs and Demo Guidance

- [ ] Update `README.md`.

Remove P0 wording that says report-id lookup is deferred. Add a short section under report workflow:

```markdown
### Stored incident reports

`generate_incident_report` stores each report under the configured report artifact directory and returns a `report_id` plus local artifact paths.

Use:

- `get_incident_report(report_id="rpt_...")` to read or download one report manually.
- `list_incident_reports(limit=20)` to find recent stored reports.
- `deliver_report(report={"report_id": "rpt_..."}, channel="email", recipients=[...])` only after the user explicitly asks to deliver the report.

Delivery accepts either inline `report.markdown` or `report.report_id`, but not both.
```

- [ ] Update `docs/phase-2/backlog.md` if it still lists stored report retrieval or report-id delivery as pending.

Move the relevant item to completed or annotate it as implemented by this change. Do not change unrelated roadmap items.

- [ ] Update `docs/phase-2/specs/reports-and-playbooks.md` if it describes report persistence as future-only.

Keep the wording concise and behavior-oriented:

- Reports persist under `report_delivery.artifact_dir / "store"`.
- Delivery PDFs remain under `report_delivery.artifact_dir`.
- Delivery remains opt-in and is not automatically chained by the server.

- [ ] Run docs whitespace check.

Command:

```bash
git diff --check
```

Expected output: no output.

---

## Task 9: Full Verification

- [ ] Run the focused test suite for changed behavior.

Command:

```bash
PYTHONPATH=src python3 -m pytest tests/test_report_store.py tests/test_report_generator.py tests/test_report_delivery.py tests/test_tools.py tests/test_handlers.py::TestIncidentReports tests/test_handlers.py::TestDeliverReportHandler tests/test_confirmation.py tests/test_read_only_guard.py -q
```

Expected output: all selected tests pass.

- [ ] Run the full suite.

Command:

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected output: all tests pass. If the full suite has known environment-gated tests, record the exact skipped or failed tests and verify the focused suite passes.

- [ ] Run static diff check.

Command:

```bash
git diff --check
```

Expected output: no output.

- [ ] Inspect changed files.

Command:

```bash
git status --short
```

Expected output includes only intended files:

```text
 M README.md
 M docs/phase-2/backlog.md
 M docs/phase-2/specs/reports-and-playbooks.md
 M src/oci_logan_mcp/confirmation.py
 M src/oci_logan_mcp/handlers.py
 M src/oci_logan_mcp/report_delivery.py
 M src/oci_logan_mcp/report_generator.py
 M src/oci_logan_mcp/tools.py
 M tests/test_confirmation.py
 M tests/test_handlers.py
 M tests/test_read_only_guard.py
 M tests/test_report_delivery.py
 M tests/test_report_generator.py
 M tests/test_tools.py
?? src/oci_logan_mcp/report_store.py
?? tests/test_report_store.py
```

The exact docs changed may differ after inspecting current wording, but no unrelated files should be edited.

---

## Manual Smoke Demo After Implementation

Use these prompts through the MCP client after deploying to the VM:

1. Generate the report:

```text
Investigate failures and issues over the last day. Focus on the top two anomalous sources and generate an incident report titled "24-hour failures and issues report".
```

Expected server behavior:

- Assistant calls `investigate_incident`.
- Assistant calls `generate_incident_report` with `title`.
- Response includes `report_id`, markdown summary, and local `markdown_path` / `metadata_path`.
- Assistant asks whether the user wants it delivered. It does not call `deliver_report` unless the user says yes.

2. Read the stored report manually:

```text
Read report rpt_<id from previous step>.
```

Expected server behavior:

- Assistant calls `get_incident_report(report_id="rpt_<id>")`.
- Response includes the markdown and local paths.

3. List recent reports:

```text
Show recent incident reports.
```

Expected server behavior:

- Assistant calls `list_incident_reports(limit=20)`.
- Response shows report IDs, titles, generated times, paths, and `warnings.corrupt_count`.

4. Deliver only after opt-in:

```text
Yes, deliver that report to demo@example.com.
```

Expected server behavior:

- Assistant calls `deliver_report(report={"report_id": "rpt_<id>"}, channel="email", recipients=["demo@example.com"])`.
- Server resolves the stored markdown and sends via the configured ONS email path.

5. Confirm destructive guard:

```text
Delete playbook pb_<id>.
```

Expected server behavior:

- Server requires the confirmation secret before deletion proceeds.

---

## Completion Criteria

The implementation is complete when:

- `generate_incident_report` returns durable artifact paths for every successful report.
- `get_incident_report` reads a stored report by ID.
- `list_incident_reports` lists recent reports and reports corrupt-entry warnings.
- `deliver_report` works with stored `report_id`, rejects ambiguous inline-plus-ID input, and keeps inline markdown delivery working.
- Delivery is still triggered only by an explicit `deliver_report` tool call.
- `delete_playbook` requires 2FA.
- Mutation drift tests enforce destructive-vs-non-destructive classification.
- Focused and full pytest suites pass, or any environment-gated full-suite exceptions are documented with focused tests passing.
