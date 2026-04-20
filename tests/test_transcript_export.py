"""Tests for AuditLogger.export_transcript."""

import json
from pathlib import Path

import pytest

from oci_logan_mcp.audit import AuditLogger


def test_export_filters_by_session(tmp_path):
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger_s1 = AuditLogger(log_dir, session_id="s1")
    logger_s1.log(user="u", tool="run_query", args={"q": "*"}, outcome="invoked")
    logger_s1.log(user="u", tool="list_fields", args={}, outcome="invoked")

    logger_s2 = AuditLogger(log_dir, session_id="s2")
    logger_s2.log(user="u", tool="run_query", args={"q": "b"}, outcome="invoked")

    result = logger_s1.export_transcript(session_id="s1", out_dir=out_dir)
    assert result["event_count"] == 2
    lines = Path(result["path"]).read_text().splitlines()
    parsed = [json.loads(ln) for ln in lines]
    tools = [e["tool"] for e in parsed]
    assert tools == ["run_query", "list_fields"]
    assert all(e["session_id"] == "s1" for e in parsed)


def test_export_roundtrips_through_jq(tmp_path):
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger = AuditLogger(log_dir, session_id="s")
    for i in range(5):
        logger.log(user="u", tool=f"t{i}", args={"i": i}, outcome="invoked")

    result = logger.export_transcript(session_id="s", out_dir=out_dir)
    lines = Path(result["path"]).read_text().splitlines()
    assert len(lines) == 5
    for ln in lines:
        entry = json.loads(ln)
        assert "timestamp" in entry
        assert "session_id" in entry


def test_export_nonexistent_session_returns_zero(tmp_path):
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger = AuditLogger(log_dir, session_id="present")
    logger.log(user="u", tool="x", args={}, outcome="invoked")
    result = logger.export_transcript(session_id="nope", out_dir=out_dir)
    assert result["event_count"] == 0


def test_export_reads_rotated_backups(tmp_path):
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    log_dir.mkdir()
    e1 = json.dumps({"timestamp": "t", "session_id": "s", "user": "u", "pid": 1, "tool": "old", "args": {}, "outcome": "invoked"})
    e2 = json.dumps({"timestamp": "t", "session_id": "s", "user": "u", "pid": 1, "tool": "new", "args": {}, "outcome": "invoked"})
    (log_dir / "audit.log.1").write_text(e1 + "\n")
    (log_dir / "audit.log").write_text(e2 + "\n")

    logger = AuditLogger(log_dir, session_id="s")
    result = logger.export_transcript(session_id="s", out_dir=out_dir)
    tools = [json.loads(ln)["tool"] for ln in Path(result["path"]).read_text().splitlines()]
    assert "old" in tools
    assert "new" in tools


def test_export_include_results_false_strips_result_preview(tmp_path):
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger = AuditLogger(log_dir, session_id="s")
    logger.log(user="u", tool="x", args={}, outcome="executed", result_summary="sensitive preview")

    result = logger.export_transcript(session_id="s", out_dir=out_dir, include_results=False)
    content = Path(result["path"]).read_text()
    assert "sensitive preview" not in content


def test_export_redact_masks_known_patterns(tmp_path):
    log_dir = tmp_path / "logs"
    out_dir = tmp_path / "transcripts"
    logger = AuditLogger(log_dir, session_id="s")
    logger.log(user="u", tool="x", args={"email": "alice@example.com"}, outcome="invoked")

    result = logger.export_transcript(session_id="s", out_dir=out_dir, redact=True)
    content = Path(result["path"]).read_text()
    assert "alice@example.com" not in content
