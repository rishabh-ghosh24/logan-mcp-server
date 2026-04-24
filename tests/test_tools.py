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
    assert tools.get("list_alerts", {}).get("destructive") is not True
    assert tools.get("list_saved_searches", {}).get("destructive") is not True


def test_guarded_tools_description_mentions_confirmation():
    """All guarded tool descriptions mention TWO-FACTOR CONFIRMATION."""
    tools = {t["name"]: t for t in get_tools()}
    for name in GUARDED_TOOLS:
        desc = tools[name]["description"]
        assert "TWO-FACTOR CONFIRMATION" in desc, (
            f"{name} description missing TWO-FACTOR CONFIRMATION"
        )


def test_run_saved_search_requires_name_or_id():
    """run_saved_search must constrain callers to provide at least one of
    name/id so the LLM cannot call it empty and get a non-actionable error."""
    tools = {t["name"]: t for t in get_tools()}
    schema = tools["run_saved_search"]["inputSchema"]
    # Accept either 'anyOf' with two required-branches, or a direct required
    # that lists both (less preferred — treats them as both required).
    any_of = schema.get("anyOf")
    assert any_of, "run_saved_search inputSchema must use anyOf to require name OR id"
    required_sets = [frozenset(b.get("required", [])) for b in any_of]
    assert frozenset({"name"}) in required_sets
    assert frozenset({"id"}) in required_sets


def test_setup_confirmation_secret_tool_schema():
    """First-run secret setup tool is available with both required inputs."""
    tools = {t["name"]: t for t in get_tools()}
    setup_tool = tools["setup_confirmation_secret"]
    props = setup_tool["inputSchema"]["properties"]
    required = setup_tool["inputSchema"]["required"]

    assert "confirmation_secret" in props
    assert "confirmation_secret_confirm" in props
    assert "confirmation_secret" in required
    assert "confirmation_secret_confirm" in required


def test_run_query_schema_carries_budget_override_fields():
    from oci_logan_mcp.tools import get_tools
    spec = next(t for t in get_tools() if t["name"] == "run_query")
    props = spec["inputSchema"]["properties"]
    assert "budget_override" in props
    assert "confirmation_token" in props
    assert "confirmation_secret" in props


def test_related_dashboards_and_searches_schema():
    tools = {t["name"]: t for t in get_tools()}
    spec = tools["related_dashboards_and_searches"]
    props = spec["inputSchema"]["properties"]

    assert "source" in props
    assert "field" in props
    assert "entity" in props
    assert props["entity"]["required"] == ["type", "value"]


def test_trace_request_id_schema():
    tools = {t["name"]: t for t in get_tools()}
    spec = tools["trace_request_id"]
    props = spec["inputSchema"]["properties"]

    assert "request_id" in props
    assert "time_range" in props
    assert "id_fields" in props
    assert spec["inputSchema"]["required"] == ["request_id", "time_range"]


def test_find_rare_events_schema():
    tools = {t["name"]: t for t in get_tools()}
    spec = tools["find_rare_events"]
    props = spec["inputSchema"]["properties"]

    assert "source" in props
    assert "field" in props
    assert "time_range" in props
    assert "rarity_threshold_percentile" in props
    assert "history_days" in props
    assert spec["inputSchema"]["required"] == ["source", "field", "time_range"]


def test_playbook_tool_schemas():
    tools = {t["name"]: t for t in get_tools()}
    assert "record_investigation" in tools
    assert "list_playbooks" in tools
    assert "get_playbook" in tools
    assert "delete_playbook" in tools

    record_schema = tools["record_investigation"]["inputSchema"]
    assert record_schema["required"] == ["name"]
    assert "since" in record_schema["properties"]
    assert "until" in record_schema["properties"]

    get_schema = tools["get_playbook"]["inputSchema"]
    assert get_schema["required"] == ["playbook_id"]


def test_generate_incident_report_schema():
    tools = {t["name"]: t for t in get_tools()}
    spec = tools["generate_incident_report"]
    schema = spec["inputSchema"]
    props = schema["properties"]

    assert schema["required"] == ["investigation"]
    assert props["format"]["enum"] == ["markdown", "html"]
    assert props["summary_length"]["enum"] == ["short", "standard", "detailed"]
    assert "include_sections" in props
