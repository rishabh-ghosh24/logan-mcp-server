"""Tests for parser_failure_triage tool."""

import pytest

from oci_logan_mcp.parser_triage import (
    _build_stats_query,
    _build_samples_query,
    _parse_stats_response,
    _parse_samples_response,
    _merge_results,
    ParserTriageTool,
)


class TestQueryBuilders:
    def test_stats_query_uses_parse_failed_filter(self):
        q = _build_stats_query(top_n=10)
        assert "'Parse Failed' = 1" in q

    def test_stats_query_aggregates_count_first_last(self):
        q = _build_stats_query(top_n=10)
        assert "stats count as failure_count" in q
        assert "earliest('Time') as first_seen" in q
        assert "latest('Time') as last_seen" in q
        # One source = one parser in OCI LA, so grouping by 'Log Source' is
        # equivalent to grouping by parser. There is no 'Parser Name' field.
        assert "by 'Log Source'" in q

    def test_stats_query_sorts_and_limits_to_top_n(self):
        q = _build_stats_query(top_n=7)
        assert "sort -failure_count" in q
        assert "| head 7" in q

    def test_samples_query_filters_to_given_sources(self):
        q = _build_samples_query(["Kubernetes Kubelet Logs", "Linux Syslog"])
        assert "'Parse Failed' = 1" in q
        assert "'Log Source' in ('Kubernetes Kubelet Logs', 'Linux Syslog')" in q
        assert "fields 'Log Source', 'Original Log Content'" in q

    def test_samples_query_limits_to_3x_source_count(self):
        q = _build_samples_query(["A", "B", "C"])  # 3 sources × 3 = 9
        assert "| head 9" in q

    def test_samples_query_escapes_embedded_single_quotes(self):
        q = _build_samples_query(["Bob's Source"])
        assert "'Bob''s Source'" in q


# ---------------------------------------------------------------------------
# Helpers shared by parser tests
# ---------------------------------------------------------------------------

def _stats_resp(rows):
    return {
        "data": {
            "columns": [
                {"name": "Log Source"},
                {"name": "failure_count"},
                {"name": "first_seen"},
                {"name": "last_seen"},
            ],
            "rows": rows,
        }
    }


def _samples_resp(rows):
    return {
        "data": {
            "columns": [
                {"name": "Log Source"},
                {"name": "Original Log Content"},
            ],
            "rows": rows,
        }
    }


class TestResponseParsers:
    def test_parse_stats_basic(self):
        # OCI returns TIMESTAMP columns as epoch-millisecond ints — verified live.
        # 1776743266000 ms = 2026-04-21T03:47:46+00:00 UTC
        # 1776829209000 ms = 2026-04-22T03:40:09+00:00 UTC
        resp = _stats_resp([
            ["Kubernetes Kubelet Logs", 42,
             1776743266000, 1776829209000],
        ])
        result = _parse_stats_response(resp)
        assert len(result) == 1
        r = result[0]
        assert r["source"] == "Kubernetes Kubelet Logs"
        assert r["failure_count"] == 42
        # Parser converts epoch-ms to ISO-8601 UTC string.
        assert r["first_seen"] == "2026-04-21T03:47:46+00:00"
        assert r["last_seen"] == "2026-04-22T03:40:09+00:00"
        assert "parser_name" not in r

    def test_parse_stats_accepts_iso_string_timestamps(self):
        """Cache/replay paths may still surface ISO strings; parse them too."""
        resp = _stats_resp([
            ["Apache Access", 5, "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        result = _parse_stats_response(resp)
        assert result[0]["first_seen"] == "2026-04-22T00:00:00+00:00"
        assert result[0]["last_seen"] == "2026-04-22T09:00:00+00:00"

    def test_parse_stats_short_row_skipped(self):
        """A row shorter than the header must not raise IndexError."""
        resp = _stats_resp([["only-source"]])
        result = _parse_stats_response(resp)
        assert result == []

    def test_parse_stats_empty_response(self):
        result = _parse_stats_response({"data": {"columns": [], "rows": []}})
        assert result == []

    def test_parse_stats_missing_data_key(self):
        result = _parse_stats_response({})
        assert result == []

    def test_parse_stats_none_failure_count_defaults_to_zero(self):
        resp = _stats_resp([
            ["Apache Access", None,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        result = _parse_stats_response(resp)
        assert result[0]["failure_count"] == 0

    def test_parse_samples_groups_by_source(self):
        resp = _samples_resp([
            ["Apache Access", "raw1"],
            ["Apache Access", "raw2"],
            ["Apache Access", "raw3"],
            ["Apache Access", "raw4"],  # 4th → capped at 3
            ["Linux Syslog", "syslog_raw"],
        ])
        samples = _parse_samples_response(resp)
        assert samples["Apache Access"] == ["raw1", "raw2", "raw3"]
        assert samples["Linux Syslog"] == ["syslog_raw"]

    def test_parse_samples_none_raw_line_becomes_empty_string(self):
        resp = _samples_resp([["Apache Access", None]])
        samples = _parse_samples_response(resp)
        assert samples["Apache Access"] == [""]

    def test_parse_samples_empty_response(self):
        result = _parse_samples_response({"data": {"columns": [], "rows": []}})
        assert result == {}

    def test_merge_results_attaches_samples(self):
        stats = [{
            "source": "Apache Access",
            "failure_count": 10,
            "first_seen": "2026-04-22T00:00:00Z",
            "last_seen": "2026-04-22T09:00:00Z",
        }]
        samples = {"Apache Access": ["line1", "line2"]}
        result = _merge_results(stats, samples)
        assert result[0]["sample_raw_lines"] == ["line1", "line2"]

    def test_merge_results_empty_samples_for_source_with_no_raw_data(self):
        stats = [{
            "source": "Silent Source",
            "failure_count": 5,
            "first_seen": "2026-04-22T00:00:00Z",
            "last_seen": "2026-04-22T09:00:00Z",
        }]
        result = _merge_results(stats, {})
        assert result[0]["sample_raw_lines"] == []

    def test_merge_results_preserves_all_stats_fields(self):
        stats = [{
            "source": "Apache Access",
            "failure_count": 10,
            "first_seen": "2026-04-22T00:00:00Z",
            "last_seen": "2026-04-22T09:00:00Z",
        }]
        result = _merge_results(stats, {"Apache Access": ["line1"]})
        r = result[0]
        assert r["source"] == "Apache Access"
        assert r["failure_count"] == 10
        assert r["first_seen"] == "2026-04-22T00:00:00Z"
        assert r["last_seen"] == "2026-04-22T09:00:00Z"
        assert r["sample_raw_lines"] == ["line1"]


# ---------------------------------------------------------------------------
# Orchestration fixtures
# ---------------------------------------------------------------------------

def _make_engine(*responses):
    """Mock engine whose execute() returns responses in order."""
    from unittest.mock import AsyncMock, MagicMock
    engine = MagicMock()
    engine.execute = AsyncMock(side_effect=list(responses))
    return engine


class TestOrchestration:
    @pytest.mark.asyncio
    async def test_run_aggregates_failures_and_attaches_samples(self):
        stats_resp = _stats_resp([
            ["Apache Access", 42,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
            ["Linux Syslog", 7,
             "2026-04-22T01:00:00Z", "2026-04-22T08:00:00Z"],
        ])
        samples_resp = _samples_resp([
            ["Apache Access", "malformed line 1"],
            ["Apache Access", "malformed line 2"],
            ["Linux Syslog", "bad syslog line"],
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run(time_range="last_24_hours", top_n=20)

        assert result["total_failure_count"] == 49  # 42 + 7
        assert len(result["failures"]) == 2
        by_source = {f["source"]: f for f in result["failures"]}
        assert by_source["Apache Access"]["failure_count"] == 42
        assert by_source["Apache Access"]["sample_raw_lines"] == [
            "malformed line 1", "malformed line 2",
        ]
        assert by_source["Linux Syslog"]["sample_raw_lines"] == ["bad syslog line"]

    @pytest.mark.asyncio
    async def test_run_empty_result_skips_samples_query(self):
        engine = _make_engine(_stats_resp([]))
        tool = ParserTriageTool(engine)

        result = await tool.run()

        assert result["total_failure_count"] == 0
        assert result["failures"] == []
        # samples query must NOT be called when stats are empty
        assert engine.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_run_passes_time_range_and_top_n_to_engine(self):
        engine = _make_engine(_stats_resp([]))
        tool = ParserTriageTool(engine)

        await tool.run(time_range="last_7_days", top_n=5)

        kwargs = engine.execute.call_args.kwargs
        assert kwargs["time_range"] == "last_7_days"
        assert "| head 5" in kwargs["query"]

    @pytest.mark.asyncio
    async def test_run_samples_capped_at_three_per_source(self):
        stats_resp = _stats_resp([
            ["Noisy Source", 100,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        # Engine returns 5 sample lines for the one source.
        samples_resp = _samples_resp([
            ["Noisy Source", f"raw line {i}"] for i in range(5)
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run()

        assert len(result["failures"][0]["sample_raw_lines"]) == 3

    @pytest.mark.asyncio
    async def test_run_samples_budget_exceeded_returns_partial_stats(self):
        """Stats succeeded + samples ran out of budget → keep stats, empty samples, flag partial."""
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        from unittest.mock import AsyncMock, MagicMock
        stats_resp = _stats_resp([
            ["Apache Access", 42,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        engine = MagicMock()
        engine.execute = AsyncMock(
            side_effect=[stats_resp, BudgetExceededError("bytes limit hit")]
        )
        tool = ParserTriageTool(engine)

        result = await tool.run()

        assert result["partial"] is True
        assert result["partial_reason"] == "samples_budget_exceeded"
        assert result["total_failure_count"] == 42
        assert len(result["failures"]) == 1
        assert result["failures"][0]["source"] == "Apache Access"
        assert result["failures"][0]["sample_raw_lines"] == []

    @pytest.mark.asyncio
    async def test_run_propagates_budget_error_when_stats_fails(self):
        """Stats query budget failure propagates — no partial stats to return."""
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        from unittest.mock import AsyncMock, MagicMock
        engine = MagicMock()
        engine.execute = AsyncMock(side_effect=BudgetExceededError("bytes limit hit"))
        tool = ParserTriageTool(engine)

        with pytest.raises(BudgetExceededError):
            await tool.run()

    @pytest.mark.asyncio
    async def test_run_success_omits_partial_flag(self):
        """Normal success path does not include partial/partial_reason keys."""
        stats_resp = _stats_resp([
            ["Apache Access", 1,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        samples_resp = _samples_resp([["Apache Access", "line"]])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run()

        assert "partial" not in result
        assert "partial_reason" not in result

    @pytest.mark.asyncio
    async def test_run_skewed_distribution_may_yield_zero_samples_for_later_sources(self):
        """Documents the known limitation: the global head cap does not guarantee
        samples for every source. If one source dominates the first head rows,
        others receive no samples. Acceptable per spec ('up to 3 samples')."""
        stats_resp = _stats_resp([
            ["Dominant Source", 200,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
            ["Quiet Source", 1,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        samples_resp = _samples_resp([
            ["Dominant Source", f"dom line {i}"] for i in range(6)
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run()

        by_source = {f["source"]: f for f in result["failures"]}
        assert len(by_source["Dominant Source"]["sample_raw_lines"]) == 3
        assert by_source["Quiet Source"]["sample_raw_lines"] == []


    @pytest.mark.asyncio
    async def test_run_forwards_compartment_id_to_engine(self):
        """ParserTriageTool.run must thread compartment_id to every engine.execute call,
        otherwise callers scoping A1 to a non-default compartment get a mismatch."""
        stats_resp = _stats_resp([
            ["Apache Access", 10, "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        samples_resp = _samples_resp([["Apache Access", "line"]])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        await tool.run(time_range="last_24_hours", top_n=5, compartment_id="ocid1.custom")

        # Both the stats and samples engine calls must carry the same compartment_id.
        for call in engine.execute.await_args_list:
            assert call.kwargs.get("compartment_id") == "ocid1.custom"

    @pytest.mark.asyncio
    async def test_run_compartment_id_defaults_to_none(self):
        """Backward compatibility: existing callers without compartment_id still work."""
        stats_resp = _stats_resp([])
        engine = _make_engine(stats_resp)
        tool = ParserTriageTool(engine)

        await tool.run()

        assert engine.execute.await_args.kwargs.get("compartment_id") is None


class TestToolSchema:
    def test_parser_failure_triage_schema_present(self):
        from oci_logan_mcp.tools import get_tools
        names = [t["name"] for t in get_tools()]
        assert "parser_failure_triage" in names

    def test_parser_failure_triage_schema_properties(self):
        from oci_logan_mcp.tools import get_tools
        tool = next(t for t in get_tools() if t["name"] == "parser_failure_triage")
        props = tool["inputSchema"]["properties"]
        assert "time_range" in props
        assert "top_n" in props
