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


def test_investigation_schemas_carry_budget_control_fields():
    tools = {t["name"]: t for t in get_tools()}
    for name in ("investigate_incident", "investigate_and_generate_report"):
        props = tools[name]["inputSchema"]["properties"]
        assert "dry_run" in props
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


def test_create_log_source_from_sample_schema():
    tools = {t["name"]: t for t in get_tools()}
    spec = tools["create_log_source_from_sample"]
    props = spec["inputSchema"]["properties"]

    assert spec["destructive"] is True
    assert "TWO-FACTOR CONFIRMATION" in spec["description"]
    assert "source_name" in props
    assert "sample_logs" in props
    assert "log_group_id" in props
    assert "format" in props
    assert props["format"]["enum"] == ["json_ndjson", "csv", "regex_text"]
    assert "regex_pattern" in props
    assert "regex_field_keys" in props
    assert "acknowledge_data_review" in props
    assert "overwrite" in props
    assert "verification_time_range" in props
    assert props["verification_time_range"]["enum"] == [
        "last_15_min",
        "last_1_hour",
        "last_24_hours",
        "last_7_days",
        "last_30_days",
    ]
    assert "field_check_limit" in props
    assert "log_set" in props
    assert "confirmation_token" in props
    assert "confirmation_secret" in props
    assert spec["inputSchema"]["required"] == [
        "source_name",
        "sample_logs",
        "log_group_id",
        "acknowledge_data_review",
    ]


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


def test_investigation_intent_tooling_guides_full_workflow():
    tools = {t["name"]: t for t in get_tools()}
    investigate = tools["investigate_incident"]
    desc = investigate["description"].lower()

    for phrase in (
        "investigate",
        "investigation mode",
        "investigator mode",
        "root cause",
        "what happened",
        "likely story",
        "triage this incident",
        "troubleshoot",
        "find the issue",
        "what is wrong",
    ):
        assert phrase in desc

    assert "generate_incident_report" in desc
    assert "deliver_report" in desc
    assert "oci notifications" in desc


def test_investigate_and_generate_report_schema():
    tools = {t["name"]: t for t in get_tools()}
    spec = tools["investigate_and_generate_report"]
    schema = spec["inputSchema"]
    props = schema["properties"]

    assert schema["required"] == ["query"]
    assert props["format"]["enum"] == ["markdown", "html"]
    assert props["summary_length"]["enum"] == ["short", "standard", "detailed"]
    assert "top_k" in props
    assert "include_sections" in props


def test_report_delivery_option_schemas():
    tools = {t["name"]: t for t in get_tools()}

    defaults = tools["get_report_delivery_options"]
    assert "OCI Notifications email" in defaults["description"]
    assert defaults["inputSchema"]["properties"] == {}

    topics = tools["list_notification_topics"]
    props = topics["inputSchema"]["properties"]
    assert "compartment_id" in props
    assert "include_subcompartments" in props
    assert "lifecycle_state" in props

    storage = tools["get_report_storage_options"]
    assert "Object Storage" in storage["description"]
    assert storage["inputSchema"]["properties"] == {}

    buckets = tools["list_report_buckets"]
    bucket_props = buckets["inputSchema"]["properties"]
    assert "compartment_id" in bucket_props
    assert "include_subcompartments" in bucket_props


def test_incident_report_lookup_schemas():
    tools = {tool["name"]: tool for tool in get_tools()}

    assert tools["get_incident_report"]["inputSchema"]["required"] == ["report_id"]
    assert "report_id" in tools["get_incident_report"]["inputSchema"]["properties"]
    assert "limit" in tools["list_incident_reports"]["inputSchema"]["properties"]


def test_deliver_report_schema_is_markdown_first():
    tools = {tool["name"]: tool for tool in get_tools()}
    schema = tools["deliver_report"]["inputSchema"]
    props = schema["properties"]
    description = tools["deliver_report"]["description"]

    assert "report" in props
    report_schema = props["report"]
    assert "markdown" in report_schema["properties"]
    assert "report_id" in report_schema["properties"]
    assert "report_id lookup is deferred" not in description
    assert "inline markdown report content only" not in description.lower()
    assert props["channels"]["items"]["enum"] == ["telegram", "email", "slack"]
    assert props["format"]["enum"] == ["pdf", "markdown", "both"]
