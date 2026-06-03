"""Integration tests for wired CAM enforcement."""
import textwrap
import pytest

from oci_logan_mcp.access_control import AccessConfigError, load_access_config


def _access_config_file(tmp_path, *, body=None):
    p = tmp_path / "ac.yaml"
    p.write_text(textwrap.dedent(body or """
        compartment_id: c
        namespace: ns
        cams:
          cam_alice: { customers: [223] }
    """), encoding="utf-8")
    return p


class _FakeClient:
    def __init__(self, settings):
        self.settings = settings
        self.namespace = "old_ns"
        self.compartment_id = "old_compartment"
        self.access_profile = None
        self.access_audit_logger = None

    async def list_entities(self, entity_type=None):
        return [{"name": "223_x"}, {"name": "999_other"}]


class _FakeSecretStore:
    def __init__(self, path):
        self.path = path

    def has_secret(self):
        return False

    def is_valid(self):
        return True


class _FakeHandlers:
    captured_profile = None

    def __init__(self, **kwargs):
        _FakeHandlers.captured_profile = kwargs.get("access_profile")


def _patch_initialize_core_dependencies(monkeypatch, tmp_path, settings):
    import oci_logan_mcp.server as server_mod

    monkeypatch.setattr(server_mod, "config_exists", lambda: True)
    monkeypatch.setattr(server_mod, "load_config", lambda: settings)
    monkeypatch.setattr(server_mod, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(server_mod, "OCILogAnalyticsClient", _FakeClient)
    monkeypatch.setattr(server_mod, "CacheManager", lambda cfg: object())
    monkeypatch.setattr(server_mod, "QueryLogger", lambda cfg: object())
    monkeypatch.setattr(server_mod, "ContextManager", lambda cfg: object())
    monkeypatch.setattr(server_mod, "PreferenceStore", lambda user_dir: object())
    monkeypatch.setattr(server_mod, "SecretStore", _FakeSecretStore)
    monkeypatch.setattr(server_mod, "AuditLogger", lambda log_dir, session_id: object())
    monkeypatch.setattr(server_mod, "MCPHandlers", _FakeHandlers)


@pytest.mark.asyncio
async def test_initialize_core_enforce_access_refuses_unknown_cam(monkeypatch, tmp_path):
    from oci_logan_mcp.config import Settings
    from oci_logan_mcp.server import OCILogAnalyticsMCPServer

    settings = Settings()
    settings.enforce_access = True
    settings.access_control_path = str(_access_config_file(tmp_path))
    monkeypatch.setenv("LOGAN_USER", "cam_ghost")
    _patch_initialize_core_dependencies(monkeypatch, tmp_path, settings)

    with pytest.raises(AccessConfigError):
        await OCILogAnalyticsMCPServer().initialize_core()


@pytest.mark.asyncio
async def test_initialize_core_builds_profile_from_user_store_identity(monkeypatch, tmp_path):
    from oci_logan_mcp.config import Settings
    from oci_logan_mcp.server import OCILogAnalyticsMCPServer

    settings = Settings()
    settings.enforce_access = True
    settings.access_control_path = str(_access_config_file(tmp_path))
    monkeypatch.setenv("LOGAN_USER", "cam_alice")
    _patch_initialize_core_dependencies(monkeypatch, tmp_path, settings)

    srv = OCILogAnalyticsMCPServer()
    await srv.initialize_core()

    assert srv.access_profile.user_id == "cam_alice"
    assert srv.access_profile.entity_names == frozenset({"223_x"})
    assert srv.oci_client.namespace == "ns"
    assert srv.oci_client.compartment_id == "c"
    assert srv.oci_client.access_profile is srv.access_profile
    assert _FakeHandlers.captured_profile is srv.access_profile


@pytest.mark.asyncio
async def test_initialize_core_enforce_access_zero_resolved_fails(monkeypatch, tmp_path):
    from oci_logan_mcp.config import Settings
    from oci_logan_mcp.server import OCILogAnalyticsMCPServer

    settings = Settings()
    settings.enforce_access = True
    settings.access_control_path = str(_access_config_file(tmp_path, body="""
        compartment_id: c
        namespace: ns
        cams:
          cam_alice: { customers: [555] }
    """))
    monkeypatch.setenv("LOGAN_USER", "cam_alice")
    _patch_initialize_core_dependencies(monkeypatch, tmp_path, settings)

    with pytest.raises(AccessConfigError):
        await OCILogAnalyticsMCPServer().initialize_core()


from types import SimpleNamespace
import pytest

from oci_logan_mcp.access_control import AccessProfile


def _profile():
    return AccessProfile(
        user_id="cam_alice",
        customer_numbers=(223,),
        entity_names=frozenset({"223_x"}),
        entity_field="Entity",
        compartment_id="allowed_compartment",
        namespace="ns",
        allow_delivery=True,
    )


@pytest.mark.asyncio
async def test_client_query_scopes_and_pins_scope(monkeypatch):
    from oci_logan_mcp.client import OCILogAnalyticsClient

    client = OCILogAnalyticsClient.__new__(OCILogAnalyticsClient)
    client.settings = SimpleNamespace(query=SimpleNamespace(max_results=100))
    client._compartment_id = "default_compartment"
    client._namespace = "ns"
    client.access_profile = _profile()
    client.access_audit_logger = None
    captured = {}

    async def fake_execute(query_string, time_start, time_end, max_results,
                           compartment_id, include_subcompartments):
        captured.update({
            "query": query_string,
            "compartment_id": compartment_id,
            "include_subcompartments": include_subcompartments,
        })
        return {"rows": [], "columns": []}

    monkeypatch.setattr(client, "_execute_single_query", fake_execute)

    await OCILogAnalyticsClient.query(
        client,
        query_string="* | stats count",
        time_start="2026-06-01T00:00:00+00:00",
        time_end="2026-06-01T01:00:00+00:00",
        compartment_id="attacker_compartment",
        include_subcompartments=True,
    )

    assert captured["query"] == "'Entity' in ('223_x') | stats count"
    assert captured["compartment_id"] == "allowed_compartment"
    assert captured["include_subcompartments"] is False


@pytest.mark.asyncio
async def test_notification_topic_listing_does_not_walk_compartments_for_cam(monkeypatch):
    from oci_logan_mcp.client import OCILogAnalyticsClient

    client = OCILogAnalyticsClient.__new__(OCILogAnalyticsClient)
    client._compartment_id = "default_compartment"
    client.access_profile = _profile()
    client.access_audit_logger = None
    listed = []

    async def fake_list_topics(compartment_id):
        listed.append(compartment_id)
        return []

    async def fail_list_compartments():
        raise AssertionError("CAM notification topic listing must not enumerate compartments")

    monkeypatch.setattr(client, "_list_notification_topics_in_compartment", fake_list_topics)
    monkeypatch.setattr(client, "list_compartments", fail_list_compartments)

    await OCILogAnalyticsClient.list_notification_topics(
        client,
        compartment_id="attacker_compartment",
        include_subcompartments=True,
    )

    assert listed == ["allowed_compartment"]


import json
from unittest.mock import AsyncMock


_HANDLER_METHODS = (
    "_list_log_sources", "_list_fields", "_list_entities", "_list_parsers",
    "_list_labels", "_list_saved_searches", "_list_log_groups",
    "_validate_query", "_run_query", "_run_saved_search", "_run_batch_queries",
    "_diff_time_windows", "_pivot_on_entity", "_ingestion_health",
    "_parser_failure_triage", "_investigate_incident",
    "_investigate_and_generate_report", "_generate_incident_report",
    "_get_report_delivery_options", "_prepare_report_delivery",
    "_list_notification_topics", "_get_incident_report", "_list_incident_reports",
    "_deliver_report", "_why_did_this_fire", "_find_rare_events",
    "_create_log_source_from_sample", "_trace_request_id",
    "_related_dashboards_and_searches", "_visualize", "_export_results",
    "_set_compartment", "_set_namespace", "_get_current_context",
    "_list_compartments", "_test_connection", "_find_compartment",
    "_get_query_examples", "_get_log_summary", "_setup_confirmation_secret",
    "_save_learned_query", "_update_tenancy_context", "_get_preferences",
    "_remember_preference", "_create_alert", "_list_alerts", "_update_alert",
    "_delete_alert", "_create_saved_search", "_update_saved_search",
    "_delete_saved_search", "_create_dashboard", "_list_dashboards",
    "_add_dashboard_tile", "_delete_dashboard", "_send_to_slack",
    "_send_to_telegram", "_explain_query", "_get_session_budget",
    "_export_transcript", "_record_investigation", "_list_playbooks",
    "_get_playbook", "_delete_playbook",
)


def _handler_with_profile():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.settings = SimpleNamespace(read_only=False)
    h.user_store = SimpleNamespace(user_id="cam_alice")
    h.audit_logger = None
    h._write_audit_event = lambda **kwargs: True
    h._extract_audit_ref = lambda args: None
    h._audit_strictness = lambda name, args: "best_effort"
    h._clean_args_for_audit = lambda name, args: args
    h._summarize_tool_result = lambda result, elapsed_ms: {"success": True}
    h.confirmation_manager = SimpleNamespace(is_guarded_call=lambda name, args: False)
    for method in _HANDLER_METHODS:
        setattr(h, method, AsyncMock(return_value=[{"type": "text", "text": "{}"}]))
    return h


@pytest.mark.asyncio
async def test_handle_tool_call_blocks_disallowed_cam_tool():
    from oci_logan_mcp.handlers import MCPHandlers

    h = _handler_with_profile()
    result = await MCPHandlers.handle_tool_call(
        h, "investigate_incident", {"incident_id": "i-1"}
    )

    payload = json.loads(result[0]["text"])
    assert payload["status"] == "access_denied"
    h._investigate_incident.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_entities_filters_via_handler():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.schema_manager = SimpleNamespace(
        get_entities=AsyncMock(return_value=[
            {"name": "223_x"},
            {"name": "999_other"},
        ])
    )

    result = await MCPHandlers._list_entities(h, {})
    payload = json.loads(result[0]["text"])

    assert [e["name"] for e in payload] == ["223_x"]


@pytest.mark.asyncio
async def test_run_saved_search_preserves_scope_and_time_args():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.saved_search = SimpleNamespace(
        get_search_by_name=AsyncMock(),
        get_search_by_id=AsyncMock(return_value={"query": "* | stats count"}),
    )
    h.query_engine = SimpleNamespace(execute=AsyncMock(return_value={"data": []}))
    h._resolve_scope = lambda args: ("allowed_compartment", False)

    await MCPHandlers._run_saved_search(
        h,
        {
            "id": "saved-1",
            "time_range": "last_24_hours",
            "time_start": "2026-06-01T00:00:00Z",
            "time_end": "2026-06-02T00:00:00Z",
        },
    )

    h.query_engine.execute.assert_awaited_once_with(
        query="* | stats count",
        time_range="last_24_hours",
        time_start="2026-06-01T00:00:00Z",
        time_end="2026-06-02T00:00:00Z",
        include_subcompartments=False,
        compartment_id="allowed_compartment",
    )


@pytest.mark.asyncio
async def test_handle_resource_read_blocks_roster_resources():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()

    result = await MCPHandlers.handle_resource_read(h, "loganalytics://tenancy-context")

    assert result["error"].startswith("Resource not permitted")


@pytest.mark.asyncio
async def test_schema_resource_filters_entities():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.schema_manager = SimpleNamespace(
        get_full_schema=AsyncMock(return_value={
            "entities": [{"name": "223_x"}, {"name": "999_other"}],
            "fields": [{"name": "Log Source"}],
        })
    )

    schema = await MCPHandlers.handle_resource_read(h, "loganalytics://schema")

    assert [e["name"] for e in schema["entities"]] == ["223_x"]
    assert schema["fields"] == [{"name": "Log Source"}]


@pytest.mark.asyncio
async def test_send_to_telegram_rejects_destination_override():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.notification_service = SimpleNamespace(send_to_telegram=AsyncMock())

    result = await MCPHandlers._send_to_telegram(
        h, {"message": "capacity report", "chat_id": "12345"}
    )

    payload = json.loads(result[0]["text"])
    assert payload["status"] == "access_denied"
    h.notification_service.send_to_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_report_rejects_recipient_override():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.report_delivery_service = SimpleNamespace(deliver=AsyncMock())

    result = await MCPHandlers._deliver_report(
        h,
        {
            "report": {"markdown": "report body", "metadata": {}},
            "recipients": {"telegram_chat_id": "12345"},
        },
    )

    payload = json.loads(result[0]["text"])
    assert payload["status"] == "access_denied"
    h.report_delivery_service.deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_fields_does_not_auto_capture_for_cam():
    from oci_logan_mcp.handlers import MCPHandlers

    h = MCPHandlers.__new__(MCPHandlers)
    h.access_profile = _profile()
    h.settings = SimpleNamespace(read_only=False)
    h.schema_manager = SimpleNamespace(
        get_fields=AsyncMock(return_value=[
            SimpleNamespace(
                name="Log Source",
                data_type="string",
                description="",
                possible_values=[],
                hint="",
            )
        ])
    )
    h.context_manager = SimpleNamespace(
        update_confirmed_fields=lambda fields: (_ for _ in ()).throw(
            AssertionError("CAM metadata reads must not update shared context")
        )
    )

    result = await MCPHandlers._list_fields(h, {})

    payload = json.loads(result[0]["text"])
    assert payload[0]["name"] == "Log Source"


def _write_legacy_report(tmp_path, legacy_id):
    legacy_dir = tmp_path / "store" / legacy_id
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "report.md").write_text("legacy result content", encoding="utf-8")
    (legacy_dir / "metadata.json").write_text(
        json.dumps({"report_id": legacy_id}), encoding="utf-8"
    )


def test_report_store_skips_legacy_shared_import_in_cam_mode(tmp_path):
    from oci_logan_mcp.report_store import ReportStore

    legacy_id = "rpt_" + ("a" * 32)
    _write_legacy_report(tmp_path, legacy_id)

    ReportStore(tmp_path, user_id="cam_alice", enforce_access=True)

    assert not (tmp_path / "users" / "cam_alice" / "store" / legacy_id).exists()


def test_report_store_imports_legacy_shared_import_without_enforce(tmp_path):
    """Inverse: prove the enforce_access flag is what drives the skip.

    Without enforce_access the legacy report (whose id satisfies the real
    REPORT_ID_RE) IS imported, so the skip test above is not passing trivially.
    """
    from oci_logan_mcp.report_store import ReportStore

    legacy_id = "rpt_" + ("a" * 32)
    _write_legacy_report(tmp_path, legacy_id)

    ReportStore(tmp_path, user_id="cam_alice", enforce_access=False)

    assert (tmp_path / "users" / "cam_alice" / "store" / legacy_id).exists()


from datetime import datetime, timezone


def test_cache_key_namespaced_by_profile_behavior():
    from oci_logan_mcp.query_engine import QueryEngine

    engine = QueryEngine(
        oci_client=SimpleNamespace(access_profile=_profile()),
        cache=SimpleNamespace(),
        logger=SimpleNamespace(),
    )
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 2, tzinfo=timezone.utc)

    key_a = engine._make_cache_key("* | stats count", start, end, False, "c")
    engine.oci_client.access_profile = AccessProfile(
        user_id="cam_bob",
        customer_numbers=(999,),
        entity_names=frozenset({"999_y"}),
        entity_field="Entity",
        compartment_id="allowed_compartment",
        namespace="ns",
        allow_delivery=True,
    )
    key_b = engine._make_cache_key("* | stats count", start, end, False, "c")

    assert key_a != key_b


@pytest.mark.asyncio
async def test_client_audits_effective_scoped_query(monkeypatch):
    from oci_logan_mcp.client import OCILogAnalyticsClient

    events = []
    client = OCILogAnalyticsClient.__new__(OCILogAnalyticsClient)
    client.settings = SimpleNamespace(query=SimpleNamespace(max_results=100))
    client._compartment_id = "default_compartment"
    client._namespace = "ns"
    client.access_profile = _profile()
    client.access_audit_logger = SimpleNamespace(log=lambda **kwargs: events.append(kwargs))

    async def fake_execute(query_string, time_start, time_end, max_results,
                           compartment_id, include_subcompartments):
        return {"rows": [], "columns": []}

    monkeypatch.setattr(client, "_execute_single_query", fake_execute)

    await OCILogAnalyticsClient.query(
        client,
        query_string="* | stats count",
        time_start="2026-06-01T00:00:00+00:00",
        time_end="2026-06-01T01:00:00+00:00",
        compartment_id="attacker_compartment",
        include_subcompartments=True,
    )

    assert events
    event = events[-1]
    assert event["user"] == "cam_alice"
    assert event["tool"] == "__access_control"
    assert event["outcome"] == "query_scoped"
    assert event["args"]["original_query"] == "* | stats count"
    assert event["args"]["effective_query"] == "'Entity' in ('223_x') | stats count"
    assert event["args"]["compartment_id"] == "allowed_compartment"
    assert event["args"]["include_subcompartments"] is False


@pytest.mark.asyncio
async def test_client_without_profile_preserves_caller_query_and_scope(monkeypatch):
    from oci_logan_mcp.client import OCILogAnalyticsClient

    client = OCILogAnalyticsClient.__new__(OCILogAnalyticsClient)
    client.settings = SimpleNamespace(query=SimpleNamespace(max_results=100))
    client._compartment_id = "default_compartment"
    client._namespace = "ns"
    client.access_profile = None
    client.access_audit_logger = SimpleNamespace(log=AsyncMock())
    captured = {}

    async def fake_execute(query_string, time_start, time_end, max_results,
                           compartment_id, include_subcompartments):
        captured.update({
            "query": query_string,
            "compartment_id": compartment_id,
            "include_subcompartments": include_subcompartments,
        })
        return {"rows": [], "columns": []}

    monkeypatch.setattr(client, "_execute_single_query", fake_execute)

    await OCILogAnalyticsClient.query(
        client,
        query_string="Entity = '999_other' | stats count",
        time_start="2026-06-01T00:00:00+00:00",
        time_end="2026-06-01T01:00:00+00:00",
        compartment_id="caller_compartment",
        include_subcompartments=True,
    )

    assert captured["query"] == "Entity = '999_other' | stats count"
    assert captured["compartment_id"] == "caller_compartment"
    assert captured["include_subcompartments"] is True
    client.access_audit_logger.log.assert_not_called()


@pytest.mark.asyncio
async def test_handler_without_profile_does_not_apply_cam_tool_gate():
    from oci_logan_mcp.handlers import MCPHandlers

    h = _handler_with_profile()
    h.access_profile = None

    await MCPHandlers.handle_tool_call(h, "investigate_incident", {"incident_id": "i-1"})

    h._investigate_incident.assert_awaited_once_with({"incident_id": "i-1"})
