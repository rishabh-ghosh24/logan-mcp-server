# tests/test_promote_queries.py
import pytest
from pathlib import Path
from oci_logan_mcp.file_lock import atomic_yaml_write, atomic_yaml_read

# Import the promotion functions (we'll create them as importable)
from oci_logan_mcp.promote import should_promote, promote_all, sanitize_for_sharing

def _make_query(name="q1", query="'Log Source' = 'Linux' | stats count by Severity | sort -count | head 10",
                interest_score=4, success_count=8, failure_count=2, use_count=10):
    return {
        "name": name, "query": query, "description": "test",
        "category": "general", "tags": [], "use_count": use_count,
        "success_count": success_count, "failure_count": failure_count,
        "interest_score": interest_score, "source": "personal",
        "created_at": "2026-01-01T00:00:00", "last_used": "2026-03-24T00:00:00",
    }

class TestShouldPromote:
    def test_single_user_high_score_high_success(self):
        q = _make_query(interest_score=4, success_count=8, failure_count=2)
        assert should_promote(q, user_count=1)

    def test_single_user_low_score_rejected(self):
        q = _make_query(interest_score=2, success_count=10, failure_count=0)
        assert not should_promote(q, user_count=1)

    def test_single_user_low_success_rate_rejected(self):
        q = _make_query(interest_score=5, success_count=3, failure_count=7)
        assert not should_promote(q, user_count=1)

    def test_multi_user_lower_thresholds(self):
        q = _make_query(interest_score=3, success_count=7, failure_count=3)
        assert should_promote(q, user_count=2)

    def test_zero_executions_rejected(self):
        q = _make_query(interest_score=5, success_count=0, failure_count=0)
        assert not should_promote(q, user_count=1)

class TestSanitizeForSharing:
    def test_redacts_sensitive_query(self):
        q = _make_query(query="compartmentId = 'ocid1.compartment.oc1..aaa123' | stats count")
        result = sanitize_for_sharing(q)
        assert result is not None
        assert "ocid1" not in result["query"]

    def test_rejects_secret_query(self):
        q = _make_query(query="password = 'hunter2'")
        result = sanitize_for_sharing(q)
        assert result is None

    def test_preserves_clean_query(self):
        q = _make_query(query="'Log Source' = 'Linux' | stats count")
        result = sanitize_for_sharing(q)
        assert result is not None
        assert result["query"] == "'Log Source' = 'Linux' | stats count"

class TestPromoteAll:
    def test_promotes_qualifying_queries(self, tmp_path):
        # Create two users with same query
        for user in ["alice", "bob"]:
            user_dir = tmp_path / "users" / user
            user_dir.mkdir(parents=True)
            atomic_yaml_write(user_dir / "learned_queries.yaml", {
                "version": 1,
                "queries": [_make_query(interest_score=4, success_count=8, failure_count=2)],
            })
        shared_dir = tmp_path / "shared"
        promote_all(tmp_path)
        shared = atomic_yaml_read(shared_dir / "promoted_queries.yaml", default={})
        assert len(shared.get("queries", [])) >= 1

    def test_does_not_promote_low_quality(self, tmp_path):
        user_dir = tmp_path / "users" / "alice"
        user_dir.mkdir(parents=True)
        atomic_yaml_write(user_dir / "learned_queries.yaml", {
            "version": 1,
            "queries": [_make_query(interest_score=1, success_count=1, failure_count=9)],
        })
        promote_all(tmp_path)
        shared_dir = tmp_path / "shared"
        shared = atomic_yaml_read(shared_dir / "promoted_queries.yaml", default={})
        assert len(shared.get("queries", [])) == 0

    def test_promote_all_creates_shared_catalog_lock(self, tmp_path):
        """promote_all should create shared/catalog.lock as part of its write protocol."""
        user_dir = tmp_path / "users" / "alice"
        user_dir.mkdir(parents=True)
        atomic_yaml_write(user_dir / "learned_queries.yaml", {
            "version": 1,
            "queries": [_make_query(interest_score=4, success_count=8, failure_count=2)],
        })
        promote_all(tmp_path)
        assert (tmp_path / "shared" / "catalog.lock").exists()
