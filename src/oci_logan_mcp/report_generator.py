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
            sentences.append(
                f"Top anomalous source: {top.get('source', 'unknown')}{pct_text}."
            )

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
            lines.append(
                f"- `{ts or 'unknown time'}` **{source or 'unknown source'}** - "
                f"{message or row}"
            )
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
                lines.append(
                    f"  - Cluster: {sample or cluster} "
                    f"({count or 'unknown'} events)"
                )
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
                    f"- Ingestion {status}: "
                    f"{finding.get('source', 'unknown source')} - "
                    f"{finding.get('message', 'no message')}"
                )
        return lines

    def _evidence(self, investigation: Dict[str, Any]) -> List[str]:
        seed = investigation.get("seed") or {}
        budget = investigation.get("budget") or {}
        return [
            f"- Seed query: `{seed.get('query', 'unknown')}`",
            f"- Time range: `{seed.get('time_range', 'unknown')}`",
            f"- Seed filter degraded: `{bool(seed.get('seed_filter_degraded', False))}`",
            f"- Elapsed seconds: `{investigation.get('elapsed_seconds', 'unknown')}`",
            f"- Budget snapshot: `{budget}`",
        ]

    def _next_steps(self, investigation: Dict[str, Any]) -> List[str]:
        steps = investigation.get("next_steps") or []
        if not steps:
            return ["No next-step suggestions were produced."]
        lines = []
        for step in steps[:10]:
            tool = step.get("tool_name", "unknown_tool")
            reason = step.get("reason", "No reason provided.")
            args = step.get("suggested_args", {})
            lines.append(f"- `{tool}` - {reason} Suggested args: `{args}`")
        return lines

    def _appendix(self, investigation: Dict[str, Any]) -> List[str]:
        keys = ", ".join(sorted(investigation.keys())) if investigation else "none"
        return [
            "- Source type: `investigation`",
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
