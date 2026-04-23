"""Tests for the read-only guard."""

import pytest

from oci_logan_mcp.read_only_guard import (
    MUTATING_TOOLS,
    ReadOnlyError,
    raise_if_read_only,
)


def test_mutating_tools_is_frozenset():
    assert isinstance(MUTATING_TOOLS, frozenset)


def test_mutating_tools_contains_known_writers():
    expected_subset = {
        "set_compartment",
        "set_namespace",
        "update_tenancy_context",
        "setup_confirmation_secret",
        "save_learned_query",
        "remember_preference",
        "create_alert",
        "update_alert",
        "delete_alert",
        "create_saved_search",
        "update_saved_search",
        "delete_saved_search",
        "create_dashboard",
        "add_dashboard_tile",
        "delete_dashboard",
        "send_to_slack",
        "send_to_telegram",
    }
    assert expected_subset <= MUTATING_TOOLS


def test_mutating_tools_excludes_readers():
    readers = {
        "run_query",
        "run_saved_search",
        "list_fields",
        "list_saved_searches",
        "validate_query",
        "visualize",
        "get_current_context",
        "export_results",
    }
    assert readers.isdisjoint(MUTATING_TOOLS)


def test_raise_if_read_only_allows_non_mutating_when_enabled():
    # Should NOT raise
    raise_if_read_only("run_query", read_only=True)


def test_raise_if_read_only_allows_everything_when_disabled():
    raise_if_read_only("delete_alert", read_only=False)


def test_raise_if_read_only_blocks_mutating_when_enabled():
    with pytest.raises(ReadOnlyError) as exc:
        raise_if_read_only("delete_alert", read_only=True)
    assert "delete_alert" in str(exc.value)
    assert "read-only" in str(exc.value).lower()


def test_read_only_error_is_exception_subclass():
    assert issubclass(ReadOnlyError, Exception)


def test_all_registered_tools_are_classified():
    """Every tool dispatched in handle_tool_call must be either in
    MUTATING_TOOLS or in the known-readers allowlist below.

    If this test fails: you added a new handler. Either add it to
    MUTATING_TOOLS in read_only_guard.py, or add it to KNOWN_READERS below.
    """
    import ast
    import pathlib

    handlers_src = (
        pathlib.Path(__file__).parent.parent / "src" / "oci_logan_mcp" / "handlers.py"
    ).read_text()
    tree = ast.parse(handlers_src)

    # Locate the `handlers = {...}` Assign statement inside handle_tool_call
    # specifically — do NOT grab the first dict in the function, which could
    # match an unrelated literal added later.
    registered: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_tool_call"):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Assign)
                and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and sub.targets[0].id == "handlers"
                and isinstance(sub.value, ast.Dict)
            ):
                for key in sub.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        registered.add(key.value)
                break
        break

    assert registered, (
        "Could not locate `handlers = {...}` assignment inside handle_tool_call. "
        "If the registry was refactored, update this test."
    )

    KNOWN_READERS = {
        "list_log_sources", "list_fields", "list_entities", "list_parsers",
        "list_labels", "list_saved_searches", "list_log_groups",
        "validate_query", "run_query", "run_saved_search", "run_batch_queries",
        "diff_time_windows",
        "pivot_on_entity",
        "ingestion_health",
        "parser_failure_triage",
        "investigate_incident",
        "why_did_this_fire",
        "find_rare_events",
        "trace_request_id",
        "related_dashboards_and_searches",
        "visualize", "export_results",
        "get_current_context", "list_compartments",
        "test_connection", "find_compartment",
        "get_query_examples", "get_log_summary",
        "get_preferences", "list_alerts", "list_dashboards",
        "explain_query", "get_session_budget",
        "export_transcript",
    }

    unclassified = registered - MUTATING_TOOLS - KNOWN_READERS
    assert not unclassified, (
        f"Unclassified tools: {sorted(unclassified)}. "
        "Add each to MUTATING_TOOLS (in read_only_guard.py) or KNOWN_READERS (in this test)."
    )
