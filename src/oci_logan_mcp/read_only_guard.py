"""Read-only mode enforcement for MCP tool calls.

The denylist enumerated here is the single source of truth for what counts as
a mutating operation. A tool is mutating if it changes state in OCI, on disk
(user state files), or on an external system (Slack, Telegram).

Any new tool registered in handlers.handle_tool_call must be classified — see
the drift-catching test in tests/test_read_only_guard.py.
"""

MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        # Session / context mutations
        "set_compartment",
        "set_namespace",
        "update_tenancy_context",
        # User state writes
        "setup_confirmation_secret",
        "save_learned_query",
        "remember_preference",
        # OCI Monitoring alarms
        "create_alert",
        "update_alert",
        "delete_alert",
        # OCI Log Analytics saved searches
        "create_saved_search",
        "update_saved_search",
        "delete_saved_search",
        # OCI Management Dashboards
        "create_dashboard",
        "add_dashboard_tile",
        "delete_dashboard",
        # Outbound notifications
        "send_to_slack",
        "send_to_telegram",
    }
)


class ReadOnlyError(Exception):
    """Raised when a mutating tool is invoked under read-only mode."""


def raise_if_read_only(tool_name: str, *, read_only: bool) -> None:
    """Raise ReadOnlyError if read_only is True and tool_name is mutating."""
    if read_only and tool_name in MUTATING_TOOLS:
        raise ReadOnlyError(
            f"Tool '{tool_name}' is blocked because the server is running in "
            f"read-only mode. Restart without --read-only (or unset "
            f"OCI_LOGAN_MCP_READ_ONLY) to enable mutating operations."
        )
