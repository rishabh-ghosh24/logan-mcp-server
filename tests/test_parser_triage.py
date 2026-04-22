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
    def test_stats_query_includes_failure_source_filter(self):
        q = _build_stats_query(top_n=10)
        assert "'Log Source' = 'Parser Failure'" in q

    def test_stats_query_aggregates_count_first_last(self):
        q = _build_stats_query(top_n=10)
        assert "stats count as failure_count" in q
        assert "earliest('Time') as first_seen" in q
        assert "latest('Time') as last_seen" in q
        assert "by 'Parser Name', 'Log Source'" in q

    def test_stats_query_sorts_and_limits_to_top_n(self):
        q = _build_stats_query(top_n=7)
        assert "sort -failure_count" in q
        assert "| head 7" in q

    def test_samples_query_filters_to_given_parser_names(self):
        q = _build_samples_query(["Apache Parser", "Syslog"])
        assert "'Log Source' = 'Parser Failure'" in q
        assert "'Parser Name' in ('Apache Parser', 'Syslog')" in q
        assert "'Original Log Content'" in q

    def test_samples_query_limits_to_3x_parser_count(self):
        q = _build_samples_query(["A", "B", "C"])  # 3 parsers × 3 = 9
        assert "| head 9" in q

    def test_samples_query_escapes_embedded_single_quotes(self):
        q = _build_samples_query(["Bob's Parser"])
        assert "'Bob''s Parser'" in q


# ---------------------------------------------------------------------------
# Helpers shared by parser tests
# ---------------------------------------------------------------------------

def _stats_resp(rows):
    return {
        "data": {
            "columns": [
                {"name": "Parser Name"}, {"name": "Log Source"},
                {"name": "failure_count"}, {"name": "first_seen"}, {"name": "last_seen"},
            ],
            "rows": rows,
        }
    }


def _samples_resp(rows):
    return {
        "data": {
            "columns": [
                {"name": "Parser Name"}, {"name": "Original Log Content"},
            ],
            "rows": rows,
        }
    }


class TestResponseParsers:
    def test_parse_stats_basic(self):
        resp = _stats_resp([
            ["Apache Parser", "Apache Access", 42,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        result = _parse_stats_response(resp)
        assert len(result) == 1
        r = result[0]
        assert r["parser_name"] == "Apache Parser"
        assert r["source"] == "Apache Access"
        assert r["failure_count"] == 42
        assert r["first_seen"] == "2026-04-22T00:00:00Z"
        assert r["last_seen"] == "2026-04-22T09:00:00Z"

    def test_parse_stats_empty_response(self):
        result = _parse_stats_response({"data": {"columns": [], "rows": []}})
        assert result == []

    def test_parse_stats_missing_data_key(self):
        result = _parse_stats_response({})
        assert result == []

    def test_parse_samples_groups_by_parser(self):
        resp = _samples_resp([
            ["Apache Parser", "raw1"],
            ["Apache Parser", "raw2"],
            ["Apache Parser", "raw3"],
            ["Apache Parser", "raw4"],  # 4th → capped at 3
            ["Syslog Parser", "syslog_raw"],
        ])
        samples = _parse_samples_response(resp)
        assert samples["Apache Parser"] == ["raw1", "raw2", "raw3"]
        assert samples["Syslog Parser"] == ["syslog_raw"]

    def test_parse_samples_empty_response(self):
        result = _parse_samples_response({"data": {"columns": [], "rows": []}})
        assert result == {}

    def test_merge_results_attaches_samples(self):
        stats = [{
            "parser_name": "Apache Parser",
            "source": "Apache Access",
            "failure_count": 10,
            "first_seen": "2026-04-22T00:00:00Z",
            "last_seen": "2026-04-22T09:00:00Z",
        }]
        samples = {"Apache Parser": ["line1", "line2"]}
        result = _merge_results(stats, samples)
        assert result[0]["sample_raw_lines"] == ["line1", "line2"]

    def test_merge_results_empty_samples_for_parser_with_no_raw_data(self):
        stats = [{
            "parser_name": "Silent Parser",
            "source": "X",
            "failure_count": 5,
            "first_seen": "2026-04-22T00:00:00Z",
            "last_seen": "2026-04-22T09:00:00Z",
        }]
        result = _merge_results(stats, {})
        assert result[0]["sample_raw_lines"] == []

    def test_parse_stats_none_failure_count_defaults_to_zero(self):
        resp = _stats_resp([
            ["Apache Parser", "Apache Access", None,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        result = _parse_stats_response(resp)
        assert result[0]["failure_count"] == 0

    def test_parse_samples_none_raw_line_becomes_empty_string(self):
        resp = _samples_resp([["Parser A", None]])
        samples = _parse_samples_response(resp)
        assert samples["Parser A"] == [""]

    def test_merge_results_preserves_all_stats_fields(self):
        stats = [{
            "parser_name": "Apache Parser",
            "source": "Apache Access",
            "failure_count": 10,
            "first_seen": "2026-04-22T00:00:00Z",
            "last_seen": "2026-04-22T09:00:00Z",
        }]
        result = _merge_results(stats, {"Apache Parser": ["line1"]})
        r = result[0]
        assert r["parser_name"] == "Apache Parser"
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
            ["Apache Parser", "Apache Access", 42,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
            ["Syslog Parser", "Linux Syslog", 7,
             "2026-04-22T01:00:00Z", "2026-04-22T08:00:00Z"],
        ])
        samples_resp = _samples_resp([
            ["Apache Parser", "malformed line 1"],
            ["Apache Parser", "malformed line 2"],
            ["Syslog Parser", "bad syslog line"],
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run(time_range="last_24h", top_n=20)

        assert result["total_failure_count"] == 49  # 42 + 7
        assert len(result["failures"]) == 2
        by_parser = {f["parser_name"]: f for f in result["failures"]}
        assert by_parser["Apache Parser"]["failure_count"] == 42
        assert by_parser["Apache Parser"]["sample_raw_lines"] == [
            "malformed line 1", "malformed line 2",
        ]
        assert by_parser["Syslog Parser"]["sample_raw_lines"] == ["bad syslog line"]

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

        await tool.run(time_range="last_4_hours", top_n=5)

        kwargs = engine.execute.call_args.kwargs
        assert kwargs["time_range"] == "last_4_hours"
        assert "| head 5" in kwargs["query"]

    @pytest.mark.asyncio
    async def test_run_samples_capped_at_three_per_parser(self):
        stats_resp = _stats_resp([
            ["Noisy Parser", "Source A", 100,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        # Engine returns 5 sample lines for the one parser.
        samples_resp = _samples_resp([
            ["Noisy Parser", f"raw line {i}"] for i in range(5)
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run()

        assert len(result["failures"][0]["sample_raw_lines"]) == 3
