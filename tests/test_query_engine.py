"""Tests for query engine."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from oci_logan_mcp.query_engine import QueryEngine


class TestQueryEngine:
    """Tests for QueryEngine class."""

    @pytest.fixture
    def mock_oci_client(self):
        """Create mock OCI client."""
        client = MagicMock()
        client.query = AsyncMock(
            return_value={
                "columns": [{"name": "count"}],
                "rows": [[100]],
                "total_count": 1,
            }
        )
        return client

    @pytest.fixture
    def mock_cache(self):
        """Create mock cache manager."""
        cache = MagicMock()
        cache.get = MagicMock(return_value=None)
        cache.set = MagicMock()
        return cache

    @pytest.fixture
    def mock_logger(self):
        """Create mock query logger."""
        logger = MagicMock()
        logger.log_query = MagicMock()
        return logger

    @pytest.fixture
    def query_engine(self, mock_oci_client, mock_cache, mock_logger):
        """Create query engine with mocks."""
        return QueryEngine(mock_oci_client, mock_cache, mock_logger)

    @pytest.mark.asyncio
    async def test_execute_query(self, query_engine, mock_oci_client):
        """Test basic query execution."""
        result = await query_engine.execute(
            query="* | stats count",
            time_range="last_1_hour",
        )

        assert result["source"] == "live"
        assert "data" in result
        mock_oci_client.query.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_hit(self, query_engine, mock_cache):
        """Test cache hit returns cached data."""
        cached_data = {"columns": [], "rows": []}
        mock_cache.get.return_value = cached_data

        result = await query_engine.execute(
            query="* | stats count",
            time_range="last_1_hour",
        )

        assert result["source"] == "cache"
        assert result["data"] == cached_data

    @pytest.mark.asyncio
    async def test_query_logged(self, query_engine, mock_logger):
        """Test query is logged after execution."""
        await query_engine.execute(
            query="* | stats count",
            time_range="last_1_hour",
        )

        mock_logger.log_query.assert_called_once()
        call_kwargs = mock_logger.log_query.call_args.kwargs
        assert call_kwargs["success"] is True
        assert "execution_time" in call_kwargs

    def test_make_cache_key(self, query_engine):
        """Test cache key generation."""
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)

        key = query_engine._make_cache_key("test query", start, end)

        assert "test query" in key
        assert "2024-01-01" in key
