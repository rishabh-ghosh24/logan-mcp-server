"""Tests for ingestion_health tool."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.ingestion_health import (
    IngestionHealthTool,
    _classify,
    _parse_ts,
)


class TestParseTs:
    def test_parses_iso_z(self):
        dt = _parse_ts("2026-04-22T10:00:00Z")
        assert dt == datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)

    def test_parses_iso_offset(self):
        dt = _parse_ts("2026-04-22T10:00:00+00:00")
        assert dt == datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)

    def test_none_passthrough(self):
        assert _parse_ts(None) is None

    def test_empty_string(self):
        assert _parse_ts("") is None

    def test_garbage(self):
        assert _parse_ts("not-a-date") is None


class TestClassify:
    def test_unknown_when_last_log_ts_none(self):
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        status, severity, age, msg = _classify(None, now, threshold_s=600)
        assert status == "unknown"
        assert severity == "warn"
        assert age is None
        assert "no records" in msg.lower()

    def test_healthy_when_age_under_threshold(self):
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(seconds=60)
        status, severity, age, msg = _classify(last, now, threshold_s=600)
        assert status == "healthy"
        assert severity == "info"
        assert age == 60
        assert "60s" in msg or "60 s" in msg

    def test_stopped_when_age_at_or_above_threshold(self):
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(seconds=1800)  # 30 min
        status, severity, age, msg = _classify(last, now, threshold_s=600)
        assert status == "stopped"
        assert severity == "critical"
        assert age == 1800
        assert "stopped" in msg.lower() or "stale" in msg.lower()

    def test_boundary_exactly_at_threshold_is_stopped(self):
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(seconds=600)
        status, _, _, _ = _classify(last, now, threshold_s=600)
        assert status == "stopped"
