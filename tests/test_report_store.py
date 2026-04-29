import json

import pytest

from oci_logan_mcp.report_store import ReportStore, ReportStoreError


def _report(report_id="rpt_1234567890abcdef1234567890abcdef"):
    return {
        "report_id": report_id,
        "markdown": "# Incident Report\n",
        "html": "<!doctype html><html></html>",
        "metadata": {"source_type": "investigation"},
    }


def test_save_writes_markdown_html_and_metadata(tmp_path):
    store = ReportStore(tmp_path)

    saved = store.save(_report())

    report_dir = tmp_path / "rpt_1234567890abcdef1234567890abcdef"
    assert (report_dir / "report.md").read_text() == "# Incident Report\n"
    assert (report_dir / "report.html").read_text() == "<!doctype html><html></html>"
    metadata = json.loads((report_dir / "metadata.json").read_text())
    assert metadata["report_id"] == "rpt_1234567890abcdef1234567890abcdef"
    assert {artifact["name"] for artifact in saved["artifacts"]} == {
        "report.md",
        "report.html",
        "metadata.json",
    }


def test_get_round_trips_stored_report(tmp_path):
    store = ReportStore(tmp_path)
    store.save(_report())

    loaded = store.get("rpt_1234567890abcdef1234567890abcdef")

    assert loaded["report_id"] == "rpt_1234567890abcdef1234567890abcdef"
    assert loaded["markdown"] == "# Incident Report\n"
    assert loaded["html"].startswith("<!doctype html>")


def test_list_orders_newest_first(tmp_path):
    store = ReportStore(tmp_path)
    store.save(_report("rpt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))
    store.save(_report("rpt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"))

    rows = store.list(limit=2)

    assert [row["report_id"] for row in rows] == [
        "rpt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "rpt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ]


@pytest.mark.parametrize("bad_id", ["../x", "rpt_bad", "rpt_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"])
def test_rejects_unsafe_report_ids(tmp_path, bad_id):
    store = ReportStore(tmp_path)

    with pytest.raises(ReportStoreError):
        store.get(bad_id)
