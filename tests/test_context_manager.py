"""Tests for context manager module."""

import pytest
import yaml
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.config import Settings
from oci_logan_mcp.context_manager import ContextManager


@pytest.fixture
def tmp_context_dir(tmp_path):
    """Create a temporary context directory."""
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    return context_dir


@pytest.fixture
def settings():
    """Create test settings."""
    s = Settings()
    s.log_analytics.namespace = "test-namespace"
    s.log_analytics.default_compartment_id = "ocid1.compartment.test"
    return s


@pytest.fixture
def ctx(settings, tmp_context_dir):
    """Create a ContextManager with temp directory."""
    return ContextManager(settings, context_dir=tmp_context_dir)


class TestContextManagerInit:
    """Tests for initialization and file loading."""

    def test_creates_context_directory(self, settings, tmp_path):
        """Context dir is created if it doesn't exist."""
        context_dir = tmp_path / "new_context"
        assert not context_dir.exists()
        ContextManager(settings, context_dir=context_dir)
        assert context_dir.exists()

    def test_default_context_on_fresh_start(self, ctx):
        """Fresh start returns default context with settings values."""
        context = ctx.get_tenancy_context()
        assert context["namespace"] == "test-namespace"
        assert context["default_compartment_id"] == "ocid1.compartment.test"
        assert context["log_sources"] == []
        assert context["notes"] == []
        assert context["version"] == 1

    def test_loads_existing_context(self, settings, tmp_context_dir):
        """Loads existing tenancy_context.yaml on init."""
        context_file = tmp_context_dir / "tenancy_context.yaml"
        context_file.write_text(yaml.dump({
            "namespace": "saved-ns",
            "log_sources": [{"name": "TestSource"}],
            "notes": ["A note"],
            "version": 1,
        }))

        ctx = ContextManager(settings, context_dir=tmp_context_dir)
        context = ctx.get_tenancy_context()
        assert context["namespace"] == "saved-ns"
        assert len(context["log_sources"]) == 1
        assert context["notes"] == ["A note"]

    def test_handles_corrupt_context_file(self, settings, tmp_context_dir):
        """Gracefully handles corrupt YAML."""
        context_file = tmp_context_dir / "tenancy_context.yaml"
        context_file.write_text("not: [valid: yaml: {{{}}")

        ctx = ContextManager(settings, context_dir=tmp_context_dir)
        context = ctx.get_tenancy_context()
        # Should fall back to defaults
        assert context["version"] == 1

class TestTenancyContext:
    """Tests for tenancy context operations."""

    def test_update_log_sources(self, ctx):
        """Update log sources."""
        sources = [
            {"name": "Linux Secure Logs", "display_name": "Linux Secure Logs"},
            {"name": "VCN Flow Logs", "display_name": "VCN Flow Logs"},
        ]
        summary = ctx.update_log_sources(sources)
        assert "2 total" in summary
        assert "2 new" in summary

        context = ctx.get_tenancy_context()
        assert len(context["log_sources"]) == 2

    def test_update_log_sources_idempotent(self, ctx):
        """Updating same sources doesn't duplicate."""
        sources = [{"name": "Source1"}]
        ctx.update_log_sources(sources)
        summary = ctx.update_log_sources(sources)
        assert "0 new" in summary

    def test_update_compartments(self, ctx):
        """Update compartments."""
        compartments = [
            {"id": "ocid1.comp.1", "name": "Prod"},
            {"id": "ocid1.comp.2", "name": "Dev"},
        ]
        summary = ctx.update_compartments(compartments)
        assert "2 total" in summary
        assert "2 new" in summary

    def test_add_and_remove_notes(self, ctx):
        """Add and remove notes."""
        ctx.add_note("Use 'Host Name (Server)' not 'Host Name'")
        ctx.add_note("VCN Flow logs have different field names")

        context = ctx.get_tenancy_context()
        assert len(context["notes"]) == 2

        # Adding duplicate is a no-op
        ctx.add_note("Use 'Host Name (Server)' not 'Host Name'")
        assert len(ctx.get_tenancy_context()["notes"]) == 2

        # Remove by index
        assert ctx.remove_note(0) is True
        assert len(ctx.get_tenancy_context()["notes"]) == 1
        assert ctx.remove_note(99) is False

    def test_context_persistence(self, settings, tmp_context_dir):
        """Context persists across instances."""
        ctx1 = ContextManager(settings, context_dir=tmp_context_dir)
        ctx1.update_log_sources([{"name": "MySource"}])
        ctx1.add_note("Remember this")

        ctx2 = ContextManager(settings, context_dir=tmp_context_dir)
        context = ctx2.get_tenancy_context()
        assert len(context["log_sources"]) == 1
        assert context["notes"] == ["Remember this"]


class TestUpdateMethods:
    """Tests for all update_* methods on tenancy context."""

    def test_update_confirmed_fields(self, ctx):
        """Update fields stores and reports new count."""
        fields = [{"name": "Severity"}, {"name": "Host Name"}]
        summary = ctx.update_confirmed_fields(fields)
        assert "2 total" in summary
        assert "2 new" in summary
        assert len(ctx.get_tenancy_context()["fields"]) == 2

    def test_update_confirmed_fields_idempotent(self, ctx):
        """Same fields again -> 0 new."""
        fields = [{"name": "Severity"}]
        ctx.update_confirmed_fields(fields)
        summary = ctx.update_confirmed_fields(fields)
        assert "0 new" in summary

    def test_update_entities(self, ctx):
        """Update entities stores and reports new count."""
        entities = [{"name": "host1"}, {"name": "host2"}, {"name": "host3"}]
        summary = ctx.update_entities(entities)
        assert "3 total" in summary
        assert "3 new" in summary

    def test_update_entities_idempotent(self, ctx):
        """Same entities -> 0 new."""
        entities = [{"name": "host1"}]
        ctx.update_entities(entities)
        summary = ctx.update_entities(entities)
        assert "0 new" in summary

    def test_update_parsers(self, ctx):
        """Update parsers stores and reports."""
        parsers = [{"name": "LinuxSyslog"}, {"name": "ApacheAccess"}]
        summary = ctx.update_parsers(parsers)
        assert "2 total" in summary
        assert "2 new" in summary

    def test_update_parsers_idempotent(self, ctx):
        parsers = [{"name": "LinuxSyslog"}]
        ctx.update_parsers(parsers)
        summary = ctx.update_parsers(parsers)
        assert "0 new" in summary

    def test_update_labels(self, ctx):
        labels = [{"name": "Critical"}, {"name": "Warning"}]
        summary = ctx.update_labels(labels)
        assert "2 total" in summary
        assert "2 new" in summary

    def test_update_labels_idempotent(self, ctx):
        labels = [{"name": "Critical"}]
        ctx.update_labels(labels)
        summary = ctx.update_labels(labels)
        assert "0 new" in summary

    def test_update_log_groups(self, ctx):
        """Log groups use 'id' for dedup, not 'name'."""
        groups = [{"id": "ocid1.lg.1", "display_name": "Default"}]
        summary = ctx.update_log_groups(groups)
        assert "1 total" in summary
        assert "1 new" in summary

    def test_update_log_groups_idempotent(self, ctx):
        groups = [{"id": "ocid1.lg.1"}]
        ctx.update_log_groups(groups)
        summary = ctx.update_log_groups(groups)
        assert "0 new" in summary

    def test_update_saved_searches(self, ctx):
        """Saved searches use 'id' for dedup."""
        searches = [{"id": "ocid1.ss.1"}, {"id": "ocid1.ss.2"}]
        summary = ctx.update_saved_searches(searches)
        assert "2 total" in summary
        assert "2 new" in summary

    def test_update_saved_searches_idempotent(self, ctx):
        searches = [{"id": "ocid1.ss.1"}]
        ctx.update_saved_searches(searches)
        summary = ctx.update_saved_searches(searches)
        assert "0 new" in summary

    def test_update_persists_to_disk(self, ctx, tmp_context_dir):
        """Updates are persisted to YAML file."""
        ctx.update_confirmed_fields([{"name": "Severity"}])
        # Verify file was written
        context_file = tmp_context_dir / "tenancy_context.yaml"
        assert context_file.exists()
        data = yaml.safe_load(context_file.read_text())
        assert len(data["fields"]) == 1


class TestRefreshSchema:
    """Tests for refresh_schema method."""

    @pytest.fixture
    def mock_oci_client(self):
        """Create a mock OCI client with async list methods."""
        client = AsyncMock()
        client.list_log_sources.return_value = [{"name": "Source1"}]
        client.list_fields.return_value = [{"name": "Field1"}]
        client.list_entities.return_value = [{"name": "Entity1"}]
        client.list_parsers.return_value = [{"name": "Parser1"}]
        client.list_labels.return_value = [{"name": "Label1"}]
        client.list_log_groups.return_value = [{"id": "ocid1.lg.1"}]
        client.list_compartments.return_value = [{"id": "ocid1.comp.1"}]
        return client

    @pytest.mark.asyncio
    async def test_refresh_all_categories_success(self, ctx, settings, mock_oci_client):
        """All categories refreshed successfully."""
        counts = await ctx.refresh_schema(mock_oci_client, settings)
        assert "log_sources" in counts
        assert "fields" in counts
        assert "entities" in counts
        assert "parsers" in counts
        assert "labels" in counts
        assert "log_groups" in counts
        assert "compartments" in counts

    @pytest.mark.asyncio
    async def test_refresh_partial_failure(self, ctx, settings, mock_oci_client):
        """Partial failures don't block other categories."""
        mock_oci_client.list_fields.side_effect = Exception("API error")
        counts = await ctx.refresh_schema(mock_oci_client, settings)
        # fields should be missing from counts, but others present
        assert "fields" not in counts
        assert "log_sources" in counts

    @pytest.mark.asyncio
    async def test_refresh_updates_namespace_and_compartment(self, ctx, settings, mock_oci_client):
        """Namespace and compartment updated from settings."""
        settings.log_analytics.namespace = "updated-ns"
        settings.log_analytics.default_compartment_id = "ocid1.comp.updated"
        await ctx.refresh_schema(mock_oci_client, settings)
        context = ctx.get_tenancy_context()
        assert context["namespace"] == "updated-ns"
        assert context["default_compartment_id"] == "ocid1.comp.updated"

    @pytest.mark.asyncio
    async def test_refresh_persists_to_disk(self, ctx, settings, mock_oci_client, tmp_context_dir):
        """Schema refresh saves to disk."""
        await ctx.refresh_schema(mock_oci_client, settings)
        context_file = tmp_context_dir / "tenancy_context.yaml"
        assert context_file.exists()
        data = yaml.safe_load(context_file.read_text())
        assert len(data["log_sources"]) == 1

    @pytest.mark.asyncio
    async def test_refresh_all_failures(self, ctx, settings):
        """All failures -> empty counts, no crash."""
        client = AsyncMock()
        client.list_log_sources.side_effect = Exception("fail")
        client.list_fields.side_effect = Exception("fail")
        client.list_entities.side_effect = Exception("fail")
        client.list_parsers.side_effect = Exception("fail")
        client.list_labels.side_effect = Exception("fail")
        client.list_log_groups.side_effect = Exception("fail")
        client.list_compartments.side_effect = Exception("fail")
        counts = await ctx.refresh_schema(client, settings)
        assert len(counts) == 0
