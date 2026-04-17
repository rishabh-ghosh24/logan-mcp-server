# tests/test_user_store.py
import pytest
from pathlib import Path
from oci_logan_mcp.user_store import UserStore

@pytest.fixture
def base_dir(tmp_path):
    return tmp_path

@pytest.fixture
def store(base_dir):
    return UserStore(base_dir=base_dir, user_id="alice")

class TestUserStoreInit:
    def test_creates_user_directory(self, store, base_dir):
        assert (base_dir / "users" / "alice").is_dir()

    def test_default_user_from_env(self, base_dir, monkeypatch):
        monkeypatch.setenv("USER", "bob")
        store = UserStore(base_dir=base_dir)
        assert store.user_id == "bob"

    def test_explicit_user_overrides_env(self, base_dir, monkeypatch):
        monkeypatch.setenv("USER", "bob")
        store = UserStore(base_dir=base_dir, user_id="alice")
        assert store.user_id == "alice"

    def test_logan_user_env_overrides_user(self, base_dir, monkeypatch):
        monkeypatch.setenv("USER", "opc")
        monkeypatch.setenv("LOGAN_USER", "alice")
        store = UserStore(base_dir=base_dir)
        assert store.user_id == "alice"

class TestLearnedQueries:
    def test_save_and_list(self, store):
        store.save_query(name="test_q", query="* | stats count", description="test", category="general")
        queries = store.list_queries()
        assert len(queries) == 1
        assert queries[0]["name"] == "test_q"

    def test_queries_persist_across_instances(self, base_dir):
        s1 = UserStore(base_dir=base_dir, user_id="alice")
        s1.save_query(name="q1", query="* | stats count", description="test", category="general")
        s2 = UserStore(base_dir=base_dir, user_id="alice")
        assert len(s2.list_queries()) == 1

    def test_users_are_isolated(self, base_dir):
        alice = UserStore(base_dir=base_dir, user_id="alice")
        bob = UserStore(base_dir=base_dir, user_id="bob")
        alice.save_query(name="q1", query="alice query", description="a", category="general")
        bob.save_query(name="q1", query="bob query", description="b", category="general")
        assert len(alice.list_queries()) == 1
        assert alice.list_queries()[0]["query"] == "alice query"
        assert bob.list_queries()[0]["query"] == "bob query"

    def test_delete_query(self, store):
        store.save_query(name="q1", query="test", description="test", category="general")
        assert store.delete_query("q1")
        assert len(store.list_queries()) == 0

    def test_record_usage_bumps_count(self, store):
        store.save_query(name="q1", query="test", description="test", category="general")
        store.record_usage("test")
        queries = store.list_queries()
        assert queries[0]["use_count"] == 2  # 1 from save + 1 from record

    def test_success_failure_tracking(self, store):
        store.save_query(name="q1", query="test", description="test", category="general")
        store.record_success("test")
        store.record_success("test")
        store.record_failure("test")
        q = store.list_queries()[0]
        assert q["success_count"] == 2
        assert q["failure_count"] == 1

class TestMergedQueries:
    def test_merges_shared_and_personal(self, base_dir):
        store = UserStore(base_dir=base_dir, user_id="alice")
        # Write a shared query manually
        shared_dir = base_dir / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        from oci_logan_mcp.file_lock import atomic_yaml_write
        atomic_yaml_write(shared_dir / "promoted_queries.yaml", {
            "version": 1,
            "queries": [{"name": "shared_q", "query": "shared", "description": "from shared",
                         "category": "general", "tags": [], "use_count": 10, "success_count": 8,
                         "failure_count": 2, "interest_score": 4, "source": "shared"}],
        })
        # Save a personal query
        store.save_query(name="personal_q", query="personal", description="mine", category="general")
        merged = store.list_merged_queries()
        names = {q["name"] for q in merged}
        assert "shared_q" in names
        assert "personal_q" in names

    def test_personal_overrides_shared_on_duplicate(self, base_dir):
        store = UserStore(base_dir=base_dir, user_id="alice")
        shared_dir = base_dir / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        from oci_logan_mcp.file_lock import atomic_yaml_write
        atomic_yaml_write(shared_dir / "promoted_queries.yaml", {
            "version": 1,
            "queries": [{"name": "q1", "query": "shared version", "description": "shared",
                         "category": "general", "tags": [], "use_count": 5, "success_count": 4,
                         "failure_count": 1, "interest_score": 3, "source": "shared"}],
        })
        store.save_query(name="q1", query="my version", description="personal", category="general")
        merged = store.list_merged_queries()
        q1 = next(q for q in merged if q["name"] == "q1")
        assert q1["query"] == "my version"

class TestLegacyMigration:
    def test_migrates_legacy_queries_on_first_init(self, base_dir):
        # Create legacy file
        legacy_dir = base_dir / "context"
        legacy_dir.mkdir(parents=True)
        from oci_logan_mcp.file_lock import atomic_yaml_write
        atomic_yaml_write(legacy_dir / "learned_queries.yaml", {
            "version": 1,
            "queries": [{"name": "old_q", "query": "legacy", "description": "old",
                         "category": "general", "tags": [], "use_count": 5,
                         "created_at": "2026-01-01", "last_used": "2026-03-01"}],
        })
        store = UserStore(base_dir=base_dir, user_id="alice")
        queries = store.list_queries()
        assert len(queries) == 1
        assert queries[0]["name"] == "old_q"

    def test_does_not_overwrite_existing_user_data(self, base_dir):
        # Create user data first
        store = UserStore(base_dir=base_dir, user_id="alice")
        store.save_query(name="new_q", query="new", description="new", category="general")
        # Create legacy file after
        legacy_dir = base_dir / "context"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        from oci_logan_mcp.file_lock import atomic_yaml_write
        atomic_yaml_write(legacy_dir / "learned_queries.yaml", {
            "version": 1, "queries": [{"name": "old_q", "query": "old", "description": "old",
                                        "category": "general", "tags": [], "use_count": 1}],
        })
        # Re-init should NOT overwrite
        store2 = UserStore(base_dir=base_dir, user_id="alice")
        queries = store2.list_queries()
        assert len(queries) == 1
        assert queries[0]["name"] == "new_q"

    def test_rejects_invalid_user_id(self, base_dir):
        import pytest
        with pytest.raises(ValueError):
            UserStore(base_dir=base_dir, user_id="../../../etc")

class TestFilterQueries:
    def test_filter_by_category(self, store):
        store.save_query(name="sec_q", query="q1", description="sec", category="security")
        store.save_query(name="perf_q", query="q2", description="perf", category="performance")
        result = store.list_queries(category="security")
        assert len(result) == 1
        assert result[0]["name"] == "sec_q"

    def test_filter_by_tag(self, store):
        store.save_query(name="tagged", query="q1", description="test", category="general", tags=["auto-saved"])
        store.save_query(name="untagged", query="q2", description="test", category="general")
        result = store.list_queries(tag="auto-saved")
        assert len(result) == 1


class TestEntryId:
    def test_save_query_generates_entry_id(self, tmp_path):
        """New entries get a UUID4 hex entry_id."""
        store = UserStore(base_dir=tmp_path, user_id="alice")
        saved = store.save_query(name="q1", query="* | head 5", description="d")
        assert "entry_id" in saved
        assert len(saved["entry_id"]) == 32  # UUID4 hex
        assert all(c in "0123456789abcdef" for c in saved["entry_id"])

    def test_save_query_name_dedup_preserves_entry_id(self, tmp_path):
        """Updating an existing entry by name preserves its original entry_id."""
        store = UserStore(base_dir=tmp_path, user_id="alice")
        first = store.save_query(name="q1", query="* | head 5", description="d1")
        original_id = first["entry_id"]
        # Save again with same name, different query text — goes through name-dedup path
        second = store.save_query(name="q1", query="* | head 10", description="d2")
        assert second["entry_id"] == original_id

    def test_save_query_text_dedup_preserves_entry_id(self, tmp_path):
        """Updating an existing entry by query text preserves its original entry_id."""
        store = UserStore(base_dir=tmp_path, user_id="alice")
        first = store.save_query(name="q1", query="* | head 5", description="d")
        original_id = first["entry_id"]
        # Same query text, different name — goes through text-dedup path
        second = store.save_query(name="q1_renamed", query="* | head 5", description="d2")
        assert second["entry_id"] == original_id
        # The entry's name was updated
        assert second["name"] == "q1_renamed"

    def test_load_backfills_entry_id_for_legacy_entries(self, tmp_path):
        """Entries in legacy YAML without entry_id get UUIDs assigned on first load,
        and the file is rewritten so subsequent loads don't regenerate them."""
        # Write a legacy-style YAML (no entry_id) directly
        user_dir = tmp_path / "users" / "alice"
        user_dir.mkdir(parents=True)
        queries_file = user_dir / "learned_queries.yaml"
        import yaml
        queries_file.write_text(yaml.dump({
            "version": 1,
            "queries": [
                {"name": "legacy1", "query": "*", "description": "old"},
                {"name": "legacy2", "query": "* | head 5", "description": "old"},
            ],
        }))

        # First load — should backfill
        store = UserStore(base_dir=tmp_path, user_id="alice")
        queries = store.list_queries()
        assert len(queries) == 2
        ids_after_first = [q["entry_id"] for q in queries]
        assert all(len(eid) == 32 for eid in ids_after_first)

        # Second load — UUIDs should be SAME (idempotent, not regenerated)
        store2 = UserStore(base_dir=tmp_path, user_id="alice")
        queries2 = store2.list_queries()
        ids_after_second = [q["entry_id"] for q in queries2]
        assert sorted(ids_after_first) == sorted(ids_after_second)

    def test_save_query_existing_entry_id_not_overwritten(self, tmp_path):
        """If an entry in YAML already has an entry_id, it stays."""
        user_dir = tmp_path / "users" / "alice"
        user_dir.mkdir(parents=True)
        queries_file = user_dir / "learned_queries.yaml"
        import yaml
        custom_id = "abcd1234" * 4  # 32 chars
        queries_file.write_text(yaml.dump({
            "version": 1,
            "queries": [
                {"name": "preset", "query": "*", "description": "d", "entry_id": custom_id},
            ],
        }))
        store = UserStore(base_dir=tmp_path, user_id="alice")
        queries = store.list_queries()
        assert queries[0]["entry_id"] == custom_id
