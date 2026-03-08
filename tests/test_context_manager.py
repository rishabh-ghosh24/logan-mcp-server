"""Tests for context manager module."""

import pytest
import yaml
from pathlib import Path

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

    def test_loads_existing_queries(self, settings, tmp_context_dir):
        """Loads existing learned_queries.yaml on init."""
        queries_file = tmp_context_dir / "learned_queries.yaml"
        queries_file.write_text(yaml.dump({
            "version": 1,
            "queries": [
                {"name": "test_q", "query": "* | stats count", "description": "test", "category": "general"},
            ],
        }))

        ctx = ContextManager(settings, context_dir=tmp_context_dir)
        queries = ctx.list_learned_queries()
        assert len(queries) == 1
        assert queries[0]["name"] == "test_q"

    def test_handles_corrupt_queries_file(self, settings, tmp_context_dir):
        """Gracefully handles corrupt queries YAML."""
        queries_file = tmp_context_dir / "learned_queries.yaml"
        queries_file.write_text("invalid yaml content [[[")

        ctx = ContextManager(settings, context_dir=tmp_context_dir)
        queries = ctx.list_learned_queries()
        assert queries == []


class TestLearnedQueries:
    """Tests for learned query operations."""

    def test_save_and_list(self, ctx):
        """Save a query and list it back."""
        saved = ctx.save_learned_query(
            name="error_count",
            query="* | where Severity = 'ERROR' | stats count",
            description="Count all errors",
            category="errors",
            tags=["errors", "count"],
        )
        assert saved["name"] == "error_count"
        assert saved["use_count"] == 1

        queries = ctx.list_learned_queries()
        assert len(queries) == 1
        assert queries[0]["query"] == "* | where Severity = 'ERROR' | stats count"

    def test_dedup_by_name(self, ctx):
        """Saving with same name updates existing."""
        ctx.save_learned_query(
            name="my_query", query="* | head 10",
            description="First version",
        )
        ctx.save_learned_query(
            name="my_query", query="* | head 20",
            description="Updated version",
        )
        queries = ctx.list_learned_queries()
        assert len(queries) == 1
        assert queries[0]["query"] == "* | head 20"
        assert queries[0]["use_count"] == 2

    def test_dedup_by_query_text(self, ctx):
        """Saving same query text under different name updates existing."""
        ctx.save_learned_query(
            name="original", query="* | stats count",
            description="Original",
        )
        ctx.save_learned_query(
            name="renamed", query="* | stats count",
            description="Renamed",
        )
        queries = ctx.list_learned_queries()
        assert len(queries) == 1
        assert queries[0]["name"] == "renamed"

    def test_filter_by_category(self, ctx):
        """Filter queries by category."""
        ctx.save_learned_query(name="q1", query="a", description="d", category="security")
        ctx.save_learned_query(name="q2", query="b", description="d", category="errors")
        ctx.save_learned_query(name="q3", query="c", description="d", category="security")

        security = ctx.list_learned_queries(category="security")
        assert len(security) == 2

        errors = ctx.list_learned_queries(category="errors")
        assert len(errors) == 1

        all_q = ctx.list_learned_queries(category="all")
        assert len(all_q) == 3

    def test_filter_by_tag(self, ctx):
        """Filter queries by tag."""
        ctx.save_learned_query(name="q1", query="a", description="d", tags=["ssh", "auth"])
        ctx.save_learned_query(name="q2", query="b", description="d", tags=["network"])

        ssh = ctx.list_learned_queries(tag="ssh")
        assert len(ssh) == 1
        assert ssh[0]["name"] == "q1"

    def test_delete(self, ctx):
        """Delete a query by name."""
        ctx.save_learned_query(name="to_delete", query="a", description="d")
        assert ctx.delete_learned_query("to_delete") is True
        assert ctx.list_learned_queries() == []
        assert ctx.delete_learned_query("nonexistent") is False

    def test_record_usage(self, ctx):
        """Bump use_count on matching query."""
        ctx.save_learned_query(name="q1", query="* | head 5", description="d")
        ctx.record_query_usage("* | head 5")
        queries = ctx.list_learned_queries()
        assert queries[0]["use_count"] == 2

    def test_max_limit_enforcement(self, ctx, tmp_context_dir):
        """Enforce max 200 learned queries."""
        for i in range(210):
            ctx.save_learned_query(
                name=f"q_{i}", query=f"query_{i}", description=f"desc_{i}",
            )
        queries = ctx.list_learned_queries()
        assert len(queries) <= 200

    def test_persistence_across_instances(self, settings, tmp_context_dir):
        """Queries persist across ContextManager instances."""
        ctx1 = ContextManager(settings, context_dir=tmp_context_dir)
        ctx1.save_learned_query(name="persistent", query="*", description="test")

        ctx2 = ContextManager(settings, context_dir=tmp_context_dir)
        queries = ctx2.list_learned_queries()
        assert len(queries) == 1
        assert queries[0]["name"] == "persistent"


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


class TestTemplateMerging:
    """Tests for merging built-in templates with learned queries."""

    def test_merge_marks_source(self, ctx):
        """Merged templates have source field."""
        builtin = [{"name": "builtin1", "query": "* | head 10"}]
        ctx.save_learned_query(name="learned1", query="* | tail 5", description="d")

        merged = ctx.get_all_templates(builtin)
        assert len(merged) == 2
        assert merged[0]["source"] == "builtin"
        assert merged[1]["source"] == "learned"

    def test_merge_dedup_query_text(self, ctx):
        """Learned query with same text as built-in is excluded."""
        builtin = [{"name": "count_all", "query": "* | stats count"}]
        ctx.save_learned_query(name="my_count", query="* | stats count", description="d")

        merged = ctx.get_all_templates(builtin)
        assert len(merged) == 1
        assert merged[0]["source"] == "builtin"

    def test_empty_merge(self, ctx):
        """Merge with no learned queries returns only built-in."""
        builtin = [{"name": "b1", "query": "q1"}, {"name": "b2", "query": "q2"}]
        merged = ctx.get_all_templates(builtin)
        assert len(merged) == 2
        assert all(t["source"] == "builtin" for t in merged)
