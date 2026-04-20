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
