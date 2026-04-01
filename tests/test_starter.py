"""Tests for the starter queries YAML loader and handler integration."""
import json
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from oci_logan_mcp.handlers import MCPHandlers
from oci_logan_mcp.config import Settings
from oci_logan_mcp.user_store import UserStore
from oci_logan_mcp.preferences import PreferenceStore


@pytest.fixture
def _handlers(tmp_path):
    """Minimal MCPHandlers for testing get_query_examples."""
    s = Settings()
    s.log_analytics.namespace = "testns"
    s.log_analytics.default_compartment_id = "ocid1.compartment.test"
    client = MagicMock()
    client.namespace = "testns"
    client.compartment_id = "ocid1.compartment.test"
    client._config = {"tenancy": "ocid1.tenancy.test"}
    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())
    logger = MagicMock()
    ctx = MagicMock(get_all_templates=MagicMock(return_value=[]))
    user_dir = tmp_path / "users" / "testuser"
    user_dir.mkdir(parents=True)
    return MCPHandlers(
        settings=s, oci_client=client, cache=cache,
        query_logger=logger, context_manager=ctx,
        user_store=UserStore(base_dir=tmp_path, user_id="testuser"),
        preference_store=PreferenceStore(user_dir=user_dir),
    )


def _reset_cache():
    """Reset the module-level starter cache between tests."""
    import oci_logan_mcp.starter as mod
    mod._cache = None
    mod._cache_loaded = False


class TestLoadStarterQueries:
    """Tests for the YAML-backed starter query loader."""

    def setup_method(self):
        _reset_cache()

    def test_loads_all_categories(self):
        """Should return all 5 categories from the packaged YAML."""
        from oci_logan_mcp.starter import load_starter_queries

        result = load_starter_queries()
        assert result is not None
        assert set(result.keys()) == {"basic", "security", "errors", "performance", "statistics"}

    def test_each_category_has_examples(self):
        """Each category should have at least one example."""
        from oci_logan_mcp.starter import load_starter_queries

        result = load_starter_queries()
        for cat, examples in result.items():
            assert len(examples) > 0, f"Category '{cat}' has no examples"

    def test_example_structure(self):
        """Each example should have name, query, description."""
        from oci_logan_mcp.starter import load_starter_queries

        result = load_starter_queries()
        for cat, examples in result.items():
            for ex in examples:
                assert "name" in ex, f"Missing 'name' in {cat} example"
                assert "query" in ex, f"Missing 'query' in {cat} example"
                assert "description" in ex, f"Missing 'description' in {cat} example"

    def test_returns_none_when_yaml_missing(self):
        """Should return None gracefully when YAML cannot be loaded."""
        from oci_logan_mcp.starter import load_starter_queries

        with patch("oci_logan_mcp.starter.importlib.resources") as mock_res:
            mock_res.files.side_effect = FileNotFoundError("not found")
            result = load_starter_queries()
        assert result is None

    def test_returns_none_for_invalid_yaml(self):
        """Should return None when YAML is malformed."""
        from oci_logan_mcp.starter import load_starter_queries

        with patch("oci_logan_mcp.starter.importlib.resources") as mock_res:
            mock_file = mock_res.files.return_value.__truediv__.return_value.__truediv__.return_value
            mock_file.read_text.return_value = "not: valid: yaml: [["
            result = load_starter_queries()
        assert result is None

    def test_skips_bad_entries_keeps_good_ones(self):
        """Mixed valid + invalid entries: bad ones skipped, good ones kept."""
        from oci_logan_mcp.starter import load_starter_queries

        mixed_yaml = """
version: 1
queries:
  - name: Good query
    query: "* | stats count"
    description: A valid entry
    category: basic
  - "just a string, not a dict"
  - name: Missing description
    query: "* | head 10"
    category: basic
  - name: Another good one
    query: "* | stats count by Entity"
    description: Also valid
    category: statistics
"""
        with patch("oci_logan_mcp.starter.importlib.resources") as mock_res:
            mock_file = mock_res.files.return_value.__truediv__.return_value.__truediv__.return_value
            mock_file.read_text.return_value = mixed_yaml
            result = load_starter_queries()

        assert result is not None
        assert len(result["basic"]) == 1
        assert result["basic"][0]["name"] == "Good query"
        assert len(result["statistics"]) == 1

    def test_returns_none_for_missing_queries_key(self):
        """Should return None when YAML has no 'queries' key."""
        from oci_logan_mcp.starter import load_starter_queries

        with patch("oci_logan_mcp.starter.importlib.resources") as mock_res:
            mock_file = mock_res.files.return_value.__truediv__.return_value.__truediv__.return_value
            mock_file.read_text.return_value = "version: 1\nother_key: []"
            result = load_starter_queries()
        assert result is None


class TestHandlerIntegration:
    """Tests that get_query_examples uses YAML data and falls back correctly."""

    def setup_method(self):
        _reset_cache()

    @pytest.mark.asyncio
    async def test_all_categories_from_yaml(self, _handlers):
        """get_query_examples(all) should return YAML-backed examples."""
        result = await _handlers._get_query_examples({"category": "all"})
        data = json.loads(result[0]["text"])
        assert "categories" in data
        assert set(data["categories"]) == {"basic", "security", "errors", "performance", "statistics"}
        assert "examples" in data
        assert "tip" in data

    @pytest.mark.asyncio
    async def test_specific_category_from_yaml(self, _handlers):
        """get_query_examples(errors) should return YAML-backed error examples."""
        result = await _handlers._get_query_examples({"category": "errors"})
        data = json.loads(result[0]["text"])
        assert data["category"] == "errors"
        assert len(data["examples"]) > 0
        assert all("name" in ex and "query" in ex for ex in data["examples"])

    @pytest.mark.asyncio
    async def test_unknown_category(self, _handlers):
        """Unknown category should return error with available list."""
        result = await _handlers._get_query_examples({"category": "nonexistent"})
        data = json.loads(result[0]["text"])
        assert "error" in data
        assert "available" in data

    @pytest.mark.asyncio
    async def test_fallback_when_yaml_fails(self, _handlers):
        """Should fall back to hardcoded examples when YAML loading fails."""
        with patch("oci_logan_mcp.starter.load_starter_queries", return_value=None):
            result = await _handlers._get_query_examples({"category": "all"})
        data = json.loads(result[0]["text"])
        assert "categories" in data
        assert "basic" in data["categories"]
        assert len(data["examples"]["basic"]) > 0
