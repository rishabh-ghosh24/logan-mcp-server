"""Tests for catalog module."""

from oci_logan_mcp.catalog import CatalogEntry, SourceType


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
