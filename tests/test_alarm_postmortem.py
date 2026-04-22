"""Tests for why_did_this_fire (A6) module."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.alarm_postmortem import (
    WhyDidThisFireTool,
    _parse_pending_duration_seconds,
)
from oci_logan_mcp.tools import get_tools


def _make_tool():
    client = MagicMock()
    engine = MagicMock()
    engine.execute = AsyncMock()
    return WhyDidThisFireTool(client, engine), client, engine


def _logan_alarm(
    *,
    logan_managed: str = "true",
    logan_kind: str = "monitoring_alarm",
    logan_query: str = "'Event' = 'error' | stats count",
    logan_schedule: str = "0 */15 * * *",
    pending_duration: str | None = "PT5M",
) -> dict:
    return {
        "id": "ocid1.alarm.oc1..test",
        "display_name": "Test Alarm",
        "severity": "CRITICAL",
        "is_enabled": True,
        "query": "logan_alert_metric[1m].count() > 0",
        "pending_duration": pending_duration,
        "compartment_id": "ocid1.compartment.oc1..test",
        "freeform_tags": {
            "logan_managed": logan_managed,
            "logan_kind": logan_kind,
            "logan_query": logan_query,
            "logan_schedule": logan_schedule,
            "logan_backing_saved_search_id": "ocid1.savedsearch.oc1..test",
            "logan_backing_metric_task_id": "ocid1.task.oc1..test",
        },
    }


def _query_result(*, columns: list[str], rows: list[list[object]]) -> dict:
    return {
        "source": "live",
        "data": {
            "columns": [{"name": col} for col in columns],
            "rows": rows,
        },
        "metadata": {
            "time_start": "2026-04-23T09:55:00+00:00",
            "time_end": "2026-04-23T10:01:00+00:00",
        },
    }


def test_parse_pending_duration_supports_minutes_and_seconds():
    assert _parse_pending_duration_seconds("PT5M") == 300
    assert _parse_pending_duration_seconds("PT30S") == 30


def test_parse_pending_duration_supports_hours_minutes_seconds():
    assert _parse_pending_duration_seconds("PT1H2M3S") == 3723


def test_parse_pending_duration_rejects_bad_input():
    assert _parse_pending_duration_seconds(None) is None
    assert _parse_pending_duration_seconds("") is None
    assert _parse_pending_duration_seconds("P1D") is None
    assert _parse_pending_duration_seconds("PT") is None


class TestWhyDidThisFireTool:
    @pytest.mark.asyncio
    async def test_rejects_non_logan_managed_alarm(self):
        tool, client, _engine = _make_tool()
        client.get_alarm = AsyncMock(return_value=_logan_alarm(logan_managed="false"))

        result = await tool.run(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time="2026-04-23T10:00:00Z",
        )

        assert result["status"] == "error"
        assert result["error_code"] == "alarm_not_logan_managed"
        assert result["observed_logan_managed"] == "false"

    @pytest.mark.asyncio
    async def test_rejects_alarm_kind_mismatch(self):
        tool, client, _engine = _make_tool()
        client.get_alarm = AsyncMock(return_value=_logan_alarm(logan_kind="alert_saved_search"))

        result = await tool.run(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time="2026-04-23T10:00:00Z",
        )

        assert result["status"] == "error"
        assert result["error_code"] == "alarm_kind_mismatch"
        assert result["observed_logan_kind"] == "alert_saved_search"

    @pytest.mark.asyncio
    async def test_rejects_alarm_missing_query_metadata(self):
        tool, client, _engine = _make_tool()
        alarm = _logan_alarm()
        del alarm["freeform_tags"]["logan_query"]
        client.get_alarm = AsyncMock(return_value=alarm)

        result = await tool.run(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time="2026-04-23T10:00:00Z",
        )

        assert result["status"] == "error"
        assert result["error_code"] == "alarm_missing_query_metadata"

    @pytest.mark.asyncio
    async def test_uses_pending_duration_when_window_before_not_provided(self):
        tool, client, engine = _make_tool()
        client.get_alarm = AsyncMock(return_value=_logan_alarm(pending_duration="PT5M"))
        engine.execute = AsyncMock(side_effect=[
            _query_result(columns=["count"], rows=[[4]]),
            _query_result(
                columns=["Time", "Log Source", "Severity", "Original Log Content"],
                rows=[["2026-04-23T09:59:00Z", "Audit Logs", "ERROR", "bad things"]],
            ),
        ])

        result = await tool.run(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time="2026-04-23T10:00:00Z",
        )

        first_call = engine.execute.await_args_list[0].kwargs
        assert first_call["time_start"] == "2026-04-23T09:55:00+00:00"
        assert first_call["time_end"] == "2026-04-23T10:01:00+00:00"
        assert result["window"]["window_before_seconds"] == 300
        assert result["evaluation"]["pending_duration_seconds"] == 300
        assert result["alarm"]["compartment_id"] == "ocid1.compartment.oc1..test"
        assert first_call["compartment_id"] == "ocid1.compartment.oc1..test"

    @pytest.mark.asyncio
    async def test_invalid_pending_duration_falls_back_to_300_seconds(self):
        tool, client, engine = _make_tool()
        client.get_alarm = AsyncMock(return_value=_logan_alarm(pending_duration="not-a-duration"))
        engine.execute = AsyncMock(side_effect=[
            _query_result(columns=["count"], rows=[[4]]),
            _query_result(
                columns=["Time", "Log Source", "Severity", "Original Log Content"],
                rows=[["2026-04-23T09:59:00Z", "Audit Logs", "ERROR", "bad things"]],
            ),
        ])

        result = await tool.run(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time="2026-04-23T10:00:00Z",
        )

        assert result["window"]["window_before_seconds"] == 300
        assert result["evaluation"]["pending_duration_seconds"] is None

    @pytest.mark.asyncio
    async def test_explicit_window_before_seconds_overrides_pending_duration(self):
        tool, client, engine = _make_tool()
        client.get_alarm = AsyncMock(return_value=_logan_alarm(pending_duration="PT5M"))
        engine.execute = AsyncMock(side_effect=[
            _query_result(columns=["count"], rows=[[4]]),
            _query_result(
                columns=["Time", "Log Source", "Severity", "Original Log Content"],
                rows=[["2026-04-23T09:59:00Z", "Audit Logs", "ERROR", "bad things"]],
            ),
        ])

        result = await tool.run(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time="2026-04-23T10:00:00Z",
            window_before_seconds=120,
        )

        first_call = engine.execute.await_args_list[0].kwargs
        assert first_call["time_start"] == "2026-04-23T09:58:00+00:00"
        assert result["window"]["window_before_seconds"] == 120

    @pytest.mark.asyncio
    async def test_datetime_fire_time_and_window_after_seconds_are_supported(self):
        tool, client, engine = _make_tool()
        client.get_alarm = AsyncMock(return_value=_logan_alarm(pending_duration="PT5M"))
        engine.execute = AsyncMock(side_effect=[
            _query_result(columns=["count"], rows=[[4]]),
            _query_result(
                columns=["Time", "Log Source", "Severity", "Original Log Content"],
                rows=[["2026-04-23T10:01:30Z", "Audit Logs", "ERROR", "bad things"]],
            ),
        ])

        result = await tool.run(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time=datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc),
            window_after_seconds=90,
        )

        first_call = engine.execute.await_args_list[0].kwargs
        assert first_call["time_end"] == "2026-04-23T10:01:30+00:00"
        assert result["window"]["window_after_seconds"] == 90

    @pytest.mark.asyncio
    async def test_degraded_seed_omits_top_rows(self):
        tool, client, engine = _make_tool()
        client.get_alarm = AsyncMock(return_value=_logan_alarm(logan_query="*"))
        engine.execute = AsyncMock(return_value=_query_result(columns=["count"], rows=[[4]]))

        result = await tool.run(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time="2026-04-23T10:00:00Z",
        )

        assert result["seed"]["seed_filter"] == "*"
        assert result["seed"]["seed_filter_degraded"] is True
        assert result["top_contributing_rows"] == []
        assert result["top_contributing_rows_omitted_reason"] == "unscoped_seed_filter"
        assert engine.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_scoped_seed_runs_fixed_top_rows_query(self):
        tool, client, engine = _make_tool()
        client.get_alarm = AsyncMock(return_value=_logan_alarm())
        engine.execute = AsyncMock(side_effect=[
            _query_result(columns=["count"], rows=[[4]]),
            _query_result(
                columns=["Time", "Log Source", "Severity", "Original Log Content"],
                rows=[["2026-04-23T09:59:00Z", "Audit Logs", "ERROR", "bad things"]],
            ),
        ])

        result = await tool.run(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time="2026-04-23T10:00:00Z",
        )

        top_call = engine.execute.await_args_list[1].kwargs
        assert top_call["query"] == (
            "('Event' = 'error') | fields Time, 'Log Source', Severity, "
            "'Original Log Content' | sort -Time | head 50"
        )
        assert result["top_contributing_rows"] == [{
            "time": "2026-04-23T09:59:00+00:00",
            "source": "Audit Logs",
            "severity": "ERROR",
            "message": "bad things",
        }]
        assert result["related_saved_search_id"] == "ocid1.savedsearch.oc1..test"
        assert result["dashboard_id"] is None


class TestWhyDidThisFireSchema:
    def test_schema_present(self):
        names = [tool["name"] for tool in get_tools()]
        assert "why_did_this_fire" in names

    def test_schema_properties(self):
        tool = next(t for t in get_tools() if t["name"] == "why_did_this_fire")
        schema = tool["inputSchema"]
        assert set(schema["required"]) == {"alarm_ocid", "fire_time"}
        assert "window_before_seconds" in schema["properties"]
        assert "window_after_seconds" in schema["properties"]
