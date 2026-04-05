"""Tests for tool definitions — confirmation params and destructive flags."""

from oci_logan_mcp.tools import get_tools
from oci_logan_mcp.confirmation import GUARDED_TOOLS


def test_guarded_tools_have_confirmation_params():
    """All guarded tools must have confirmation_token and confirmation_secret in schema."""
    tools = {t["name"]: t for t in get_tools()}
    for name in GUARDED_TOOLS:
        props = tools[name]["inputSchema"]["properties"]
        assert "confirmation_token" in props, f"{name} missing confirmation_token"
        assert "confirmation_secret" in props, f"{name} missing confirmation_secret"
        # Confirmation params must NOT be required
        assert "confirmation_token" not in tools[name]["inputSchema"].get("required", [])
        assert "confirmation_secret" not in tools[name]["inputSchema"].get("required", [])


def test_guarded_tools_have_destructive_flag():
    """Guarded tools are flagged with destructive=True metadata."""
    tools = {t["name"]: t for t in get_tools()}
    for name in GUARDED_TOOLS:
        assert tools[name].get("destructive") is True, (
            f"{name} missing destructive=True flag"
        )
    # Non-guarded tools should not have the flag
    assert tools.get("run_query", {}).get("destructive") is not True
    assert tools.get("create_alert", {}).get("destructive") is not True
    assert tools.get("list_alerts", {}).get("destructive") is not True


def test_guarded_tools_description_mentions_confirmation():
    """All guarded tool descriptions mention TWO-FACTOR CONFIRMATION."""
    tools = {t["name"]: t for t in get_tools()}
    for name in GUARDED_TOOLS:
        desc = tools[name]["description"]
        assert "TWO-FACTOR CONFIRMATION" in desc, (
            f"{name} description missing TWO-FACTOR CONFIRMATION"
        )
