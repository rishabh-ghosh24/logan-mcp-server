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
