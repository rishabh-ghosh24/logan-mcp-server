"""Tests for cache manager module."""

import time
import pytest
from unittest.mock import patch

from oci_logan_mcp.cache import CacheManager, CacheEntry
from oci_logan_mcp.config import CacheConfig


@pytest.fixture
def cache():
    """Create a CacheManager with default config."""
    return CacheManager(CacheConfig(enabled=True, query_ttl_minutes=5, schema_ttl_minutes=15))


@pytest.fixture
def disabled_cache():
    """Create a disabled CacheManager."""
    return CacheManager(CacheConfig(enabled=False))


# ---------------------------------------------------------------
# Basic operations
# ---------------------------------------------------------------


class TestCacheManagerBasic:
    """Tests for basic cache operations."""

    def test_set_and_get_query_category(self, cache):
        """Set and get from default query category."""
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_set_and_get_schema_category(self, cache):
        """Set and get from schema category."""
        cache.set("schema_key", {"fields": [1, 2]}, category="schema")
        assert cache.get("schema_key", category="schema") == {"fields": [1, 2]}

    def test_categories_are_separate(self, cache):
        """Query and schema caches are independent."""
        cache.set("key", "query_val", category="query")
        cache.set("key", "schema_val", category="schema")
        assert cache.get("key", category="query") == "query_val"
        assert cache.get("key", category="schema") == "schema_val"

    def test_get_missing_key_returns_none(self, cache):
        """Missing key returns None."""
        assert cache.get("nonexistent") is None

    def test_delete_existing_key(self, cache):
        """Delete existing key returns True."""
        cache.set("k", "v")
        assert cache.delete("k") is True
        assert cache.get("k") is None

    def test_delete_nonexistent_key(self, cache):
        """Delete missing key returns False."""
        assert cache.delete("nope") is False

    def test_clear_all_categories(self, cache):
        """clear(None) empties both caches."""
        cache.set("q1", "v1", category="query")
        cache.set("s1", "v1", category="schema")
        cache.clear(None)
        assert cache.get("q1") is None
        assert cache.get("s1", category="schema") is None

    def test_clear_query_only(self, cache):
        """clear('query') preserves schema."""
        cache.set("q1", "v1", category="query")
        cache.set("s1", "v1", category="schema")
        cache.clear("query")
        assert cache.get("q1") is None
        assert cache.get("s1", category="schema") == "v1"

    def test_clear_schema_only(self, cache):
        """clear('schema') preserves query."""
        cache.set("q1", "v1", category="query")
        cache.set("s1", "v1", category="schema")
        cache.clear("schema")
        assert cache.get("q1") == "v1"
        assert cache.get("s1", category="schema") is None


# ---------------------------------------------------------------
# TTL & Expiration
# ---------------------------------------------------------------


class TestCacheManagerTTL:
    """Tests for TTL and expiration."""

    def test_expired_entry_returns_none(self, cache):
        """Expired entry returns None."""
        cache.set("key", "val", ttl_seconds=0.01)
        time.sleep(0.02)
        assert cache.get("key") is None

    def test_expired_entry_is_removed(self, cache):
        """Expired entry is deleted on access."""
        cache.set("key", "val", ttl_seconds=0.01)
        time.sleep(0.02)
        cache.get("key")
        # Verify it's actually gone from internal storage
        assert "key" not in cache._query_cache

    def test_non_expired_entry_returned(self, cache):
        """Within TTL -> value returned."""
        cache.set("key", "val", ttl_seconds=60)
        assert cache.get("key") == "val"

    def test_custom_ttl_on_set(self, cache):
        """Custom ttl_seconds overrides default."""
        cache.set("key", "val", ttl_seconds=1)
        entry = cache._query_cache["key"]
        assert entry.ttl_seconds == 1


# ---------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------


class TestCacheManagerCleanup:
    """Tests for cleanup behavior."""

    def test_cleanup_triggered_above_100(self, cache):
        """Adding >100 items triggers cleanup."""
        for i in range(101):
            cache.set(f"k{i}", f"v{i}", ttl_seconds=0.01)
        time.sleep(0.02)
        # The 101st set triggers cleanup, removing expired entries
        cache.set("final", "val", ttl_seconds=60)
        # After cleanup, most expired keys should be gone
        assert len(cache._query_cache) < 102

    def test_cleanup_removes_expired_only(self, cache):
        """Cleanup removes only expired entries."""
        # Add expired entries
        for i in range(60):
            cache.set(f"expired_{i}", "v", ttl_seconds=0.01)
        # Add valid entries
        for i in range(50):
            cache.set(f"valid_{i}", "v", ttl_seconds=3600)
        time.sleep(0.02)
        # Trigger cleanup by exceeding 100
        cache.set("trigger", "v", ttl_seconds=3600)
        # All valid entries should remain
        for i in range(50):
            assert cache.get(f"valid_{i}") == "v"


# ---------------------------------------------------------------
# Disabled cache
# ---------------------------------------------------------------


class TestCacheManagerDisabled:
    """Tests for disabled cache."""

    def test_disabled_get_returns_none(self, disabled_cache):
        """Disabled cache always returns None."""
        disabled_cache._query_cache["key"] = CacheEntry(value="val")
        assert disabled_cache.get("key") is None

    def test_disabled_set_is_noop(self, disabled_cache):
        """Disabled cache set does nothing."""
        disabled_cache.set("key", "val")
        assert "key" not in disabled_cache._query_cache


# ---------------------------------------------------------------
# Stats
# ---------------------------------------------------------------


class TestCacheManagerStats:
    """Tests for cache statistics."""

    def test_stats_empty_cache(self, cache):
        stats = cache.get_stats()
        assert stats["enabled"] is True
        assert stats["query_entries"] == 0
        assert stats["schema_entries"] == 0
        assert stats["query_ttl_minutes"] == 5
        assert stats["schema_ttl_minutes"] == 15

    def test_stats_with_entries(self, cache):
        cache.set("q1", "v1", category="query")
        cache.set("q2", "v2", category="query")
        cache.set("s1", "v1", category="schema")
        stats = cache.get_stats()
        assert stats["query_entries"] == 2
        assert stats["schema_entries"] == 1


# ---------------------------------------------------------------
# CacheEntry
# ---------------------------------------------------------------


class TestCacheEntry:
    """Tests for CacheEntry dataclass."""

    def test_default_ttl(self):
        entry = CacheEntry(value="test")
        assert entry.ttl_seconds == 300

    def test_created_at_is_current_time(self):
        before = time.time()
        entry = CacheEntry(value="test")
        after = time.time()
        assert before <= entry.created_at <= after
