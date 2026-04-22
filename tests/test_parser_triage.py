"""Tests for parser_failure_triage tool."""

import pytest

from oci_logan_mcp.parser_triage import _build_stats_query, _build_samples_query


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
