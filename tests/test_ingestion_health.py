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


# ---------------------------------------------------------------------------
# Fixtures for orchestration tests
# ---------------------------------------------------------------------------

def _make_engine(response):
    """Mock QueryEngine whose execute() returns `response` on one call."""
    engine = MagicMock()
    engine.execute = AsyncMock(return_value=response)
    return engine


def _make_schema(source_names):
    """Mock SchemaManager whose get_log_sources() returns these source dicts."""
    schema = MagicMock()
    schema.get_log_sources = AsyncMock(
        return_value=[{"name": n} for n in source_names]
    )
    return schema


def _make_settings(threshold_s=600, probe_window="last_1_hour"):
    from oci_logan_mcp.config import IngestionHealthConfig, Settings
    s = Settings()
    s.ingestion_health = IngestionHealthConfig(
        stoppage_threshold_seconds=threshold_s,
        freshness_probe_window=probe_window,
    )
    return s


def _probe_result(rows):
    """Shape a QueryEngine response around `[(source, last_log_ts), ...]` rows."""
    return {
        "source": "live",
        "data": {
            "columns": [{"name": "Log Source"}, {"name": "last_log_ts"}],
            "rows": [[src, ts] for src, ts in rows],
        },
        "metadata": {},
    }


class TestOrchestration:
    @pytest.mark.asyncio
    async def test_healthy_source_recent_record(self, monkeypatch):
        # "now" is frozen inside the tool by monkeypatching _utcnow.
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([
            ("Linux Syslog", "2026-04-22T09:59:30Z"),  # 30s ago → healthy
        ]))
        schema = _make_schema(["Linux Syslog"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run()

        # Summary counts are global — healthy source is counted here.
        assert result["summary"] == {
            "sources_healthy": 1,
            "sources_stopped": 0,
            "sources_unknown": 0,
        }
        # But `severity_filter` defaults to "warn", which drops info-severity
        # (healthy) findings from the list. Pin that: summary shows 1 healthy
        # source; findings is empty. The positive-case shape assertions live
        # in `test_severity_filter_all_shows_healthy` below.
        assert result["findings"] == []

    @pytest.mark.asyncio
    async def test_severity_filter_all_shows_healthy(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([
            ("Linux Syslog", "2026-04-22T09:59:30Z"),
        ]))
        schema = _make_schema(["Linux Syslog"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run(severity_filter="all")

        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["source"] == "Linux Syslog"
        assert f["status"] == "healthy"
        assert f["severity"] == "info"
        assert f["age_seconds"] == 30
        assert f["last_log_ts"] == "2026-04-22T09:59:30+00:00"

    @pytest.mark.asyncio
    async def test_stopped_source_30min_stale(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([
            ("Apache Access", "2026-04-22T09:30:00Z"),  # 30 min ago → stopped
        ]))
        schema = _make_schema(["Apache Access"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run()  # default severity_filter="warn"

        assert result["summary"]["sources_stopped"] == 1
        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["source"] == "Apache Access"
        assert f["status"] == "stopped"
        assert f["severity"] == "critical"
        assert f["age_seconds"] == 1800

    @pytest.mark.asyncio
    async def test_unknown_source_no_records(self, monkeypatch):
        """Source enumerated via schema but absent from the probe result → unknown."""
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([]))  # nothing in window
        schema = _make_schema(["Silent Source"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run()

        assert result["summary"]["sources_unknown"] == 1
        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["source"] == "Silent Source"
        assert f["status"] == "unknown"
        assert f["severity"] == "warn"
        assert f["age_seconds"] is None
        assert f["last_log_ts"] is None

    @pytest.mark.asyncio
    async def test_checked_at_is_included(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([]))
        schema = _make_schema(["Anything"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run()

        assert result["checked_at"] == "2026-04-22T10:00:00+00:00"

    @pytest.mark.asyncio
    async def test_probe_query_uses_configured_window(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([]))
        schema = _make_schema([])
        settings = _make_settings(probe_window="last_4_hours")
        tool = IngestionHealthTool(engine, schema, settings)

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        await tool.run()

        kwargs = engine.execute.call_args.kwargs
        assert kwargs["time_range"] == "last_4_hours"
        assert "stats max('Time') as last_log_ts by 'Log Source'" in kwargs["query"]


class TestSourcesFilter:
    @pytest.mark.asyncio
    async def test_sources_arg_limits_probe_and_skips_enumeration(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        # Probe returns only web; audit was in the request but had no records.
        engine = _make_engine(_probe_result([
            ("web", "2026-04-22T09:59:00Z"),
        ]))
        # Schema returns something wildly different — must NOT be used.
        schema = _make_schema(["UNEXPECTED"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run(
            sources=["web", "audit"],
            severity_filter="all",
        )

        # schema.get_log_sources was not called — caller's list wins.
        schema.get_log_sources.assert_not_called()

        # Probe query filters to the caller's two sources.
        query = engine.execute.call_args.kwargs["query"]
        assert "'Log Source' in ('web', 'audit')" in query

        # Findings cover exactly the caller's two sources, one healthy, one unknown.
        by_source = {f["source"]: f for f in result["findings"]}
        assert set(by_source.keys()) == {"web", "audit"}
        assert by_source["web"]["status"] == "healthy"
        assert by_source["audit"]["status"] == "unknown"
        assert result["summary"] == {
            "sources_healthy": 1,
            "sources_stopped": 0,
            "sources_unknown": 1,
        }

    def test_compose_probe_query_no_filter(self):
        from oci_logan_mcp.ingestion_health import _compose_probe_query
        q = _compose_probe_query(None)
        assert q == "* | stats max('Time') as last_log_ts by 'Log Source'"

    def test_compose_probe_query_with_sources(self):
        from oci_logan_mcp.ingestion_health import _compose_probe_query
        q = _compose_probe_query(["A", "B"])
        assert q == "'Log Source' in ('A', 'B') | stats max('Time') as last_log_ts by 'Log Source'"
