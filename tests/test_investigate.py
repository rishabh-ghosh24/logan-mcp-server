"""Tests for investigate_incident (A1) module."""
from datetime import datetime, timedelta, timezone

import pytest

from oci_logan_mcp.investigate import _extract_seed_filter, _compose_source_scoped_query, _compute_windows, _templated_summary


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


class TestComputeWindows:
    def test_equal_length_and_zero_gap_adjacency(self):
        anchor = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        current, comparison = _compute_windows("last_1_hour", anchor)
        # Zero-gap: comparison.end == current.start
        assert comparison["time_end"] == current["time_start"]
        # Equal length
        c_start = datetime.fromisoformat(current["time_start"])
        c_end = datetime.fromisoformat(current["time_end"])
        p_start = datetime.fromisoformat(comparison["time_start"])
        p_end = datetime.fromisoformat(comparison["time_end"])
        assert (c_end - c_start) == (p_end - p_start) == timedelta(hours=1)

    def test_current_ends_at_anchor(self):
        anchor = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        current, _ = _compute_windows("last_1_hour", anchor)
        assert current["time_end"] == anchor.isoformat()

    def test_comparison_starts_at_anchor_minus_2_delta(self):
        anchor = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        _, comparison = _compute_windows("last_1_hour", anchor)
        expected = (anchor - timedelta(hours=2)).isoformat()
        assert comparison["time_start"] == expected

    def test_supports_longer_ranges(self):
        anchor = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        current, comparison = _compute_windows("last_7_days", anchor)
        c_start = datetime.fromisoformat(current["time_start"])
        c_end = datetime.fromisoformat(current["time_end"])
        assert (c_end - c_start) == timedelta(days=7)
        assert comparison["time_end"] == current["time_start"]

    def test_unknown_time_range_raises(self):
        with pytest.raises(ValueError, match="Unknown time_range"):
            _compute_windows("last_42_years", datetime(2026, 4, 22, tzinfo=timezone.utc))


from oci_logan_mcp.investigate import _rank_anomalous_sources


from oci_logan_mcp.investigate import _select_top_entities


def _entity_resp(field_name: str, rows):
    """Shape an engine response for `| stats count as n by '{field}'`."""
    return {
        "data": {
            "columns": [{"name": field_name}, {"name": "n"}],
            "rows": rows,
        }
    }


class TestSelectTopEntities:
    def test_parses_entity_rows(self):
        resp = _entity_resp("Host Name (Server)", [
            ["web-01", 50],
            ["web-02", 30],
            ["web-03", 10],
        ])
        entities = _select_top_entities(resp, "host", "Host Name (Server)")
        assert entities == [
            {"entity_type": "host", "entity_value": "web-01", "count": 50},
            {"entity_type": "host", "entity_value": "web-02", "count": 30},
            {"entity_type": "host", "entity_value": "web-03", "count": 10},
        ]

    def test_empty_response_returns_empty(self):
        resp = {"data": {"columns": [], "rows": []}}
        assert _select_top_entities(resp, "host", "Host Name (Server)") == []

    def test_missing_data_key_returns_empty(self):
        assert _select_top_entities({}, "user", "User Name") == []

    def test_missing_field_column_returns_empty(self):
        # Response doesn't contain the expected field column.
        resp = _entity_resp("Wrong Name", [["x", 1]])
        assert _select_top_entities(resp, "host", "Host Name (Server)") == []

    def test_none_entity_value_skipped(self):
        resp = _entity_resp("Host Name (Server)", [
            [None, 99],
            ["real-host", 1],
        ])
        entities = _select_top_entities(resp, "host", "Host Name (Server)")
        assert entities == [
            {"entity_type": "host", "entity_value": "real-host", "count": 1},
        ]

    def test_null_count_defaults_to_zero(self):
        resp = _entity_resp("Host Name (Server)", [["x", None]])
        entities = _select_top_entities(resp, "host", "Host Name (Server)")
        assert entities == [
            {"entity_type": "host", "entity_value": "x", "count": 0},
        ]


from oci_logan_mcp.investigate import _merge_cross_source_timeline


class TestMergeCrossSourceTimeline:
    def test_merges_and_sorts_by_time(self):
        per_source = {
            "A": [
                {"time": "2026-04-22T10:00:00+00:00", "severity": "error", "message": "A2"},
                {"time": "2026-04-22T09:00:00+00:00", "severity": "warn",  "message": "A1"},
            ],
            "B": [
                {"time": "2026-04-22T09:30:00+00:00", "severity": None, "message": "B1"},
            ],
        }
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert out == [
            {"time": "2026-04-22T09:00:00+00:00", "source": "A", "severity": "warn",  "message": "A1"},
            {"time": "2026-04-22T09:30:00+00:00", "source": "B", "severity": None,    "message": "B1"},
            {"time": "2026-04-22T10:00:00+00:00", "source": "A", "severity": "error", "message": "A2"},
        ]

    def test_cap_enforced(self):
        per_source = {
            "X": [
                {"time": f"2026-04-22T10:{i:02d}:00+00:00", "severity": None, "message": f"m{i}"}
                for i in range(60)
            ]
        }
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert len(out) == 50

    def test_null_source_timeline_skipped(self):
        per_source = {"A": None, "B": [{"time": "2026-04-22T10:00:00+00:00", "severity": None, "message": "B"}]}
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert out == [{"time": "2026-04-22T10:00:00+00:00", "source": "B", "severity": None, "message": "B"}]

    def test_all_null_returns_none(self):
        # All sources had their timeline dropped. Distinguish from empty-success.
        per_source = {"A": None, "B": None}
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert out is None

    def test_all_empty_returns_empty_list(self):
        # All sources ran cleanly but produced no rows. Not the same as null.
        per_source = {"A": [], "B": []}
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert out == []

    def test_empty_input_returns_none(self):
        assert _merge_cross_source_timeline({}, cap=50) is None


def _diff_delta(*entries):
    """Build a DiffTool-shaped delta list. Each entry is
    (dimension, current, comparison, pct_change)."""
    return [
        {"dimension": d, "current": cur, "comparison": comp, "pct_change": pct}
        for d, cur, comp, pct in entries
    ]


class TestRankAnomalousSources:
    def test_sort_by_absolute_pct_change_desc(self):
        delta = _diff_delta(
            ("A", 100, 100, 0.0),
            ("B", 200, 100, 100.0),
            ("C", 0, 100, -100.0),
            ("D", 150, 100, 50.0),
        )
        ranked = _rank_anomalous_sources(delta, stopped_sources=set(), top_k=3)
        sources = [r["source"] for r in ranked]
        assert sources == ["B", "C", "D"]  # |100|, |-100|, |50|

    def test_top_k_trims_result(self):
        delta = _diff_delta(
            ("A", 10, 0, 1000.0),
            ("B", 20, 0, 2000.0),
            ("C", 30, 0, 3000.0),
            ("D", 40, 0, 4000.0),
        )
        ranked = _rank_anomalous_sources(delta, stopped_sources=set(), top_k=2)
        assert len(ranked) == 2

    def test_stopped_sources_excluded(self):
        delta = _diff_delta(
            ("Broken", 999, 0, 9999.0),     # highest delta but stopped
            ("Working", 100, 0, 100.0),
        )
        ranked = _rank_anomalous_sources(delta, stopped_sources={"Broken"}, top_k=5)
        assert [r["source"] for r in ranked] == ["Working"]

    def test_zero_comparison_handles_infinite_pct(self):
        # pct_change may be None or an inf marker when comparison=0. Rank
        # falls back to absolute current count in that case.
        delta = [
            {"dimension": "NewSource", "current": 500, "comparison": 0, "pct_change": None},
            {"dimension": "OldSource", "current": 10, "comparison": 5, "pct_change": 100.0},
        ]
        ranked = _rank_anomalous_sources(delta, stopped_sources=set(), top_k=2)
        assert ranked[0]["source"] == "NewSource"

    def test_ranked_entries_preserve_shape(self):
        delta = _diff_delta(("A", 50, 10, 400.0))
        ranked = _rank_anomalous_sources(delta, stopped_sources=set(), top_k=1)
        r = ranked[0]
        assert r["source"] == "A"
        assert r["current_count"] == 50
        assert r["comparison_count"] == 10
        assert r["pct_change"] == 400.0

    def test_empty_delta_returns_empty(self):
        assert _rank_anomalous_sources([], stopped_sources=set(), top_k=3) == []


class TestTemplatedSummary:
    def test_clean_report(self):
        acc = {
            "seed": {"seed_filter": "'Event' = 'error'", "seed_filter_degraded": False, "time_range": "last_1_hour"},
            "ingestion_health": {"snapshot": {"summary": {"sources_stopped": 0}}},
            "parser_failures": {"total_failure_count": 0},
            "anomalous_sources": [
                {"source": "Apache", "pct_change": 150.0},
                {"source": "Syslog", "pct_change": 80.0},
            ],
            "partial_reasons": set(),
        }
        s = _templated_summary(acc)
        assert "'Event' = 'error'" in s
        assert "last_1_hour" in s
        assert "Apache" in s
        assert "partial" not in s.lower()

    def test_degraded_seed_mentions_unscoped(self):
        acc = {
            "seed": {"seed_filter": "*", "seed_filter_degraded": True, "time_range": "last_1_hour"},
            "ingestion_health": {"snapshot": {"summary": {"sources_stopped": 0}}},
            "parser_failures": {"total_failure_count": 0},
            "anomalous_sources": [],
            "partial_reasons": set(),
        }
        s = _templated_summary(acc)
        assert "unscoped" in s.lower()

    def test_partial_appended(self):
        acc = {
            "seed": {"seed_filter": "'x' = 'y'", "seed_filter_degraded": False, "time_range": "last_1_hour"},
            "ingestion_health": {"snapshot": {"summary": {"sources_stopped": 0}}},
            "parser_failures": {"total_failure_count": 0},
            "anomalous_sources": [],
            "partial_reasons": {"budget_exceeded", "timeline_omitted"},
        }
        s = _templated_summary(acc)
        assert "partial" in s.lower()
        assert "budget_exceeded" in s
        assert "timeline_omitted" in s

    def test_stopped_and_parser_failures_mentioned(self):
        acc = {
            "seed": {"seed_filter": "*", "seed_filter_degraded": True, "time_range": "last_1_hour"},
            "ingestion_health": {"snapshot": {"summary": {"sources_stopped": 2}}},
            "parser_failures": {"total_failure_count": 500},
            "anomalous_sources": [],
            "partial_reasons": set(),
        }
        s = _templated_summary(acc)
        assert "2" in s and "stopped" in s.lower()
        assert "500" in s and ("parse" in s.lower() or "parser" in s.lower())


from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.investigate import InvestigateIncidentTool


def _make_engine():
    engine = MagicMock()
    engine.execute = AsyncMock(return_value={"data": {"columns": [], "rows": []}})
    return engine


def _make_deps(schema_sources=None, ih_result=None, j2_result=None):
    """Stub out J1, J2, A2 tool instances the orchestrator composes."""
    from unittest.mock import MagicMock, AsyncMock
    schema = MagicMock()
    schema.get_log_sources = AsyncMock(return_value=[{"name": n} for n in (schema_sources or [])])
    ih_tool = MagicMock()
    ih_tool.run = AsyncMock(return_value=ih_result or {
        "summary": {"sources_healthy": 0, "sources_stopped": 0, "sources_unknown": 0},
        "findings": [],
        "checked_at": "2026-04-22T10:00:00+00:00",
        "metadata": {},
    })
    j2_tool = MagicMock()
    j2_tool.run = AsyncMock(return_value=j2_result or {"failures": [], "total_failure_count": 0})
    diff_tool = MagicMock()
    diff_tool.run = AsyncMock(return_value={"current": {}, "comparison": {}, "delta": [], "summary": "no change"})
    return schema, ih_tool, j2_tool, diff_tool


def _make_settings():
    from oci_logan_mcp.config import Settings
    return Settings()


def _make_budget():
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    return BudgetTracker(
        session_id="test",
        limits=BudgetLimits(enabled=False, max_queries_per_session=100,
                            max_bytes_per_session=0, max_cost_usd_per_session=0),
    )


class TestInvestigateSkeleton:
    @pytest.mark.asyncio
    async def test_minimal_report_shape(self):
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="'Event' = 'error'", time_range="last_1_hour", top_k=3)

        # Minimal required keys regardless of phase content.
        for key in ("summary", "seed", "ingestion_health", "parser_failures",
                    "anomalous_sources", "cross_source_timeline", "next_steps",
                    "budget", "partial", "partial_reasons", "elapsed_seconds"):
            assert key in report, f"missing {key}"

    @pytest.mark.asyncio
    async def test_seed_section_populated(self):
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="'Event' = 'error' | stats count", time_range="last_1_hour", top_k=3)
        assert report["seed"]["query"] == "'Event' = 'error' | stats count"
        assert report["seed"]["seed_filter"] == "'Event' = 'error'"
        assert report["seed"]["seed_filter_degraded"] is False
        assert report["seed"]["time_range"] == "last_1_hour"

    @pytest.mark.asyncio
    async def test_wildcard_seed_sets_degraded(self):
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        assert report["seed"]["seed_filter_degraded"] is True

    @pytest.mark.asyncio
    async def test_budget_exception_returns_partial_report_not_raised(self):
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=BudgetExceededError("over"))
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )

        # Must NOT raise; must return a partial InvestigationReport instead.
        report = await tool.run(query="'x' = 'y'", time_range="last_1_hour", top_k=3)
        assert report["partial"] is True
        assert "budget_exceeded" in report["partial_reasons"]


class TestPhase1SeedExecution:
    @pytest.mark.asyncio
    async def test_seed_query_executed_with_time_range(self):
        engine = _make_engine()
        engine.execute = AsyncMock(return_value={"data": {"columns": [{"name": "n"}], "rows": [[5]]}})
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        await tool.run(query="'Event' = 'error'", time_range="last_1_hour", top_k=3, compartment_id="ocid1.test")

        # The seed query is executed once as-is (phase 1).
        calls = [c for c in engine.execute.await_args_list
                 if c.kwargs.get("query") == "'Event' = 'error'"]
        assert len(calls) == 1
        assert calls[0].kwargs.get("time_range") == "last_1_hour"
        assert calls[0].kwargs.get("compartment_id") == "ocid1.test"


class TestPhase2IngestionHealth:
    @pytest.mark.asyncio
    async def test_j1_invoked_with_all_severity(self):
        ih_result = {
            "summary": {"sources_healthy": 2, "sources_stopped": 1, "sources_unknown": 0},
            "findings": [
                {"source": "Broken", "status": "stopped", "severity": "critical"},
            ],
            "checked_at": "2026-04-22T10:00:00+00:00",
            "metadata": {},
        }
        schema, ih, j2, diff = _make_deps(ih_result=ih_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3, compartment_id="ocid1.xyz")
        ih.run.assert_awaited_once()
        kwargs = ih.run.await_args.kwargs
        assert kwargs["severity_filter"] == "all"
        assert kwargs["compartment_id"] == "ocid1.xyz"

    @pytest.mark.asyncio
    async def test_report_wraps_j1_with_probe_window_and_note(self):
        schema, ih, j2, diff = _make_deps()
        settings = _make_settings()
        settings.ingestion_health.freshness_probe_window = "last_1_hour"
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=settings, budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_7_days", top_k=3)
        ih_section = report["ingestion_health"]
        assert ih_section["probe_window"] == "last_1_hour"
        assert "probe window" in ih_section["note"].lower()
        assert "snapshot" in ih_section


class TestPhase3ParserFailures:
    @pytest.mark.asyncio
    async def test_j2_invoked_with_time_range(self):
        j2_result = {"failures": [{"source": "X", "failure_count": 42}], "total_failure_count": 42}
        schema, ih, j2, diff = _make_deps(j2_result=j2_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_24_hours", top_k=3)

        j2.run.assert_awaited_once()
        kwargs = j2.run.await_args.kwargs
        assert kwargs["time_range"] == "last_24_hours"
        assert kwargs["top_n"] == 10
        assert report["parser_failures"]["total_failure_count"] == 42


class TestPhase4AnomalyRanking:
    @pytest.mark.asyncio
    async def test_diff_query_is_seed_scoped_by_log_source(self):
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        await tool.run(query="'Event' = 'error'", time_range="last_1_hour", top_k=3)

        diff.run.assert_awaited_once()
        kwargs = diff.run.await_args.kwargs
        assert kwargs["query"] == "'Event' = 'error' | stats count as n by 'Log Source'"
        # Both windows are absolute timestamps (anchored).
        assert "time_start" in kwargs["current_window"]
        assert "time_end" in kwargs["current_window"]
        assert "time_start" in kwargs["comparison_window"]
        assert "time_end" in kwargs["comparison_window"]
        assert kwargs["current_window"]["time_start"] == kwargs["comparison_window"]["time_end"]

    @pytest.mark.asyncio
    async def test_anomalous_sources_populated_from_diff_delta(self):
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [
                {"dimension": "A", "current": 100, "comparison": 50, "pct_change": 100.0},
                {"dimension": "B", "current": 30, "comparison": 60, "pct_change": -50.0},
                {"dimension": "C", "current": 15, "comparison": 10, "pct_change": 50.0},
                {"dimension": "D", "current": 5, "comparison": 5, "pct_change": 0.0},
            ],
        }
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        srcs = [s["source"] for s in report["anomalous_sources"]]
        assert srcs == ["A", "B", "C"]  # by |pct_change|: 100, 50, 50

    @pytest.mark.asyncio
    async def test_stopped_sources_excluded_from_ranking(self):
        ih_result = {
            "summary": {"sources_healthy": 1, "sources_stopped": 1, "sources_unknown": 0},
            "findings": [{"source": "Broken", "status": "stopped", "severity": "critical"}],
            "checked_at": "2026-04-22T10:00:00+00:00", "metadata": {},
        }
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [
                {"dimension": "Broken", "current": 999, "comparison": 0, "pct_change": 9999.0},
                {"dimension": "Working", "current": 10, "comparison": 5, "pct_change": 100.0},
            ],
        }
        schema, ih, j2, diff = _make_deps(ih_result=ih_result)
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        srcs = [s["source"] for s in report["anomalous_sources"]]
        assert "Broken" not in srcs
        assert "Working" in srcs
