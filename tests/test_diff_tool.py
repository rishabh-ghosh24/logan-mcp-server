"""Tests for diff_time_windows tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.diff_tool import DiffTool


def _make_engine(results):
    """Create a mock QueryEngine whose execute() returns `results` items in order."""
    engine = MagicMock()
    engine.execute = AsyncMock(side_effect=list(results))
    return engine


def _count_result(count: int) -> dict:
    """Shape a QueryEngine response around a single scalar count."""
    return {
        "source": "live",
        "data": {
            "columns": [{"name": "count"}],
            "rows": [[count]],
        },
        "metadata": {},
    }


class TestScalarDelta:
    @pytest.mark.asyncio
    async def test_identical_windows_produce_zero_delta(self):
        engine = _make_engine([_count_result(100), _count_result(100)])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},  # same label → same data (mocked)
        )

        assert result["current"]["total"] == 100
        assert result["comparison"]["total"] == 100
        # Spec: identical windows yield an empty delta (only significant rows surface).
        assert result["delta"] == []
        assert "no significant change" in result["summary"].lower()

    @pytest.mark.asyncio
    async def test_double_volume_yields_100_pct_delta(self):
        engine = _make_engine([_count_result(200), _count_result(100)])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_start": "2026-04-19T10:00:00Z", "time_end": "2026-04-19T11:00:00Z"},
        )

        assert result["current"]["total"] == 200
        assert result["comparison"]["total"] == 100
        assert result["delta"][0]["pct_change"] == pytest.approx(100.0)
        assert result["delta"][0]["tag"] == "spike"


def _grouped_result(rows_by_dim):
    """Shape a QueryEngine response around grouped rows (dim_val, count)."""
    return {
        "source": "live",
        "data": {
            "columns": [{"name": "host"}, {"name": "count"}],
            "rows": [[k, v] for k, v in rows_by_dim],
        },
        "metadata": {},
    }


class TestDimensionedDelta:
    @pytest.mark.asyncio
    async def test_dimensions_join_on_key(self):
        current = _grouped_result([("web-01", 400), ("web-02", 100)])
        comparison = _grouped_result([("web-01", 200), ("web-02", 100)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        composed = engine.execute.call_args_list[0].kwargs["query"]
        assert "stats count as count by 'host'" in composed

        by_key = {r["dimension"]: r for r in result["delta"]}
        # web-01 is a spike (present). web-02 is stable, therefore filtered out of `delta`.
        assert by_key["host=web-01"]["pct_change"] == pytest.approx(100.0)
        assert by_key["host=web-01"]["tag"] == "spike"
        assert "host=web-02" not in by_key
