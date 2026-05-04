from __future__ import annotations

import json

import pytest

from oci_logan_mcp.report_store import (
    InvalidReportIdError,
    ReportNotFoundError,
    ReportStoreCorruptError,
    ReportStoreError,
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
            "word_count": 298,
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
    assert saved["artifacts"] == [
        {"name": "markdown", "type": "markdown", "path": str(report_dir / "report.md")},
        {"name": "html", "type": "html", "path": str(report_dir / "report.html")},
        {"name": "metadata", "type": "json", "path": str(report_dir / "metadata.json")},
    ]


def test_user_scoped_store_isolates_reports_with_shared_artifact_dir(tmp_path):
    alice = ReportStore(tmp_path, user_id="alice")
    bob = ReportStore(tmp_path, user_id="bob")

    saved = alice.save(_report())

    assert saved["markdown_path"] == str(
        tmp_path / "users" / "alice" / "store" / "rpt_0123456789abcdef0123456789abcdef" / "report.md"
    )
    assert alice.get("rpt_0123456789abcdef0123456789abcdef")["metadata"]["title"] == (
        "24-hour failures and issues report"
    )
    with pytest.raises(ReportNotFoundError):
        bob.get("rpt_0123456789abcdef0123456789abcdef")


def test_user_scoped_store_rejects_invalid_user_id(tmp_path):
    with pytest.raises(ReportStoreError):
        ReportStore(tmp_path, user_id="../alice")


def test_user_scoped_store_imports_legacy_shared_reports(tmp_path):
    legacy = ReportStore(tmp_path)
    legacy.save(_report())

    alice = ReportStore(tmp_path, user_id="alice")

    listed = alice.list(limit=10)
    loaded = alice.get("rpt_0123456789abcdef0123456789abcdef")
    expected_dir = tmp_path / "users" / "alice" / "store" / "rpt_0123456789abcdef0123456789abcdef"
    legacy_dir = tmp_path / "store" / "rpt_0123456789abcdef0123456789abcdef"
    assert listed["reports"][0]["report_id"] == "rpt_0123456789abcdef0123456789abcdef"
    assert loaded["metadata"]["legacy_shared_imported_from"] == str(legacy_dir)
    assert loaded["metadata"]["markdown_path"] == str(expected_dir / "report.md")
    assert loaded["metadata"]["html_path"] == str(expected_dir / "report.html")
    assert loaded["metadata"]["metadata_path"] == str(expected_dir / "metadata.json")
    assert (legacy_dir / "report.md").exists()
    assert (legacy_dir / "report.html").exists()
    assert (legacy_dir / "metadata.json").exists()

    first_metadata = (expected_dir / "metadata.json").read_text(encoding="utf-8")
    alice_again = ReportStore(tmp_path, user_id="alice")

    assert alice_again.list(limit=10)["reports"][0]["report_id"] == (
        "rpt_0123456789abcdef0123456789abcdef"
    )
    assert (expected_dir / "metadata.json").read_text(encoding="utf-8") == first_metadata


def test_incomplete_report_without_metadata_is_not_listed_or_loaded(tmp_path):
    store = ReportStore(tmp_path)
    report_id = "rpt_44444444444444444444444444444444"
    report_dir = tmp_path / "store" / report_id
    report_dir.mkdir(parents=True)
    (report_dir / "report.md").write_text("# Half-written report\n", encoding="utf-8")

    listed = store.list(limit=10)

    assert listed["reports"] == []
    assert listed["warnings"] == {"corrupt_count": 1}
    with pytest.raises(ReportNotFoundError):
        store.get(report_id)


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


def test_update_metadata_merges_patch_safely(tmp_path):
    store = ReportStore(tmp_path)
    store.save(_report())

    updated = store.update_metadata(
        "rpt_0123456789abcdef0123456789abcdef",
        {"delivery_state": {"status": "awaiting_final_confirmation"}},
    )

    assert updated["metadata"]["title"] == "24-hour failures and issues report"
    assert updated["metadata"]["delivery_state"]["status"] == "awaiting_final_confirmation"
    loaded = store.get("rpt_0123456789abcdef0123456789abcdef")
    assert loaded["metadata"]["delivery_state"]["status"] == "awaiting_final_confirmation"


def test_markdown_only_resave_removes_stale_html(tmp_path):
    store = ReportStore(tmp_path)
    report_id = "rpt_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    store.save(_report(report_id))

    markdown_only_report = _report(report_id)
    markdown_only_report["markdown"] = "# Updated Incident Report\n"
    markdown_only_report["html"] = None
    saved = store.save(markdown_only_report)
    loaded = store.get(report_id)

    assert saved["html_path"] is None
    assert loaded["html"] is None
    assert loaded["html_path"] is None
    assert not (tmp_path / "store" / report_id / "report.html").exists()
    assert loaded["markdown"] == "# Updated Incident Report\n"


@pytest.mark.parametrize("metadata", [[], "", 0, False])
def test_save_rejects_falsy_non_object_metadata(tmp_path, metadata):
    store = ReportStore(tmp_path)
    report = _report("rpt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    report["metadata"] = metadata

    with pytest.raises(ReportStoreError, match="metadata"):
        store.save(report)


def test_save_defaults_missing_or_none_metadata_to_empty_object(tmp_path):
    store = ReportStore(tmp_path)
    missing_metadata = _report("rpt_cccccccccccccccccccccccccccccccc")
    del missing_metadata["metadata"]
    none_metadata = _report("rpt_dddddddddddddddddddddddddddddddd")
    none_metadata["metadata"] = None

    missing_saved = store.save(missing_metadata)
    none_saved = store.save(none_metadata)

    assert missing_saved["metadata"]["title"] == "Incident Report"
    assert none_saved["metadata"]["title"] == "Incident Report"


@pytest.mark.parametrize(
    "report_id",
    [
        "../rpt_0123456789abcdef0123456789abcdef",
        "rpt_0123456789ABCDEF0123456789abcdef",
        "rpt_0123456789abcdef0123456789abcde",
        "rpt_" + "0" * 32 + "\n",
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


def test_save_rejects_symlinked_report_dir_without_writing_outside_store(tmp_path):
    store = ReportStore(tmp_path)
    report_id = "rpt_55555555555555555555555555555555"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    report_dir = tmp_path / "store" / report_id
    report_dir.parent.mkdir()
    report_dir.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(ReportStoreError):
        store.save(_report(report_id))

    assert not (outside_dir / "report.md").exists()
    assert not (outside_dir / "report.html").exists()
    assert not (outside_dir / "metadata.json").exists()


def test_get_rejects_symlinked_report_dir_without_reading_outside_store(tmp_path):
    store = ReportStore(tmp_path)
    report_id = "rpt_66666666666666666666666666666666"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "report.md").write_text("# Outside\n", encoding="utf-8")
    (outside_dir / "metadata.json").write_text('{"title": "Outside"}', encoding="utf-8")
    report_dir = tmp_path / "store" / report_id
    report_dir.parent.mkdir()
    report_dir.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(ReportStoreCorruptError):
        store.get(report_id)


def test_get_rejects_symlinked_store_root_without_reading_outside_store(tmp_path):
    store = ReportStore(tmp_path)
    report_id = "rpt_66666666666666666666666666666667"
    outside_root = tmp_path / "outside_store"
    outside_report_dir = outside_root / report_id
    outside_report_dir.mkdir(parents=True)
    (outside_report_dir / "report.md").write_text("# Outside\n", encoding="utf-8")
    (outside_report_dir / "metadata.json").write_text(
        '{"title": "Outside"}',
        encoding="utf-8",
    )
    store.root.symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(ReportStoreCorruptError):
        store.get(report_id)


def test_list_counts_symlinked_report_dir_as_corrupt_without_loading_it(tmp_path):
    store = ReportStore(tmp_path)
    report_id = "rpt_77777777777777777777777777777777"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "report.md").write_text("# Outside\n", encoding="utf-8")
    (outside_dir / "metadata.json").write_text(
        '{"title": "Outside", "generated_at": "2026-04-27T12:00:00Z"}',
        encoding="utf-8",
    )
    report_dir = tmp_path / "store" / report_id
    report_dir.parent.mkdir()
    report_dir.symlink_to(outside_dir, target_is_directory=True)

    listed = store.list(limit=10)

    assert listed["reports"] == []
    assert listed["warnings"] == {"corrupt_count": 1}


def test_non_object_metadata_is_corrupt_for_list_and_get(tmp_path):
    store = ReportStore(tmp_path)
    report_id = "rpt_88888888888888888888888888888888"
    report_dir = tmp_path / "store" / report_id
    report_dir.mkdir(parents=True)
    (report_dir / "report.md").write_text("# Incident Report\n", encoding="utf-8")
    (report_dir / "metadata.json").write_text("[]", encoding="utf-8")

    listed = store.list(limit=10)

    assert listed["reports"] == []
    assert listed["warnings"] == {"corrupt_count": 1}
    with pytest.raises(ReportStoreCorruptError):
        store.get(report_id)


def test_atomic_write_removes_temp_file_when_replace_fails(tmp_path, monkeypatch):
    store = ReportStore(tmp_path)
    report_id = "rpt_99999999999999999999999999999999"

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("oci_logan_mcp.report_store.os.replace", fail_replace)

    with pytest.raises(ReportStoreError, match="replace failed"):
        store.save(_report(report_id))

    report_dir = tmp_path / "store" / report_id
    assert list(report_dir.glob(".*.tmp")) == []
    assert not (report_dir / "report.md").exists()


def test_list_returns_computed_paths_when_metadata_paths_are_tampered(tmp_path):
    store = ReportStore(tmp_path)
    report_id = "rpt_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    store.save(_report(report_id))
    report_dir = tmp_path / "store" / report_id
    metadata_path = report_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["markdown_path"] = str(tmp_path / "outside" / "report.md")
    metadata["html_path"] = str(tmp_path / "outside" / "report.html")
    metadata["metadata_path"] = str(tmp_path / "outside" / "metadata.json")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    listed = store.list(limit=10)

    assert listed["warnings"] == {"corrupt_count": 0}
    assert listed["reports"] == [
        {
            "report_id": report_id,
            "title": "24-hour failures and issues report",
            "generated_at": "2026-04-27T12:00:00Z",
            "time_range": "last_24_hours",
            "summary_length": "standard",
            "word_count": 298,
            "markdown_path": str(report_dir / "report.md"),
            "html_path": str(report_dir / "report.html"),
            "metadata_path": str(report_dir / "metadata.json"),
        }
    ]


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
