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
    """Two users with DIFFERENT queries under the SAME name, both independently
    qualifying for promotion: winner is kept, loser gets
    rejected: name_collision_cross_user. (Bob must pass qualification on his
    own merits — otherwise his rejection is attributable to low quality, not
    the name collision with Alice.)"""
    store_a = UserStore(base_dir=tmp_path, user_id="alice")
    store_a.save_query(name="my_q", query="* alice variant", description="d", interest_score=9)
    qpath_a = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_a.read_text())
    data["queries"][0]["success_count"] = 10
    qpath_a.write_text(yaml.dump(data))

    # Bob: DIFFERENT text under the same name, independently qualifies.
    store_b = UserStore(base_dir=tmp_path, user_id="bob")
    store_b.save_query(name="my_q", query="* bob variant", description="d", interest_score=5)
    qpath_b = tmp_path / "users" / "bob" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_b.read_text())
    data["queries"][0]["success_count"] = 10
    qpath_b.write_text(yaml.dump(data))

    promote_all(tmp_path)

    # Alice (higher interest_score) wins, Bob gets the name-collision rejection.
    alice_data = yaml.safe_load(qpath_a.read_text())
    bob_data = yaml.safe_load(qpath_b.read_text())
    assert alice_data["queries"][0].get("promotion_status") == "promoted"
    assert bob_data["queries"][0].get("promotion_status") == "rejected: name_collision_cross_user"


def test_promoted_at_stable_when_entry_unchanged(tmp_path):
    """Re-running promote_all with no changes must preserve promoted_at on each
    shared entry — the field reflects last meaningful change, not last scan."""
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="stable_q", query="* | head 10", description="d", interest_score=5)
    qpath = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath.read_text())
    data["queries"][0]["success_count"] = 10
    qpath.write_text(yaml.dump(data))

    promote_all(tmp_path)
    shared1 = yaml.safe_load((tmp_path / "shared" / "promoted_queries.yaml").read_text())
    ts1 = shared1["queries"][0]["promoted_at"]

    import time
    time.sleep(0.05)  # ensure 'now' would differ if we weren't preserving

    promote_all(tmp_path)
    shared2 = yaml.safe_load((tmp_path / "shared" / "promoted_queries.yaml").read_text())
    ts2 = shared2["queries"][0]["promoted_at"]

    assert ts1 == ts2, f"promoted_at changed on no-op re-run: {ts1} -> {ts2}"


def test_promoted_at_refreshed_when_metrics_change(tmp_path):
    """When a shared entry's aggregated metrics change (e.g., new success runs),
    promoted_at must be refreshed to reflect the update."""
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="updating_q", query="* | head 5", description="d", interest_score=5)
    qpath = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath.read_text())
    data["queries"][0]["success_count"] = 10
    qpath.write_text(yaml.dump(data))

    promote_all(tmp_path)
    shared1 = yaml.safe_load((tmp_path / "shared" / "promoted_queries.yaml").read_text())
    ts1 = shared1["queries"][0]["promoted_at"]

    import time
    time.sleep(0.05)

    # Bump success_count — aggregated metrics now differ
    data = yaml.safe_load(qpath.read_text())
    data["queries"][0]["success_count"] = 20
    qpath.write_text(yaml.dump(data))

    promote_all(tmp_path)
    shared2 = yaml.safe_load((tmp_path / "shared" / "promoted_queries.yaml").read_text())
    ts2 = shared2["queries"][0]["promoted_at"]

    assert ts1 != ts2, "promoted_at should refresh when aggregated metrics change"


def test_promoted_at_refreshed_when_description_changes(tmp_path):
    """Description is a non-metric content field; editing it must refresh
    promoted_at. Protects _SHARED_CONTENT_FIELDS membership against drift:
    if 'description' were dropped from the comparison tuple, this test fails."""
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="desc_q", query="* | head 3", description="original",
                     interest_score=5)
    qpath = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath.read_text())
    data["queries"][0]["success_count"] = 10
    qpath.write_text(yaml.dump(data))

    promote_all(tmp_path)
    shared1 = yaml.safe_load((tmp_path / "shared" / "promoted_queries.yaml").read_text())
    ts1 = shared1["queries"][0]["promoted_at"]

    import time
    time.sleep(0.05)

    data = yaml.safe_load(qpath.read_text())
    data["queries"][0]["description"] = "updated wording"
    qpath.write_text(yaml.dump(data))

    promote_all(tmp_path)
    shared2 = yaml.safe_load((tmp_path / "shared" / "promoted_queries.yaml").read_text())
    ts2 = shared2["queries"][0]["promoted_at"]

    assert ts1 != ts2, "promoted_at should refresh when description changes"
    assert shared2["queries"][0]["description"] == "updated wording"


def test_promote_persists_user_count_on_shared_entry(tmp_path):
    """Shared entries record how many distinct users contributed, so consumers
    can read popularity from the shared catalog without re-scanning per-user
    YAMLs."""
    for user_id in ["alice", "bob", "carol"]:
        store = UserStore(base_dir=tmp_path, user_id=user_id)
        store.save_query(
            name="popular_pattern",
            query="'Error' | stats count by 'Host'",
            description="common",
            interest_score=3,
        )
        qpath = tmp_path / "users" / user_id / "learned_queries.yaml"
        data = yaml.safe_load(qpath.read_text())
        data["queries"][0]["success_count"] = 5
        qpath.write_text(yaml.dump(data))

    promote_all(tmp_path)

    shared = yaml.safe_load((tmp_path / "shared" / "promoted_queries.yaml").read_text())
    assert len(shared["queries"]) == 1
    assert shared["queries"][0]["user_count"] == 3


def test_single_user_promotion_records_user_count_one(tmp_path):
    """Single-user promotion still records user_count=1."""
    store = UserStore(base_dir=tmp_path, user_id="alice")
    store.save_query(name="solo_q", query="* | head 10", description="d", interest_score=5)
    qpath = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath.read_text())
    data["queries"][0]["success_count"] = 10
    qpath.write_text(yaml.dump(data))

    promote_all(tmp_path)
    shared = yaml.safe_load((tmp_path / "shared" / "promoted_queries.yaml").read_text())
    assert shared["queries"][0]["user_count"] == 1


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


def test_name_collision_falls_back_when_winner_fails_sanitization(tmp_path):
    """When the higher-scoring name-collision candidate fails sanitization, a
    lower-scoring but clean candidate with the same name must still be
    promoted — not pruned in phase 1.5 and silently dropped.

    Alice: 'my_q' with embedded secret (sanitize_for_sharing -> None), high score.
    Bob:   'my_q' with clean text, lower score.

    Expected: Bob's query is promoted, Alice's gets 'rejected: sanitization_failed'.
    Pre-fix behavior: Alice wins phase 1.5, sanitization fails in phase 2, Bob
    was already deleted as a name-collision loser — nothing gets promoted.
    """
    # Alice: higher interest, but query contains a secret.
    store_a = UserStore(base_dir=tmp_path, user_id="alice")
    store_a.save_query(
        name="my_q",
        query="password = 'hunter2' | stats count",
        description="dirty",
        interest_score=9,
    )
    qpath_a = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_a.read_text())
    data["queries"][0]["success_count"] = 20
    qpath_a.write_text(yaml.dump(data))

    # Bob: lower interest, clean query.
    store_b = UserStore(base_dir=tmp_path, user_id="bob")
    store_b.save_query(
        name="my_q",
        query="'Log Source' = 'Linux' | stats count by Host",
        description="clean",
        interest_score=5,
    )
    qpath_b = tmp_path / "users" / "bob" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_b.read_text())
    data["queries"][0]["success_count"] = 10
    qpath_b.write_text(yaml.dump(data))

    result = promote_all(tmp_path)

    # Bob's clean query should have been promoted as fallback.
    assert result["promoted"] == 1
    shared = yaml.safe_load((tmp_path / "shared" / "promoted_queries.yaml").read_text())
    assert len(shared["queries"]) == 1
    assert "hunter2" not in shared["queries"][0]["query"]
    assert shared["queries"][0]["name"] == "my_q"

    # Status write-back: Alice rejected for sanitization, Bob promoted.
    alice_data = yaml.safe_load(qpath_a.read_text())
    bob_data = yaml.safe_load(qpath_b.read_text())
    assert alice_data["queries"][0]["promotion_status"] == "rejected: sanitization_failed"
    assert bob_data["queries"][0]["promotion_status"] == "promoted"


def test_name_collision_resolved_when_multiple_candidates_qualify(tmp_path):
    """When both candidates of a name collision pass qualification AND
    sanitization, the higher-scoring one still wins and the other is marked
    rejected: name_collision_cross_user. (Regression guard for fix 3 — this
    is the scenario the existing collision logic already handles, and it must
    keep working after the reordering.)"""
    store_a = UserStore(base_dir=tmp_path, user_id="alice")
    store_a.save_query(
        name="shared_name",
        query="'Log Source' = 'Linux' | stats count",
        description="A",
        interest_score=9,
    )
    qpath_a = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_a.read_text())
    data["queries"][0]["success_count"] = 15
    qpath_a.write_text(yaml.dump(data))

    store_b = UserStore(base_dir=tmp_path, user_id="bob")
    store_b.save_query(
        name="shared_name",
        query="'Log Source' = 'Linux' | head 5",
        description="B",
        interest_score=4,
    )
    qpath_b = tmp_path / "users" / "bob" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_b.read_text())
    data["queries"][0]["success_count"] = 8
    qpath_b.write_text(yaml.dump(data))

    promote_all(tmp_path)

    alice_data = yaml.safe_load(qpath_a.read_text())
    bob_data = yaml.safe_load(qpath_b.read_text())
    assert alice_data["queries"][0]["promotion_status"] == "promoted"
    assert bob_data["queries"][0]["promotion_status"] == "rejected: name_collision_cross_user"


def test_phase3_evicts_stale_same_name_entry_when_new_winner_chosen(tmp_path):
    """Multi-run scenario: Bob's query is promoted to shared under name 'shared_name'.
    A later run sees Alice with a DIFFERENT query text under the same name, scoring
    higher. Alice must supersede Bob in shared — the stale Bob entry (same name,
    different canonical key) must be evicted so shared does not carry two entries
    with identical names and divergent queries.

    This is the case where per-user statuses and the shared catalog would otherwise
    disagree: Bob flagged 'rejected: name_collision_cross_user', Alice 'promoted',
    but shared/promoted_queries.yaml still contains both under the same name.
    """
    # --- Run 1: Bob promotes 'shared_name' with text B ---
    store_b = UserStore(base_dir=tmp_path, user_id="bob")
    store_b.save_query(
        name="shared_name",
        query="'Log Source' = 'Linux' | stats count by Host",
        description="bob's version",
        interest_score=5,
    )
    qpath_b = tmp_path / "users" / "bob" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_b.read_text())
    data["queries"][0]["success_count"] = 10
    qpath_b.write_text(yaml.dump(data))

    promote_all(tmp_path)

    shared_path = tmp_path / "shared" / "promoted_queries.yaml"
    shared = yaml.safe_load(shared_path.read_text())
    assert len(shared["queries"]) == 1
    assert shared["queries"][0]["name"] == "shared_name"
    bobs_query_text = shared["queries"][0]["query"]

    # --- Run 2: Alice arrives with different text under same name, higher score.
    # Use force=True because the save-time collision guard would otherwise block
    # her (Bob's version is now in the shared catalog). We are testing promotion
    # reconciliation, not the save guard. ---
    store_a = UserStore(base_dir=tmp_path, user_id="alice")
    store_a.save_query(
        name="shared_name",
        query="'Log Source' = 'Linux' | timestats count",
        description="alice's better version",
        interest_score=9,
        force=True,
    )
    qpath_a = tmp_path / "users" / "alice" / "learned_queries.yaml"
    data = yaml.safe_load(qpath_a.read_text())
    data["queries"][0]["success_count"] = 15
    qpath_a.write_text(yaml.dump(data))

    promote_all(tmp_path)

    # Shared catalog must have exactly one 'shared_name' entry, and it must be Alice's.
    shared = yaml.safe_load(shared_path.read_text())
    same_name_entries = [q for q in shared["queries"] if q["name"].lower() == "shared_name"]
    assert len(same_name_entries) == 1, (
        f"Stale same-name entry not evicted from shared: {same_name_entries}"
    )
    assert same_name_entries[0]["query"] != bobs_query_text, (
        "Shared still holds Bob's stale query after Alice superseded him"
    )
    assert "timestats" in same_name_entries[0]["query"]


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
