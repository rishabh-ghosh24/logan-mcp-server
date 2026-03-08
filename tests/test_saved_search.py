"""Tests for saved search service module."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.saved_search import SavedSearchService
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import CacheConfig


@pytest.fixture
def mock_client():
    """Create a mock OCI client."""
    client = AsyncMock()
    client.list_saved_searches.return_value = [
        {"id": "ocid1.ss.1", "display_name": "Error Summary"},
        {"id": "ocid1.ss.2", "display_name": "Network Stats"},
        {"id": "ocid1.ss.3", "display_name": "Security Audit"},
    ]
    client.get_saved_search.return_value = {
        "id": "ocid1.ss.1",
        "display_name": "Error Summary",
        "query": "* | where Severity = 'ERROR' | stats count",
    }
    return client


@pytest.fixture
def cache():
    """Create a real CacheManager."""
    return CacheManager(CacheConfig(enabled=True))


@pytest.fixture
def svc(mock_client, cache):
    """Create a SavedSearchService."""
    return SavedSearchService(mock_client, cache)


# ---------------------------------------------------------------
# list_searches
# ---------------------------------------------------------------


class TestListSearches:
    """Tests for list_searches method."""

    @pytest.mark.asyncio
    async def test_fetches_from_client_when_no_cache(self, svc, mock_client):
        """Cache miss -> fetches from client."""
        result = await svc.list_searches()
        assert len(result) == 3
        mock_client.list_saved_searches.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_from_cache_if_available(self, svc, mock_client):
        """Cache hit -> client not called again."""
        await svc.list_searches()  # First call populates cache
        await svc.list_searches()  # Second call should use cache
        assert mock_client.list_saved_searches.call_count == 1

    @pytest.mark.asyncio
    async def test_caches_result_after_fetch(self, svc, cache):
        """Result is cached after fetch."""
        await svc.list_searches()
        cached = cache.get("saved_searches")
        assert cached is not None
        assert len(cached) == 3


# ---------------------------------------------------------------
# get_search_by_name
# ---------------------------------------------------------------


class TestGetSearchByName:
    """Tests for get_search_by_name method."""

    @pytest.mark.asyncio
    async def test_finds_matching_search(self, svc):
        """Finds search by display name (case-insensitive)."""
        result = await svc.get_search_by_name("error summary")
        assert result is not None
        assert result["id"] == "ocid1.ss.1"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, svc):
        """No match -> None."""
        result = await svc.get_search_by_name("nonexistent search")
        assert result is None

    @pytest.mark.asyncio
    async def test_calls_get_search_by_id_on_match(self, svc, mock_client):
        """On match, calls get_search_by_id for detailed fetch."""
        await svc.get_search_by_name("Error Summary")
        mock_client.get_saved_search.assert_called_once_with("ocid1.ss.1")


# ---------------------------------------------------------------
# get_search_by_id
# ---------------------------------------------------------------


class TestGetSearchById:
    """Tests for get_search_by_id method."""

    @pytest.mark.asyncio
    async def test_fetches_from_client_when_no_cache(self, svc, mock_client):
        """Cache miss -> fetches from client."""
        result = await svc.get_search_by_id("ocid1.ss.1")
        assert result["display_name"] == "Error Summary"
        mock_client.get_saved_search.assert_called_once_with("ocid1.ss.1")

    @pytest.mark.asyncio
    async def test_returns_from_cache_if_available(self, svc, mock_client):
        """Cache hit -> client not called again."""
        await svc.get_search_by_id("ocid1.ss.1")
        await svc.get_search_by_id("ocid1.ss.1")
        assert mock_client.get_saved_search.call_count == 1

    @pytest.mark.asyncio
    async def test_caches_result_after_fetch(self, svc, cache):
        """Result is cached with search-specific key."""
        await svc.get_search_by_id("ocid1.ss.1")
        cached = cache.get("saved_search:ocid1.ss.1")
        assert cached is not None


# ---------------------------------------------------------------
# find_searches
# ---------------------------------------------------------------


class TestFindSearches:
    """Tests for find_searches method."""

    @pytest.mark.asyncio
    async def test_filter_by_keyword(self, svc):
        """Keyword filters searches."""
        result = await svc.find_searches(keyword="error")
        assert len(result) == 1
        assert result[0]["display_name"] == "Error Summary"

    @pytest.mark.asyncio
    async def test_no_keyword_returns_all(self, svc):
        """No keyword -> all searches."""
        result = await svc.find_searches()
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_respects_limit(self, svc):
        """Limit caps results."""
        result = await svc.find_searches(limit=1)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_keyword_case_insensitive(self, svc):
        """Keyword match is case-insensitive."""
        result = await svc.find_searches(keyword="NETWORK")
        assert len(result) == 1
        assert result[0]["display_name"] == "Network Stats"
