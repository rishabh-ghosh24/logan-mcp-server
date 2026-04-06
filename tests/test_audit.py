"""Tests for AuditLogger – JSON-lines audit log with rotation."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from oci_logan_mcp.audit import AuditLogger


@pytest.fixture()
def log_dir(tmp_path: Path) -> Path:
    return tmp_path / "audit"


@pytest.fixture()
def logger(log_dir: Path) -> AuditLogger:
    return AuditLogger(log_dir)


class TestAuditLogger:
    def test_creates_log_dir(self, log_dir: Path) -> None:
        AuditLogger(log_dir)
        assert log_dir.is_dir()

    def test_log_creates_audit_file(self, logger: AuditLogger, log_dir: Path) -> None:
        logger.log(user="alice", tool="delete_dashboard", args={"id": "1"}, outcome="success")
        assert (log_dir / "audit.log").is_file()

    def test_log_format_is_json_lines(self, logger: AuditLogger, log_dir: Path) -> None:
        logger.log(user="alice", tool="delete_dashboard", args={"id": "1"}, outcome="success")
        logger.log(user="bob", tool="create_alert", args={"name": "a"}, outcome="denied")
        lines = (log_dir / "audit.log").read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            entry = json.loads(line)
            for key in ("timestamp", "user", "pid", "tool", "args", "outcome"):
                assert key in entry, f"Missing key: {key}"

    def test_result_summary_and_error_fields(self, logger: AuditLogger, log_dir: Path) -> None:
        logger.log(
            user="alice", tool="delete_dashboard", args={}, outcome="error",
            result_summary="deleted 1 item", error="timeout",
        )
        entry = json.loads((log_dir / "audit.log").read_text().strip())
        assert entry["result_summary"] == "deleted 1 item"
        assert entry["error"] == "timeout"

    def test_strips_confirmation_fields(self, logger: AuditLogger, log_dir: Path) -> None:
        logger.log(
            user="alice", tool="delete_dashboard",
            args={"id": "1", "confirmation_token": "tok", "confirmation_secret": "sec"},
            outcome="success",
        )
        entry = json.loads((log_dir / "audit.log").read_text().strip())
        assert "confirmation_token" not in entry["args"]
        assert "confirmation_secret" not in entry["args"]
        assert entry["args"]["id"] == "1"

    def test_timestamp_is_utc(self, logger: AuditLogger, log_dir: Path) -> None:
        logger.log(user="alice", tool="t", args={}, outcome="ok")
        entry = json.loads((log_dir / "audit.log").read_text().strip())
        assert entry["timestamp"].endswith("Z")

    def test_secret_management_events(self, logger: AuditLogger, log_dir: Path) -> None:
        logger.log(
            user="alice", tool="__secret_management", args={"action": "set"},
            outcome="secret_set",
        )
        entry = json.loads((log_dir / "audit.log").read_text().strip())
        assert entry["tool"] == "__secret_management"
        assert entry["outcome"] == "secret_set"

    def test_audit_file_permissions(self, logger: AuditLogger, log_dir: Path) -> None:
        logger.log(user="alice", tool="t", args={}, outcome="ok")
        mode = stat.S_IMODE(os.stat(log_dir / "audit.log").st_mode)
        assert mode == 0o640

    def test_log_dir_permissions(self, log_dir: Path) -> None:
        AuditLogger(log_dir)
        mode = stat.S_IMODE(os.stat(log_dir).st_mode)
        assert mode == 0o750

    def test_rotation_at_10mb(self, logger: AuditLogger, log_dir: Path) -> None:
        audit_log = log_dir / "audit.log"
        # Create a file just at the 10MB threshold
        audit_log.write_bytes(b"x" * (10 * 1024 * 1024))
        logger.log(user="alice", tool="t", args={}, outcome="ok")
        assert (log_dir / "audit.log.1").is_file()
        # New audit.log should contain only the new entry
        lines = audit_log.read_text().strip().splitlines()
        assert len(lines) == 1

    def test_rotation_shifts_backups(self, logger: AuditLogger, log_dir: Path) -> None:
        audit_log = log_dir / "audit.log"
        # Create existing backups .1, .2, .3
        for i in range(1, 4):
            (log_dir / f"audit.log.{i}").write_text(f"backup-{i}")
        # Create a file at 10MB to trigger rotation
        audit_log.write_bytes(b"x" * (10 * 1024 * 1024))
        logger.log(user="alice", tool="t", args={}, outcome="ok")
        # .1-.3 should have shifted to .2-.4
        assert (log_dir / "audit.log.2").read_text() == "backup-1"
        assert (log_dir / "audit.log.3").read_text() == "backup-2"
        assert (log_dir / "audit.log.4").read_text() == "backup-3"

    def test_rotation_max_5_backups(self, logger: AuditLogger, log_dir: Path) -> None:
        audit_log = log_dir / "audit.log"
        # Create existing backups .1 through .5
        for i in range(1, 6):
            (log_dir / f"audit.log.{i}").write_text(f"backup-{i}")
        # Trigger rotation
        audit_log.write_bytes(b"x" * (10 * 1024 * 1024))
        logger.log(user="alice", tool="t", args={}, outcome="ok")
        # .6 should NOT exist (max 5 backups)
        assert not (log_dir / "audit.log.6").is_file()
        # .5 should still exist (was .4 before rotation)
        assert (log_dir / "audit.log.5").is_file()
