"""Tests for QueryAutoSaver — auto-saves interesting queries to learned_queries.yaml."""

import pytest
from unittest.mock import MagicMock, patch

from oci_logan_mcp.query_auto_saver import QueryAutoSaver


# ----------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------

@pytest.fixture
def mock_context_manager():
    """Create a mock ContextManager with the methods QueryAutoSaver uses."""
    cm = MagicMock()
    cm.record_query_usage.return_value = False  # query not yet saved
    cm.list_learned_queries.return_value = []   # no existing queries
    cm.save_learned_query.return_value = {
        "name": "test", "query": "test", "use_count": 1
    }
    return cm


@pytest.fixture
def auto_saver(mock_context_manager):
    return QueryAutoSaver(mock_context_manager)


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
    def test_trivial_not_saved(self, auto_saver, mock_context_manager, query):
        result = auto_saver.process_successful_query(query, DUMMY_RESULT)
        assert result is None
        mock_context_manager.save_learned_query.assert_not_called()


# ================================================================
# Deduplication
# ================================================================

class TestDeduplication:
    """Already-saved queries should bump use_count, not create duplicates."""

    def test_existing_query_bumps_usage(self, auto_saver, mock_context_manager):
        mock_context_manager.record_query_usage.return_value = True
        q = "'Log Source' != null | stats count by 'Log Source' | sort -count | head 10"
        result = auto_saver.process_successful_query(q, DUMMY_RESULT)
        assert result is None
        mock_context_manager.save_learned_query.assert_not_called()


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

    def test_unique_name_avoids_collision(self, auto_saver, mock_context_manager):
        mock_context_manager.list_learned_queries.return_value = [
            {"name": "linux_secure_count_by_entity"}
        ]
        q = "'Log Source' = 'Linux Secure Logs' | stats count by 'Entity'"
        name, desc, cat = auto_saver._generate_metadata(q)
        assert name != "linux_secure_count_by_entity"
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

    def test_interesting_query_is_saved(self, auto_saver, mock_context_manager):
        q = "'Log Source' != null | stats count as 'Log Count' by 'Log Source' | sort -'Log Count' | head 10"
        result = auto_saver.process_successful_query(q, DUMMY_RESULT)
        assert result is not None
        mock_context_manager.save_learned_query.assert_called_once()

        call_kwargs = mock_context_manager.save_learned_query.call_args
        assert "[auto-saved]" in call_kwargs.kwargs.get("description", call_kwargs[1].get("description", ""))
        tags = call_kwargs.kwargs.get("tags", call_kwargs[1].get("tags", []))
        assert "auto-saved" in tags

    def test_auto_save_never_breaks_execution(self, auto_saver, mock_context_manager):
        """Auto-save errors must be swallowed, never crash query execution."""
        mock_context_manager.record_query_usage.side_effect = RuntimeError("boom")
        q = "'Log Source' != null | stats count by 'Log Source' | sort -count | head 10"
        # Should not raise
        result = auto_saver.process_successful_query(q, DUMMY_RESULT)
        assert result is None  # gracefully returned None

    def test_save_called_with_correct_query(self, auto_saver, mock_context_manager):
        q = "  'Log Source' = 'OCI Audit Logs' | stats count by 'Status' | sort -Count  "
        auto_saver.process_successful_query(q, DUMMY_RESULT)
        call_kwargs = mock_context_manager.save_learned_query.call_args
        saved_query = call_kwargs.kwargs.get("query", call_kwargs[1].get("query", ""))
        assert saved_query == q.strip()


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
