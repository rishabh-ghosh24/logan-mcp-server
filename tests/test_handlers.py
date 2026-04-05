"""Tests for MCP request handlers — routing, visualization, scope resolution."""

import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from oci_logan_mcp.handlers import MCPHandlers
from oci_logan_mcp.config import Settings
from oci_logan_mcp.user_store import UserStore
from oci_logan_mcp.preferences import PreferenceStore


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
    ctx.save_learned_query = MagicMock(return_value={"name": "test", "use_count": 1})
    ctx.list_learned_queries = MagicMock(return_value=[])
    ctx.delete_learned_query = MagicMock(return_value=True)
    ctx.record_query_usage = MagicMock()
    ctx.add_note = MagicMock()
    ctx.get_tenancy_context = MagicMock(return_value={"namespace": "testns"})
    ctx.get_all_templates = MagicMock(return_value=[])
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
def handlers(settings, mock_oci_client, mock_cache, mock_query_logger, mock_context_manager,
             mock_user_store, mock_preference_store):
    """Create MCPHandlers with all mocked dependencies."""
    return MCPHandlers(
        settings=settings,
        oci_client=mock_oci_client,
        cache=mock_cache,
        query_logger=mock_query_logger,
        context_manager=mock_context_manager,
        user_store=mock_user_store,
        preference_store=mock_preference_store,
    )


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
            "save_learned_query", "list_learned_queries",
            "update_tenancy_context", "delete_learned_query",
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
    async def test_list_learned_queries(self, handlers, mock_user_store):
        """Should return list of learned queries."""
        mock_user_store.save_query(
            name="q1", query="* | head 10", description="test", category="general"
        )
        result = await handlers._list_learned_queries({"category": "all"})
        data = json.loads(result[0]["text"])
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_delete_learned_query_found(self, handlers, mock_user_store):
        """Should delete and return success status."""
        mock_user_store.save_query(
            name="q1", query="test", description="test", category="general"
        )
        result = await handlers._delete_learned_query({"name": "q1"})
        data = json.loads(result[0]["text"])
        assert data["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_delete_learned_query_not_found(self, handlers, mock_user_store):
        """Should return not_found when query doesn't exist."""
        result = await handlers._delete_learned_query({"name": "nonexistent"})
        data = json.loads(result[0]["text"])
        assert data["status"] == "not_found"

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
