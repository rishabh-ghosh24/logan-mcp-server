"""Tests for MCP request handlers — routing, visualization, scope resolution."""

import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from oci_logan_mcp.handlers import MCPHandlers
from oci_logan_mcp.config import Settings
from oci_logan_mcp.user_store import UserStore
from oci_logan_mcp.preferences import PreferenceStore
from oci_logan_mcp.secret_store import SecretStore
from oci_logan_mcp.audit import AuditLogger
from oci_logan_mcp.report_delivery import ReportDeliveryError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings():
    """Create test Settings."""
    s = Settings()
    s.log_analytics.namespace = "testns"
    s.log_analytics.default_compartment_id = "ocid1.compartment.default"
    s.query.max_results = 1000
    s.query.default_time_range = "last_1_hour"
    return s


@pytest.fixture
def mock_oci_client():
    """Create mock OCI client."""
    client = MagicMock()
    client.namespace = "testns"
    client.compartment_id = "ocid1.compartment.default"
    client._config = {"tenancy": "ocid1.tenancy.test"}
    client.list_log_sources = AsyncMock(return_value=[])
    client.list_fields = AsyncMock(return_value=[])
    client.list_entities = AsyncMock(return_value=[])
    client.list_parsers = AsyncMock(return_value=[])
    client.list_labels = AsyncMock(return_value=[])
    client.list_log_groups = AsyncMock(return_value=[])
    client.list_saved_searches = AsyncMock(return_value=[])
    client.list_compartments = AsyncMock(return_value=[])
    client.query = AsyncMock(return_value={
        "columns": [{"name": "count"}],
        "rows": [[42]],
        "total_count": 1,
        "is_partial": False,
    })
    return client


@pytest.fixture
def mock_cache():
    """Create mock cache."""
    cache = MagicMock()
    cache.get = MagicMock(return_value=None)
    cache.set = MagicMock()
    cache.clear = MagicMock()
    return cache


@pytest.fixture
def mock_query_logger():
    """Create mock query logger."""
    logger = MagicMock()
    logger.log_query = MagicMock()
    logger.get_recent_queries = MagicMock(return_value=[])
    return logger


@pytest.fixture
def mock_context_manager():
    """Create mock context manager."""
    ctx = MagicMock()
    ctx.update_log_sources = MagicMock(return_value="5 total (0 new)")
    ctx.update_confirmed_fields = MagicMock(return_value="10 total (0 new)")
    ctx.update_compartments = MagicMock(return_value="3 total (0 new)")
    ctx.add_note = MagicMock()
    ctx.get_tenancy_context = MagicMock(return_value={"namespace": "testns"})
    return ctx


@pytest.fixture
def mock_user_store(tmp_path):
    """Create a real UserStore backed by a temp directory."""
    return UserStore(base_dir=tmp_path, user_id="testuser")


@pytest.fixture
def mock_preference_store(tmp_path):
    """Create a real PreferenceStore backed by a temp directory."""
    user_dir = tmp_path / "users" / "testuser"
    user_dir.mkdir(parents=True, exist_ok=True)
    return PreferenceStore(user_dir=user_dir)


@pytest.fixture
def mock_secret_store(tmp_path):
    """Create a SecretStore backed by a temp file (no secret set)."""
    return SecretStore(tmp_path / "secret.yaml")


@pytest.fixture
def mock_audit_logger(tmp_path):
    """Create an AuditLogger backed by a temp directory."""
    return AuditLogger(tmp_path / "audit")


@pytest.fixture
def handlers(settings, mock_oci_client, mock_cache, mock_query_logger, mock_context_manager,
             mock_user_store, mock_preference_store, mock_secret_store, mock_audit_logger):
    """Create MCPHandlers with all mocked dependencies."""
    return MCPHandlers(
        settings=settings,
        oci_client=mock_oci_client,
        cache=mock_cache,
        query_logger=mock_query_logger,
        context_manager=mock_context_manager,
        user_store=mock_user_store,
        preference_store=mock_preference_store,
        secret_store=mock_secret_store,
        audit_logger=mock_audit_logger,
    )


# ---------------------------------------------------------------------------
# Catalog Wiring Tests
# ---------------------------------------------------------------------------

def test_mcp_handlers_exposes_unified_catalog(handlers):
    """Handler should have a UnifiedCatalog when user_store is provided."""
    from oci_logan_mcp.catalog import UnifiedCatalog
    assert isinstance(handlers.catalog, UnifiedCatalog)




# ---------------------------------------------------------------------------
# Tool Routing Tests
# ---------------------------------------------------------------------------

class TestToolRouting:
    """Test handle_tool_call routes to correct handlers."""

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, handlers):
        """Unknown tool name should return error message."""
        result = await handlers.handle_tool_call("nonexistent_tool", {})
        assert len(result) == 1
        assert "Unknown tool" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_all_tool_names_registered(self, handlers):
        """All expected tool names should be in the handlers dict."""
        expected_tools = [
            "list_log_sources", "list_fields", "list_entities",
            "list_parsers", "list_labels", "list_saved_searches",
            "list_log_groups", "validate_query", "run_query",
            "run_saved_search", "run_batch_queries", "visualize",
            "export_results", "set_compartment", "set_namespace",
            "get_current_context", "list_compartments", "test_connection",
            "find_compartment", "get_query_examples", "get_log_summary",
            "find_rare_events",
            "trace_request_id",
            "related_dashboards_and_searches",
            "setup_confirmation_secret",
            "save_learned_query",
            "update_tenancy_context",
            "get_preferences", "remember_preference",
        ]
        # Invoke handle_tool_call to initialize the handlers dict
        # (handlers dict is built inside handle_tool_call)
        for tool_name in expected_tools:
            # We just verify it doesn't return "Unknown tool"
            # Some will fail with missing args, but that's a different error
            result = await handlers.handle_tool_call(tool_name, {})
            for r in result:
                if "text" in r:
                    assert "Unknown tool" not in r["text"], f"Tool '{tool_name}' not registered"

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error(self, handlers):
        """Handler exceptions should be caught and returned as error text."""
        handlers.schema_manager.get_log_sources = AsyncMock(side_effect=Exception("boom"))

        result = await handlers.handle_tool_call("list_log_sources", {})
        assert len(result) == 1
        assert "Error" in result[0]["text"]
        assert "boom" in result[0]["text"]


# ---------------------------------------------------------------------------
# Scope Resolution Tests
# ---------------------------------------------------------------------------

class TestResolveScope:
    """Test _resolve_scope for tenancy-wide queries."""

    def test_default_scope(self, handlers):
        """Default scope returns None compartment and True include_subs."""
        compartment_id, include_subs = handlers._resolve_scope({})
        assert compartment_id is None
        assert include_subs is True

    def test_default_scope_with_explicit_true(self, handlers):
        """Explicit include_subcompartments=True should be preserved."""
        compartment_id, include_subs = handlers._resolve_scope(
            {"include_subcompartments": True}
        )
        assert include_subs is True

    def test_default_scope_with_explicit_false(self, handlers):
        """Explicit include_subcompartments=False should be preserved."""
        compartment_id, include_subs = handlers._resolve_scope(
            {"include_subcompartments": False}
        )
        assert include_subs is False

    def test_tenancy_scope(self, handlers):
        """Tenancy scope should use tenancy OCID and force include_subs=True."""
        compartment_id, include_subs = handlers._resolve_scope(
            {"scope": "tenancy"}
        )
        assert compartment_id == "ocid1.tenancy.test"
        assert include_subs is True

    def test_tenancy_scope_overrides_false_subs(self, handlers):
        """Tenancy scope should override include_subcompartments=False."""
        compartment_id, include_subs = handlers._resolve_scope(
            {"scope": "tenancy", "include_subcompartments": False}
        )
        assert include_subs is True

    def test_explicit_compartment_id(self, handlers):
        """Should pass through explicit compartment_id."""
        compartment_id, include_subs = handlers._resolve_scope(
            {"compartment_id": "ocid1.compartment.custom"}
        )
        assert compartment_id == "ocid1.compartment.custom"

    def test_string_boolean_conversion(self, handlers):
        """Should handle string 'true'/'false' for include_subcompartments."""
        _, include_subs = handlers._resolve_scope({"include_subcompartments": "true"})
        assert include_subs is True

        _, include_subs = handlers._resolve_scope({"include_subcompartments": "false"})
        assert include_subs is False

        _, include_subs = handlers._resolve_scope({"include_subcompartments": "yes"})
        assert include_subs is True


# ---------------------------------------------------------------------------
# Visualize Handler Tests
# ---------------------------------------------------------------------------

class TestVisualizeHandler:
    """Test the _visualize handler."""

    @pytest.mark.asyncio
    async def test_returns_image_and_text(self, handlers):
        """Should return both image and text blocks."""
        # Mock query_engine.execute
        handlers.query_engine.execute = AsyncMock(return_value={
            "data": {
                "columns": [{"name": "Source"}, {"name": "count"}],
                "rows": [["Syslog", 100], ["Windows", 50]],
            }
        })

        result = await handlers._visualize({
            "query": "* | stats count by 'Log Source'",
            "chart_type": "pie",
        })

        assert len(result) == 2
        assert result[0]["type"] == "image"
        assert result[0]["mimeType"] == "image/png"
        assert result[1]["type"] == "text"
        assert "Raw data" in result[1]["text"]

    @pytest.mark.asyncio
    async def test_supports_all_chart_types(self, handlers):
        """Should accept all valid chart types without error."""
        handlers.query_engine.execute = AsyncMock(return_value={
            "data": {
                "columns": [{"name": "Label"}, {"name": "count"}],
                "rows": [["A", 100], ["B", 50], ["C", 25]],
            }
        })

        valid_types = [
            "pie", "bar", "vertical_bar", "line", "area",
            "table", "tile", "treemap", "heatmap", "histogram",
        ]
        for chart_type in valid_types:
            result = await handlers._visualize({
                "query": "* | stats count by Label",
                "chart_type": chart_type,
            })
            assert result[0]["type"] == "image", f"Chart type '{chart_type}' failed"

    @pytest.mark.asyncio
    async def test_invalid_chart_type_raises(self, handlers):
        """Should raise ValueError for invalid chart type."""
        handlers.query_engine.execute = AsyncMock(return_value={
            "data": {
                "columns": [{"name": "count"}],
                "rows": [[100]],
            }
        })

        # This should get caught by the handle_tool_call error handler
        result = await handlers.handle_tool_call("visualize", {
            "query": "* | stats count",
            "chart_type": "invalid_type",
        })
        assert "Error" in result[0]["text"]


# ---------------------------------------------------------------------------
# Configuration Handler Tests
# ---------------------------------------------------------------------------

class TestConfigurationHandlers:
    """Test set_compartment, set_namespace, get_current_context."""

    @pytest.mark.asyncio
    async def test_set_compartment(self, handlers, mock_oci_client, mock_cache):
        """Should update client compartment and clear cache."""
        result = await handlers._set_compartment({"compartment_id": "ocid1.comp.new"})

        assert mock_oci_client.compartment_id == "ocid1.comp.new"
        mock_cache.clear.assert_called_once()
        assert "ocid1.comp.new" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_set_namespace(self, handlers, mock_oci_client, mock_cache):
        """Should update client namespace and clear cache."""
        result = await handlers._set_namespace({"namespace": "new-ns"})

        assert mock_oci_client.namespace == "new-ns"
        mock_cache.clear.assert_called_once()
        assert "new-ns" in result[0]["text"]

    @pytest.mark.asyncio
    async def test_get_current_context(self, handlers):
        """Should return current context settings."""
        result = await handlers._get_current_context({})
        context = json.loads(result[0]["text"])

        assert "namespace" in context
        assert "compartment_id" in context
        assert "max_results" in context


# ---------------------------------------------------------------------------
# Memory & Context Handler Tests
# ---------------------------------------------------------------------------

class TestMemoryHandlers:
    """Test learned query and context handlers."""

    @pytest.mark.asyncio
    async def test_save_learned_query(self, handlers, mock_user_store):
        """Should save query and return success."""
        result = await handlers._save_learned_query({
            "name": "error_count",
            "query": "* | where Severity = 'ERROR' | stats count",
            "description": "Count errors",
        })

        data = json.loads(result[0]["text"])
        assert data["status"] == "saved"
        # Verify it was saved via user_store
        queries = mock_user_store.list_queries()
        assert len(queries) == 1
        assert queries[0]["name"] == "error_count"

    @pytest.mark.asyncio
    async def test_save_learned_query_surfaces_collision_warning(self, handlers, mock_user_store):
        """When save_query returns a collision_warning, handler should surface it as status=collision."""
        result = await handlers._save_learned_query({
            "name": "errors_last_hour",  # builtin name — should collide
            "query": "* | head 1",
            "description": "my copy",
        })
        data = json.loads(result[0]["text"])
        assert data["status"] == "collision"
        assert "collision_warning" in data
        # Nothing should have been saved
        assert mock_user_store.list_queries() == []

    @pytest.mark.asyncio
    async def test_save_learned_query_force_flag_passed_through(self, handlers, mock_user_store):
        """force=True should be passed through so the save succeeds despite collision."""
        result = await handlers._save_learned_query({
            "name": "errors_last_hour",
            "query": "* | head 1",
            "description": "my override",
            "force": True,
        })
        data = json.loads(result[0]["text"])
        assert data["status"] == "saved"

    @pytest.mark.asyncio
    async def test_save_learned_query_rename_to_passed_through(self, handlers, mock_user_store):
        """rename_to should be passed through and the entry saved under the new name."""
        result = await handlers._save_learned_query({
            "name": "errors_last_hour",
            "query": "* | head 1",
            "description": "my version",
            "rename_to": "my_errors_last_hour",
        })
        data = json.loads(result[0]["text"])
        assert data["status"] == "saved"
        assert data["query"]["name"] == "my_errors_last_hour"

    @pytest.mark.asyncio
    async def test_update_tenancy_context_notes(self, handlers, mock_context_manager):
        """Should add notes to tenancy context."""
        result = await handlers._update_tenancy_context({
            "notes": ["Note 1", "Note 2"]
        })

        assert mock_context_manager.add_note.call_count == 2
        data = json.loads(result[0]["text"])
        assert data["status"] == "updated"
        assert "2 note(s)" in data["changes"][0]

    @pytest.mark.asyncio
    async def test_update_tenancy_context_fields(self, handlers, mock_context_manager):
        """Should update confirmed fields."""
        result = await handlers._update_tenancy_context({
            "confirmed_fields": [{"name": "Severity"}]
        })

        mock_context_manager.update_confirmed_fields.assert_called_once()
        data = json.loads(result[0]["text"])
        assert data["status"] == "updated"


# ---------------------------------------------------------------------------
# Query Examples Handler Tests
# ---------------------------------------------------------------------------

class TestQueryExamplesHandler:
    """Test get_query_examples handler."""

    @pytest.mark.asyncio
    async def test_all_categories(self, handlers):
        """Should return all categories."""
        result = await handlers._get_query_examples({"category": "all"})
        data = json.loads(result[0]["text"])
        assert "categories" in data
        assert "basic" in data["categories"]
        assert "security" in data["categories"]
        assert "errors" in data["categories"]

    @pytest.mark.asyncio
    async def test_specific_category(self, handlers):
        """Should return specific category examples."""
        result = await handlers._get_query_examples({"category": "security"})
        data = json.loads(result[0]["text"])
        assert data["category"] == "security"
        assert len(data["examples"]) > 0

    @pytest.mark.asyncio
    async def test_unknown_category(self, handlers):
        """Should return error for unknown category."""
        result = await handlers._get_query_examples({"category": "invalid"})
        data = json.loads(result[0]["text"])
        assert "error" in data


# ---------------------------------------------------------------------------
# Resource Read Tests
# ---------------------------------------------------------------------------

class TestResourceRead:
    """Test handle_resource_read."""

    @pytest.mark.asyncio
    async def test_tenancy_context_resource(self, handlers, mock_context_manager):
        """Should return tenancy context."""
        result = await handlers.handle_resource_read("loganalytics://tenancy-context")
        assert result == {"namespace": "testns"}

    @pytest.mark.asyncio
    async def test_unknown_resource_raises(self, handlers):
        """Should raise ValueError for unknown resource URI."""
        with pytest.raises(ValueError, match="Unknown resource"):
            await handlers.handle_resource_read("loganalytics://unknown")

    @pytest.mark.asyncio
    async def test_query_templates_resource_includes_shared(self, tmp_path):
        """After rewiring, the query-templates resource returns builtin + shared entries
        (personal and starter excluded per for_templates_resource precedence)."""
        import yaml
        from oci_logan_mcp.user_store import UserStore
        from oci_logan_mcp.preferences import PreferenceStore
        from oci_logan_mcp.secret_store import SecretStore
        from oci_logan_mcp.audit import AuditLogger

        # Seed shared/promoted_queries.yaml
        shared_dir = tmp_path / "shared"
        shared_dir.mkdir()
        (shared_dir / "promoted_queries.yaml").write_text(yaml.dump({
            "queries": [{"name": "my_shared_query", "query": "* shared",
                         "description": "promoted from users"}]
        }))

        # Build handler with base_dir=tmp_path so catalog reads from tmp_path
        user_store = UserStore(base_dir=tmp_path, user_id="testuser")
        user_dir = tmp_path / "users" / "testuser"
        user_dir.mkdir(parents=True, exist_ok=True)
        pref_store = PreferenceStore(user_dir=user_dir)
        secret_store = SecretStore(tmp_path / "secret.yaml")
        audit = AuditLogger(tmp_path / "audit")

        settings = Settings()
        settings.log_analytics.namespace = "testns"
        settings.log_analytics.default_compartment_id = "ocid1.compartment.default"
        settings.query.max_results = 1000
        settings.query.default_time_range = "last_1_hour"

        from unittest.mock import MagicMock, AsyncMock
        oci_client = MagicMock()
        oci_client.namespace = "testns"
        oci_client.compartment_id = "ocid1.compartment.default"
        oci_client._config = {"tenancy": "ocid1.tenancy.test"}
        cache = MagicMock()
        cache.get = MagicMock(return_value=None)
        cache.set = MagicMock()
        query_logger = MagicMock()
        query_logger.get_recent_queries = MagicMock(return_value=[])
        ctx = MagicMock()
        ctx.get_tenancy_context = MagicMock(return_value={"namespace": "testns"})

        handler = MCPHandlers(
            settings=settings,
            oci_client=oci_client,
            cache=cache,
            query_logger=query_logger,
            context_manager=ctx,
            user_store=user_store,
            preference_store=pref_store,
            secret_store=secret_store,
            audit_logger=audit,
        )

        result = await handler.handle_resource_read("loganalytics://query-templates")
        names = {t["name"] for t in result["templates"]}
        assert "my_shared_query" in names, "shared query should appear in templates resource"
        # Still has builtins
        assert "errors_last_hour" in names, "builtin query should still appear"
        # Response shape unchanged
        assert "templates" in result
        assert isinstance(result["templates"], list)
        for t in result["templates"]:
            assert "name" in t
            assert "query" in t
            assert "description" in t

    @pytest.mark.asyncio
    async def test_query_templates_resource_shape_preserved(self, handlers):
        """Pin the exact response shape so a future refactor doesn't break MCP clients."""
        result = await handlers.handle_resource_read("loganalytics://query-templates")
        # Top-level: exactly one key "templates" whose value is a list
        assert set(result.keys()) == {"templates"}
        assert isinstance(result["templates"], list)
        # Every entry: exactly name, query, description as strings
        for t in result["templates"]:
            assert set(t.keys()) == {"name", "description", "query"}
            assert isinstance(t["name"], str)
            assert isinstance(t["query"], str)
            assert isinstance(t["description"], str)


# ---------------------------------------------------------------------------
# Find Compartment Tests
# ---------------------------------------------------------------------------

class TestFindCompartment:
    """Test fuzzy compartment search."""

    @pytest.mark.asyncio
    async def test_exact_match(self, handlers, mock_oci_client):
        """Should return exact name match with score 100."""
        mock_oci_client.list_compartments = AsyncMock(return_value=[
            {"name": "Production", "id": "ocid1.comp.prod", "description": ""},
            {"name": "Development", "id": "ocid1.comp.dev", "description": ""},
        ])

        result = await handlers._find_compartment({"name": "Production"})
        data = json.loads(result[0]["text"])
        assert data["found"] >= 1
        assert data["matches"][0]["match_score"] == 100

    @pytest.mark.asyncio
    async def test_partial_match(self, handlers, mock_oci_client):
        """Should find partial name matches."""
        mock_oci_client.list_compartments = AsyncMock(return_value=[
            {"name": "production-east", "id": "ocid1.comp.pe", "description": ""},
            {"name": "staging", "id": "ocid1.comp.s", "description": ""},
        ])

        result = await handlers._find_compartment({"name": "prod"})
        data = json.loads(result[0]["text"])
        assert data["found"] >= 1

    @pytest.mark.asyncio
    async def test_no_match(self, handlers, mock_oci_client):
        """Should return empty matches when no compartment found."""
        mock_oci_client.list_compartments = AsyncMock(return_value=[
            {"name": "Production", "id": "ocid1.comp.prod", "description": ""},
        ])

        result = await handlers._find_compartment({"name": "zzzznonexistent"})
        data = json.loads(result[0]["text"])
        assert data["found"] == 0

    @pytest.mark.asyncio
    async def test_empty_name(self, handlers):
        """Should return error for empty search name."""
        result = await handlers._find_compartment({"name": ""})
        data = json.loads(result[0]["text"])
        assert "error" in data


# ---------------------------------------------------------------------------
# New Tool Routing Tests (v0.4 automation tools)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,mock_args", [
    ("create_alert", {"display_name": "t", "query": "* | stats count", "destination_topic_id": "ocid1.topic.1"}),
    ("list_alerts", {}),
    ("update_alert", {"alert_id": "ocid1.alarm.1"}),
    ("delete_alert", {"alert_id": "ocid1.alarm.1"}),
    ("create_saved_search", {"display_name": "x", "query": "* | stats count"}),
    ("update_saved_search", {"saved_search_id": "ocid1.task.1"}),
    ("delete_saved_search", {"saved_search_id": "ocid1.task.1"}),
    ("create_dashboard", {"display_name": "x", "tiles": [{"title": "t", "query": "* | stats count", "visualization_type": "bar"}]}),
    ("list_dashboards", {}),
    ("add_dashboard_tile", {"dashboard_id": "ocid1.dash.1", "title": "t", "query": "* | stats count", "visualization_type": "bar"}),
    ("delete_dashboard", {"dashboard_id": "ocid1.dash.1"}),
    ("send_to_slack", {"message": "hello"}),
    ("send_to_telegram", {"message": "hello"}),
])
async def test_new_tools_are_routed(tool_name, mock_args, handlers):
    """New v0.4 tools should be registered and not return 'Unknown tool'."""
    # Patch the new service methods to return a default dict value
    from unittest.mock import AsyncMock
    handlers.alarm_service.create_alert = AsyncMock(return_value={"alarm_id": "ocid1.alarm.1"})
    handlers.alarm_service.list_alerts = AsyncMock(return_value=[])
    handlers.alarm_service.update_alert = AsyncMock(return_value={"alarm_id": "ocid1.alarm.1", "updated": []})
    handlers.alarm_service.delete_alert = AsyncMock(return_value={"deleted": ["alarm"], "partial_failure": False})
    handlers.saved_search.create_search = AsyncMock(return_value={"id": "ocid1.task.1"})
    handlers.saved_search.update_search = AsyncMock(return_value={"id": "ocid1.task.1"})
    handlers.saved_search.delete_search = AsyncMock(return_value=None)
    handlers.dashboard_service.create_dashboard = AsyncMock(return_value={"dashboard_id": "ocid1.dash.1"})
    handlers.dashboard_service.list_dashboards = AsyncMock(return_value=[])
    handlers.dashboard_service.add_tile = AsyncMock(return_value={"dashboard_id": "ocid1.dash.1", "saved_search_id": "s1", "title": "t"})
    handlers.dashboard_service.delete_dashboard = AsyncMock(return_value={"deleted": [], "partial_failure": False})
    handlers.notification_service.send_to_slack = AsyncMock(return_value={"status": "sent", "destination": "slack"})
    handlers.notification_service.send_to_telegram = AsyncMock(return_value={"status": "sent", "destination": "telegram"})

    result = await handlers.handle_tool_call(tool_name, mock_args)
    text = result[0]["text"] if result else ""
    assert "Unknown tool" not in text


# ---------------------------------------------------------------------------
# Confirmation Flow Tests
# ---------------------------------------------------------------------------

class TestConfirmationFlow:
    """Destructive/modifying tools require two-factor confirmation."""

    @pytest.fixture
    def handlers_with_secret(self, settings, mock_oci_client, mock_cache,
                             mock_query_logger, mock_context_manager,
                             mock_user_store, mock_preference_store,
                             mock_secret_store, mock_audit_logger):
        mock_secret_store.set_secret("my-secret")
        h = MCPHandlers(
            settings=settings,
            oci_client=mock_oci_client,
            cache=mock_cache,
            query_logger=mock_query_logger,
            context_manager=mock_context_manager,
            user_store=mock_user_store,
            preference_store=mock_preference_store,
            secret_store=mock_secret_store,
            audit_logger=mock_audit_logger,
        )
        # Mock service methods
        h.alarm_service.delete_alert = AsyncMock(return_value={"deleted": True})
        h.alarm_service.update_alert = AsyncMock(return_value={"alarm_id": "a1", "updated": ["severity"]})
        h.saved_search.update_search = AsyncMock(return_value={"id": "s1"})
        h.saved_search.delete_search = AsyncMock(return_value=None)
        h.dashboard_service.add_tile = AsyncMock(return_value={"dashboard_id": "d1"})
        h.dashboard_service.delete_dashboard = AsyncMock(return_value={"deleted": True})
        return h

    @pytest.mark.asyncio
    async def test_guarded_tool_without_secret_refuses(self, handlers):
        """Fail-closed: no secret configured → confirmation_unavailable."""
        result = await handlers.handle_tool_call(
            "delete_alert", {"alert_id": "ocid1.alarm.oc1..abc"}
        )
        text = json.loads(result[0]["text"])
        assert text["status"] == "confirmation_unavailable"
        assert "setup_confirmation_secret" in text["next_step"]

    @pytest.mark.asyncio
    async def test_guarded_tool_with_invalid_secret_gives_recovery_guidance(self, handlers):
        """Invalid secret files should fail closed with recovery instructions."""
        handlers.secret_store.set_secret("abcdefgh")
        handlers.secret_store._path.write_text("not valid yaml", encoding="utf-8")

        result = await handlers.handle_tool_call(
            "delete_alert", {"alert_id": "ocid1.alarm.oc1..abc"}
        )
        text = json.loads(result[0]["text"])
        assert text["status"] == "confirmation_unavailable"
        assert "invalid" in text["error"].lower()
        assert "setup_confirmation_secret" in text["message"]

    @pytest.mark.asyncio
    async def test_guarded_tool_without_token_returns_confirmation(
        self, handlers_with_secret
    ):
        """First call without token returns confirmation_required."""
        result = await handlers_with_secret.handle_tool_call(
            "delete_alert", {"alert_id": "ocid1.alarm.oc1..abc"}
        )
        text = json.loads(result[0]["text"])
        assert text["status"] == "confirmation_required"
        assert "confirmation_token" in text

    @pytest.mark.asyncio
    async def test_guarded_tool_with_valid_token_and_secret_executes(
        self, handlers_with_secret
    ):
        """Correct token + secret + matching args → execution."""
        args = {"alert_id": "ocid1.alarm.oc1..abc"}
        # Step 1: get token
        result = await handlers_with_secret.handle_tool_call("delete_alert", args)
        token = json.loads(result[0]["text"])["confirmation_token"]

        # Step 2: confirm
        confirmed_args = dict(args)
        confirmed_args["confirmation_token"] = token
        confirmed_args["confirmation_secret"] = "my-secret"
        result = await handlers_with_secret.handle_tool_call("delete_alert", confirmed_args)
        text = json.loads(result[0]["text"])
        assert "deleted" in text

    @pytest.mark.asyncio
    async def test_guarded_tool_wrong_secret_rejected(self, handlers_with_secret):
        """Wrong secret is rejected even with valid token."""
        args = {"alert_id": "ocid1.alarm.oc1..abc"}
        result = await handlers_with_secret.handle_tool_call("delete_alert", args)
        token = json.loads(result[0]["text"])["confirmation_token"]

        confirmed_args = dict(args)
        confirmed_args["confirmation_token"] = token
        confirmed_args["confirmation_secret"] = "wrong-secret"
        result = await handlers_with_secret.handle_tool_call("delete_alert", confirmed_args)
        text = json.loads(result[0]["text"])
        assert text["status"] == "confirmation_failed"

    @pytest.mark.asyncio
    async def test_guarded_tool_changed_args_rejected(self, handlers_with_secret):
        """Token for alert A cannot authorize delete of alert B."""
        args_a = {"alert_id": "ocid1.alarm.oc1..aaa"}
        result = await handlers_with_secret.handle_tool_call("delete_alert", args_a)
        token = json.loads(result[0]["text"])["confirmation_token"]

        args_b = {"alert_id": "ocid1.alarm.oc1..bbb"}
        args_b["confirmation_token"] = token
        args_b["confirmation_secret"] = "my-secret"
        result = await handlers_with_secret.handle_tool_call("delete_alert", args_b)
        text = json.loads(result[0]["text"])
        assert text["status"] == "confirmation_failed"

    @pytest.mark.asyncio
    async def test_guarded_tool_token_reuse_rejected(self, handlers_with_secret):
        """Token can only be used once."""
        args = {"alert_id": "ocid1.alarm.oc1..abc"}
        result = await handlers_with_secret.handle_tool_call("delete_alert", args)
        token = json.loads(result[0]["text"])["confirmation_token"]

        confirmed_args = dict(args)
        confirmed_args["confirmation_token"] = token
        confirmed_args["confirmation_secret"] = "my-secret"
        # First use succeeds
        await handlers_with_secret.handle_tool_call("delete_alert", confirmed_args)
        # Second use fails
        result = await handlers_with_secret.handle_tool_call("delete_alert", confirmed_args)
        text = json.loads(result[0]["text"])
        assert text["status"] == "confirmation_failed"

    @pytest.mark.asyncio
    async def test_non_guarded_tool_executes_directly(self, handlers_with_secret):
        """Non-guarded tools are not affected by confirmation."""
        handlers_with_secret.alarm_service.list_alerts = AsyncMock(return_value=[])
        result = await handlers_with_secret.handle_tool_call("list_alerts", {})
        text = result[0]["text"]
        assert "confirmation_required" not in text
        assert "confirmation_unavailable" not in text

    @pytest.mark.asyncio
    async def test_create_alert_is_guarded(self, handlers_with_secret):
        """create_* tools go through the same two-factor confirmation flow
        as update_* and delete_*. Previously these were ungated despite
        descriptions claiming "APPROVAL REQUIRED" — see docs/reviews/
        2026-04-22-mcp-builder-review.md.
        """
        handlers_with_secret.alarm_service.create_alert = AsyncMock(
            return_value={"alarm_id": "new"}
        )
        # First call (no token) must return a confirmation request, not execute.
        result = await handlers_with_secret.handle_tool_call(
            "create_alert",
            {"display_name": "Test", "query": "* | stats count",
             "destination_topic_id": "ocid1.topic.1"},
        )
        text = json.loads(result[0]["text"])
        assert text.get("status") == "confirmation_required"
        assert "confirmation_token" in text
        # The underlying service must NOT be called on the first (unconfirmed) call.
        handlers_with_secret.alarm_service.create_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_confirmation_secret_succeeds(self, handlers):
        """First-time setup should persist a hashed secret."""
        result = await handlers.handle_tool_call(
            "setup_confirmation_secret",
            {
                "confirmation_secret": "my-secret",
                "confirmation_secret_confirm": "my-secret",
            },
        )
        text = json.loads(result[0]["text"])
        assert text["status"] == "configured"
        assert handlers.secret_store.has_secret() is True
        assert handlers.secret_store.is_valid() is True

    @pytest.mark.asyncio
    async def test_setup_confirmation_secret_rejects_mismatch(self, handlers):
        """Mismatched setup entries should fail validation."""
        result = await handlers.handle_tool_call(
            "setup_confirmation_secret",
            {
                "confirmation_secret": "my-secret",
                "confirmation_secret_confirm": "other-secret",
            },
        )
        text = json.loads(result[0]["text"])
        assert text["status"] == "validation_error"
        assert handlers.secret_store.has_secret() is False

    @pytest.mark.asyncio
    async def test_setup_confirmation_secret_rejects_short_secret(self, handlers):
        """Minimum length validation should match SecretStore rules."""
        result = await handlers.handle_tool_call(
            "setup_confirmation_secret",
            {
                "confirmation_secret": "short",
                "confirmation_secret_confirm": "short",
            },
        )
        text = json.loads(result[0]["text"])
        assert text["status"] == "validation_error"
        assert "at least 8 characters" in text["error"]

    @pytest.mark.asyncio
    async def test_setup_confirmation_secret_refuses_overwrite(self, handlers_with_secret):
        """In-band setup is initial-creation only."""
        result = await handlers_with_secret.handle_tool_call(
            "setup_confirmation_secret",
            {
                "confirmation_secret": "new-secret",
                "confirmation_secret_confirm": "new-secret",
            },
        )
        text = json.loads(result[0]["text"])
        assert text["status"] == "already_configured"

    @pytest.mark.asyncio
    async def test_guarded_tool_uses_normal_confirmation_after_setup(self, handlers):
        """Once a secret is set in-band, guarded tools should use the normal flow."""
        handlers.alarm_service.delete_alert = AsyncMock(return_value={"deleted": True})

        await handlers.handle_tool_call(
            "setup_confirmation_secret",
            {
                "confirmation_secret": "my-secret",
                "confirmation_secret_confirm": "my-secret",
            },
        )
        result = await handlers.handle_tool_call(
            "delete_alert", {"alert_id": "ocid1.alarm.oc1..abc"}
        )
        text = json.loads(result[0]["text"])
        assert text["status"] == "confirmation_required"


# ---------------------------------------------------------------------------
# Parametrized Integration Tests — all 6 guarded tools
# ---------------------------------------------------------------------------

GUARDED_TOOL_ARGS = {
    "delete_alert": {"alert_id": "ocid1.alarm.test"},
    "delete_saved_search": {"saved_search_id": "ocid1.ss.test"},
    "delete_dashboard": {"dashboard_id": "ocid1.db.test"},
    "update_alert": {"alert_id": "ocid1.alarm.test", "severity": "WARNING"},
    "update_saved_search": {"saved_search_id": "ocid1.ss.test", "query": "* | head 5"},
    "add_dashboard_tile": {
        "dashboard_id": "ocid1.db.test", "title": "T",
        "query": "* | stats count", "visualization_type": "bar",
    },
}


class TestConfirmationIntegration:
    """End-to-end confirmation flow across all 6 guarded tools."""

    @pytest.fixture
    def handlers_confirmed(self, settings, mock_oci_client, mock_cache,
                           mock_query_logger, mock_context_manager,
                           mock_user_store, mock_preference_store,
                           mock_secret_store, mock_audit_logger):
        mock_secret_store.set_secret("integration-secret")
        h = MCPHandlers(
            settings=settings,
            oci_client=mock_oci_client,
            cache=mock_cache,
            query_logger=mock_query_logger,
            context_manager=mock_context_manager,
            user_store=mock_user_store,
            preference_store=mock_preference_store,
            secret_store=mock_secret_store,
            audit_logger=mock_audit_logger,
        )
        # Mock all service methods to prevent actual OCI calls
        h.alarm_service.delete_alert = AsyncMock(return_value={"deleted": True})
        h.alarm_service.update_alert = AsyncMock(return_value={"alarm_id": "a", "updated": []})
        h.saved_search.delete_search = AsyncMock(return_value=None)
        h.saved_search.update_search = AsyncMock(return_value={"id": "s"})
        h.dashboard_service.delete_dashboard = AsyncMock(return_value={"deleted": True})
        h.dashboard_service.add_tile = AsyncMock(return_value={"dashboard_id": "d"})
        return h

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", list(GUARDED_TOOL_ARGS.keys()))
    async def test_all_guarded_tools_require_confirmation(
        self, handlers_confirmed, tool_name
    ):
        """Every guarded tool must return confirmation_required on first call."""
        args = GUARDED_TOOL_ARGS[tool_name]
        result = await handlers_confirmed.handle_tool_call(tool_name, dict(args))
        text = json.loads(result[0]["text"])
        assert text["status"] == "confirmation_required", (
            f"{tool_name} did not require confirmation"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", list(GUARDED_TOOL_ARGS.keys()))
    async def test_all_guarded_tools_execute_with_valid_confirmation(
        self, handlers_confirmed, tool_name
    ):
        """Every guarded tool executes after valid token + secret."""
        args = dict(GUARDED_TOOL_ARGS[tool_name])

        # Get token
        result = await handlers_confirmed.handle_tool_call(tool_name, dict(args))
        token = json.loads(result[0]["text"])["confirmation_token"]

        # Confirm
        confirmed_args = dict(args)
        confirmed_args["confirmation_token"] = token
        confirmed_args["confirmation_secret"] = "integration-secret"
        result = await handlers_confirmed.handle_tool_call(tool_name, confirmed_args)
        text = json.loads(result[0]["text"])
        assert text.get("status") != "confirmation_required", (
            f"{tool_name} still required confirmation"
        )
        assert text.get("status") != "confirmation_failed", (
            f"{tool_name} confirmation failed"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", list(GUARDED_TOOL_ARGS.keys()))
    async def test_all_guarded_tools_reject_arg_change(
        self, handlers_confirmed, tool_name
    ):
        """Token bound to original args — changing any arg causes failure."""
        args = dict(GUARDED_TOOL_ARGS[tool_name])

        # Get token with original args
        result = await handlers_confirmed.handle_tool_call(tool_name, dict(args))
        token = json.loads(result[0]["text"])["confirmation_token"]

        # Modify an arg
        modified_args = dict(args)
        first_key = list(modified_args.keys())[0]
        modified_args[first_key] = modified_args[first_key] + "_CHANGED"
        modified_args["confirmation_token"] = token
        modified_args["confirmation_secret"] = "integration-secret"

        result = await handlers_confirmed.handle_tool_call(tool_name, modified_args)
        text = json.loads(result[0]["text"])
        assert text["status"] == "confirmation_failed", (
            f"{tool_name} did not reject changed args"
        )


# ---------------------------------------------------------------------------
# Audit Logging Tests
# ---------------------------------------------------------------------------

class TestAuditLogging:
    """Audit logger records guarded tool outcomes."""

    @pytest.fixture
    def audit_dir(self, tmp_path):
        return tmp_path / "audit_test"

    @pytest.fixture
    def audit_logger(self, audit_dir):
        return AuditLogger(audit_dir)

    @pytest.fixture
    def secret_store(self, tmp_path):
        ss = SecretStore(tmp_path / "secret_audit.yaml")
        ss.set_secret("audit-test-secret")
        return ss

    @pytest.fixture
    def audit_handlers(self, settings, mock_oci_client, mock_cache,
                       mock_query_logger, mock_context_manager,
                       mock_user_store, mock_preference_store,
                       secret_store, audit_logger):
        h = MCPHandlers(
            settings=settings,
            oci_client=mock_oci_client,
            cache=mock_cache,
            query_logger=mock_query_logger,
            context_manager=mock_context_manager,
            user_store=mock_user_store,
            preference_store=mock_preference_store,
            secret_store=secret_store,
            audit_logger=audit_logger,
        )
        h.alarm_service.delete_alert = AsyncMock(return_value={"deleted": True})
        return h

    @pytest.mark.asyncio
    async def test_guarded_tool_audit_trail(self, audit_handlers, audit_dir):
        """Full confirmation flow produces audit entries for each outcome."""
        args = {"alert_id": "ocid1.alarm.oc1..audit"}

        # Step 1: request confirmation  ->  confirmation_requested
        result = await audit_handlers.handle_tool_call("delete_alert", dict(args))
        token = json.loads(result[0]["text"])["confirmation_token"]

        # Step 2: confirm and execute  ->  confirmed + executed
        confirmed_args = dict(args)
        confirmed_args["confirmation_token"] = token
        confirmed_args["confirmation_secret"] = "audit-test-secret"
        await audit_handlers.handle_tool_call("delete_alert", confirmed_args)

        # Read audit log and check entries
        log_file = audit_dir / "audit.log"
        assert log_file.is_file(), "Audit log file should exist"
        lines = log_file.read_text().strip().splitlines()
        outcomes = [json.loads(line)["outcome"] for line in lines]
        assert "confirmation_requested" in outcomes
        assert "confirmed" in outcomes
        assert "executed" in outcomes


# ---------------------------------------------------------------------------
# get_query_examples via UnifiedCatalog (Task 8)
# ---------------------------------------------------------------------------

def _make_handler_with_catalog(tmp_path):
    """Helper: build MCPHandlers with a real UserStore + UnifiedCatalog at tmp_path."""
    from unittest.mock import MagicMock
    from oci_logan_mcp.user_store import UserStore
    from oci_logan_mcp.preferences import PreferenceStore
    from oci_logan_mcp.secret_store import SecretStore
    from oci_logan_mcp.audit import AuditLogger

    user_store = UserStore(base_dir=tmp_path, user_id="testuser")
    user_dir = tmp_path / "users" / "testuser"
    user_dir.mkdir(parents=True, exist_ok=True)
    pref_store = PreferenceStore(user_dir=user_dir)
    secret_store = SecretStore(tmp_path / "secret.yaml")
    audit = AuditLogger(tmp_path / "audit")

    settings = Settings()
    settings.log_analytics.namespace = "testns"
    settings.log_analytics.default_compartment_id = "ocid1.compartment.default"
    settings.query.max_results = 1000
    settings.query.default_time_range = "last_1_hour"

    oci_client = MagicMock()
    oci_client.namespace = "testns"
    oci_client.compartment_id = "ocid1.compartment.default"
    oci_client._config = {"tenancy": "ocid1.tenancy.test"}
    cache = MagicMock()
    cache.get = MagicMock(return_value=None)
    cache.set = MagicMock()
    query_logger = MagicMock()
    query_logger.get_recent_queries = MagicMock(return_value=[])
    ctx = MagicMock()
    ctx.get_tenancy_context = MagicMock(return_value={"namespace": "testns"})

    return MCPHandlers(
        settings=settings,
        oci_client=oci_client,
        cache=cache,
        query_logger=query_logger,
        context_manager=ctx,
        user_store=user_store,
        preference_store=pref_store,
        secret_store=secret_store,
        audit_logger=audit,
    )


@pytest.mark.asyncio
async def test_get_query_examples_uses_catalog(tmp_path):
    """After rewiring, get_query_examples sources from UnifiedCatalog."""
    handler = _make_handler_with_catalog(tmp_path)
    result = await handler._get_query_examples({"category": "all"})
    body = json.loads(result[0]["text"])
    assert "categories" in body
    assert "examples" in body
    assert isinstance(body["examples"], dict)
    # Entries grouped by category — each entry has exactly name/query/description
    for cat, entries in body["examples"].items():
        for e in entries:
            assert set(e.keys()) == {"name", "query", "description"}


@pytest.mark.asyncio
async def test_get_query_examples_filter_by_category(tmp_path):
    """Category filter works through catalog."""
    handler = _make_handler_with_catalog(tmp_path)
    result = await handler._get_query_examples({"category": "basic"})
    body = json.loads(result[0]["text"])
    # Either returns the category entries or an error if category unknown — shape matches existing behavior
    assert "category" in body or "error" in body


# ---------------------------------------------------------------------------
# Tenancy-context auto-capture suppression under read-only (Task 2.5)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_only_skips_tenancy_context_update_for_log_sources(
    handlers, settings, monkeypatch
):
    settings.read_only = True
    captured = {"called": False}

    async def fake_get_log_sources(compartment_id=None):
        return [{"name": "linux_syslog"}]

    monkeypatch.setattr(handlers.schema_manager, "get_log_sources", fake_get_log_sources)
    monkeypatch.setattr(
        handlers.context_manager,
        "update_log_sources",
        lambda sources: captured.__setitem__("called", True),
    )

    result = await handlers.handle_tool_call("list_log_sources", {})
    assert "linux_syslog" in result[0]["text"]
    assert captured["called"] is False


@pytest.mark.asyncio
async def test_non_read_only_still_updates_tenancy_context_for_log_sources(
    handlers, settings, monkeypatch
):
    settings.read_only = False
    captured = {"called": False}

    async def fake_get_log_sources(compartment_id=None):
        return [{"name": "linux_syslog"}]

    monkeypatch.setattr(handlers.schema_manager, "get_log_sources", fake_get_log_sources)
    monkeypatch.setattr(
        handlers.context_manager,
        "update_log_sources",
        lambda sources: captured.__setitem__("called", True),
    )

    await handlers.handle_tool_call("list_log_sources", {})
    assert captured["called"] is True


@pytest.mark.asyncio
async def test_read_only_skips_tenancy_context_update_for_fields(
    handlers, settings, monkeypatch
):
    settings.read_only = True
    captured = {"called": False}

    async def fake_get_fields(source_name=None):
        return []

    monkeypatch.setattr(handlers.schema_manager, "get_fields", fake_get_fields)
    monkeypatch.setattr(
        handlers.context_manager,
        "update_confirmed_fields",
        lambda fields: captured.__setitem__("called", True),
    )

    await handlers.handle_tool_call("list_fields", {})
    assert captured["called"] is False


@pytest.mark.asyncio
async def test_non_read_only_still_updates_tenancy_context_for_fields(
    handlers, settings, monkeypatch
):
    settings.read_only = False
    captured = {"called": False}

    async def fake_get_fields(source_name=None):
        return []

    monkeypatch.setattr(handlers.schema_manager, "get_fields", fake_get_fields)
    monkeypatch.setattr(
        handlers.context_manager,
        "update_confirmed_fields",
        lambda fields: captured.__setitem__("called", True),
    )

    await handlers.handle_tool_call("list_fields", {})
    assert captured["called"] is True


@pytest.mark.asyncio
async def test_read_only_skips_tenancy_context_update_for_compartments(
    handlers, settings, monkeypatch
):
    settings.read_only = True
    captured = {"called": False}

    handlers.oci_client.list_compartments = AsyncMock(return_value=[])
    monkeypatch.setattr(
        handlers.context_manager,
        "update_compartments",
        lambda compartments: captured.__setitem__("called", True),
    )

    await handlers.handle_tool_call("list_compartments", {})
    assert captured["called"] is False


@pytest.mark.asyncio
async def test_non_read_only_still_updates_tenancy_context_for_compartments(
    handlers, settings, monkeypatch
):
    settings.read_only = False
    captured = {"called": False}

    handlers.oci_client.list_compartments = AsyncMock(return_value=[])
    monkeypatch.setattr(
        handlers.context_manager,
        "update_compartments",
        lambda compartments: captured.__setitem__("called", True),
    )

    await handlers.handle_tool_call("list_compartments", {})
    assert captured["called"] is True


# ---------------------------------------------------------------------------
# Read-only guard integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_only_blocks_mutating_tool(handlers, settings):
    settings.read_only = True
    result = await handlers.handle_tool_call("delete_alert", {"alert_id": "ocid1.alert.x"})
    assert len(result) == 1
    payload = json.loads(result[0]["text"])
    assert payload["status"] == "read_only_blocked"
    assert payload["tool"] == "delete_alert"
    assert "read-only" in payload["error"].lower()


@pytest.mark.asyncio
async def test_read_only_allows_reader(handlers, settings, monkeypatch):
    settings.read_only = True
    # Stub the reader to avoid OCI calls
    async def fake_list_saved_searches(args):
        return [{"type": "text", "text": "[]"}]
    monkeypatch.setattr(handlers, "_list_saved_searches", fake_list_saved_searches)
    result = await handlers.handle_tool_call("list_saved_searches", {})
    assert result == [{"type": "text", "text": "[]"}]


@pytest.mark.asyncio
async def test_read_only_disabled_does_not_block(handlers, settings, monkeypatch):
    settings.read_only = False
    # Stub a mutator so it doesn't actually hit OCI
    async def fake_delete_alert(args):
        return [{"type": "text", "text": "deleted"}]
    monkeypatch.setattr(handlers, "_delete_alert", fake_delete_alert)
    # Bypass confirmation gate for this test
    monkeypatch.setattr(handlers.confirmation_manager, "is_guarded", lambda name: False)
    monkeypatch.setattr(handlers.confirmation_manager, "is_guarded_call", lambda name, args: False)
    result = await handlers.handle_tool_call("delete_alert", {})
    assert result == [{"type": "text", "text": "deleted"}]


# ---------------------------------------------------------------------------
# explain_query and get_session_budget tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_query_returns_estimate(handlers):
    result = await handlers.handle_tool_call(
        "explain_query",
        {"query": "'Log Source' = 'x'", "time_range": "last_1_hour"},
    )
    payload = json.loads(result[0]["text"])
    assert "estimated_bytes" in payload
    assert "estimated_cost_usd" in payload
    assert "estimated_eta_seconds" in payload
    assert payload["confidence"] in {"low", "medium", "high"}


@pytest.mark.asyncio
async def test_get_session_budget_returns_usage(handlers):
    result = await handlers.handle_tool_call("get_session_budget", {})
    payload = json.loads(result[0]["text"])
    assert "used" in payload
    assert "remaining" in payload
    assert "limits" in payload
    for key in ("queries", "bytes", "cost_usd"):
        assert key in payload["used"]
        assert key in payload["remaining"]


# ---------------------------------------------------------------------------
# Invoked audit event tests (Task N6)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoked_event_fires_for_non_guarded_tool(handlers, tmp_path):
    """A non-guarded tool call produces an 'invoked' audit entry."""
    audit_dir = tmp_path / "audit_invoked"
    audit = AuditLogger(audit_dir)
    handlers.audit_logger = audit

    await handlers.handle_tool_call("get_current_context", {})

    log_file = audit_dir / "audit.log"
    assert log_file.is_file()
    lines = log_file.read_text().strip().splitlines()
    outcomes = [json.loads(ln)["outcome"] for ln in lines]
    assert "invoked" in outcomes


@pytest.mark.asyncio
async def test_invoked_event_fires_before_read_only_block(handlers, settings, tmp_path):
    """A read-only-blocked tool produces both 'invoked' and 'read_only_blocked', in that order."""
    audit_dir = tmp_path / "audit_ro_invoked"
    audit = AuditLogger(audit_dir)
    handlers.audit_logger = audit
    settings.read_only = True

    await handlers.handle_tool_call("delete_alert", {"alert_id": "ocid1.alarm.x"})

    log_file = audit_dir / "audit.log"
    assert log_file.is_file()
    lines = log_file.read_text().strip().splitlines()
    outcomes = [json.loads(ln)["outcome"] for ln in lines]
    assert outcomes == ["invoked", "read_only_blocked"]


@pytest.mark.asyncio
async def test_invoked_event_strips_confirmation_secret(handlers, tmp_path):
    """The 'invoked' entry must not contain confirmation_secret in args."""
    audit_dir = tmp_path / "audit_secret_strip"
    audit = AuditLogger(audit_dir)
    handlers.audit_logger = audit

    await handlers.handle_tool_call(
        "get_current_context",
        {"confirmation_secret": "hunter2", "some_arg": "ok"},
    )

    log_file = audit_dir / "audit.log"
    lines = log_file.read_text().strip().splitlines()
    invoked_entries = [json.loads(ln) for ln in lines if json.loads(ln)["outcome"] == "invoked"]
    assert invoked_entries, "No invoked entry found"
    args = invoked_entries[0]["args"]
    assert "confirmation_secret" not in args
    assert args.get("some_arg") == "ok"


@pytest.mark.asyncio
async def test_export_transcript_tool_returns_path_and_count(handlers, tmp_path):
    """export_transcript returns path and event_count >= number of tool calls made."""
    import os

    audit_dir = tmp_path / "audit_export"
    audit = AuditLogger(audit_dir, session_id="test-export-session")
    handlers.audit_logger = audit
    handlers.settings.transcript_dir = tmp_path / "transcripts"

    await handlers.handle_tool_call("get_current_context", {})
    await handlers.handle_tool_call("list_saved_searches", {})

    result = await handlers.handle_tool_call(
        "export_transcript", {"session_id": "current"},
    )
    payload = json.loads(result[0]["text"])
    assert "path" in payload
    assert "event_count" in payload
    assert payload["event_count"] >= 2
    assert os.path.isfile(payload["path"])


class TestDiffTimeWindows:
    @pytest.mark.asyncio
    async def test_diff_time_windows_routes_through_handler(self, handlers):
        """diff_time_windows tool routes to DiffTool and returns JSON payload."""
        # Stub DiffTool.run to avoid calling QueryEngine in unit tests.
        handlers.diff_tool.run = AsyncMock(return_value={
            "current": {"total": 100, "rows": []},
            "comparison": {"total": 100, "rows": []},
            "delta": [],
            "summary": "No significant change between windows.",
            "metadata": {},
        })

        result = await handlers.handle_tool_call(
            "diff_time_windows",
            {
                "query": "'Log Source' = 'Audit Logs'",
                "current_window": {"time_range": "last_1_hour"},
                "comparison_window": {"time_range": "last_1_hour"},
            },
        )

        assert result[0]["type"] == "text"
        payload = json.loads(result[0]["text"])
        assert payload["summary"] == "No significant change between windows."
        handlers.diff_tool.run.assert_awaited_once_with(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=None,
        )

    @pytest.mark.asyncio
    async def test_diff_time_windows_budget_exceeded_structured(self, handlers):
        """BudgetExceededError surfaces as a structured payload, not plain text."""
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        handlers.diff_tool.run = AsyncMock(side_effect=BudgetExceededError("bytes limit hit"))

        result = await handlers.handle_tool_call(
            "diff_time_windows",
            {
                "query": "*",
                "current_window": {"time_range": "last_1_hour"},
                "comparison_window": {"time_range": "last_1_hour"},
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "bytes limit hit" in payload["error"]
        # Pin the snapshot-to-dict wiring so a rename of snapshot()/to_dict()
        # fails here instead of silently degrading A1's budget awareness.
        assert "budget" in payload
        assert isinstance(payload["budget"], dict)


class TestPivotOnEntity:
    @pytest.mark.asyncio
    async def test_pivot_on_entity_routes_through_handler(self, handlers):
        """pivot_on_entity tool routes to PivotTool and returns JSON payload."""
        handlers.pivot_tool.run = AsyncMock(return_value={
            "entity": {"type": "host", "value": "web-01", "field": "Host"},
            "by_source": [{"source": "Audit Logs", "rows": [{"Time": "2026-04-20T10:00:00Z"}], "truncated": False}],
            "cross_source_timeline": [{"timestamp": "2026-04-20T10:00:00Z", "source": "Audit Logs"}],
            "stats": {"total_events": 1, "sources_matched": 1},
            "partial": False,
            "metadata": {},
        })

        result = await handlers.handle_tool_call(
            "pivot_on_entity",
            {
                "entity_type": "host",
                "entity_value": "web-01",
                "time_range": {"time_range": "last_1_hour"},
            },
        )

        assert result[0]["type"] == "text"
        payload = json.loads(result[0]["text"])
        assert payload["entity"]["value"] == "web-01"
        assert payload["stats"]["total_events"] == 1
        handlers.pivot_tool.run.assert_awaited_once_with(
            entity_type="host",
            entity_value="web-01",
            time_range={"time_range": "last_1_hour"},
            sources=None,
            max_rows_per_source=100,
            field_name=None,
        )

    @pytest.mark.asyncio
    async def test_pivot_on_entity_budget_exceeded_structured(self, handlers):
        """BudgetExceededError returns structured payload, not plain text."""
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        handlers.pivot_tool.run = AsyncMock(side_effect=BudgetExceededError("cost limit hit"))

        result = await handlers.handle_tool_call(
            "pivot_on_entity",
            {
                "entity_type": "host",
                "entity_value": "web-01",
                "time_range": {"time_range": "last_1_hour"},
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "cost limit hit" in payload["error"]
        assert "budget" in payload
        assert isinstance(payload["budget"], dict)

    @pytest.mark.asyncio
    async def test_pivot_on_entity_value_error_structured(self, handlers):
        """ValueError (e.g. custom entity_type without field_name) returns structured error."""
        handlers.pivot_tool.run = AsyncMock(side_effect=ValueError("field_name is required"))

        result = await handlers.handle_tool_call(
            "pivot_on_entity",
            {"entity_type": "custom", "entity_value": "x", "time_range": {"time_range": "last_1_hour"}},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert "field_name is required" in payload["error"]


class TestIngestionHealth:
    @pytest.mark.asyncio
    async def test_ingestion_health_routes_through_handler(self, handlers):
        """ingestion_health tool routes to IngestionHealthTool and returns JSON."""
        handlers.ingestion_health_tool.run = AsyncMock(return_value={
            "summary": {"sources_healthy": 1, "sources_stopped": 0, "sources_unknown": 0},
            "checked_at": "2026-04-22T10:00:00+00:00",
            "findings": [],
            "metadata": {},
        })

        result = await handlers.handle_tool_call(
            "ingestion_health",
            {"severity_filter": "warn"},
        )

        assert result[0]["type"] == "text"
        payload = json.loads(result[0]["text"])
        assert payload["summary"]["sources_healthy"] == 1
        handlers.ingestion_health_tool.run.assert_awaited_once_with(
            compartment_id=None,
            sources=None,
            severity_filter="warn",
        )

    @pytest.mark.asyncio
    async def test_ingestion_health_budget_exceeded_structured(self, handlers):
        """BudgetExceededError surfaces as a structured payload, not plain text."""
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        handlers.ingestion_health_tool.run = AsyncMock(
            side_effect=BudgetExceededError("bytes limit hit")
        )

        result = await handlers.handle_tool_call("ingestion_health", {})

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "bytes limit hit" in payload["error"]


class TestParserFailureTriage:
    @pytest.mark.asyncio
    async def test_routes_to_parser_triage_tool(self, handlers):
        """parser_failure_triage routes to ParserTriageTool and returns JSON."""
        handlers.parser_triage_tool.run = AsyncMock(return_value={
            "failures": [],
            "total_failure_count": 0,
        })

        result = await handlers.handle_tool_call(
            "parser_failure_triage",
            {"time_range": "last_7_days", "top_n": 5},
        )

        assert result[0]["type"] == "text"
        payload = json.loads(result[0]["text"])
        assert "failures" in payload
        assert payload["total_failure_count"] == 0
        handlers.parser_triage_tool.run.assert_awaited_once_with(
            time_range="last_7_days",
            top_n=5,
        )

    @pytest.mark.asyncio
    async def test_default_time_range_is_valid_token(self, handlers):
        """Default time_range must be a token the time_parser accepts."""
        from oci_logan_mcp.time_parser import TIME_RANGES
        handlers.parser_triage_tool.run = AsyncMock(return_value={
            "failures": [], "total_failure_count": 0,
        })

        await handlers.handle_tool_call("parser_failure_triage", {})

        kwargs = handlers.parser_triage_tool.run.await_args.kwargs
        assert kwargs["time_range"] in TIME_RANGES

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_structured_payload(self, handlers):
        """BudgetExceededError surfaces as structured payload, not plain text."""
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        handlers.parser_triage_tool.run = AsyncMock(
            side_effect=BudgetExceededError("query limit hit")
        )

        result = await handlers.handle_tool_call(
            "parser_failure_triage",
            {},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "query limit hit" in payload["error"]
        assert "budget" in payload

    @pytest.mark.asyncio
    async def test_bad_top_n_returns_structured_error(self, handlers):
        """Non-integer top_n returns a structured error, not a stringified ValueError."""
        result = await handlers.handle_tool_call(
            "parser_failure_triage",
            {"top_n": "abc"},
        )
        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert "top_n" in payload["error"]

    @pytest.mark.asyncio
    async def test_negative_top_n_rejected_before_engine_call(self, handlers):
        """Negative top_n would produce `| head -N`, which Logan rejects with a 400.
        Verified live: `InvalidParameter: Invalid value for LIMIT: -1 must be
        between 0 and 50000.` Reject at the handler instead of letting the
        engine bounce it back as an opaque error."""
        handlers.parser_triage_tool.run = AsyncMock()
        result = await handlers.handle_tool_call(
            "parser_failure_triage",
            {"top_n": -1},
        )
        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert "between 1 and 50000" in payload["error"]
        # Never reached the engine.
        handlers.parser_triage_tool.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_top_n_rejected(self, handlers):
        """top_n=0 is a syntactically valid but useless Logan query; reject it."""
        handlers.parser_triage_tool.run = AsyncMock()
        result = await handlers.handle_tool_call(
            "parser_failure_triage",
            {"top_n": 0},
        )
        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        handlers.parser_triage_tool.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_top_n_exceeding_logan_limit_rejected(self, handlers):
        """Logan's LIMIT bound is 50000 (verified live). Reject above that."""
        handlers.parser_triage_tool.run = AsyncMock()
        result = await handlers.handle_tool_call(
            "parser_failure_triage",
            {"top_n": 50001},
        )
        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        handlers.parser_triage_tool.run.assert_not_awaited()


class TestInvestigateIncident:
    @pytest.mark.asyncio
    async def test_routes_to_investigate_tool(self, handlers):
        handlers.investigate_tool.run = AsyncMock(return_value={
            "summary": "ok",
            "seed": {"query": "'x' = 'y'", "seed_filter": "'x' = 'y'",
                     "seed_filter_degraded": False, "time_range": "last_1_hour",
                     "compartment_id": None},
            "ingestion_health": None, "parser_failures": None,
            "anomalous_sources": [], "cross_source_timeline": None,
            "next_steps": [], "budget": {}, "partial": False,
            "partial_reasons": [], "elapsed_seconds": 0.1,
        })
        result = await handlers.handle_tool_call(
            "investigate_incident",
            {"query": "'x' = 'y'", "time_range": "last_1_hour", "top_k": 3},
        )
        payload = json.loads(result[0]["text"])
        assert payload["summary"] == "ok"
        handlers.investigate_tool.run.assert_awaited_once_with(
            query="'x' = 'y'",
            time_range="last_1_hour",
            top_k=3,
            compartment_id=None,
        )

    @pytest.mark.asyncio
    async def test_partial_report_forwarded_verbatim(self, handlers):
        """A1 diverges from other triage tools: no {status: budget_exceeded} wrapper."""
        handlers.investigate_tool.run = AsyncMock(return_value={
            "summary": "partial", "seed": {},
            "ingestion_health": None, "parser_failures": None,
            "anomalous_sources": [], "cross_source_timeline": None,
            "next_steps": [], "budget": {}, "partial": True,
            "partial_reasons": ["budget_exceeded"], "elapsed_seconds": 3.0,
        })
        result = await handlers.handle_tool_call(
            "investigate_incident",
            {"query": "*"},
        )
        payload = json.loads(result[0]["text"])
        # Partial report shape forwarded verbatim, NOT wrapped in {status: "..."}
        assert "status" not in payload
        assert payload["partial"] is True
        assert payload["partial_reasons"] == ["budget_exceeded"]

    @pytest.mark.asyncio
    async def test_missing_query_returns_structured_error(self, handlers):
        handlers.investigate_tool.run = AsyncMock()
        result = await handlers.handle_tool_call("investigate_incident", {})
        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert "query" in payload["error"]
        handlers.investigate_tool.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bad_top_k_returns_structured_error(self, handlers):
        handlers.investigate_tool.run = AsyncMock()
        for bad in (-1, 0, 4, 10, "abc"):
            result = await handlers.handle_tool_call(
                "investigate_incident", {"query": "*", "top_k": bad},
            )
            payload = json.loads(result[0]["text"])
            assert payload["status"] == "error", f"top_k={bad!r} did not error"
        handlers.investigate_tool.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_budget_exception_surfaces_as_error(self, handlers):
        handlers.investigate_tool.run = AsyncMock(side_effect=RuntimeError("unexpected"))
        result = await handlers.handle_tool_call("investigate_incident", {"query": "*"})
        payload = json.loads(result[0]["text"])
        # Falls through to handle_tool_call's generic exception path
        assert "unexpected" in payload.get("error", "") or "unexpected" in result[0]["text"]


class TestWhyDidThisFire:
    @pytest.mark.asyncio
    async def test_routes_to_why_did_this_fire_tool(self, handlers):
        handlers.why_did_this_fire_tool.run = AsyncMock(return_value={
            "alarm": {"alarm_id": "ocid1.alarm.oc1..test"},
            "top_contributing_rows": [],
        })

        result = await handlers.handle_tool_call(
            "why_did_this_fire",
            {"alarm_ocid": "ocid1.alarm.oc1..test", "fire_time": "2026-04-23T10:00:00Z"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["alarm"]["alarm_id"] == "ocid1.alarm.oc1..test"
        handlers.why_did_this_fire_tool.run.assert_awaited_once_with(
            alarm_ocid="ocid1.alarm.oc1..test",
            fire_time="2026-04-23T10:00:00Z",
            window_before_seconds=None,
            window_after_seconds=60,
        )

    @pytest.mark.asyncio
    async def test_missing_alarm_ocid_returns_structured_error(self, handlers):
        handlers.why_did_this_fire_tool.run = AsyncMock()

        result = await handlers.handle_tool_call(
            "why_did_this_fire",
            {"fire_time": "2026-04-23T10:00:00Z"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert "alarm_ocid" in payload["error"]
        handlers.why_did_this_fire_tool.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_window_before_seconds_returns_structured_error(self, handlers):
        handlers.why_did_this_fire_tool.run = AsyncMock()

        result = await handlers.handle_tool_call(
            "why_did_this_fire",
            {
                "alarm_ocid": "ocid1.alarm.oc1..test",
                "fire_time": "2026-04-23T10:00:00Z",
                "window_before_seconds": "abc",
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert "window_before_seconds" in payload["error"]
        handlers.why_did_this_fire_tool.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_window_after_seconds_returns_structured_error(self, handlers):
        handlers.why_did_this_fire_tool.run = AsyncMock()

        result = await handlers.handle_tool_call(
            "why_did_this_fire",
            {
                "alarm_ocid": "ocid1.alarm.oc1..test",
                "fire_time": "2026-04-23T10:00:00Z",
                "window_after_seconds": -1,
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert "window_after_seconds" in payload["error"]
        handlers.why_did_this_fire_tool.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_structured_payload(self, handlers):
        from oci_logan_mcp.budget_tracker import BudgetExceededError

        handlers.why_did_this_fire_tool.run = AsyncMock(
            side_effect=BudgetExceededError("bytes limit hit")
        )

        result = await handlers.handle_tool_call(
            "why_did_this_fire",
            {"alarm_ocid": "ocid1.alarm.oc1..test", "fire_time": "2026-04-23T10:00:00Z"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "bytes limit hit" in payload["error"]
        assert "budget" in payload


class TestTraceRequestId:
    @pytest.mark.asyncio
    async def test_trace_request_id_routes_through_handler(self, handlers):
        handlers.trace_request_id_tool.run = AsyncMock(return_value={
            "request_id": "req-42",
            "events": [{"timestamp": "2026-04-23T10:00:00Z", "source": "App Logs"}],
            "sources_matched": ["App Logs"],
        })

        result = await handlers.handle_tool_call(
            "trace_request_id",
            {"request_id": "req-42", "time_range": {"time_range": "last_1_hour"}},
        )

        payload = json.loads(result[0]["text"])
        assert payload["request_id"] == "req-42"
        handlers.trace_request_id_tool.run.assert_awaited_once_with(
            request_id="req-42",
            time_range={"time_range": "last_1_hour"},
            id_fields=None,
        )

    @pytest.mark.asyncio
    async def test_missing_request_id_returns_structured_error(self, handlers):
        result = await handlers.handle_tool_call(
            "trace_request_id",
            {"time_range": {"time_range": "last_1_hour"}},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_request_id"
        assert "request_id" in payload["error"]

    @pytest.mark.asyncio
    async def test_invalid_time_range_returns_structured_error(self, handlers):
        result = await handlers.handle_tool_call(
            "trace_request_id",
            {"request_id": "req-42", "time_range": "last_1_hour"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_time_range"
        assert "time_range" in payload["error"]

    @pytest.mark.asyncio
    async def test_invalid_id_fields_returns_structured_error(self, handlers):
        result = await handlers.handle_tool_call(
            "trace_request_id",
            {
                "request_id": "req-42",
                "time_range": {"time_range": "last_1_hour"},
                "id_fields": ["Request ID", ""],
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_id_fields"
        assert "id_fields" in payload["error"]

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_structured_payload(self, handlers):
        from oci_logan_mcp.budget_tracker import BudgetExceededError

        handlers.trace_request_id_tool.run = AsyncMock(
            side_effect=BudgetExceededError("bytes limit hit")
        )

        result = await handlers.handle_tool_call(
            "trace_request_id",
            {"request_id": "req-42", "time_range": {"time_range": "last_1_hour"}},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "bytes limit hit" in payload["error"]
        assert "budget" in payload


class TestFindRareEvents:
    @pytest.mark.asyncio
    async def test_routes_through_handler(self, handlers):
        handlers.find_rare_events_tool.run = AsyncMock(return_value={
            "source": "Linux Syslog Logs",
            "field": "Severity",
            "time_range": {"time_range": "last_24_hours"},
            "history_days": 30,
            "rarity_threshold_percentile": 5.0,
            "rare_values": [],
        })

        result = await handlers.handle_tool_call(
            "find_rare_events",
            {
                "source": "Linux Syslog Logs",
                "field": "Severity",
                "time_range": {"time_range": "last_24_hours"},
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["field"] == "Severity"
        handlers.find_rare_events_tool.run.assert_awaited_once_with(
            source="Linux Syslog Logs",
            field="Severity",
            time_range={"time_range": "last_24_hours"},
            rarity_threshold_percentile=5.0,
            history_days=30,
        )

    @pytest.mark.asyncio
    async def test_missing_source_returns_structured_error(self, handlers):
        result = await handlers.handle_tool_call(
            "find_rare_events",
            {"field": "Severity", "time_range": {"time_range": "last_24_hours"}},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_source"

    @pytest.mark.asyncio
    async def test_missing_field_returns_structured_error(self, handlers):
        result = await handlers.handle_tool_call(
            "find_rare_events",
            {"source": "Linux Syslog Logs", "time_range": {"time_range": "last_24_hours"}},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_field"

    @pytest.mark.asyncio
    async def test_invalid_time_range_returns_structured_error(self, handlers):
        result = await handlers.handle_tool_call(
            "find_rare_events",
            {"source": "Linux Syslog Logs", "field": "Severity", "time_range": "last_24_hours"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_time_range"

    @pytest.mark.asyncio
    async def test_invalid_threshold_returns_structured_error(self, handlers):
        result = await handlers.handle_tool_call(
            "find_rare_events",
            {
                "source": "Linux Syslog Logs",
                "field": "Severity",
                "time_range": {"time_range": "last_24_hours"},
                "rarity_threshold_percentile": 0,
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_rarity_threshold_percentile"

    @pytest.mark.asyncio
    async def test_threshold_above_hundred_returns_structured_error(self, handlers):
        result = await handlers.handle_tool_call(
            "find_rare_events",
            {
                "source": "Linux Syslog Logs",
                "field": "Severity",
                "time_range": {"time_range": "last_24_hours"},
                "rarity_threshold_percentile": 101,
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_rarity_threshold_percentile"

    @pytest.mark.asyncio
    async def test_invalid_history_days_returns_structured_error(self, handlers):
        result = await handlers.handle_tool_call(
            "find_rare_events",
            {
                "source": "Linux Syslog Logs",
                "field": "Severity",
                "time_range": {"time_range": "last_24_hours"},
                "history_days": 0,
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_history_days"

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_structured_payload(self, handlers):
        from oci_logan_mcp.budget_tracker import BudgetExceededError

        handlers.find_rare_events_tool.run = AsyncMock(
            side_effect=BudgetExceededError("bytes limit hit")
        )

        result = await handlers.handle_tool_call(
            "find_rare_events",
            {
                "source": "Linux Syslog Logs",
                "field": "Severity",
                "time_range": {"time_range": "last_24_hours"},
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "bytes limit hit" in payload["error"]
        assert "budget" in payload

class TestRelatedDashboardsAndSearches:
    @pytest.mark.asyncio
    async def test_routes_to_related_resources_tool(self, handlers):
        handlers.related_dashboards_and_searches_tool.run = AsyncMock(return_value={
            "dashboards": [
                {
                    "id": "dash-1",
                    "name": "Audit Dashboard",
                    "score": 3,
                    "reason": "source matched display_name",
                }
            ],
            "saved_searches": [],
            "learned_queries": [],
        })

        result = await handlers.handle_tool_call(
            "related_dashboards_and_searches",
            {"source": "Audit"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["dashboards"][0]["id"] == "dash-1"
        handlers.related_dashboards_and_searches_tool.run.assert_awaited_once_with(
            source="Audit",
            entity=None,
            field=None,
            user_id="testuser",
        )

    @pytest.mark.asyncio
    async def test_missing_inputs_returns_structured_error(self, handlers):
        handlers.related_dashboards_and_searches_tool.run = AsyncMock(return_value={
            "status": "error",
            "error_code": "missing_search_input",
            "error": "Provide at least one of source, entity, or field.",
        })

        result = await handlers.handle_tool_call(
            "related_dashboards_and_searches",
            {},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_search_input"
        handlers.related_dashboards_and_searches_tool.run.assert_awaited_once_with(
            source=None,
            entity=None,
            field=None,
            user_id="testuser",
        )


class TestInvestigationPlaybooks:
    @pytest.mark.asyncio
    async def test_record_investigation_routes_to_recorder(self, handlers):
        handlers.playbook_recorder.record = MagicMock(
            return_value={"id": "pb_1", "name": "incident", "steps": []}
        )

        result = await handlers.handle_tool_call(
            "record_investigation",
            {"name": "incident", "description": "desc"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["id"] == "pb_1"
        handlers.playbook_recorder.record.assert_called_once_with(
            name="incident",
            description="desc",
            since=None,
            until=None,
        )

    @pytest.mark.asyncio
    async def test_record_investigation_requires_name(self, handlers):
        result = await handlers.handle_tool_call("record_investigation", {})

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_name"

    @pytest.mark.asyncio
    async def test_list_playbooks_routes_to_store(self, handlers):
        handlers.playbook_store.list = MagicMock(return_value=[{"id": "pb_1"}])

        result = await handlers.handle_tool_call("list_playbooks", {})

        payload = json.loads(result[0]["text"])
        assert payload == {"playbooks": [{"id": "pb_1"}]}

    @pytest.mark.asyncio
    async def test_get_playbook_returns_not_found(self, handlers):
        from oci_logan_mcp.playbook_store import PlaybookNotFoundError

        handlers.playbook_store.get = MagicMock(
            side_effect=PlaybookNotFoundError("pb_missing")
        )

        result = await handlers.handle_tool_call(
            "get_playbook",
            {"playbook_id": "pb_missing"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "playbook_not_found"

    @pytest.mark.asyncio
    async def test_delete_playbook_returns_deleted_flag(self, handlers):
        handlers.playbook_store.delete = MagicMock(return_value=True)

        result = await handlers.handle_tool_call(
            "delete_playbook",
            {"playbook_id": "pb_1"},
        )

        payload = json.loads(result[0]["text"])
        assert payload == {"deleted": True, "playbook_id": "pb_1"}


class TestIncidentReports:
    @pytest.mark.asyncio
    async def test_generate_incident_report_routes_to_generator(self, handlers):
        handlers.report_generator.generate = MagicMock(
            return_value={
                "report_id": "rpt_1",
                "markdown": "# Incident Report\n",
                "html": None,
                "metadata": {"source_type": "investigation"},
                "artifacts": [],
            }
        )

        result = await handlers.handle_tool_call(
            "generate_incident_report",
            {
                "investigation": {"summary": "x"},
                "format": "markdown",
                "include_sections": ["executive_summary"],
                "summary_length": "short",
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["report_id"] == "rpt_1"
        handlers.report_generator.generate.assert_called_once_with(
            investigation={"summary": "x"},
            output_format="markdown",
            include_sections=["executive_summary"],
            summary_length="short",
        )

    @pytest.mark.asyncio
    async def test_generate_incident_report_requires_investigation_dict(self, handlers):
        result = await handlers.handle_tool_call("generate_incident_report", {})

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_investigation"

    @pytest.mark.asyncio
    async def test_generate_incident_report_returns_validation_error(self, handlers):
        result = await handlers.handle_tool_call(
            "generate_incident_report",
            {"investigation": {}, "format": "pdf"},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_report_options"
        assert "format must be one of" in payload["error"]


class TestDeliverReportHandler:
    @pytest.mark.asyncio
    async def test_deliver_report_routes_to_service(self, handlers):
        handlers.report_delivery_service.deliver = AsyncMock(
            return_value={"status": "sent", "delivered": [], "pdf_path": "/tmp/r.pdf"}
        )

        result = await handlers.handle_tool_call(
            "deliver_report",
            {
                "report": {"markdown": "# Report", "title": "Report"},
                "channels": ["telegram"],
                "format": "pdf",
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "sent"
        handlers.report_delivery_service.deliver.assert_awaited_once_with(
            report={"markdown": "# Report", "title": "Report"},
            channels=["telegram"],
            recipients={},
            output_format="pdf",
            title=None,
        )

    @pytest.mark.asyncio
    async def test_deliver_report_rejects_missing_markdown(self, handlers):
        result = await handlers.handle_tool_call(
            "deliver_report",
            {"report": {"report_id": "r-123"}},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "missing_report_markdown"

    @pytest.mark.asyncio
    async def test_deliver_report_returns_delivery_option_errors(self, handlers):
        handlers.report_delivery_service.deliver = AsyncMock(
            side_effect=ReportDeliveryError("unsupported channels: ['sms']")
        )

        result = await handlers.handle_tool_call(
            "deliver_report",
            {
                "report": {"markdown": "# Report"},
                "channels": ["sms"],
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert payload["error_code"] == "invalid_delivery_options"
        assert "sms" in payload["error"]

    @pytest.mark.asyncio
    async def test_deliver_report_invoked_audit_redacts_recipients(self, handlers):
        handlers.audit_logger.log = MagicMock()
        handlers.report_delivery_service.deliver = AsyncMock(
            return_value={"status": "sent", "delivered": [], "pdf_path": None}
        )

        await handlers.handle_tool_call(
            "deliver_report",
            {
                "report": {"markdown": "# Report"},
                "channels": ["telegram"],
                "recipients": {
                    "telegram_chat_id": "-100999",
                    "email_topic_ocid": "ocid1.onstopic.oc1..secret",
                },
            },
        )

        invoked = [
            call.kwargs for call in handlers.audit_logger.log.call_args_list
            if call.kwargs["outcome"] == "invoked"
        ][-1]
        assert invoked["tool"] == "deliver_report"
        assert invoked["args"]["recipients"] == "<redacted>"
        assert "-100999" not in str(invoked["args"])
        assert "secret" not in str(invoked["args"])

    @pytest.mark.asyncio
    async def test_non_delivery_invoked_audit_args_are_unchanged(self, handlers):
        handlers.audit_logger.log = MagicMock()
        handlers.investigate_tool.run = AsyncMock(return_value={
            "summary": "ok",
            "partial": False,
            "partial_reasons": [],
        })

        await handlers.handle_tool_call(
            "investigate_incident",
            {
                "query": "'Severity' = 'ERROR'",
                "time_range": "last_1_hour",
                "top_k": 2,
                "compartment_id": "ocid1.compartment.oc1..abc",
            },
        )

        invoked = [
            call.kwargs for call in handlers.audit_logger.log.call_args_list
            if call.kwargs["outcome"] == "invoked"
        ][-1]
        assert invoked["tool"] == "investigate_incident"
        assert invoked["args"] == {
            "query": "'Severity' = 'ERROR'",
            "time_range": "last_1_hour",
            "top_k": 2,
            "compartment_id": "ocid1.compartment.oc1..abc",
        }
