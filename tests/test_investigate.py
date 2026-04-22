"""Tests for investigate_incident (A1) module."""
import pytest

from oci_logan_mcp.investigate import _extract_seed_filter, _compose_source_scoped_query


class TestExtractSeedFilter:
    def test_empty_query_returns_wildcard(self):
        assert _extract_seed_filter("") == "*"

    def test_bare_wildcard_returns_wildcard(self):
        assert _extract_seed_filter("*") == "*"

    def test_whitespace_wildcard_returns_wildcard(self):
        assert _extract_seed_filter("   *   ") == "*"

    def test_filter_only_returns_filter(self):
        assert _extract_seed_filter("'Event' = 'error'") == "'Event' = 'error'"

    def test_filter_with_trailing_stats_pipe(self):
        assert _extract_seed_filter("'Event' = 'error' | stats count") == "'Event' = 'error'"

    def test_filter_with_trailing_where_pipe_dropped(self):
        # Later pipeline stages (where/eval/etc.) are dropped for drill-down scoping.
        assert _extract_seed_filter(
            "'Event' = 'error' | where X = 'y'"
        ) == "'Event' = 'error'"

    def test_pipe_inside_single_quoted_literal_preserved(self):
        # Pipes inside a quoted string literal must not terminate the filter.
        q = "'Log Source' = 'http://x.com/a|b' | stats count"
        assert _extract_seed_filter(q) == "'Log Source' = 'http://x.com/a|b'"

    def test_doubled_single_quote_escape(self):
        # OCI LA escapes embedded quotes by doubling: 'O''Brien'.
        q = "'User' = 'O''Brien' | stats count"
        assert _extract_seed_filter(q) == "'User' = 'O''Brien'"

    def test_doubled_double_quote_escape(self):
        # Symmetrical with test_doubled_single_quote_escape: OCI LA's
        # doubled-quote escape works for double-quoted literals too.
        q = '"Field" = "val""ue" | stats count'
        assert _extract_seed_filter(q) == '"Field" = "val""ue"'

    def test_pipe_inside_double_quoted_literal_preserved(self):
        q = '"Field" = "val|with|pipes" | stats count'
        assert _extract_seed_filter(q) == '"Field" = "val|with|pipes"'

    def test_source_pinned_seed_returns_unchanged(self):
        # User who pre-scopes to one source gets it respected verbatim.
        assert _extract_seed_filter("'Log Source' = 'X'") == "'Log Source' = 'X'"


class TestComposeSourceScopedQuery:
    def test_wildcard_seed_omits_and_and_parens(self):
        # "*" means no seed scoping — emit just the source predicate.
        q = _compose_source_scoped_query("*", "Apache Access", "cluster | sort -Count | head 3")
        assert q == "'Log Source' = 'Apache Access' | cluster | sort -Count | head 3"

    def test_simple_seed_gets_parens(self):
        q = _compose_source_scoped_query("'Event' = 'error'", "Apache", "cluster")
        assert q == "('Event' = 'error') and 'Log Source' = 'Apache' | cluster"

    def test_boolean_precedence_safety_with_or(self):
        # Precedence regression: without parens, `or` would escape the source predicate.
        q = _compose_source_scoped_query(
            "'Severity' = 'ERROR' or 'Level' = 'FATAL'",
            "Apache Access",
            "cluster",
        )
        assert "(" in q and ")" in q
        assert "(\'Severity\' = 'ERROR' or 'Level' = 'FATAL')" in q
        assert "and 'Log Source' = 'Apache Access'" in q

    def test_source_with_embedded_single_quote_escaped(self):
        q = _compose_source_scoped_query("*", "Bob's Logs", "stats count")
        # Embedded single quote is doubled per OCI LA grammar.
        assert "'Bob''s Logs'" in q

    def test_seed_already_source_pinned_still_wraps(self):
        # User pre-scoped to source Y; we still wrap in parens and append
        # the current source. The resulting `('Log Source' = 'Y') and 'Log Source' = 'X'`
        # correctly returns no rows for X != Y (desired).
        q = _compose_source_scoped_query("'Log Source' = 'Y'", "X", "cluster")
        assert q == "('Log Source' = 'Y') and 'Log Source' = 'X' | cluster"
