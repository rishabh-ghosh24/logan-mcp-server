"""Tests for schema manager."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.schema_manager import SchemaManager, FieldInfo


class TestSchemaManager:
    """Tests for SchemaManager class."""

    @pytest.fixture
    def mock_oci_client(self):
        """Create mock OCI client."""
        client = MagicMock()
        client.list_log_sources = AsyncMock(
            return_value=[
                {
                    "name": "Linux Syslog",
                    "display_name": "Linux Syslog",
                    "description": "Linux system logs",
                    "entity_types": ["Host"],
                    "is_system": True,
                }
            ]
        )
        client.list_fields = AsyncMock(
            return_value=[
                {
                    "name": "Severity",
                    "display_name": "Severity",
                    "data_type": "STRING",
                    "description": "Log severity level",
                },
                {
                    "name": "Host Name",
                    "display_name": "Host Name",
                    "data_type": "STRING",
                    "description": "Hostname",
                },
            ]
        )
        client.list_entities = AsyncMock(return_value=[])
        client.list_parsers = AsyncMock(return_value=[])
        client.list_labels = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def mock_cache(self):
        """Create mock cache manager."""
        cache = MagicMock()
        cache.get = MagicMock(return_value=None)
        cache.set = MagicMock()
        return cache

    @pytest.fixture
    def schema_manager(self, mock_oci_client, mock_cache):
        """Create schema manager with mocks."""
        return SchemaManager(mock_oci_client, mock_cache)

    @pytest.mark.asyncio
    async def test_get_log_sources(self, schema_manager):
        """Test fetching log sources."""
        sources = await schema_manager.get_log_sources()

        assert len(sources) == 1
        assert sources[0]["name"] == "Linux Syslog"

    @pytest.mark.asyncio
    async def test_get_fields(self, schema_manager):
        """Test fetching fields."""
        fields = await schema_manager.get_fields()

        assert len(fields) == 2
        assert isinstance(fields[0], FieldInfo)
        assert fields[0].name == "Severity"

    @pytest.mark.asyncio
    async def test_get_all_field_names(self, schema_manager):
        """Test getting field name list."""
        names = await schema_manager.get_all_field_names()

        assert "Severity" in names
        assert "Host Name" in names

    @pytest.mark.asyncio
    async def test_cache_used(self, schema_manager, mock_cache):
        """Test that cache is used for repeated calls."""
        # First call
        await schema_manager.get_log_sources()
        mock_cache.set.assert_called_once()

        # Simulate cache hit
        mock_cache.get.return_value = [{"name": "cached"}]

        # Second call should use cache
        sources = await schema_manager.get_log_sources()
        assert sources[0]["name"] == "cached"

    @pytest.mark.asyncio
    async def test_get_full_schema(self, schema_manager):
        """Test getting full schema."""
        schema = await schema_manager.get_full_schema()

        assert "log_sources" in schema
        assert "fields" in schema
        assert "entities" in schema
        assert "parsers" in schema
        assert "labels" in schema

    def test_generate_semantic_hint(self, schema_manager):
        """Test semantic hint generation."""
        hint = schema_manager._generate_semantic_hint("Severity", "")
        assert "severity" in hint.lower()

        hint = schema_manager._generate_semantic_hint("Unknown Field", "Custom description")
        assert hint == "Custom description"
