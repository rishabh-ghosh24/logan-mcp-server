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
