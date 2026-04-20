"""Tests for pivot_on_entity tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.pivot_tool import PivotTool, ENTITY_FIELD_MAP


def _make_engine(side_effects):
    """Mock QueryEngine whose execute() returns side_effects in order."""
    engine = MagicMock()
    engine.execute = AsyncMock(side_effect=list(side_effects))
    return engine


def _discovery_result(sources_counts):
    """QueryEngine response for a discovery query: [(source_name, count), ...]."""
    return {
        "source": "live",
        "data": {
            "columns": [{"name": "Log Source"}, {"name": "count"}],
            "rows": [[src, cnt] for src, cnt in sources_counts],
        },
        "metadata": {},
    }


def _source_result(column_names, rows):
    """QueryEngine response for a per-source query."""
    return {
        "source": "live",
        "data": {
            "columns": [{"name": n} for n in column_names],
            "rows": rows,
        },
        "metadata": {},
    }


class TestFieldResolution:
    def test_known_entity_types_resolve_to_field_names(self):
        assert ENTITY_FIELD_MAP["host"] == "Host"
        assert ENTITY_FIELD_MAP["user"] == "User"
        assert ENTITY_FIELD_MAP["request_id"] == "Request ID"
        assert ENTITY_FIELD_MAP["ip"] == "IP Address"

    def test_resolve_field_returns_mapped_name(self):
        assert PivotTool._resolve_field("host", None) == "Host"
        assert PivotTool._resolve_field("user", None) == "User"

    def test_unknown_entity_type_raises(self):
        with pytest.raises(ValueError, match="Unknown entity_type"):
            PivotTool._resolve_field("database", None)

    def test_custom_without_field_name_raises(self):
        with pytest.raises(ValueError, match="field_name is required"):
            PivotTool._resolve_field("custom", None)

    def test_custom_with_field_name_returns_it(self):
        assert PivotTool._resolve_field("custom", "Request ID") == "Request ID"


class TestSourceDiscovery:
    @pytest.mark.asyncio
    async def test_discovery_returns_sources_with_data(self):
        discovery = _discovery_result([("Audit Logs", 500), ("Web Logs", 30)])
        engine = _make_engine([discovery])
        tool = PivotTool(engine)

        sources = await tool._discover_sources("Host", "web-01", {"time_range": "last_1_hour"})

        assert sources == ["Audit Logs", "Web Logs"]
        call_kwargs = engine.execute.call_args.kwargs
        assert "'Host' = 'web-01' | stats count by 'Log Source'" == call_kwargs["query"]
        assert call_kwargs["time_range"] == "last_1_hour"

    @pytest.mark.asyncio
    async def test_discovery_excludes_zero_count_sources(self):
        discovery = _discovery_result([("Audit Logs", 5), ("Empty Source", 0)])
        engine = _make_engine([discovery])
        tool = PivotTool(engine)

        sources = await tool._discover_sources("Host", "web-01", {"time_range": "last_1_hour"})

        assert sources == ["Audit Logs"]
        assert "Empty Source" not in sources

    @pytest.mark.asyncio
    async def test_discovery_no_log_source_column_returns_empty(self):
        # Discovery query returns columns without 'Log Source' (unusual, but defensive)
        no_src_col = {
            "source": "live",
            "data": {"columns": [{"name": "count"}], "rows": [[42]]},
            "metadata": {},
        }
        engine = _make_engine([no_src_col])
        tool = PivotTool(engine)

        sources = await tool._discover_sources("Host", "web-01", {"time_range": "last_1_hour"})

        assert sources == []


class TestPerSourceQuerying:
    @pytest.mark.asyncio
    async def test_query_sources_returns_rows_per_source(self):
        audit_res = _source_result(
            ["Time", "Host", "Event"],
            [["2026-04-20T10:00:00Z", "web-01", "login"], ["2026-04-20T10:01:00Z", "web-01", "logout"]],
        )
        web_res = _source_result(
            ["Time", "Host", "Status"],
            [["2026-04-20T10:00:30Z", "web-01", "200"]],
        )
        engine = _make_engine([audit_res, web_res])
        tool = PivotTool(engine)

        by_source, partial = await tool._query_sources(
            sources=["Audit Logs", "Web Access Logs"],
            field="Host",
            value="web-01",
            time_range={"time_range": "last_1_hour"},
            max_rows=100,
        )

        assert partial is False
        assert len(by_source) == 2
        assert by_source[0]["source"] == "Audit Logs"
        assert len(by_source[0]["rows"]) == 2
        assert by_source[0]["rows"][0]["Host"] == "web-01"
        assert by_source[1]["source"] == "Web Access Logs"

    @pytest.mark.asyncio
    async def test_extract_rows_converts_to_dicts(self):
        res = _source_result(["Time", "Host", "count"], [["2026-04-20T10:00:00Z", "web-01", 42]])
        rows = PivotTool._extract_rows(res)
        assert rows == [{"Time": "2026-04-20T10:00:00Z", "Host": "web-01", "count": 42}]

    @pytest.mark.asyncio
    async def test_truncated_flag_when_rows_fill_max(self):
        # 3 rows returned, max_rows=3 → truncated=True
        rows = [["2026-04-20T10:0%d:00Z" % i, "web-01", str(i)] for i in range(3)]
        res = _source_result(["Time", "Host", "Status"], rows)
        engine = _make_engine([res])
        tool = PivotTool(engine)

        by_source, _ = await tool._query_sources(
            sources=["Web Logs"],
            field="Host",
            value="web-01",
            time_range={"time_range": "last_1_hour"},
            max_rows=3,
        )

        assert by_source[0]["truncated"] is True

    @pytest.mark.asyncio
    async def test_query_uses_log_source_and_field_filter(self):
        engine = _make_engine([_source_result(["Time"], [])])
        tool = PivotTool(engine)

        await tool._query_sources(
            sources=["Audit Logs"],
            field="User",
            value="alice",
            time_range={"time_range": "last_1_hour"},
            max_rows=100,
        )

        issued_query = engine.execute.call_args.kwargs["query"]
        assert "'Log Source' = 'Audit Logs'" in issued_query
        assert "'User' = 'alice'" in issued_query


class TestTimelineBuilding:
    def test_timeline_merges_and_sorts_by_timestamp(self):
        by_source = [
            {
                "source": "Audit Logs",
                "rows": [
                    {"Time": "2026-04-20T10:00:00Z", "Event": "login"},
                    {"Time": "2026-04-20T10:02:00Z", "Event": "logout"},
                ],
                "truncated": False,
            },
            {
                "source": "Web Logs",
                "rows": [
                    {"Time": "2026-04-20T10:01:00Z", "Status": "200"},
                ],
                "truncated": False,
            },
        ]

        timeline = PivotTool._build_timeline(by_source)

        timestamps = [e["timestamp"] for e in timeline]
        assert timestamps == sorted(timestamps)
        assert timeline[0]["source"] == "Audit Logs"
        assert timeline[1]["source"] == "Web Logs"
        assert timeline[2]["source"] == "Audit Logs"

    def test_timeline_events_include_source_and_row_fields(self):
        by_source = [
            {
                "source": "Audit Logs",
                "rows": [{"Time": "2026-04-20T10:00:00Z", "Host": "web-01"}],
                "truncated": False,
            }
        ]

        timeline = PivotTool._build_timeline(by_source)

        assert timeline[0]["source"] == "Audit Logs"
        assert timeline[0]["timestamp"] == "2026-04-20T10:00:00Z"
        assert timeline[0]["Host"] == "web-01"

    def test_timeline_rows_without_timestamp_sort_last(self):
        by_source = [
            {
                "source": "S1",
                "rows": [
                    {"Time": None, "Event": "no-ts"},
                    {"Time": "2026-04-20T10:00:00Z", "Event": "has-ts"},
                ],
                "truncated": False,
            }
        ]

        timeline = PivotTool._build_timeline(by_source)

        assert timeline[0]["Event"] == "has-ts"
        assert timeline[1]["Event"] == "no-ts"


class TestBudgetHandling:
    @pytest.mark.asyncio
    async def test_budget_exceeded_on_first_source_returns_partial(self):
        from oci_logan_mcp.budget_tracker import BudgetExceededError

        discovery = _discovery_result([("Audit Logs", 100), ("Web Logs", 50)])
        engine = _make_engine([
            discovery,
            BudgetExceededError("bytes limit hit"),  # Audit Logs query raises
        ])
        tool = PivotTool(engine)

        result = await tool.run(
            entity_type="host",
            entity_value="web-01",
            time_range={"time_range": "last_1_hour"},
        )

        # No source results because the first one exceeded budget
        assert result["partial"] is True
        assert result["by_source"] == []
        assert result["stats"]["total_events"] == 0

    @pytest.mark.asyncio
    async def test_budget_exceeded_mid_pivot_returns_completed_sources(self):
        from oci_logan_mcp.budget_tracker import BudgetExceededError

        discovery = _discovery_result([
            ("Audit Logs", 100),
            ("Web Logs", 50),
            ("System Logs", 25),
        ])
        audit_rows = _source_result(["Time", "Host"], [["2026-04-20T10:00:00Z", "web-01"]])
        engine = _make_engine([
            discovery,
            audit_rows,
            BudgetExceededError("bytes limit hit"),  # Web Logs raises
        ])
        tool = PivotTool(engine)

        result = await tool.run(
            entity_type="host",
            entity_value="web-01",
            time_range={"time_range": "last_1_hour"},
        )

        assert result["partial"] is True
        # Audit Logs completed before budget hit
        assert len(result["by_source"]) == 1
        assert result["by_source"][0]["source"] == "Audit Logs"
        # System Logs was never attempted (aborted after Web Logs raised)
        source_names = [s["source"] for s in result["by_source"]]
        assert "System Logs" not in source_names


class TestRunIntegration:
    @pytest.mark.asyncio
    async def test_run_two_sources_returns_full_result(self):
        discovery = _discovery_result([("Audit Logs", 10), ("Web Logs", 5)])
        audit_rows = _source_result(
            ["Time", "Host", "Event"],
            [["2026-04-20T10:01:00Z", "web-01", "login"]],
        )
        web_rows = _source_result(
            ["Time", "Host", "Status"],
            [["2026-04-20T10:00:00Z", "web-01", "200"]],
        )
        engine = _make_engine([discovery, audit_rows, web_rows])
        tool = PivotTool(engine)

        result = await tool.run(
            entity_type="host",
            entity_value="web-01",
            time_range={"time_range": "last_1_hour"},
        )

        assert result["entity"] == {"type": "host", "value": "web-01", "field": "Host"}
        assert len(result["by_source"]) == 2
        assert result["stats"]["total_events"] == 2
        assert result["stats"]["sources_matched"] == 2
        assert result["partial"] is False
        # Timeline is time-sorted: Web log (10:00) before Audit (10:01)
        assert result["cross_source_timeline"][0]["source"] == "Web Logs"
        assert result["cross_source_timeline"][1]["source"] == "Audit Logs"

    @pytest.mark.asyncio
    async def test_run_nonexistent_entity_returns_empty(self):
        # Discovery finds no sources
        discovery = _discovery_result([])
        engine = _make_engine([discovery])
        tool = PivotTool(engine)

        result = await tool.run(
            entity_type="host",
            entity_value="ghost-host",
            time_range={"time_range": "last_1_hour"},
        )

        assert result["stats"]["total_events"] == 0
        assert result["stats"]["sources_matched"] == 0
        assert result["cross_source_timeline"] == []
        assert result["by_source"] == []
        assert result["partial"] is False
