"""Tests for catalog module."""

from oci_logan_mcp.catalog import CatalogEntry, SourceType
from oci_logan_mcp.resources import get_query_templates


def test_catalog_entry_minimum_fields():
    """CatalogEntry constructs with minimum required fields."""
    entry = CatalogEntry(
        entry_id="abc-123",
        name="errors_last_hour",
        query="'Error' | timestats count",
        description="find errors",
        source=SourceType.BUILTIN,
    )
    assert entry.entry_id == "abc-123"
    assert entry.source == SourceType.BUILTIN
    assert entry.tags == []  # default


def test_source_type_values():
    """SourceType enum has correct values."""
    assert SourceType.BUILTIN.value == "builtin"
    assert SourceType.STARTER.value == "starter"
    assert SourceType.PERSONAL.value == "personal"
    assert SourceType.SHARED.value == "shared"


def test_get_query_templates_reads_yaml():
    """get_query_templates() reads from YAML and returns correct structure."""
    result = get_query_templates()

    # Check overall structure
    assert isinstance(result, dict)
    assert "templates" in result
    assert isinstance(result["templates"], list)
    assert len(result["templates"]) == 13

    # Check that key templates exist by name
    names = {t["name"] for t in result["templates"]}
    assert "errors_last_hour" in names
    assert "top_n_by_field" in names
    assert "trend_over_time" in names
    assert "search_keyword" in names
    assert "filter_by_severity" in names
    assert "error_by_host" in names
    assert "recent_logs" in names
    assert "log_volume_by_source" in names
    assert "top_sources_with_alias" in names
    assert "volume_trend_by_source" in names
    assert "audit_status_breakdown" in names
    assert "total_log_volume_kpi" in names
    assert "source_entity_heatmap" in names

    # Verify each template has required fields
    for template in result["templates"]:
        assert "name" in template
        assert "description" in template
        assert "query" in template
        assert isinstance(template["query"], str)
        assert len(template["query"]) > 0
