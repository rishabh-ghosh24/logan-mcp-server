"""Tests for deterministic N3 incident report generation."""

import re

import pytest

from oci_logan_mcp.report_generator import (
    ReportGenerationError,
    ReportGenerator,
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
                {
                    "source": "Apache Access",
                    "status": "stopped",
                    "message": "No recent logs",
                }
            ],
        },
        "parser_failures": {
            "total_failure_count": 7,
            "failures": [{"source": "Apache Access", "failure_count": 7}],
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
                "top_entities": [{"field": "host", "value": "web-1", "count": 9}],
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
