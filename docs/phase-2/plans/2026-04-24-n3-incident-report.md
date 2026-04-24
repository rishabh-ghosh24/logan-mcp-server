# N3 Incident Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `generate_incident_report`, a deterministic N3 P0 tool that turns an A1 `InvestigationReport` dict into a reproducible Markdown report and optional HTML rendering.

**Architecture:** Keep P0 template-first and side-effect-free. `ReportGenerator` is a pure renderer that validates a small option surface, composes fixed sections from A1 output, computes report metadata, and returns `{report_id, markdown, html, metadata, artifacts}`. MCP wiring is thin: schema validation in the handler, no OCI calls, no internal LLM provider, no persistence.

**Tech Stack:** Python standard library only (`datetime`, `html`, `re`, `uuid`), existing MCP handler/tool schema patterns, pytest.

---

## Scope Decisions

### P0 Included

- `generate_incident_report(investigation, format="markdown", include_sections=None, summary_length="standard")`
- Markdown output for every call.
- HTML output only when `format="html"`.
- Deterministic prose assembled from A1 fields: `summary`, `seed`, `ingestion_health`, `parser_failures`, `anomalous_sources`, `cross_source_timeline`, `next_steps`, `budget`, `partial`, `partial_reasons`, `elapsed_seconds`.
- Section filtering via stable section ids:
  - `executive_summary`
  - `timeline`
  - `top_findings`
  - `evidence`
  - `recommended_next_steps`
  - `appendix`
- `summary_length` controls the executive summary sentence cap:
  - `short`: 3 sentences
  - `standard`: 5 sentences
  - `detailed`: 8 sentences

### P1 Deferred

- Internal LLM prose synthesis. The MCP client is already an LLM, and P0 should not add provider auth/config/budget surface area. Add this to `docs/phase-2/backlog.md` as `N3-F1`.
- `source.playbook_run`, because N1 P0 records/catalogs only and does not replay. Add this to backlog as `N3-F2`.
- `source.session_id`, because N6 only has process-scoped session ids. Add this to backlog as `N3-F3`.
- PDF/email/Telegram delivery; this belongs to the separate Report Delivery lane.

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/oci_logan_mcp/report_generator.py` | Create | Pure deterministic report renderer |
| `tests/test_report_generator.py` | Create | Unit tests for sections, options, empty inputs, HTML, and metadata |
| `src/oci_logan_mcp/tools.py` | Modify | Register `generate_incident_report` schema |
| `src/oci_logan_mcp/handlers.py` | Modify | Instantiate `ReportGenerator`, route the MCP tool, and return structured validation errors |
| `tests/test_tools.py` | Modify | Schema coverage for the new tool |
| `tests/test_handlers.py` | Modify | Handler routing and validation tests |
| `tests/test_read_only_guard.py` | Modify | Classify `generate_incident_report` as a reader |
| `README.md` | Modify | Document N3 usage and P0 deterministic scope |
| `docs/phase-2/specs/reports-and-playbooks.md` | Modify | Replace P0 internal-LLM language with template-first behavior |
| `docs/phase-2/backlog.md` | Modify | Track N3 P1 deferrals |

## Data Contract

`ReportGenerator.generate()` returns:

```python
{
    "report_id": "rpt_<uuidhex>",
    "markdown": "# Incident Report\n\n## Executive Summary\n\nNo findings were reported by the investigation.\n",
    "html": None,  # or a complete HTML document when format="html"
    "metadata": {
        "generated_at": "2026-04-24T12:00:00+00:00",
        "source_type": "investigation",
        "summary_length": "standard",
        "included_sections": [
            "executive_summary",
            "timeline",
            "top_findings",
            "evidence",
            "recommended_next_steps",
            "appendix",
        ],
        "word_count": 123,
    },
    "artifacts": [],
}
```

## Task 1: Add Pure Report Generator

**Files:**
- Create: `src/oci_logan_mcp/report_generator.py`
- Create: `tests/test_report_generator.py`

- [ ] **Step 1: Write the failing generator tests**

Create `tests/test_report_generator.py`:

```python
"""Tests for deterministic N3 incident report generation."""

import re

import pytest

from oci_logan_mcp.report_generator import (
    ReportGenerator,
    ReportGenerationError,
)


def _investigation():
    return {
        "summary": "Apache errors spiked and parser failures were present.",
        "seed": {
            "query": "'Event' = 'error'",
            "time_range": "last_1_hour",
            "seed_filter": "'Event' = 'error'",
            "seed_filter_degraded": False,
        },
        "ingestion_health": {
            "summary": {"healthy": 2, "stopped": 1, "unknown": 0},
            "findings": [
                {"source": "Apache Access", "status": "stopped", "message": "No recent logs"}
            ],
        },
        "parser_failures": {
            "total_failure_count": 7,
            "failures": [
                {"source": "Apache Access", "failure_count": 7}
            ],
        },
        "anomalous_sources": [
            {
                "source": "Apache Access",
                "pct_change": 250.0,
                "current_count": 35,
                "comparison_count": 10,
                "top_error_clusters": [
                    {"Cluster Sample": "HTTP 500 from checkout", "Count": 12}
                ],
                "top_entities": [
                    {"field": "host", "value": "web-1", "count": 9}
                ],
                "timeline": [
                    {"Time": "2026-04-24T10:00:00Z", "Message": "HTTP 500"}
                ],
                "errors": [],
            }
        ],
        "cross_source_timeline": [
            {
                "timestamp": "2026-04-24T10:00:00Z",
                "source": "Apache Access",
                "message": "HTTP 500",
            }
        ],
        "next_steps": [
            {
                "tool_name": "trace_request_id",
                "reason": "A request id was present.",
                "suggested_args": {"request_id": "abc"},
            }
        ],
        "budget": {"queries_used": 4},
        "partial": True,
        "partial_reasons": ["timeline_omitted"],
        "elapsed_seconds": 3.2,
    }


def test_generate_default_markdown_sections():
    report = ReportGenerator().generate(_investigation())

    assert re.match(r"^rpt_[0-9a-f]{32}$", report["report_id"])
    assert report["html"] is None
    markdown = report["markdown"]
    assert markdown.startswith("# Incident Report")
    assert "## Executive Summary" in markdown
    assert "Apache errors spiked" in markdown
    assert "Partial investigation: timeline_omitted" in markdown
    assert "## Timeline" in markdown
    assert "HTTP 500" in markdown
    assert "## Top Findings" in markdown
    assert "Apache Access" in markdown
    assert "## Recommended Next Steps" in markdown
    assert "trace_request_id" in markdown
    assert report["metadata"]["source_type"] == "investigation"
    assert report["metadata"]["included_sections"] == [
        "executive_summary",
        "timeline",
        "top_findings",
        "evidence",
        "recommended_next_steps",
        "appendix",
    ]
    assert report["metadata"]["word_count"] > 0
    assert report["artifacts"] == []


def test_include_sections_filters_output():
    report = ReportGenerator().generate(
        _investigation(),
        include_sections=["executive_summary", "evidence"],
    )

    assert "## Executive Summary" in report["markdown"]
    assert "## Evidence" in report["markdown"]
    assert "## Timeline" not in report["markdown"]
    assert report["metadata"]["included_sections"] == ["executive_summary", "evidence"]


def test_html_format_returns_escaped_html_document():
    investigation = _investigation()
    investigation["summary"] = "Observed <critical> failures."

    report = ReportGenerator().generate(investigation, output_format="html")

    assert report["html"].startswith("<!doctype html>")
    assert "<h1>Incident Report</h1>" in report["html"]
    assert "&lt;critical&gt;" in report["html"]
    assert "<critical>" not in report["html"]


def test_empty_investigation_produces_no_findings_report():
    report = ReportGenerator().generate({})

    assert "No findings were reported by the investigation." in report["markdown"]
    assert "No cross-source timeline events were included." in report["markdown"]
    assert "No anomalous sources were included." in report["markdown"]


def test_short_summary_caps_sentences():
    investigation = _investigation()
    investigation["summary"] = "One. Two. Three. Four."

    report = ReportGenerator().generate(investigation, summary_length="short")
    summary = report["markdown"].split("## Timeline", 1)[0]

    assert "One." in summary
    assert "Two." in summary
    assert "Three." in summary
    assert "Four." not in summary


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"output_format": "pdf"}, "format must be one of"),
        ({"summary_length": "tiny"}, "summary_length must be one of"),
        ({"include_sections": ["missing"]}, "unknown section"),
    ],
)
def test_invalid_options_raise_structured_error(kwargs, message):
    with pytest.raises(ReportGenerationError) as exc:
        ReportGenerator().generate(_investigation(), **kwargs)

    assert message in str(exc.value)
```

- [ ] **Step 2: Run generator tests and verify failure**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_generator.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'oci_logan_mcp.report_generator'`.

- [ ] **Step 3: Implement `report_generator.py`**

Create `src/oci_logan_mcp/report_generator.py` with:

```python
"""Deterministic N3 incident report generation from A1 output."""

from __future__ import annotations

import html
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


SECTION_ORDER = [
    "executive_summary",
    "timeline",
    "top_findings",
    "evidence",
    "recommended_next_steps",
    "appendix",
]

SECTION_TITLES = {
    "executive_summary": "Executive Summary",
    "timeline": "Timeline",
    "top_findings": "Top Findings",
    "evidence": "Evidence",
    "recommended_next_steps": "Recommended Next Steps",
    "appendix": "Appendix",
}

SUMMARY_SENTENCE_LIMITS = {"short": 3, "standard": 5, "detailed": 8}
FORMATS = {"markdown", "html"}


class ReportGenerationError(ValueError):
    """Raised when report generation options are invalid."""


class ReportGenerator:
    """Pure renderer for deterministic incident reports."""

    def generate(
        self,
        investigation: Dict[str, Any],
        output_format: str = "markdown",
        include_sections: Optional[List[str]] = None,
        summary_length: str = "standard",
    ) -> Dict[str, Any]:
        if output_format not in FORMATS:
            raise ReportGenerationError("format must be one of: html, markdown")
        if summary_length not in SUMMARY_SENTENCE_LIMITS:
            raise ReportGenerationError(
                "summary_length must be one of: detailed, short, standard"
            )

        sections = self._resolve_sections(include_sections)
        report_id = f"rpt_{uuid.uuid4().hex}"
        generated_at = datetime.now(timezone.utc).isoformat()

        parts = ["# Incident Report", ""]
        for section_id in sections:
            parts.append(f"## {SECTION_TITLES[section_id]}")
            parts.append("")
            parts.extend(self._render_section(section_id, investigation, summary_length))
            parts.append("")
        markdown = "\n".join(parts).strip() + "\n"

        html_output = None
        if output_format == "html":
            html_output = self._render_html(markdown)

        return {
            "report_id": report_id,
            "markdown": markdown,
            "html": html_output,
            "metadata": {
                "generated_at": generated_at,
                "source_type": "investigation",
                "summary_length": summary_length,
                "included_sections": sections,
                "word_count": len(re.findall(r"\b\w+\b", markdown)),
            },
            "artifacts": [],
        }

    def _resolve_sections(self, include_sections: Optional[List[str]]) -> List[str]:
        if include_sections is None:
            return list(SECTION_ORDER)
        unknown = [s for s in include_sections if s not in SECTION_TITLES]
        if unknown:
            raise ReportGenerationError(f"unknown section: {unknown[0]}")
        return [s for s in SECTION_ORDER if s in include_sections]

    def _render_section(
        self,
        section_id: str,
        investigation: Dict[str, Any],
        summary_length: str,
    ) -> List[str]:
        if section_id == "executive_summary":
            return self._executive_summary(investigation, summary_length)
        if section_id == "timeline":
            return self._timeline(investigation)
        if section_id == "top_findings":
            return self._top_findings(investigation)
        if section_id == "evidence":
            return self._evidence(investigation)
        if section_id == "recommended_next_steps":
            return self._next_steps(investigation)
        return self._appendix(investigation)

    def _executive_summary(
        self,
        investigation: Dict[str, Any],
        summary_length: str,
    ) -> List[str]:
        sentences = []
        summary = str(investigation.get("summary") or "").strip()
        if summary:
            sentences.extend(_split_sentences(summary))
        else:
            sentences.append("No findings were reported by the investigation.")

        if investigation.get("partial"):
            reasons = ", ".join(investigation.get("partial_reasons") or ["unknown"])
            sentences.append(f"Partial investigation: {reasons}.")

        anomalous = investigation.get("anomalous_sources") or []
        if anomalous:
            top = anomalous[0]
            pct = top.get("pct_change")
            pct_text = f" ({pct:+.1f}%)" if isinstance(pct, (int, float)) else ""
            sentences.append(f"Top anomalous source: {top.get('source', 'unknown')}{pct_text}.")

        failures = investigation.get("parser_failures") or {}
        failure_count = failures.get("total_failure_count")
        if failure_count:
            sentences.append(f"Parser failures reported: {failure_count}.")

        limit = SUMMARY_SENTENCE_LIMITS[summary_length]
        return [sentence for sentence in sentences[:limit]]

    def _timeline(self, investigation: Dict[str, Any]) -> List[str]:
        timeline = investigation.get("cross_source_timeline") or []
        if not timeline:
            return ["No cross-source timeline events were included."]
        lines = []
        for row in timeline[:10]:
            ts = _first_present(row, ["timestamp", "Time", "Datetime", "datetime"])
            source = _first_present(row, ["source", "Log Source", "log_source"])
            message = _first_present(row, ["message", "Message", "Event", "Summary"])
            lines.append(f"- `{ts or 'unknown time'}` **{source or 'unknown source'}** — {message or row}")
        return lines

    def _top_findings(self, investigation: Dict[str, Any]) -> List[str]:
        lines = []
        anomalous = investigation.get("anomalous_sources") or []
        if not anomalous:
            lines.append("No anomalous sources were included.")
        for source in anomalous[:5]:
            pct = source.get("pct_change")
            pct_text = f" ({pct:+.1f}%)" if isinstance(pct, (int, float)) else ""
            lines.append(f"- **{source.get('source', 'unknown source')}**{pct_text}")
            for cluster in (source.get("top_error_clusters") or [])[:2]:
                sample = _first_present(cluster, ["Cluster Sample", "sample", "message"])
                count = _first_present(cluster, ["Count", "count"])
                lines.append(f"  - Cluster: {sample or cluster} ({count or 'unknown'} events)")
            for entity in (source.get("top_entities") or [])[:2]:
                field = entity.get("field") or entity.get("name") or "entity"
                value = entity.get("value") or entity.get("entity") or "unknown"
                count = entity.get("count", "unknown")
                lines.append(f"  - Entity: {field}={value} ({count})")
            for error in (source.get("errors") or [])[:2]:
                lines.append(f"  - Error: {error}")

        failures = (investigation.get("parser_failures") or {}).get("failures") or []
        for failure in failures[:3]:
            lines.append(
                f"- Parser failures: {failure.get('source', 'unknown source')} "
                f"({failure.get('failure_count', 'unknown')})"
            )

        health = (investigation.get("ingestion_health") or {}).get("findings") or []
        for finding in health[:3]:
            status = finding.get("status", "unknown")
            if status != "healthy":
                lines.append(
                    f"- Ingestion {status}: {finding.get('source', 'unknown source')} — "
                    f"{finding.get('message', 'no message')}"
                )
        return lines

    def _evidence(self, investigation: Dict[str, Any]) -> List[str]:
        seed = investigation.get("seed") or {}
        budget = investigation.get("budget") or {}
        lines = [
            f"- Seed query: `{seed.get('query', 'unknown')}`",
            f"- Time range: `{seed.get('time_range', 'unknown')}`",
            f"- Seed filter degraded: `{bool(seed.get('seed_filter_degraded', False))}`",
            f"- Elapsed seconds: `{investigation.get('elapsed_seconds', 'unknown')}`",
            f"- Budget snapshot: `{budget}`",
        ]
        return lines

    def _next_steps(self, investigation: Dict[str, Any]) -> List[str]:
        steps = investigation.get("next_steps") or []
        if not steps:
            return ["No next-step suggestions were produced."]
        lines = []
        for step in steps[:10]:
            tool = step.get("tool_name", "unknown_tool")
            reason = step.get("reason", "No reason provided.")
            args = step.get("suggested_args", {})
            lines.append(f"- `{tool}` — {reason} Suggested args: `{args}`")
        return lines

    def _appendix(self, investigation: Dict[str, Any]) -> List[str]:
        keys = ", ".join(sorted(investigation.keys())) if investigation else "none"
        return [
            f"- Source type: `investigation`",
            f"- Partial: `{bool(investigation.get('partial', False))}`",
            f"- Partial reasons: `{investigation.get('partial_reasons') or []}`",
            f"- Investigation keys: `{keys}`",
            "- Transcript export is available separately through `export_transcript`.",
        ]

    def _render_html(self, markdown: str) -> str:
        lines = markdown.splitlines()
        html_lines = [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            "<title>Incident Report</title>",
            "</head>",
            "<body>",
        ]
        in_list = False
        for line in lines:
            if not line:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                continue
            if line.startswith("# "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<h1>{html.escape(line[2:])}</h1>")
            elif line.startswith("## "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<h2>{html.escape(line[3:])}</h2>")
            elif line.startswith("- "):
                if not in_list:
                    html_lines.append("<ul>")
                    in_list = True
                html_lines.append(f"<li>{html.escape(line[2:])}</li>")
            else:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<p>{html.escape(line)}</p>")
        if in_list:
            html_lines.append("</ul>")
        html_lines.extend(["</body>", "</html>"])
        return "\n".join(html_lines)


def _split_sentences(text: str) -> List[str]:
    pieces = re.findall(r"[^.!?]+[.!?]?", text)
    return [piece.strip() for piece in pieces if piece.strip()]


def _first_present(row: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None
```

- [ ] **Step 4: Run generator tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_generator.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit generator**

```bash
git add src/oci_logan_mcp/report_generator.py tests/test_report_generator.py
git commit -m "feat(n3): add deterministic incident report generator"
```

## Task 2: Register MCP Tool and Handler

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_read_only_guard.py`

- [ ] **Step 1: Write failing schema and handler tests**

Add to `tests/test_tools.py`:

```python
def test_generate_incident_report_schema():
    tools = {t["name"]: t for t in get_tools()}
    spec = tools["generate_incident_report"]
    schema = spec["inputSchema"]
    props = schema["properties"]

    assert schema["required"] == ["investigation"]
    assert props["format"]["enum"] == ["markdown", "html"]
    assert props["summary_length"]["enum"] == ["short", "standard", "detailed"]
    assert "include_sections" in props
```

Add to `tests/test_handlers.py`:

```python
class TestIncidentReports:
    @pytest.mark.asyncio
    async def test_generate_incident_report_routes_to_generator(self, handlers):
        handlers.report_generator.generate = MagicMock(
            return_value={
                "report_id": "rpt_1",
                "markdown": "# Incident Report\n",
                "html": None,
                "metadata": {"source_type": "investigation"},
                "artifacts": [],
            }
        )

        result = await handlers.handle_tool_call(
            "generate_incident_report",
            {
                "investigation": {"summary": "x"},
                "format": "markdown",
                "include_sections": ["executive_summary"],
                "summary_length": "short",
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["report_id"] == "rpt_1"
        handlers.report_generator.generate.assert_called_once_with(
            investigation={"summary": "x"},
            output_format="markdown",
            include_sections=["executive_summary"],
            summary_length="short",
        )

    @pytest.mark.asyncio
    async def test_generate_incident_report_requires_investigation_dict(self, handlers):
        result = await handlers.handle_tool_call("generate_incident_report", {})

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_investigation"

    @pytest.mark.asyncio
    async def test_generate_incident_report_returns_validation_error(self, handlers):
        result = await handlers.handle_tool_call(
            "generate_incident_report",
            {"investigation": {}, "format": "pdf"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_report_options"
        assert "format must be one of" in payload["error"]
```

In `tests/test_read_only_guard.py`, add `"generate_incident_report"` to the `KNOWN_READERS` set in `test_all_registered_tools_are_classified`.

Run:

```bash
PYTHONPATH=src pytest tests/test_tools.py::test_generate_incident_report_schema tests/test_handlers.py::TestIncidentReports tests/test_read_only_guard.py -q
```

Expected: FAIL because the tool is not registered.

- [ ] **Step 2: Register tool schema**

In `src/oci_logan_mcp/tools.py`, add this tool definition near `investigate_incident`:

```python
        {
            "name": "generate_incident_report",
            "description": (
                "Generate a deterministic Markdown incident report, and optional "
                "HTML rendering, from an A1 investigate_incident response. P0 is "
                "template-first and does not call an internal LLM."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "investigation": {
                        "type": "object",
                        "description": "A1 InvestigationReport object returned by investigate_incident.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "html"],
                        "description": "Output renderer. Markdown is always returned; html adds an HTML rendering.",
                        "default": "markdown",
                    },
                    "include_sections": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "executive_summary",
                                "timeline",
                                "top_findings",
                                "evidence",
                                "recommended_next_steps",
                                "appendix",
                            ],
                        },
                        "description": "Optional ordered section allowlist. Defaults to all sections.",
                    },
                    "summary_length": {
                        "type": "string",
                        "enum": ["short", "standard", "detailed"],
                        "description": "Executive summary sentence cap. Default: standard.",
                        "default": "standard",
                    },
                },
                "required": ["investigation"],
            },
        },
```

- [ ] **Step 3: Wire handler**

In `src/oci_logan_mcp/handlers.py`, import:

```python
from .report_generator import ReportGenerationError, ReportGenerator
```

In `MCPHandlers.__init__`, add after `self.find_rare_events_tool = RareEventsTool(self.query_engine)`:

```python
        self.report_generator = ReportGenerator()
```

In the `handlers` dict inside `handle_tool_call`, add:

```python
            "generate_incident_report": self._generate_incident_report,
```

Add handler method near `_investigate_incident`:

```python
    async def _generate_incident_report(self, args: Dict) -> List[Dict]:
        investigation = args.get("investigation")
        if not isinstance(investigation, dict):
            return [{"type": "text", "text": json.dumps({
                "status": "error",
                "error_code": "missing_investigation",
                "error": "investigation is required and must be an object",
            }, indent=2)}]

        try:
            result = self.report_generator.generate(
                investigation=investigation,
                output_format=args.get("format", "markdown"),
                include_sections=args.get("include_sections"),
                summary_length=args.get("summary_length", "standard"),
            )
        except ReportGenerationError as e:
            return [{"type": "text", "text": json.dumps({
                "status": "error",
                "error_code": "invalid_report_options",
                "error": str(e),
            }, indent=2)}]
        return [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
```

- [ ] **Step 4: Run focused MCP tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_tools.py::test_generate_incident_report_schema tests/test_handlers.py::TestIncidentReports tests/test_read_only_guard.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit MCP wiring**

```bash
git add src/oci_logan_mcp/tools.py src/oci_logan_mcp/handlers.py tests/test_tools.py tests/test_handlers.py tests/test_read_only_guard.py
git commit -m "feat(n3): expose incident report MCP tool"
```

## Task 3: Update Docs and Backlog

**Files:**
- Modify: `README.md`
- Modify: `docs/phase-2/specs/reports-and-playbooks.md`
- Modify: `docs/phase-2/backlog.md`

- [ ] **Step 1: README update**

Add an `### generate_incident_report` section under the Investigation Toolkit with this content:

````markdown
### `generate_incident_report` — deterministic incident report

Convert an `investigate_incident` response into a deterministic Markdown incident report, with optional HTML rendering:

```json
{
  "tool": "generate_incident_report",
  "investigation": {
    "summary": "Apache errors spiked and parser failures were present.",
    "anomalous_sources": []
  },
  "format": "html",
  "summary_length": "standard"
}
```

Returns `{report_id, markdown, html, metadata, artifacts}`. Markdown is always returned. `html` is populated only when `format="html"`.

P0 behavior:
- Template-first and deterministic; no internal LLM provider is called.
- Source is an A1 `InvestigationReport` object only.
- Supported sections are `executive_summary`, `timeline`, `top_findings`, `evidence`, `recommended_next_steps`, and `appendix`.
- Playbook-run reports, session-id reports, PDF generation, and report delivery are separate follow-ups.
````

Also update the `What You Can Do` table to include `generate_incident_report` under the Investigation playbooks or triage/reporting capability row.

- [ ] **Step 2: Spec update**

In `docs/phase-2/specs/reports-and-playbooks.md`, update N3:

- Replace P0 internal LLM wording with deterministic template-first wording.
- Keep the existing `source.playbook_run` and `source.session_id` deferrals.
- Add internal LLM prose synthesis to P1.

Use this text in the N3 LLM usage section:

```markdown
### Prose generation
- P0 uses deterministic templates only. This keeps incident reports reproducible, auditable, and easy to test.
- The MCP client is already an LLM and can synthesize prose from the structured report when needed.
- Internal LLM prose synthesis is deferred to P1 because it requires provider config, auth, cost controls, prompt management, and failure-mode coverage.
```

- [ ] **Step 3: Backlog update**

Add an N3 section to `docs/phase-2/backlog.md`:

```markdown
#### N3 — Incident report generation
Source: [reports-and-playbooks.md](specs/reports-and-playbooks.md)

- `N3-F1` — Internal LLM prose synthesis for executive summaries and findings narratives, with provider config, prompt management, cost controls, and fallback behavior.
- `N3-F2` — `source.playbook_run` report generation once N1 replay exists.
- `N3-F3` — `source.session_id` report generation once N6 has true per-investigation session boundaries.
```

- [ ] **Step 4: Run docs-adjacent focused tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_generator.py tests/test_tools.py::test_generate_incident_report_schema tests/test_handlers.py::TestIncidentReports tests/test_read_only_guard.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit docs**

```bash
git add README.md docs/phase-2/specs/reports-and-playbooks.md docs/phase-2/backlog.md
git commit -m "docs(n3): document deterministic incident reports"
```

## Task 4: Final Verification

**Files:**
- All files changed in Tasks 1-3.

- [ ] **Step 1: Run focused N3 suite**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_generator.py tests/test_tools.py::test_generate_incident_report_schema tests/test_handlers.py::TestIncidentReports tests/test_read_only_guard.py -q
```

Expected: PASS.

- [ ] **Step 2: Run full suite**

Run:

```bash
PYTHONPATH=src pytest -q
```

Expected: PASS.

- [ ] **Step 3: Inspect git status and commit stack**

Run:

```bash
git status --short --branch
git log --oneline --decorate main..HEAD
```

Expected:
- No unstaged or staged changes except unrelated root-checkout files outside this worktree.
- Commits include:
  - `docs(phase-2): track n1 follow-ups`
  - `feat(n3): add deterministic incident report generator`
  - `feat(n3): expose incident report MCP tool`
  - `docs(n3): document deterministic incident reports`

## Self-Review

- Spec coverage: N3 P0 source type, output shape, markdown/html rendering, section filtering, summary length, deterministic behavior, schema/handler wiring, read-only classification, README, spec, and backlog are all covered.
- P1 deferrals: internal LLM prose synthesis, playbook-run reports, session-id reports, PDF/delivery are excluded from implementation and tracked in docs/backlog.
- Placeholder scan target: this plan avoids placeholder tokens and incomplete code blocks.
- Type consistency: handler uses `format` from the MCP payload and passes it as `output_format` to avoid shadowing Python's built-in `format`.
