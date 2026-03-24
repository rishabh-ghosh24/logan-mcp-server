# tests/test_multi_user_integration.py
"""Integration tests for the multi-user learning system.

Verifies end-to-end flows: independent user stores, promotion visibility,
and per-user preference isolation.
"""
import pytest

from oci_logan_mcp.user_store import UserStore
from oci_logan_mcp.preferences import PreferenceStore


class TestMultiUserFlow:
    def test_two_users_save_independently(self, tmp_path):
        alice_store = UserStore(base_dir=tmp_path, user_id="alice")
        bob_store = UserStore(base_dir=tmp_path, user_id="bob")
        alice_store.save_query(name="q1", query="alice q", description="a", category="general")
        bob_store.save_query(name="q1", query="bob q", description="b", category="general")
        assert alice_store.list_queries()[0]["query"] == "alice q"
        assert bob_store.list_queries()[0]["query"] == "bob q"

    def test_promotion_makes_query_visible_to_new_user(self, tmp_path):
        alice = UserStore(base_dir=tmp_path, user_id="alice")
        alice.save_query(
            name="good_q",
            query="'Log Source' = 'Linux' | stats count by Severity | sort -count | head 10",
            description="test",
            category="general",
            interest_score=5,
        )
        # Simulate high success
        for _ in range(10):
            alice.record_success(
                "'Log Source' = 'Linux' | stats count by Severity | sort -count | head 10"
            )
        # Run promotion
        from oci_logan_mcp.promote import promote_all

        promote_all(tmp_path)
        # New user should see it
        charlie = UserStore(base_dir=tmp_path, user_id="charlie")
        merged = charlie.list_merged_queries()
        assert any(q["name"] == "good_q" for q in merged)

    def test_preferences_are_per_user(self, tmp_path):
        alice_prefs = PreferenceStore(user_dir=tmp_path / "users" / "alice")
        bob_prefs = PreferenceStore(user_dir=tmp_path / "users" / "bob")
        alice_prefs.remember("pg_source", resolved_value="source_a")
        bob_prefs.remember("pg_source", resolved_value="source_b")
        assert alice_prefs.get("pg_source")["resolved_value"] == "source_a"
        assert bob_prefs.get("pg_source")["resolved_value"] == "source_b"
