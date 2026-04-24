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
