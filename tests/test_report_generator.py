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


def test_generate_uses_custom_title_when_provided():
    report = ReportGenerator().generate(
        _investigation(),
        title="24-hour failures and issues report",
    )

    assert report["metadata"]["title"] == "24-hour failures and issues report"
    assert report["markdown"].startswith("# 24-hour failures and issues report")


def test_generate_renders_current_a1_timeline_clusters_and_entities():
    investigation = _investigation()
    investigation["cross_source_timeline"] = [
        {
            "time": "2026-04-27T03:53:15+00:00",
            "source": "Kubernetes Kubelet Logs",
            "message": "Error syncing pod, skipping: prometheus-server CrashLoopBackOff",
        }
    ]
    investigation["anomalous_sources"][0]["top_error_clusters"] = [
        {
            "pattern": "Readiness probe failed for prometheus-server",
            "count": 42,
        }
    ]
    investigation["anomalous_sources"][0]["top_entities"] = [
        {
            "entity_type": "host",
            "entity_value": "oke-cfqzhq4c4qa-ncpncm7ivua-ssyzrpi2qxa-2",
            "count": 368,
        }
    ]

    report = ReportGenerator().generate(investigation)

    markdown = report["markdown"]
    assert "`2026-04-27T03:53:15+00:00` **Kubernetes Kubelet Logs**" in markdown
    assert "Readiness probe failed for prometheus-server (42 events)" in markdown
    assert "host=oke-cfqzhq4c4qa-ncpncm7ivua-ssyzrpi2qxa-2 (368)" in markdown
    assert "unknown time" not in markdown
    assert "entity=unknown" not in markdown


def test_generate_sanitizes_cluster_template_markup_and_long_samples():
    investigation = _investigation()
    investigation["anomalous_sources"][0]["top_error_clusters"] = [
        {
            "pattern": (
                '{"metadata":{"name":"prometheus-'
                '<#v t="v" id="1:0">7d7bc46676-xdmtm</#v>",'
                '"managedFields":[{"manager":"kube-controller-manager",'
                '"fieldsV1":{"f:metadata":{"f:labels":{"f:app.kubernetes.io/name":{}}}}}]}}'
            ),
            "count": 15384,
        }
    ]

    report = ReportGenerator().generate(investigation)

    markdown = report["markdown"]
    assert "<#v" not in markdown
    assert "</#v>" not in markdown
    assert "prometheus-7d7bc46676-xdmtm" in markdown
    assert "managedFields" not in markdown
    cluster_line = next(line for line in markdown.splitlines() if "Cluster:" in line)
    assert len(cluster_line) < 180


def test_generate_humanizes_query_summary_and_vcn_flow_clusters():
    investigation = _investigation()
    query = (
        "'Log Source' in ('OCI VCN Flow Unified Schema Logs', 'ExaWatcher Top Logs', "
        "'Kubernetes Core DNS Logs') and ('Original Log Content' like '%error%' or "
        "'Original Log Content' like '%REJECT%' or 'Original Log Content' like '%NXDOMAIN%')"
    )
    investigation["summary"] = (
        f"Investigated {query} over last_15_min. "
        "1 anomalous source(s) (top: OCI VCN Flow Unified Schema Logs pct_change=None)."
    )
    investigation["seed"] = {
        "query": query,
        "time_range": "last_15_min",
        "seed_filter_degraded": False,
    }
    investigation["anomalous_sources"] = [
        {
            "source": "OCI VCN Flow Unified Schema Logs",
            "pct_change": None,
            "top_error_clusters": [
                {
                    "pattern": (
                        '{"id":"<#v t="v" id="1:0">3170112b</#v>",'
                        '"time":"2026-05-08T23:34:49Z",'
                        '"oracle":{"compartmentid":"ocid1.compartment.oc1..aaaa",'
                        '"resourceType":"<#v t="v" id="1:1">OKE</#v>"},'
                        '"data":{"sourceAddress":"139.87.113.253",'
                        '"destinationAddress":"10.0.0.11","sourcePort":61875,'
                        '"destinationPort":217,"protocolName":"TCP",'
                        '"action":"REJECT"}}'
                    ),
                    "count": 31311,
                }
            ],
            "top_entities": [],
            "errors": [],
        }
    ]

    report = ReportGenerator().generate(investigation, summary_length="short")

    markdown = report["markdown"]
    summary = markdown.split("## Timeline", 1)[0]
    assert "'Log Source' in" not in summary
    assert "Investigated error-like activity across 3 log sources over last_15_min." in summary
    assert "Top anomalous source: OCI VCN Flow Unified Schema Logs." in summary
    assert "Rejected TCP flow 139.87.113.253:61875 -> 10.0.0.11:217" in markdown
    assert "resource=OKE" in markdown
    assert "(31,311 events)" in markdown
    assert "<#v" not in markdown
    assert "compartmentid" not in markdown
    assert '"oracle"' not in markdown


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


def test_both_format_returns_markdown_and_html_document():
    report = ReportGenerator().generate(_investigation(), output_format="both")

    assert report["markdown"].startswith("# Incident Report")
    assert report["html"].startswith("<!doctype html>")
    assert "<h1>Incident Report</h1>" in report["html"]


def test_html_format_uses_custom_title():
    report = ReportGenerator().generate(
        _investigation(),
        output_format="html",
        title="24-hour failures and issues report",
    )

    assert "<title>24-hour failures and issues report</title>" in report["html"]
    assert "<h1>24-hour failures and issues report</h1>" in report["html"]


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


@pytest.mark.parametrize("title", [123, ["Incident Report"], False])
def test_invalid_title_type_raises_report_generation_error(title):
    with pytest.raises(ReportGenerationError) as exc:
        ReportGenerator().generate(_investigation(), title=title)

    assert "title must be a string" in str(exc.value)


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
