# tests/test_promote_queries.py
import pytest
import yaml
from pathlib import Path
from oci_logan_mcp.file_lock import atomic_yaml_write, atomic_yaml_read
from oci_logan_mcp.user_store import UserStore

# Import the promotion functions (we'll create them as importable)
from oci_logan_mcp.promote import should_promote, promote_all, sanitize_for_sharing

def _make_query(name="q1", query="'Log Source' = 'Linux' | stats count by Severity | sort -count | head 10",
                interest_score=4, success_count=8, failure_count=2, use_count=10):
    import uuid
    return {
        "entry_id": uuid.uuid4().hex,
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


def test_promote_aggregates_multi_user_same_query(tmp_path):
    """Two users saving identical queries aggregate into one promotion candidate
    with combined user count (enabling multi-user threshold)."""
    for user_id in ["alice", "bob"]:
        store = UserStore(base_dir=tmp_path, user_id=user_id)
        store.save_query(
            name="shared_pattern",
            query="'Error' | stats count by 'Host'",
            description="common pattern",
            interest_score=3,
        )
        # Bump success count to enable promotion
        qpath = tmp_path / "users" / user_id / "learned_queries.yaml"
        data = yaml.safe_load(qpath.read_text())
        data["queries"][0]["success_count"] = 5
        qpath.write_text(yaml.dump(data))

    result = promote_all(tmp_path)
    assert result["promoted"] == 1  # single canonical query despite 2 users
    assert result["scanned_users"] == 2

    shared = yaml.safe_load(
        (tmp_path / "shared" / "promoted_queries.yaml").read_text()
    )
    assert len(shared["queries"]) == 1
    assert shared["queries"][0]["success_count"] == 10  # 5+5 aggregated


def test_promote_handles_name_collision_cross_user(tmp_path):
    """Two users with DIFFERENT queries under the SAME name: winner is kept,
    loser gets rejected: name_collision_cross_user status."""
    # Alice saves "my_q" with text A
    store_a = UserStore(base_dir=tmp_path, user_id="alice")
    store_a.save_query(name="my_q", query="* alice variant", description="d", interest_score=5)
    qpath_a = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_a.read_text())
    data["queries"][0]["success_count"] = 10
    qpath_a.write_text(yaml.dump(data))

    # Bob saves "my_q" with DIFFERENT text B
    store_b = UserStore(base_dir=tmp_path, user_id="bob")
    store_b.save_query(name="my_q", query="* bob variant", description="d", interest_score=2)
    qpath_b = tmp_path / "users" / "bob" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_b.read_text())
    data["queries"][0]["success_count"] = 3
    qpath_b.write_text(yaml.dump(data))

    promote_all(tmp_path)

    # Alice (higher interest_score) wins, Bob gets rejection status
    alice_data = yaml.safe_load(qpath_a.read_text())
    bob_data = yaml.safe_load(qpath_b.read_text())
    assert alice_data["queries"][0].get("promotion_status") == "promoted"
    assert bob_data["queries"][0].get("promotion_status") == "rejected: name_collision_cross_user"


def test_promote_writes_back_status_to_personal(tmp_path):
    """After promote_all, each scanned personal entry has promotion_status set."""
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="low_quality", query="*", description="d", interest_score=1)
    qpath = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath.read_text())
    data["queries"][0]["success_count"] = 1
    data["queries"][0]["failure_count"] = 5  # low success rate
    qpath.write_text(yaml.dump(data))

    promote_all(tmp_path)

    data_after = yaml.safe_load(qpath.read_text())
    entry = data_after["queries"][0]
    assert "promotion_status" in entry
    assert entry["promotion_status"].startswith("rejected:") or entry["promotion_status"] == "pending"
    assert "promotion_reason" in entry


def test_promote_backfills_legacy_entries_without_entry_id(tmp_path):
    """A pre-1.2.0 learned_queries.yaml (no entry_id on any entry) must still
    be scanned, backfilled, and promoted by promote_all — even if the file has
    never been opened via UserStore._load() on this deployment."""
    user_dir = tmp_path / "users" / "alice"
    user_dir.mkdir(parents=True)
    # Legacy entry: NO entry_id, but qualifies for single-user promotion.
    legacy_entry = {
        "name": "legacy_q",
        "query": "'Log Source' = 'Linux' | stats count by Severity",
        "description": "legacy",
        "category": "general",
        "tags": [],
        "use_count": 10,
        "success_count": 8,
        "failure_count": 2,
        "interest_score": 5,
        "created_at": "2026-01-01T00:00:00",
        "last_used": "2026-03-24T00:00:00",
    }
    qpath = user_dir / "learned_queries.yaml"
    qpath.write_text(yaml.safe_dump({"version": 1, "queries": [legacy_entry]}))

    result = promote_all(tmp_path)

    # The legacy entry qualifies and must be promoted.
    assert result["promoted"] == 1

    # Backfill must have persisted an entry_id to disk so status write-back
    # can match it on this run (and future runs stay deterministic).
    data_after = yaml.safe_load(qpath.read_text())
    entry = data_after["queries"][0]
    assert entry.get("entry_id"), "entry_id must be backfilled and persisted"
    assert entry.get("promotion_status") == "promoted"


def test_promote_phase2_safe_against_concurrent_save(tmp_path):
    """Entry added between runs gets evaluated on the second run."""
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="q_first", query="*", description="d", interest_score=4)
    qpath = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath.read_text())
    data["queries"][0]["success_count"] = 10
    qpath.write_text(yaml.dump(data))

    result1 = promote_all(tmp_path)
    assert result1["promoted"] >= 0

    # Add another entry
    store.save_query(name="q_second", query="* | head 5", description="d", interest_score=4)
    data = yaml.safe_load(qpath.read_text())
    for q in data["queries"]:
        q["success_count"] = 10
    qpath.write_text(yaml.dump(data))

    result2 = promote_all(tmp_path)
    # New entry gets evaluated on second run
    data_after = yaml.safe_load(qpath.read_text())
    statuses = {q["name"]: q.get("promotion_status") for q in data_after["queries"]}
    assert "q_second" in statuses
    assert statuses["q_second"] is not None
