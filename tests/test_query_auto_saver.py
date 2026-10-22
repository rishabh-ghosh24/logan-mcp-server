"""Tests for QueryAutoSaver — auto-saves interesting queries to learned_queries.yaml."""

import pytest
from unittest.mock import MagicMock, patch

from oci_logan_mcp.query_auto_saver import QueryAutoSaver
from oci_logan_mcp.user_store import UserStore


# ----------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------

@pytest.fixture
def mock_context_manager():
    """Create a mock ContextManager (passed through but not used for query storage)."""
    return MagicMock()


@pytest.fixture
def mock_user_store(tmp_path):
    """Create a real UserStore backed by a temp directory."""
    return UserStore(base_dir=tmp_path, user_id="testuser")


@pytest.fixture
def auto_saver(mock_context_manager, mock_user_store):
    return QueryAutoSaver(mock_context_manager, user_store=mock_user_store)


DUMMY_RESULT = {"data": {"rows": [[1]], "columns": [{"name": "count"}]}}


class TestVariantCaps:
    SHAPE = "'Log Source' = 'VCN Flow Logs' | stats count by 'Action' | sort -count | head 10"
    BASE = "top_10_vcn_flow_logs_count_by_action"

    @pytest.mark.parametrize("auto_slot", [True, False])
    def test_overall_store_limit_preserves_manual_entries(self, auto_saver, mock_user_store, auto_slot):
        from oci_logan_mcp.file_lock import atomic_yaml_write
        from oci_logan_mcp.user_store import MAX_QUERIES_PER_USER

        probe = auto_saver.process_successful_query("'Log Source' = 'Old' | stats count", DUMMY_RESULT)
        assert probe is not None
        manual = [dict(entry_id=f"manual-{i}", name=f"manual_{i}", query=f"manual query {i}",
                       description="mine", origin="manual", tags=[], use_count=1,
                       last_used="2000-01-01T00:00:00Z", success_count=0, interest_score=4)
                  for i in range(MAX_QUERIES_PER_USER - int(auto_slot))]
        seeded = manual + ([probe] if auto_slot else [])
        atomic_yaml_write(mock_user_store._queries_path, {"version": 1, "queries": seeded})
        new_query = "'Log Source' = 'New' | stats count"
        saved = auto_saver.process_successful_query(new_query, DUMMY_RESULT)
        entries = mock_user_store.list_queries()
        assert {entry["entry_id"] for entry in manual} <= {entry["entry_id"] for entry in entries}
        assert len(entries) == MAX_QUERIES_PER_USER
        if auto_slot:
            assert saved is not None
            assert any(entry["query"] == new_query for entry in entries)
            assert not any(entry["entry_id"] == probe["entry_id"] for entry in entries)
        else:
            assert saved is None
            assert {entry["entry_id"]: entry for entry in entries} == {
                entry["entry_id"]: entry for entry in seeded
            }

    def test_concurrent_savers_plan_and_apply_source_retention_together(self, auto_saver, mock_user_store, monkeypatch):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        for index in range(19):
            assert auto_saver.process_successful_query(self.audit_shape(index), DUMMY_RESULT) is not None
        other_store = UserStore(mock_user_store.base_dir, user_id=mock_user_store.user_id)
        other = QueryAutoSaver(MagicMock(), other_store)
        planned, release, attempted, other_planned = (threading.Event() for _ in range(4))
        save = mock_user_store.save_query
        metadata = other._generate_metadata

        def gated_save(*args, **kwargs):
            planned.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release the first saver")
            return save(*args, **kwargs)

        def observed_metadata(*args, **kwargs):
            other_planned.set()
            return metadata(*args, **kwargs)

        def second_save():
            attempted.set()
            return other.process_successful_query(self.audit_shape(20), DUMMY_RESULT)

        monkeypatch.setattr(mock_user_store, "save_query", gated_save)
        monkeypatch.setattr(other, "_generate_metadata", observed_metadata)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(auto_saver.process_successful_query, self.audit_shape(19), DUMMY_RESULT)
            try:
                assert planned.wait(3)
                second = executor.submit(second_save)
                assert attempted.wait(3)
                entered_while_first_pending = other_planned.wait(0.2)
            finally:
                release.set()
            assert first.result(timeout=5) is not None
            assert second.result(timeout=5) is not None
        entries = mock_user_store.list_queries()
        assert len(entries) == 20
        assert not entered_while_first_pending
        assert {self.audit_shape(19), self.audit_shape(20)} <= {entry["query"] for entry in entries}

    def variant(self, index):
        return self.SHAPE + f" | eval v = {index}"

    @staticmethod
    def audit_shape(index):
        return f"'Log Source' = 'OCI Audit Logs' | stats count by 'Field{index}' | sort -count | head 10"

    def test_fourth_variant_replaces_weakest_auto_saved(self, auto_saver, mock_user_store):
        for index in range(3):
            assert auto_saver.process_successful_query(self.variant(index), DUMMY_RESULT) is not None
        mock_user_store.record_success(self.variant(1))
        mock_user_store.record_success(self.variant(2))
        saved = auto_saver.process_successful_query(self.variant(3), DUMMY_RESULT)
        queries = mock_user_store.list_queries()
        assert saved is not None and len(queries) == 3
        assert {entry["query"] for entry in queries} == {self.variant(i) for i in (1, 2, 3)}
        assert not any(entry["name"].endswith("_v4") for entry in queries)

    def test_source_cap_evicts_weakest_across_distinct_shapes(self, auto_saver, mock_user_store):
        for index in range(22):
            assert auto_saver.process_successful_query(self.audit_shape(index), DUMMY_RESULT) is not None
        queries = mock_user_store.list_queries()
        assert len(queries) == 20
        assert {entry["query"] for entry in queries} == {self.audit_shape(i) for i in range(2, 22)}

    def test_source_cap_never_evicts_manual_saves(self, auto_saver, mock_user_store):
        manual = mock_user_store.save_query(name="mine", query=self.audit_shape(99), description="manual")
        for index in range(25):
            assert auto_saver.process_successful_query(self.audit_shape(index), DUMMY_RESULT) is not None
        queries = mock_user_store.list_queries()
        assert len(queries) == 21
        assert sum(entry.get("origin") == "auto" for entry in queries) == 20
        assert next(entry for entry in queries if entry["entry_id"] == manual["entry_id"])["query"] == self.audit_shape(99)

    def test_legacy_ten_variants_are_reduced_to_cap(self, auto_saver, mock_user_store):
        from oci_logan_mcp.file_lock import atomic_yaml_write

        entries = [dict(entry_id=f"id{i}", name=self.BASE if i == 0 else f"{self.BASE}_v{i+1}",
                        query=self.variant(i), description="legacy auto", category="network",
                        tags=["auto-saved"], use_count=1, success_count=i,
                        failure_count=0, interest_score=5) for i in range(10)]
        atomic_yaml_write(mock_user_store._queries_path, {"version": 1, "queries": entries})
        saved = auto_saver.process_successful_query(self.variant(99), DUMMY_RESULT)
        queries = mock_user_store.list_queries()
        assert saved is not None and len(queries) == 3
        assert {entry["query"] for entry in queries} == {self.variant(i) for i in (8, 9, 99)}

    def test_legacy_without_tags_is_manual_and_new_variants_are_bounded(self, auto_saver, mock_user_store):
        from oci_logan_mcp.file_lock import atomic_yaml_write

        entry = dict(entry_id="legacy", name="legacy", query=self.variant(50),
                     description="old manual", category="general", use_count=1,
                     success_count=0, failure_count=0, interest_score=5)
        atomic_yaml_write(mock_user_store._queries_path, {"version": 1, "queries": [entry]})
        for index in range(5):
            assert auto_saver.process_successful_query(self.variant(index), DUMMY_RESULT) is not None
        queries = mock_user_store.list_queries()
        assert len(queries) == 4
        assert next(item for item in queries if item["entry_id"] == "legacy") == entry
        assert sum(item.get("origin") == "auto" for item in queries) == 3
        assert any(item["query"] == self.variant(4) for item in queries)

    def test_collision_does_not_evict_existing_auto_queries(self, auto_saver, mock_user_store):
        from oci_logan_mcp.file_lock import atomic_yaml_write

        for index in range(3):
            assert auto_saver.process_successful_query(self.variant(index), DUMMY_RESULT) is not None
        original = mock_user_store.list_queries()
        atomic_yaml_write(mock_user_store._shared_dir / "promoted_queries.yaml", {"queries": [
            {"name": self.BASE, "query": "protected shared query", "description": "shared"}
        ]})
        saved = auto_saver.process_successful_query(self.variant(3), DUMMY_RESULT)
        assert saved is None
        assert mock_user_store.list_queries() == original


def test_semantic_auto_save_preserves_a_manual_entry(auto_saver, mock_user_store):
    query = (
        "Severity in ('ERROR', 'CRITICAL', 'FATAL') or "
        "'Original Log Content' like '%error%' or 'Original Log Content' like '%fail%' or "
        "'Original Log Content' like '%fatal%' or 'Original Log Content' like '%critical%' or "
        "'Original Log Content' like '%exception%' | stats count by 'Log Source' | sort -count"
    )
    original = mock_user_store.save_query(name="my_error_sources", query=query,
                                          description="Keep my explanation", category="errors")
    auto_saver.process_successful_query(query, DUMMY_RESULT)
    [entry] = mock_user_store.list_queries()
    for field in ("entry_id", "name", "query", "description", "category"):
        assert entry[field] == original[field]
    assert entry["origin"] == "manual" and entry["use_count"] == 2


def test_semantic_canonical_name_collision_keeps_both_queries(auto_saver, mock_user_store):
    query = (
        "Severity in ('ERROR', 'CRITICAL', 'FATAL') or "
        "'Original Log Content' like '%error%' or 'Original Log Content' like '%fail%' or "
        "'Original Log Content' like '%fatal%' or 'Original Log Content' like '%critical%' or "
        "'Original Log Content' like '%exception%' | stats count by 'Log Source' | sort -count"
    )
    auto = mock_user_store.save_query(name="legacy_auto", query=query, description="automatic",
                                      origin="auto")
    manual = mock_user_store.save_query(name="error_like_sources_by_log_source",
                                        query="my different query", description="mine")
    auto_saver.process_successful_query(query, DUMMY_RESULT)
    entries = mock_user_store.list_queries()
    assert {entry["query"] for entry in entries} == {query, "my different query"}
    assert {entry["entry_id"] for entry in entries} == {auto["entry_id"], manual["entry_id"]}
    assert next(entry for entry in entries if entry["entry_id"] == manual["entry_id"])["name"] == manual["name"]


# ================================================================
# Interest scoring
# ================================================================

class TestInterestScoring:
    """Test _compute_interest_score."""

    def test_bare_wildcard_is_trivial(self, auto_saver):
        assert auto_saver._compute_interest_score("*") == 0

    def test_wildcard_head_is_trivial(self, auto_saver):
        assert auto_saver._compute_interest_score("* | head 10") == 0

    def test_wildcard_stats_count_is_trivial(self, auto_saver):
        assert auto_saver._compute_interest_score("* | stats count") == 0

    def test_stats_gives_one_point(self, auto_saver):
        # stats (+1) but nothing else -> score 1, below threshold
        score = auto_saver._compute_interest_score("'Log Source' != null | stats count")
        assert score >= 1

    def test_stats_plus_sort_head_is_interesting(self, auto_saver):
        q = "'Log Source' != null | stats count by 'Log Source' | sort -count | head 10"
        score = auto_saver._compute_interest_score(q)
        assert score >= 2  # stats +1, sort+head +1, 3+ stages +1

    def test_where_adds_point(self, auto_saver):
        q = "* | where Severity = 'ERROR' | stats count"
        score = auto_saver._compute_interest_score(q)
        assert score >= 2  # where +1, stats +1

    def test_log_source_filter_adds_point(self, auto_saver):
        q = "'Log Source' = 'Linux Secure Logs' | stats count"
        score = auto_saver._compute_interest_score(q)
        assert score >= 2  # source +1, stats +1

    def test_advanced_commands_add_points(self, auto_saver):
        q = "* | stats count by Entity | eval ratio = count / 100 | dedup Entity"
        score = auto_saver._compute_interest_score(q)
        assert score >= 3  # stats +1, eval +1, dedup +1 (capped at +2)

    def test_advanced_commands_capped_at_two(self, auto_saver):
        q = "* | eval x = 1 | dedup y | distinct z | rename a as b | cluster"
        score = auto_saver._compute_interest_score(q)
        # 5 advanced commands but capped at +2, plus 3+ stages +1 = at least 3
        assert score <= 5  # sanity: can't be unreasonably high

    def test_three_plus_stages_adds_point(self, auto_saver):
        q = "* | where x = 1 | stats count | sort -count"
        score = auto_saver._compute_interest_score(q)
        assert score >= 3  # where +1, stats +1, 3+ stages +1

    def test_timestats_gives_point(self, auto_saver):
        q = "* | timestats span = 1hour count by 'Log Source'"
        score = auto_saver._compute_interest_score(q)
        assert score >= 1


# ================================================================
# Trivial rejection
# ================================================================

class TestTrivialRejection:
    """Trivial queries should NOT be auto-saved."""

    @pytest.mark.parametrize("query", [
        "*",
        "  *  ",
        "* | head 5",
        "* | head 100",
        "* | stats count",
        "  *  |  stats  count  ",
    ])
    def test_trivial_not_saved(self, auto_saver, mock_user_store, query):
        result = auto_saver.process_successful_query(query, DUMMY_RESULT)
        assert result is None
        assert len(mock_user_store.list_queries()) == 0


# ================================================================
# Deduplication
# ================================================================

class TestDeduplication:
    """Already-saved queries should bump use_count, not create duplicates."""

    def test_existing_query_bumps_usage(self, auto_saver, mock_user_store):
        q = "'Log Source' != null | stats count by 'Log Source' | sort -count | head 10"
        # First call saves it
        auto_saver.process_successful_query(q, DUMMY_RESULT)
        initial_count = mock_user_store.list_queries()[0]["use_count"]
        # Second call should bump usage, not create duplicate
        result = auto_saver.process_successful_query(q, DUMMY_RESULT)
        assert result is None
        queries = mock_user_store.list_queries()
        assert len(queries) == 1
        assert queries[0]["use_count"] > initial_count


# ================================================================
# Name generation
# ================================================================

class TestNameGeneration:
    """Test _generate_metadata name, description, category."""

    def test_source_filter_in_name(self, auto_saver):
        q = "'Log Source' = 'Linux Secure Logs' | stats count by 'Entity'"
        name, desc, cat = auto_saver._generate_metadata(q)
        assert "linux_secure" in name
        assert "by_entity" in name

    def test_top_n_in_name(self, auto_saver):
        q = "* | stats count by 'Log Source' | sort -count | head 10"
        name, desc, cat = auto_saver._generate_metadata(q)
        assert "top_10" in name

    def test_timestats_action(self, auto_saver):
        q = "'Log Source' != null | timestats span = 1hour count by 'Log Source'"
        name, desc, cat = auto_saver._generate_metadata(q)
        assert "trend" in name

    def test_name_max_length(self, auto_saver):
        q = "'Log Source' = 'Very Long Log Source Name That Goes On Forever' | stats count by 'Another Very Long Field Name'"
        name, desc, cat = auto_saver._generate_metadata(q)
        assert len(name) <= 60

    def test_unique_name_avoids_collision(self, auto_saver, mock_user_store):
        # First, find out what name the auto-saver generates for this query
        q = "'Log Source' = 'Linux Secure Logs' | stats count by 'Entity'"
        base_name, _, _ = auto_saver._generate_metadata(q)
        # Save a query with that exact name to cause collision
        mock_user_store.save_query(
            name=base_name, query="other query", description="test", category="general"
        )
        name, desc, cat = auto_saver._generate_metadata(q)
        assert name != base_name
        assert "_v2" in name

    def test_error_like_sources_query_gets_semantic_name(self, auto_saver):
        q = (
            "(Severity in ('ERROR', 'CRITICAL', 'FATAL') "
            "or 'Original Log Content' like '%error%' "
            "or 'Original Log Content' like '%fail%' "
            "or 'Original Log Content' like '%fatal%' "
            "or 'Original Log Content' like '%critical%' "
            "or 'Original Log Content' like '%exception%') "
            "and 'Original Log Content' not like '%NOERROR%' "
            "| stats count by 'Log Source' | sort -count"
        )

        saved = auto_saver.process_successful_query(q, DUMMY_RESULT)

        assert saved is not None
        assert saved["name"] == "error_like_sources_by_log_source"
        assert saved["category"] == "errors"
        assert saved["intent_key"] == "error_like_sources"
        assert saved["query_shape"] == "count_by_log_source"

    def test_legacy_generic_error_source_query_is_canonicalized(self, auto_saver, mock_user_store):
        q = (
            "(Severity in ('ERROR', 'CRITICAL', 'FATAL') "
            "or 'Original Log Content' like '%error%' "
            "or 'Original Log Content' like '%fail%' "
            "or 'Original Log Content' like '%fatal%' "
            "or 'Original Log Content' like '%critical%' "
            "or 'Original Log Content' like '%exception%') "
            "and 'Original Log Content' not like '%NOERROR%' "
            "| stats count by 'Log Source' | sort -count"
        )
        mock_user_store.save_query(
            name="count_by_log_source_v19",
            query=q,
            description="Legacy generic name",
            category="general",
            tags=["auto-saved"],
        )
        # This fixture represents a pre-origin YAML file, not a new manual save.
        data = mock_user_store._load()
        data["queries"][0].pop("origin", None)
        mock_user_store._save(data)

        saved = auto_saver.process_successful_query(q, DUMMY_RESULT)

        assert saved is not None
        assert saved["name"] == "error_like_sources_by_log_source"
        queries = mock_user_store.list_queries()
        assert [query["name"] for query in queries] == ["error_like_sources_by_log_source"]
        assert queries[0]["intent_key"] == "error_like_sources"
        assert queries[0]["query_shape"] == "count_by_log_source"

    def test_legacy_canonicalization_preserves_history_and_is_idempotent(self, auto_saver, mock_user_store):
        q = (
            "(Severity in ('ERROR', 'CRITICAL', 'FATAL') "
            "or 'Original Log Content' like '%error%' "
            "or 'Original Log Content' like '%fail%' "
            "or 'Original Log Content' like '%fatal%' "
            "or 'Original Log Content' like '%critical%' "
            "or 'Original Log Content' like '%exception%') "
            "and 'Original Log Content' not like '%NOERROR%' "
            "| stats count by 'Log Source' | sort -count"
        )
        legacy = mock_user_store.save_query(
            name="count_by_log_source_v19",
            query=q,
            description="Legacy generic name",
            category="general",
            tags=["auto-saved"],
        )
        data = mock_user_store._load()
        data["queries"][0].pop("origin", None)
        data["queries"][0]["use_count"] = 7
        data["queries"][0]["success_count"] = 4
        data["queries"][0]["failure_count"] = 2
        data["queries"][0]["created_at"] = "2026-04-01T00:00:00+00:00"
        mock_user_store._save(data)

        first = auto_saver.process_successful_query(q, DUMMY_RESULT)
        second = auto_saver.process_successful_query(q, DUMMY_RESULT)

        queries = mock_user_store.list_queries()
        assert [query["name"] for query in queries] == ["error_like_sources_by_log_source"]
        assert first["name"] == "error_like_sources_by_log_source"
        assert second["name"] == "error_like_sources_by_log_source"
        assert queries[0]["entry_id"] == legacy["entry_id"]
        assert queries[0]["use_count"] >= 9
        assert queries[0]["success_count"] == 4
        assert queries[0]["failure_count"] == 2
        assert queries[0]["created_at"] == "2026-04-01T00:00:00+00:00"


# ================================================================
# Category inference
# ================================================================

class TestCategoryInference:
    """Test category detection from query keywords."""

    def test_security_category(self, auto_saver):
        q = "'Log Source' = 'Linux Secure Logs' | where Message contains 'Failed password'"
        _, _, cat = auto_saver._generate_metadata(q)
        assert cat == "security"

    def test_errors_category(self, auto_saver):
        q = "* | where Severity = 'ERROR' | stats count"
        _, _, cat = auto_saver._generate_metadata(q)
        assert cat == "errors"

    def test_audit_category(self, auto_saver):
        q = "'Log Source' = 'OCI Audit Logs' | stats count by 'Action'"
        _, _, cat = auto_saver._generate_metadata(q)
        assert cat == "audit"

    def test_network_category(self, auto_saver):
        q = "'Log Source' = 'OCI VCN Flow Unified Schema Logs' | stats count"
        _, _, cat = auto_saver._generate_metadata(q)
        assert cat == "network"

    def test_general_fallback(self, auto_saver):
        q = "* | stats count by 'Log Source' | sort -count | head 10"
        _, _, cat = auto_saver._generate_metadata(q)
        assert cat == "general"


# ================================================================
# End-to-end auto-save
# ================================================================

class TestAutoSaveIntegration:
    """Test the full process_successful_query flow."""

    def test_interesting_query_is_saved(self, auto_saver, mock_user_store):
        q = "'Log Source' != null | stats count as 'Log Count' by 'Log Source' | sort -'Log Count' | head 10"
        result = auto_saver.process_successful_query(q, DUMMY_RESULT)
        assert result is not None
        queries = mock_user_store.list_queries()
        assert len(queries) == 1
        assert "[auto-saved]" in queries[0]["description"]
        assert "auto-saved" in queries[0]["tags"]

    def test_auto_save_never_breaks_execution(self, auto_saver, mock_user_store):
        """Auto-save errors must be swallowed, never crash query execution."""
        # Simulate user_store.record_usage raising unexpectedly
        original = mock_user_store.record_usage
        mock_user_store.record_usage = lambda q: (_ for _ in ()).throw(RuntimeError("boom"))
        q = "'Log Source' != null | stats count by 'Log Source' | sort -count | head 10"
        # Should not raise
        result = auto_saver.process_successful_query(q, DUMMY_RESULT)
        assert result is None  # gracefully returned None
        mock_user_store.record_usage = original  # restore

    def test_requires_user_store(self, mock_context_manager):
        """QueryAutoSaver must fail fast if user_store is not provided."""
        with pytest.raises(TypeError):
            QueryAutoSaver(mock_context_manager)  # missing required arg

    def test_save_called_with_correct_query(self, auto_saver, mock_user_store):
        q = "  'Log Source' = 'OCI Audit Logs' | stats count by 'Status' | sort -Count  "
        auto_saver.process_successful_query(q, DUMMY_RESULT)
        queries = mock_user_store.list_queries()
        assert len(queries) == 1
        assert queries[0]["query"] == q.strip()


# ================================================================
# Helper methods
# ================================================================

class TestHelpers:
    """Test individual extraction/helper methods."""

    def test_extract_source(self, auto_saver):
        q = "'Log Source' = 'Linux Audit Logs' | stats count"
        assert auto_saver._extract_source(q) == "Linux Audit Logs"

    def test_extract_source_none(self, auto_saver):
        assert auto_saver._extract_source("* | stats count") is None

    def test_extract_groupby(self, auto_saver):
        q = "* | stats count by 'Entity'"
        assert auto_saver._extract_groupby(q) == "Entity"

    def test_extract_limit(self, auto_saver):
        q = "* | head 20"
        assert auto_saver._extract_limit(q) == "20"

    def test_extract_action_stats(self, auto_saver):
        stages = ["'Log Source' != null", "stats count by 'Log Source'"]
        assert auto_saver._extract_action(stages) == "count"

    def test_extract_action_timestats(self, auto_saver):
        stages = ["*", "timestats span = 1hour count"]
        assert auto_saver._extract_action(stages) == "trend"

    def test_slugify(self, auto_saver):
        assert auto_saver._slugify("Linux Secure Logs") == "linux_secure_logs"
        assert auto_saver._slugify("OCI VCN Flow") == "oci_vcn_flow"

    def test_infer_category_security(self, auto_saver):
        assert auto_saver._infer_category("failed password attempt") == "security"

    def test_infer_category_general(self, auto_saver):
        assert auto_saver._infer_category("just a random query") == "general"
