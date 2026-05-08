"""Deterministic N3 incident report generation from A1 output."""

from __future__ import annotations

import html
import json
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
FORMATS = {"markdown", "html", "both"}


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
        title: str | None = None,
    ) -> Dict[str, Any]:
        if output_format not in FORMATS:
            raise ReportGenerationError("format must be one of: both, html, markdown")
        if summary_length not in SUMMARY_SENTENCE_LIMITS:
            raise ReportGenerationError(
                "summary_length must be one of: detailed, short, standard"
            )
        if title is not None and not isinstance(title, str):
            raise ReportGenerationError("title must be a string")

        sections = self._resolve_sections(include_sections)
        report_id = f"rpt_{uuid.uuid4().hex}"
        report_title = title.strip() if title and title.strip() else "Incident Report"
        generated_at = datetime.now(timezone.utc).isoformat()

        parts = [f"# {report_title}", ""]
        for section_id in sections:
            parts.append(f"## {SECTION_TITLES[section_id]}")
            parts.append("")
            parts.extend(self._render_section(section_id, investigation, summary_length))
            parts.append("")
        markdown = "\n".join(parts).strip() + "\n"

        html_output = None
        if output_format in {"html", "both"}:
            html_output = self._render_html(markdown, title=report_title)

        return {
            "report_id": report_id,
            "markdown": markdown,
            "html": html_output,
            "metadata": {
                "title": report_title,
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
        summary = _humanize_query_heavy_summary(investigation, summary)
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
            vcn_observations = _collect_vcn_flow_observations(top)
            if vcn_observations:
                action = _dominant_flow_action(vcn_observations)
                total = _total_observed_events(vcn_observations)
                total_text = (
                    f"{_format_event_count(total)} " if total is not None else ""
                )
                sentences.append(
                    "Assessment: high-volume "
                    f"{action} VCN traffic represents {total_text}blocked network "
                    "events, not automatically an application failure."
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
            ts = _first_present(row, ["timestamp", "time", "Time", "Datetime", "datetime"])
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
            vcn_observations = _collect_vcn_flow_observations(source)
            if vcn_observations:
                lines.extend(_render_vcn_flow_finding(vcn_observations))
            else:
                for cluster in (source.get("top_error_clusters") or [])[:2]:
                    sample = _first_present(
                        cluster,
                        ["Cluster Sample", "pattern", "sample", "message"],
                    )
                    count = _first_present(cluster, ["Count", "count"])
                    lines.append(
                        f"  - Cluster: {_clean_cluster_sample(sample or cluster)} "
                        f"({_format_event_count(count)} events)"
                    )
            for entity in (source.get("top_entities") or [])[:2]:
                field = (
                    entity.get("field")
                    or entity.get("name")
                    or entity.get("entity_type")
                    or "entity"
                )
                value = (
                    entity.get("value")
                    or entity.get("entity")
                    or entity.get("entity_value")
                    or "unknown"
                )
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
        operator_steps = _operator_next_steps(investigation)
        if not steps and not operator_steps:
            return ["No next-step suggestions were produced."]
        lines = list(operator_steps)
        for step in steps[:10]:
            if operator_steps and _is_low_value_row_limit_step(step):
                continue
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

    def _render_html(self, markdown: str, title: str = "Incident Report") -> str:
        lines = markdown.splitlines()
        html_lines = [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            f"<title>{html.escape(title)}</title>",
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


def _humanize_query_heavy_summary(investigation: Dict[str, Any], summary: str) -> str:
    seed = investigation.get("seed") or {}
    query = str(seed.get("query") or "")
    if not summary or not query:
        return summary
    if not summary.startswith("Investigated "):
        return summary
    if query not in summary and "'Log Source'" not in summary:
        return summary

    time_range = seed.get("time_range") or _extract_summary_time_range(summary) or "the selected window"
    sources = _extract_log_sources(query)
    activity = _describe_query_activity(query)
    if sources:
        source_word = "log source" if len(sources) == 1 else "log sources"
        return f"Investigated {activity} across {len(sources)} {source_word} over {time_range}."
    return f"Investigated {activity} over {time_range}."


def _extract_summary_time_range(summary: str) -> str | None:
    match = re.search(r"\bover\s+([A-Za-z0-9_ -]+?)(?:\.|$)", summary)
    if not match:
        return None
    return match.group(1).strip()


def _extract_log_sources(query: str) -> List[str]:
    match = re.search(r"'Log Source'\s+in\s*\((.*?)\)", query, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return [value.strip() for value in re.findall(r"'([^']+)'", match.group(1))]
    match = re.search(r"'Log Source'\s*=\s*'([^']+)'", query, flags=re.IGNORECASE)
    return [match.group(1).strip()] if match else []


def _describe_query_activity(query: str) -> str:
    lowered = query.lower()
    error_terms = (
        "error",
        "fail",
        "fatal",
        "critical",
        "exception",
        "timeout",
        "reject",
        "deny",
        "drop",
        "nxdomain",
        "servfail",
        "refused",
    )
    if any(term in lowered for term in error_terms):
        return "error-like activity"
    return "log activity"


def _format_event_count(count: Any) -> str:
    if count in (None, ""):
        return "unknown"
    try:
        return f"{int(count):,}"
    except (TypeError, ValueError):
        return str(count)


def _clean_cluster_sample(sample: Any, max_len: int = 120) -> str:
    text = _strip_cluster_markup(sample)

    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        flow_summary = _summarize_vcn_flow_cluster(obj)
        if flow_summary:
            return flow_summary
        metadata = obj.get("metadata")
        if isinstance(metadata, dict) and metadata.get("name"):
            return f"Kubernetes object metadata: {metadata['name']}"

    dns_summary = _summarize_coredns_cluster(text)
    if dns_summary:
        return dns_summary

    if len(text) > max_len:
        return text[:max_len].rstrip() + "..."
    return text


def _strip_cluster_markup(sample: Any) -> str:
    text = str(sample or "")
    text = re.sub(r"<#v[^>]*>", "", text)
    text = text.replace("</#v>", "")
    return " ".join(text.split())


def _summarize_vcn_flow_cluster(obj: Dict[str, Any]) -> str | None:
    data = obj.get("data")
    if not isinstance(data, dict):
        return None
    if not any(
        key in data
        for key in ("sourceAddress", "destinationAddress", "action", "protocolName")
    ):
        return None

    action = str(data.get("action") or "").strip().upper()
    verb = {"REJECT": "Rejected", "ACCEPT": "Accepted"}.get(
        action,
        action.title() if action else "Observed",
    )
    protocol = data.get("protocolName") or data.get("protocol")
    flow = f"{verb} {protocol} flow" if protocol else f"{verb} flow"
    src = _format_endpoint(data.get("sourceAddress"), data.get("sourcePort"))
    dst = _format_endpoint(data.get("destinationAddress"), data.get("destinationPort"))
    parts = [flow]
    if src and dst:
        parts.append(f"{src} -> {dst}")
    elif src:
        parts.append(f"from {src}")
    elif dst:
        parts.append(f"to {dst}")

    oracle = obj.get("oracle")
    resource_type = oracle.get("resourceType") if isinstance(oracle, dict) else None
    if resource_type:
        parts.append(f"resource={resource_type}")
    return " ".join(parts)


def _collect_vcn_flow_observations(source: Dict[str, Any]) -> List[Dict[str, Any]]:
    observations: List[Dict[str, Any]] = []
    for cluster in source.get("top_error_clusters") or []:
        sample = _first_present(cluster, ["Cluster Sample", "pattern", "sample", "message"])
        obj = _load_cluster_json(sample)
        if not isinstance(obj, dict):
            continue

        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        summary = _summarize_vcn_flow_cluster(obj)
        if not summary:
            continue

        oracle = obj.get("oracle")
        resource_type = oracle.get("resourceType") if isinstance(oracle, dict) else None
        observations.append(
            {
                "summary": summary,
                "count": _coerce_int(_first_present(cluster, ["Count", "count"])),
                "action": str(data.get("action") or "").strip().upper(),
                "protocol": str(data.get("protocolName") or data.get("protocol") or ""),
                "source": _format_endpoint(
                    data.get("sourceAddress"),
                    data.get("sourcePort"),
                ),
                "destination": _format_endpoint(
                    data.get("destinationAddress"),
                    data.get("destinationPort"),
                ),
                "destination_port": data.get("destinationPort"),
                "resource_type": resource_type,
            }
        )
    return observations


def _load_cluster_json(sample: Any) -> Dict[str, Any] | None:
    text = _strip_cluster_markup(sample)
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _render_vcn_flow_finding(observations: List[Dict[str, Any]]) -> List[str]:
    total = _total_observed_events(observations)
    total_text = _format_event_count(total) if total is not None else "unknown"
    action = _dominant_flow_action(observations)
    protocol = _dominant_protocol(observations)
    ports = _join_unique(
        str(obs["destination_port"])
        for obs in observations
        if obs.get("destination_port") not in (None, "")
    )
    destinations = _join_unique(obs["destination"] for obs in observations if obs.get("destination"))
    resources = _join_unique(
        obs["resource_type"] for obs in observations if obs.get("resource_type")
    )

    action_phrase = f"{action} {protocol} flow events" if protocol else f"{action} flow events"
    scope_parts = [f"{total_text} {action_phrase} in the top clusters"]
    if ports:
        scope_parts.append(f"destination ports {ports}")
    if destinations:
        scope_parts.append(f"top destinations {destinations}")
    if resources:
        scope_parts.append(f"resources {resources}")

    lines = [
        f"  - Assessment: high-volume {action} VCN traffic.",
        (
            "  - Why it matters: these are blocked network traffic events, "
            "not automatically an application failure; the volume can indicate "
            "internet scanning, misrouted clients, or denied dependency traffic."
        ),
        f"  - Scope: {'; '.join(scope_parts)}.",
        (
            "  - Recommended action: decide whether these rejects are expected "
            "policy enforcement; review security lists, NSGs, route tables, "
            "and load balancer/backend health for the affected destinations."
        ),
    ]
    for observation in observations[:2]:
        count = _format_event_count(observation.get("count"))
        lines.append(f"  - Evidence: {observation['summary']} ({count} events)")
    return lines


def _operator_next_steps(investigation: Dict[str, Any]) -> List[str]:
    seed = investigation.get("seed") or {}
    time_range = seed.get("time_range") or "the selected window"
    observations: List[Dict[str, Any]] = []
    for source in investigation.get("anomalous_sources") or []:
        observations.extend(_collect_vcn_flow_observations(source))
    if not observations:
        return []

    ports = _join_unique(
        str(obs["destination_port"])
        for obs in observations
        if obs.get("destination_port") not in (None, "")
    )
    port_text = f" Focus first on destination ports {ports}." if ports else ""
    return [
        (
            "- Decide whether the rejected traffic is expected policy enforcement "
            "or an unintended connectivity break. Review security lists, NSGs, "
            "route tables, and load balancer/backend health for the affected "
            "destinations."
        ),
        (
            "- Rank rejected VCN flows by source IP, destination IP, and port "
            f"for `{time_range}`.{port_text}"
        ),
        (
            "- If those ports are supposed to be reachable, treat this as a "
            "connectivity incident; if they are not supposed to be reachable, "
            "document it as blocked exposure/noise and consider upstream controls."
        ),
    ]


def _is_low_value_row_limit_step(step: Dict[str, Any]) -> bool:
    reason = str(step.get("reason") or "").lower()
    return "1000 rows" in reason or "tighter/narrower" in reason


def _total_observed_events(observations: List[Dict[str, Any]]) -> int | None:
    counts = [obs["count"] for obs in observations if isinstance(obs.get("count"), int)]
    return sum(counts) if counts else None


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dominant_flow_action(observations: List[Dict[str, Any]]) -> str:
    actions = [str(obs.get("action") or "").upper() for obs in observations]
    if "REJECT" in actions:
        return "rejected"
    if "ACCEPT" in actions:
        return "accepted"
    return "observed"


def _dominant_protocol(observations: List[Dict[str, Any]]) -> str:
    protocols = [str(obs.get("protocol") or "").upper() for obs in observations]
    for protocol in protocols:
        if protocol:
            return protocol
    return ""


def _join_unique(values: Iterable[Any], limit: int = 4) -> str:
    seen = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.append(text)
    if len(seen) > limit:
        return ", ".join(seen[:limit]) + f", +{len(seen) - limit} more"
    return ", ".join(seen)


def _format_endpoint(address: Any, port: Any) -> str:
    if not address:
        return ""
    if port in (None, ""):
        return str(address)
    return f"{address}:{port}"


def _summarize_coredns_cluster(text: str) -> str | None:
    match = re.search(
        r"(?P<client>\d+\.\d+\.\d+\.\d+):\d+\s+-\s+\d+\s+"
        r'"(?P<qtype>[A-Z]+)\s+IN\s+(?P<name>[^"]+?)\.\s+\w+[^"]*"\s+'
        r"(?P<rcode>NOERROR|NXDOMAIN|SERVFAIL|REFUSED)",
        text,
    )
    if not match:
        return None
    return (
        f"DNS {match.group('rcode')} for {match.group('qtype')} "
        f"{match.group('name')} from {match.group('client')}"
    )
