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
