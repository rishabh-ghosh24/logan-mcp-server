"""Tests for trace_request_id tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.trace_lookup import TraceRequestIdTool


def _make_result(field_name, rows):
    return {
        "entity": {"type": "custom", "value": "req-42", "field": field_name},
        "by_source": [],
        "cross_source_timeline": rows,
        "stats": {"total_events": len(rows), "sources_matched": len({r["source"] for r in rows})},
        "partial": False,
        "metadata": {},
    }


@pytest.fixture
def tool():
    pivot_tool = MagicMock()
    pivot_tool.run = AsyncMock()
    return TraceRequestIdTool(pivot_tool)


class TestTraceRequestIdTool:
    @pytest.mark.asyncio
    async def test_default_id_fields_are_probed_in_order(self, tool):
        tool._pivot.run = AsyncMock(side_effect=[
            _make_result("Request ID", []),
            _make_result("Trace ID", [{"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "_id": "r1"}]),
            _make_result("traceId", []),
            _make_result("x-request-id", []),
        ])

        await tool.run(request_id="req-42", time_range={"time_range": "last_1_hour"})

        assert [call.kwargs["field_name"] for call in tool._pivot.run.await_args_list] == [
            "Request ID",
            "Trace ID",
            "traceId",
            "x-request-id",
        ]

    @pytest.mark.asyncio
    async def test_custom_id_fields_override_default_order(self, tool):
        tool._pivot.run = AsyncMock(side_effect=[
            _make_result("Trace ID", []),
            _make_result("Request ID", []),
        ])

        await tool.run(
            request_id="req-42",
            time_range={"time_range": "last_1_hour"},
            id_fields=["Trace ID", "Request ID"],
        )

        assert [call.kwargs["field_name"] for call in tool._pivot.run.await_args_list] == [
            "Trace ID",
            "Request ID",
        ]

    @pytest.mark.asyncio
    async def test_unknown_field_errors_are_soft_misses(self, tool):
        tool._pivot.run = AsyncMock(side_effect=[
            RuntimeError("Unknown field name: Trace ID"),
            _make_result(
                "traceId",
                [{"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "_id": "r1"}],
            ),
        ])

        result = await tool.run(
            request_id="req-42",
            time_range={"time_range": "last_1_hour"},
            id_fields=["Trace ID", "traceId"],
        )

        assert result["events"][0]["_id"] == "r1"
        assert result["sources_matched"] == ["App Logs"]

    @pytest.mark.asyncio
    async def test_results_merge_and_sort_events_across_candidate_fields(self, tool):
        tool._pivot.run = AsyncMock(side_effect=[
            _make_result(
                "Request ID",
                [{"Time": "2026-04-23T10:02:00Z", "source": "Audit Logs", "_id": "r2"}],
            ),
            _make_result(
                "Trace ID",
                [{"Time": "2026-04-23T10:01:00Z", "source": "App Logs", "_id": "r1"}],
            ),
        ])

        result = await tool.run(
            request_id="req-42",
            time_range={"time_range": "last_1_hour"},
            id_fields=["Request ID", "Trace ID"],
        )

        assert [event["_id"] for event in result["events"]] == ["r1", "r2"]
        assert result["sources_matched"] == ["App Logs", "Audit Logs"]

    @pytest.mark.asyncio
    async def test_record_id_dedup_beats_timestamp_message_collisions(self, tool):
        tool._pivot.run = AsyncMock(side_effect=[
            _make_result(
                "Request ID",
                [
                    {"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "_id": "r1", "Message": "started"},
                    {"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "_id": "r2", "Message": "started"},
                ],
            ),
        ])

        result = await tool.run(
            request_id="req-42",
            time_range={"time_range": "last_1_hour"},
            id_fields=["Request ID"],
        )

        assert [event["_id"] for event in result["events"]] == ["r1", "r2"]

    @pytest.mark.asyncio
    async def test_fallback_fingerprint_keeps_distinct_rows_with_same_timestamp_message(self, tool):
        tool._pivot.run = AsyncMock(side_effect=[
            _make_result(
                "Request ID",
                [
                    {"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "Message": "started", "Thread": "t1"},
                    {"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "Message": "started", "Thread": "t2"},
                ],
            ),
        ])

        result = await tool.run(
            request_id="req-42",
            time_range={"time_range": "last_1_hour"},
            id_fields=["Request ID"],
        )

        assert [event["Thread"] for event in result["events"]] == ["t1", "t2"]

    @pytest.mark.asyncio
    async def test_sources_matched_ignores_queried_sources_with_zero_events(self, tool):
        tool._pivot.run = AsyncMock(side_effect=[
            {
                "entity": {"type": "custom", "value": "req-42", "field": "Request ID"},
                "by_source": [{"source": "Audit Logs", "rows": [], "truncated": False}],
                "cross_source_timeline": [],
                "stats": {"total_events": 0, "sources_matched": 0},
                "partial": False,
                "metadata": {"sources_queried": ["Audit Logs"]},
            },
            _make_result(
                "Trace ID",
                [{"Time": "2026-04-23T10:00:00Z", "source": "App Logs", "_id": "r1"}],
            ),
        ])

        result = await tool.run(
            request_id="req-42",
            time_range={"time_range": "last_1_hour"},
            id_fields=["Request ID", "Trace ID"],
        )

        assert result["sources_matched"] == ["App Logs"]
