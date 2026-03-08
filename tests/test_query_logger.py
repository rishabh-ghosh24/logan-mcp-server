"""Tests for query logger module."""

import pytest
from datetime import datetime, timezone
from pathlib import Path

from oci_logan_mcp.query_logger import QueryLogger
from oci_logan_mcp.config import LoggingConfig


@pytest.fixture
def logger_enabled(tmp_path):
    """Create a QueryLogger with logging enabled, using temp dir."""
    config = LoggingConfig(query_logging=True, log_path=tmp_path / "logs")
    return QueryLogger(config)


@pytest.fixture
def logger_disabled():
    """Create a QueryLogger with logging disabled."""
    config = LoggingConfig(query_logging=False)
    return QueryLogger(config)


def _log_sample(logger, success=True, query="* | stats count", execution_time=0.5,
                result_count=10, error=None, compartment_id=None, namespace=None):
    """Helper to log a sample query."""
    logger.log_query(
        query=query,
        time_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        time_end=datetime(2025, 1, 2, tzinfo=timezone.utc),
        execution_time=execution_time,
        result_count=result_count,
        success=success,
        error=error,
        compartment_id=compartment_id,
        namespace=namespace,
    )


# ---------------------------------------------------------------
# Init
# ---------------------------------------------------------------


class TestQueryLoggerInit:
    """Tests for initialization."""

    def test_init_with_logging_enabled(self, logger_enabled):
        """Enabled logger sets up file logger."""
        assert logger_enabled._enabled is True
        assert hasattr(logger_enabled, "_file_logger")

    def test_init_with_logging_disabled(self, logger_disabled):
        """Disabled logger has no file logger."""
        assert logger_disabled._enabled is False
        assert not hasattr(logger_disabled, "_file_logger")

    def test_default_config_used_if_none(self):
        """None config -> default LoggingConfig."""
        # This will try to create log dir at default path, but won't fail
        # because logging defaults to True
        logger = QueryLogger(LoggingConfig(query_logging=False))
        assert logger._enabled is False


# ---------------------------------------------------------------
# log_query
# ---------------------------------------------------------------


class TestLogQuery:
    """Tests for log_query method."""

    def test_log_successful_query(self, logger_disabled):
        """Successful query is logged."""
        _log_sample(logger_disabled, success=True)
        assert len(logger_disabled._recent_queries) == 1
        assert logger_disabled._recent_queries[0]["success"] is True

    def test_log_failed_query(self, logger_disabled):
        """Failed query with error is logged."""
        _log_sample(logger_disabled, success=False, error="OCI timeout")
        entry = logger_disabled._recent_queries[0]
        assert entry["success"] is False
        assert entry["error"] == "OCI timeout"

    def test_log_with_optional_fields(self, logger_disabled):
        """Compartment and namespace stored when provided."""
        _log_sample(logger_disabled, compartment_id="ocid1.comp.test", namespace="ns1")
        entry = logger_disabled._recent_queries[0]
        assert entry["compartment_id"] == "ocid1.comp.test"
        assert entry["namespace"] == "ns1"

    def test_optional_fields_absent_when_not_provided(self, logger_disabled):
        """Compartment/namespace absent when not provided."""
        _log_sample(logger_disabled)
        entry = logger_disabled._recent_queries[0]
        assert "compartment_id" not in entry
        assert "namespace" not in entry

    def test_capped_at_100(self, logger_disabled):
        """Recent queries list capped at 100."""
        for i in range(110):
            _log_sample(logger_disabled, query=f"query_{i}")
        assert len(logger_disabled._recent_queries) == 100

    def test_newest_first_order(self, logger_disabled):
        """Most recent query is first."""
        _log_sample(logger_disabled, query="first")
        _log_sample(logger_disabled, query="second")
        assert logger_disabled._recent_queries[0]["query"] == "second"

    def test_log_written_to_file_when_enabled(self, logger_enabled, tmp_path):
        """Log file gets content when enabled."""
        _log_sample(logger_enabled)
        log_file = tmp_path / "logs" / "queries.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "SUCCESS" in content

    def test_log_not_written_when_disabled(self, logger_disabled):
        """No file logger when disabled."""
        _log_sample(logger_disabled)
        # Should not raise, just silently skip file logging
        assert len(logger_disabled._recent_queries) == 1


# ---------------------------------------------------------------
# get_recent_queries
# ---------------------------------------------------------------


class TestGetRecentQueries:
    """Tests for get_recent_queries method."""

    def test_returns_only_successful(self, logger_disabled):
        """Only successful queries returned."""
        _log_sample(logger_disabled, success=True, query="good")
        _log_sample(logger_disabled, success=False, query="bad")
        _log_sample(logger_disabled, success=True, query="good2")
        result = logger_disabled.get_recent_queries()
        assert len(result) == 2
        assert all(q["success"] for q in result)

    def test_respects_limit(self, logger_disabled):
        """Limit caps results."""
        for i in range(20):
            _log_sample(logger_disabled, success=True, query=f"q{i}")
        result = logger_disabled.get_recent_queries(limit=5)
        assert len(result) == 5

    def test_empty_when_no_queries(self, logger_disabled):
        """Fresh logger -> empty list."""
        assert logger_disabled.get_recent_queries() == []


# ---------------------------------------------------------------
# get_all_recent
# ---------------------------------------------------------------


class TestGetAllRecent:
    """Tests for get_all_recent method."""

    def test_returns_all_including_failures(self, logger_disabled):
        """Returns both successful and failed queries."""
        _log_sample(logger_disabled, success=True)
        _log_sample(logger_disabled, success=False, error="timeout")
        result = logger_disabled.get_all_recent()
        assert len(result) == 2

    def test_respects_limit(self, logger_disabled):
        """Limit caps results."""
        for i in range(20):
            _log_sample(logger_disabled, query=f"q{i}")
        result = logger_disabled.get_all_recent(limit=3)
        assert len(result) == 3


# ---------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------


class TestGetStats:
    """Tests for get_stats method."""

    def test_stats_no_queries(self, logger_disabled):
        stats = logger_disabled.get_stats()
        assert stats["total_queries"] == 0
        assert stats["successful"] == 0
        assert stats["failed"] == 0
        assert stats["success_rate"] == 0
        assert stats["avg_execution_time_seconds"] == 0

    def test_stats_with_mixed_queries(self, logger_disabled):
        _log_sample(logger_disabled, success=True, execution_time=1.0)
        _log_sample(logger_disabled, success=True, execution_time=2.0)
        _log_sample(logger_disabled, success=False, execution_time=0.5, error="err")
        stats = logger_disabled.get_stats()
        assert stats["total_queries"] == 3
        assert stats["successful"] == 2
        assert stats["failed"] == 1
        assert stats["success_rate"] == pytest.approx(66.7, abs=0.1)

    def test_avg_execution_time(self, logger_disabled):
        _log_sample(logger_disabled, execution_time=1.0)
        _log_sample(logger_disabled, execution_time=3.0)
        stats = logger_disabled.get_stats()
        assert stats["avg_execution_time_seconds"] == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------
# _format_log_line
# ---------------------------------------------------------------


class TestFormatLogLine:
    """Tests for log line formatting."""

    def test_success_format(self, logger_disabled):
        entry = {
            "success": True,
            "execution_time_seconds": 0.123,
            "result_count": 10,
            "query": "* | stats count",
        }
        line = logger_disabled._format_log_line(entry)
        assert "SUCCESS" in line
        assert "0.123s" in line
        assert "10 results" in line
        assert "* | stats count" in line

    def test_failure_format(self, logger_disabled):
        entry = {
            "success": False,
            "execution_time_seconds": 0.5,
            "result_count": 0,
            "query": "bad query",
            "error": "OCI timeout",
        }
        line = logger_disabled._format_log_line(entry)
        assert "FAILED" in line
        assert "Error: OCI timeout" in line

    def test_long_query_truncated(self, logger_disabled):
        entry = {
            "success": True,
            "execution_time_seconds": 0.1,
            "result_count": 0,
            "query": "x" * 200,
        }
        line = logger_disabled._format_log_line(entry)
        assert "..." in line
        # Query portion should be at most 103 chars ("x"*100 + "...")
        query_part = line.split("Query: ")[1]
        assert len(query_part) <= 103

    def test_newlines_removed(self, logger_disabled):
        entry = {
            "success": True,
            "execution_time_seconds": 0.1,
            "result_count": 0,
            "query": "* | where\nSeverity = 'ERROR'\n| stats count",
        }
        line = logger_disabled._format_log_line(entry)
        assert "\n" not in line
