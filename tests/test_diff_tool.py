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


class TestReuseBreakout:
    def test_extract_by_clause_single_field(self):
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause("* | stats count by 'Host'") == ["Host"]

    def test_extract_by_clause_multiple_fields(self):
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause("* | stats count by 'Host', 'Status'") == ["Host", "Status"]

    def test_extract_by_clause_unquoted(self):
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause("* | stats count by Host") == ["Host"]

    def test_extract_by_clause_none(self):
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause("* | stats count") == []

    def test_extract_by_clause_ignores_by_in_string_literal(self):
        """`by` inside a filter-value string literal must NOT be extracted.

        Without the pipe-stats anchor in the regex, this would mis-extract "X'"
        from the literal and then silently produce a degenerate delta downstream.
        """
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause("'msg' = 'caused by X' | stats count") == []

    def test_extract_by_clause_multiple_stats_returns_last(self):
        """When a query has multiple stats pipes, reuse the final grouping."""
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause(
            "* | eventstats count as c by Host | stats sum(c) by Source"
        ) == ["Source"]

    @pytest.mark.asyncio
    async def test_reuses_breakout_from_query_by_clause(self):
        current = _grouped_result([("web-01", 400)])
        comparison = _grouped_result([("web-01", 200)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs' | stats count by 'host'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            # dimensions omitted → reuse `by 'host'` from query
        )

        assert result["metadata"]["dimensions"] == ["host"]
        # The user-provided stats is preserved; no double-append.
        composed = engine.execute.call_args_list[0].kwargs["query"]
        assert composed.count("stats count") == 1
        assert result["delta"][0]["dimension"] == "host=web-01"


class TestAsymmetricWindows:
    @pytest.mark.asyncio
    async def test_new_value_in_current_only(self):
        current = _grouped_result([("web-01", 100), ("web-99-new", 50)])
        comparison = _grouped_result([("web-01", 100)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        by_key = {r["dimension"]: r for r in result["delta"]}
        assert by_key["host=web-99-new"] == {
            "dimension": "host=web-99-new",
            "current": 50,
            "comparison": 0,
            "pct_change": None,
            "tag": "new",
        }

    @pytest.mark.asyncio
    async def test_disappeared_value_in_current_only(self):
        current = _grouped_result([("web-01", 100)])
        comparison = _grouped_result([("web-01", 100), ("web-99-gone", 200)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        by_key = {r["dimension"]: r for r in result["delta"]}
        assert by_key["host=web-99-gone"]["tag"] == "disappeared"
        assert by_key["host=web-99-gone"]["pct_change"] == -100.0


class TestSummary:
    @pytest.mark.asyncio
    async def test_summary_prefers_high_volume_change(self):
        # web-01: 10000 → 12000 (+20%, high volume) — should be named
        # web-02: 1 → 5 (+400%, tiny volume) — should NOT dominate
        current = _grouped_result([("web-01", 12000), ("web-02", 5)])
        comparison = _grouped_result([("web-01", 10000), ("web-02", 1)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        # Summary names web-01 before web-02 despite web-02's larger pct.
        idx_01 = result["summary"].find("web-01")
        idx_02 = result["summary"].find("web-02")
        assert idx_01 != -1
        assert idx_01 < idx_02 or idx_02 == -1

    @pytest.mark.asyncio
    async def test_summary_quiet_when_all_stable(self):
        current = _grouped_result([("web-01", 100), ("web-02", 105)])
        comparison = _grouped_result([("web-01", 100), ("web-02", 100)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        assert "no significant change" in result["summary"].lower()
