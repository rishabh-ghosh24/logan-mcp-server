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

    @pytest.fixture
    def budgeted_engine(self, query_engine, mock_oci_client):
        from oci_logan_mcp.budget_tracker import BudgetLimits, BudgetTracker
        from oci_logan_mcp.config import Settings
        from oci_logan_mcp.query_estimator import QueryEstimator

        query_engine.budget_tracker = BudgetTracker("batch", BudgetLimits())
        query_engine.estimator = QueryEstimator(mock_oci_client, Settings())
        return query_engine

    @pytest.mark.asyncio
    async def test_batch_respects_query_budget(self, budgeted_engine, mock_oci_client):
        budgeted_engine.budget_tracker.limits.max_queries_per_session = 0
        [result] = await budgeted_engine.execute_batch([{"query": "* | stats count"}])
        assert result["success"] is False and result["error_type"] == "BudgetExceededError"
        assert "run_query" in result["error"] and "budget_override" in result["error"]
        assert mock_oci_client.query.await_count == 0

    @pytest.mark.asyncio
    async def test_batch_records_usage(self, budgeted_engine):
        results = await budgeted_engine.execute_batch([{"query": "* | stats count"}, {"query": "* | head 1"}])
        assert [r["result"]["data"]["rows"] for r in results] == [[[100]], [[100]]]
        assert budgeted_engine.budget_tracker.snapshot().queries == 2

    @pytest.mark.asyncio
    async def test_batch_concurrent_items_cannot_overbook(self, budgeted_engine, mock_oci_client):
        budgeted_engine.budget_tracker.limits.max_queries_per_session = 1
        results = await budgeted_engine.execute_batch([{"query": "* | stats count"}, {"query": "* | head 1"}])
        assert [r["success"] for r in results] == [True, False]
        assert results[0]["result"]["data"]["rows"] == [[100]]
        assert results[1]["error_type"] == "BudgetExceededError"
        assert budgeted_engine.budget_tracker.snapshot().queries == mock_oci_client.query.await_count == 1

    @pytest.mark.asyncio
    async def test_batch_size_is_bounded_before_any_query(self, budgeted_engine, mock_oci_client):
        with pytest.raises(ValueError, match="Batch limited to 10 queries; got 11"):
            await budgeted_engine.execute_batch([{"query": "* | stats count"}] * 11)
        assert mock_oci_client.query.await_count == 0

    @pytest.mark.asyncio
    async def test_batch_item_failure_preserves_type_and_releases_usage(self, budgeted_engine, mock_oci_client):
        mock_oci_client.query.side_effect = ValueError("bad query")
        [result] = await budgeted_engine.execute_batch([{"query": "* | stats count"}])
        assert result == {"success": False, "error": "bad query", "error_type": "ValueError"}
        assert budgeted_engine.budget_tracker.snapshot().queries == 0

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
