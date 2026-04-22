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
        # With `'Parse Failed' = 1` as the filter, 'Log Source' in the by-clause
        # identifies the actual source each failed line came from.
        assert "by 'Parser Name', 'Log Source'" in q

    def test_stats_query_sorts_and_limits_to_top_n(self):
        q = _build_stats_query(top_n=7)
        assert "sort -failure_count" in q
        assert "| head 7" in q

    def test_samples_query_filters_to_given_parser_source_pairs(self):
        q = _build_samples_query([("Apache Parser", "Apache Access"), ("Syslog", "Linux Syslog")])
        assert "'Parse Failed' = 1" in q
        # Parser names AND Log Sources both appear in the filter
        assert "'Parser Name' in ('Apache Parser', 'Syslog')" in q
        assert "'Log Source' in ('Apache Access', 'Linux Syslog')" in q
        # The samples query fetches Log Source too so the parser can disambiguate
        # which source each raw line came from.
        assert "fields 'Parser Name', 'Log Source', 'Original Log Content'" in q

    def test_samples_query_limits_to_3x_pair_count(self):
        # 3 pairs × 3 samples each = 9
        q = _build_samples_query([("A", "S1"), ("B", "S2"), ("C", "S3")])
        assert "| head 9" in q

    def test_samples_query_escapes_embedded_single_quotes_in_parser_and_source(self):
        q = _build_samples_query([("Bob's Parser", "Rick's Source")])
        assert "'Bob''s Parser'" in q
        assert "'Rick''s Source'" in q


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
                {"name": "Parser Name"}, {"name": "Log Source"},
                {"name": "Original Log Content"},
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

    def test_parse_samples_groups_by_parser_and_source(self):
        resp = _samples_resp([
            ["Apache Parser", "Apache Access", "raw1"],
            ["Apache Parser", "Apache Access", "raw2"],
            ["Apache Parser", "Apache Access", "raw3"],
            ["Apache Parser", "Apache Access", "raw4"],  # 4th → capped at 3
            ["Syslog Parser", "Linux Syslog", "syslog_raw"],
        ])
        samples = _parse_samples_response(resp)
        assert samples[("Apache Parser", "Apache Access")] == ["raw1", "raw2", "raw3"]
        assert samples[("Syslog Parser", "Linux Syslog")] == ["syslog_raw"]

    def test_parse_samples_same_parser_different_sources_isolated(self):
        """Samples from the same parser attached to different sources must not bleed."""
        resp = _samples_resp([
            ["Apache Parser", "Source A", "a_line_1"],
            ["Apache Parser", "Source A", "a_line_2"],
            ["Apache Parser", "Source B", "b_line_1"],
        ])
        samples = _parse_samples_response(resp)
        assert samples[("Apache Parser", "Source A")] == ["a_line_1", "a_line_2"]
        assert samples[("Apache Parser", "Source B")] == ["b_line_1"]

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
        samples = {("Apache Parser", "Apache Access"): ["line1", "line2"]}
        result = _merge_results(stats, samples)
        assert result[0]["sample_raw_lines"] == ["line1", "line2"]

    def test_merge_results_isolates_samples_per_source_for_shared_parser(self):
        """A parser shared across sources must get per-source samples, not a mixed pool."""
        stats = [
            {"parser_name": "P", "source": "A", "failure_count": 5,
             "first_seen": "2026-04-22T00:00:00Z", "last_seen": "2026-04-22T09:00:00Z"},
            {"parser_name": "P", "source": "B", "failure_count": 3,
             "first_seen": "2026-04-22T01:00:00Z", "last_seen": "2026-04-22T08:00:00Z"},
        ]
        samples = {
            ("P", "A"): ["lineA1", "lineA2"],
            ("P", "B"): ["lineB1"],
        }
        result = _merge_results(stats, samples)
        by_source = {r["source"]: r for r in result}
        assert by_source["A"]["sample_raw_lines"] == ["lineA1", "lineA2"]
        assert by_source["B"]["sample_raw_lines"] == ["lineB1"]

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
        resp = _samples_resp([["Parser A", "Source X", None]])
        samples = _parse_samples_response(resp)
        assert samples[("Parser A", "Source X")] == [""]

    def test_merge_results_preserves_all_stats_fields(self):
        stats = [{
            "parser_name": "Apache Parser",
            "source": "Apache Access",
            "failure_count": 10,
            "first_seen": "2026-04-22T00:00:00Z",
            "last_seen": "2026-04-22T09:00:00Z",
        }]
        result = _merge_results(stats, {("Apache Parser", "Apache Access"): ["line1"]})
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
            ["Apache Parser", "Apache Access", "malformed line 1"],
            ["Apache Parser", "Apache Access", "malformed line 2"],
            ["Syslog Parser", "Linux Syslog", "bad syslog line"],
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run(time_range="last_24_hours", top_n=20)

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
            ["Noisy Parser", "Source A", f"raw line {i}"] for i in range(5)
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run()

        assert len(result["failures"][0]["sample_raw_lines"]) == 3

    @pytest.mark.asyncio
    async def test_run_skewed_distribution_may_yield_zero_samples_for_later_parsers(self):
        """Documents the known limitation: the global head cap does not guarantee
        samples for every parser. If one parser dominates the first head rows, others
        receive no samples. This is acceptable per spec ('up to 3 samples')."""
        stats_resp = _stats_resp([
            ["Dominant Parser", "Source A", 200,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
            ["Quiet Parser", "Source B", 1,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        # Engine returns only Dominant Parser rows — Quiet Parser gets nothing.
        samples_resp = _samples_resp([
            ["Dominant Parser", "Source A", f"dom line {i}"] for i in range(6)
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run()

        by_parser = {f["parser_name"]: f for f in result["failures"]}
        assert len(by_parser["Dominant Parser"]["sample_raw_lines"]) == 3
        # Quiet Parser received no rows in the global cap — this is expected behaviour.
        assert by_parser["Quiet Parser"]["sample_raw_lines"] == []

    @pytest.mark.asyncio
    async def test_run_same_parser_attached_to_multiple_sources_isolates_samples(self):
        """A parser attached to >1 sources must get per-source samples, not a mixed pool.

        Regression test for the bug where samples were keyed on parser name
        alone and both stats rows for the same parser received the same mixed
        sample set.
        """
        stats_resp = _stats_resp([
            ["Shared Parser", "Source A", 10,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
            ["Shared Parser", "Source B", 5,
             "2026-04-22T01:00:00Z", "2026-04-22T08:00:00Z"],
        ])
        samples_resp = _samples_resp([
            ["Shared Parser", "Source A", "a_line_1"],
            ["Shared Parser", "Source A", "a_line_2"],
            ["Shared Parser", "Source B", "b_line_1"],
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run()

        by_source = {f["source"]: f for f in result["failures"]}
        assert by_source["Source A"]["sample_raw_lines"] == ["a_line_1", "a_line_2"]
        assert by_source["Source B"]["sample_raw_lines"] == ["b_line_1"]


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
