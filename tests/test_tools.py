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


def test_run_saved_search_documents_name_or_id_requirement():
    """run_saved_search must tell callers to provide name or id via the
    description (handler enforces it at runtime). Top-level combinators are
    not allowed because OpenAI strict tool-calling rejects them, which would
    400 every Codex turn."""
    tools = {t["name"]: t for t in get_tools()}
    tool = tools["run_saved_search"]
    description = tool["description"].lower()
    assert "name" in description and "id" in description, (
        "run_saved_search description must mention both name and id so the "
        "model knows the requirement (schema can't enforce it under OpenAI "
        "strict tool-calling)"
    )


def test_no_tool_has_top_level_schema_combinators():
    """OpenAI strict tool-calling rejects any tool whose inputSchema has a
    top-level oneOf/anyOf/allOf/not/enum. A single bad tool fails the entire
    tool list and 400s every chat turn — guard against regressions across
    every tool we expose."""
    forbidden = {"oneOf", "anyOf", "allOf", "not", "enum"}
    for tool in get_tools():
        schema = tool.get("inputSchema") or {}
        offenders = forbidden & set(schema.keys())
        assert not offenders, (
            f"Tool {tool['name']!r} has forbidden top-level schema "
            f"keys {offenders}; OpenAI will reject the whole tool list."
        )


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
    assert props["format"]["enum"] == ["markdown", "html", "both"]
    assert props["summary_length"]["enum"] == ["short", "standard", "detailed"]
    assert "include_sections" in props
    assert props["title"]["type"] == "string"
    assert "title" not in schema.get("required", [])


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


def test_manual_query_tools_defer_investigation_intent_to_investigate_incident():
    tools = {t["name"]: t for t in get_tools()}

    for name in ("run_query", "get_log_summary", "ingestion_health", "parser_failure_triage"):
        desc = tools[name]["description"].lower()
        assert "investigate_incident" in desc
        assert "investigate" in desc
        assert "rca" in desc


def test_investigate_and_generate_report_schema():
    tools = {t["name"]: t for t in get_tools()}
    spec = tools["investigate_and_generate_report"]
    schema = spec["inputSchema"]
    props = schema["properties"]

    assert schema["required"] == ["query"]
    assert props["format"]["enum"] == ["markdown", "html", "both"]
    assert props["summary_length"]["enum"] == ["short", "standard", "detailed"]
    assert "top_k" in props
    assert props["mode"]["enum"] == ["quick", "standard", "deep"]
    assert "include_sections" in props
    assert props["title"]["type"] == "string"
    assert "title" not in schema.get("required", [])


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


def test_incident_report_read_tool_schemas():
    tools = {t["name"]: t for t in get_tools()}

    get_schema = tools["get_incident_report"]["inputSchema"]
    assert get_schema["required"] == ["report_id"]
    assert get_schema["properties"]["report_id"]["type"] == "string"

    prepare_schema = tools["prepare_report_delivery"]["inputSchema"]
    assert prepare_schema["required"] == ["report_id"]
    assert prepare_schema["properties"]["channel"]["enum"] == ["email"]

    list_schema = tools["list_incident_reports"]["inputSchema"]
    limit_schema = list_schema["properties"]["limit"]
    assert "required" not in list_schema
    assert limit_schema["type"] == "integer"
    assert limit_schema["minimum"] == 1
    assert limit_schema["maximum"] == 100
    assert limit_schema["default"] == 20


def test_deliver_report_schema_accepts_markdown_or_report_id():
    tools = {tool["name"]: tool for tool in get_tools()}
    schema = tools["deliver_report"]["inputSchema"]
    props = schema["properties"]
    report_schema = props["report"]

    assert "report" in props
    assert schema["required"] == ["report"]
    assert "required" not in report_schema or "markdown" not in report_schema["required"]
    assert "markdown" in report_schema["properties"]
    assert "report_id" in report_schema["properties"]
    assert props["channels"]["items"]["enum"] == ["telegram", "email", "slack"]
    assert props["recipients"]["type"] == "object"
    assert "email_topic_ocid" in props["recipients"]["properties"]
    assert props["format"]["enum"] == ["pdf", "markdown", "both"]
