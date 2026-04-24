"""Tests for N1 investigation recording from audit entries."""

from datetime import datetime

from oci_logan_mcp.audit import AuditLogger
from oci_logan_mcp.playbook_recorder import PlaybookRecorder
from oci_logan_mcp.playbook_store import PlaybookStore


def _recorder(tmp_path):
    audit = AuditLogger(tmp_path / "audit", session_id="session-a")
    store = PlaybookStore(tmp_path / "playbooks.sqlite3")
    return audit, store, PlaybookRecorder(
        audit_logger=audit,
        store=store,
        owner="testuser",
    )


def test_record_filters_current_session_events_by_window(tmp_path):
    audit, store, recorder = _recorder(tmp_path)
    audit.log(user="testuser", tool="run_query", args={"query": "*"}, outcome="invoked")
    audit.log(user="testuser", tool="list_fields", args={}, outcome="invoked")

    playbook = recorder.record(
        name="incident",
        description="two steps",
        since="2026-01-01T00:00:00+00:00",
        until="2099-01-01T00:00:00+00:00",
    )

    assert playbook["name"] == "incident"
    assert playbook["description"] == "two steps"
    assert playbook["owner"] == "testuser"
    assert playbook["source_process_session_id"] == "session-a"
    assert [step["tool"] for step in playbook["steps"]] == [
        "run_query",
        "list_fields",
    ]
    assert playbook["steps"][0]["args"] == {"query": "*"}
    assert store.get(playbook["id"]) == playbook


def test_record_zero_events_adds_warning(tmp_path):
    _, _, recorder = _recorder(tmp_path)

    playbook = recorder.record(
        name="empty",
        description=None,
        since="2026-01-01T00:00:00+00:00",
        until="2026-01-01T00:01:00+00:00",
    )

    assert playbook["steps"] == []
    assert playbook["warning"] == "No audit events matched the requested time window."


def test_record_rejects_since_after_until(tmp_path):
    _, _, recorder = _recorder(tmp_path)

    try:
        recorder.record(
            name="bad",
            description=None,
            since="2026-01-01T01:00:00+00:00",
            until="2026-01-01T00:00:00+00:00",
        )
    except ValueError as exc:
        assert "since must be before until" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_defaults_capture_process_lifetime(tmp_path):
    audit, _, recorder = _recorder(tmp_path)
    audit.log(user="testuser", tool="test_connection", args={}, outcome="invoked")

    playbook = recorder.record(name="default-window")

    assert len(playbook["steps"]) == 1
    assert datetime.fromisoformat(playbook["window"]["since"]) <= datetime.fromisoformat(
        playbook["steps"][0]["ts"]
    )
    assert datetime.fromisoformat(playbook["steps"][0]["ts"]) <= datetime.fromisoformat(
        playbook["window"]["until"]
    )


def test_record_excludes_record_investigation_bookkeeping_call(tmp_path):
    audit, _, recorder = _recorder(tmp_path)
    audit.log(user="testuser", tool="run_query", args={"query": "*"}, outcome="invoked")
    audit.log(
        user="testuser",
        tool="record_investigation",
        args={"name": "incident"},
        outcome="invoked",
    )

    playbook = recorder.record(
        name="incident",
        since="2026-01-01T00:00:00+00:00",
        until="2099-01-01T00:00:00+00:00",
    )

    assert [step["tool"] for step in playbook["steps"]] == ["run_query"]
