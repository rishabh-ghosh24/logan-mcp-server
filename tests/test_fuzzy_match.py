"""Tests for fuzzy matching module."""

import pytest
from unittest.mock import patch, MagicMock

from oci_logan_mcp.fuzzy_match import (
    find_similar_fields,
    _fuzzy_match_simple,
    _simple_similarity,
    _common_substring_score,
    normalize_field_name,
)


# Sample field lists for testing
SAMPLE_FIELDS = [
    "Severity",
    "Host Name (Server)",
    "Entity Type",
    "Content",
    "Log Source",
    "Time",
    "User Name",
    "IP Address",
    "Error Code",
    "Request URL",
]


# ---------------------------------------------------------------
# find_similar_fields (top-level)
# ---------------------------------------------------------------


class TestFindSimilarFields:
    """Tests for the main find_similar_fields function."""

    def test_empty_available_fields(self):
        """Empty field list returns empty."""
        assert find_similar_fields("test", []) == []

    def test_exact_match_returned(self):
        """Exact match is returned."""
        result = find_similar_fields("Severity", SAMPLE_FIELDS)
        assert "Severity" in result

    def test_similar_match_found(self):
        """Typo should still find close match."""
        result = find_similar_fields("Severitty", SAMPLE_FIELDS)
        assert "Severity" in result

    def test_limit_respected(self):
        """Limit caps number of results."""
        result = find_similar_fields("a", SAMPLE_FIELDS, limit=2)
        assert len(result) <= 2

    def test_threshold_filters_low_scores(self):
        """High threshold filters out weak matches."""
        result = find_similar_fields("zzzzz", SAMPLE_FIELDS, threshold=90)
        # "zzzzz" has very low similarity to any field
        assert len(result) == 0


# ---------------------------------------------------------------
# Simple fallback algorithm
# ---------------------------------------------------------------


class TestFuzzyMatchSimple:
    """Tests for the simple fallback matching."""

    @patch("oci_logan_mcp.fuzzy_match.RAPIDFUZZ_AVAILABLE", False)
    def test_fallback_used_when_no_rapidfuzz(self):
        """Falls back to simple matching when rapidfuzz unavailable."""
        result = find_similar_fields("Severity", SAMPLE_FIELDS)
        assert "Severity" in result

    def test_case_insensitive_matching(self):
        """Matching is case-insensitive."""
        result = _fuzzy_match_simple("severity", SAMPLE_FIELDS, limit=5, threshold=50)
        assert "Severity" in result

    def test_sorts_by_score_descending(self):
        """Best match comes first."""
        result = _fuzzy_match_simple("Severity", SAMPLE_FIELDS, limit=5, threshold=10)
        assert result[0] == "Severity"

    def test_limit_applied(self):
        """Respects limit parameter."""
        result = _fuzzy_match_simple("a", SAMPLE_FIELDS, limit=2, threshold=0)
        assert len(result) <= 2

    def test_threshold_normalized(self):
        """Threshold is normalized from 0-100 to 0-1 scale."""
        # threshold=0 should return everything
        result = _fuzzy_match_simple("x", SAMPLE_FIELDS, limit=100, threshold=0)
        assert len(result) > 0

        # threshold=100 should return almost nothing (only exact matches score 1.0)
        result = _fuzzy_match_simple("nonexistent", SAMPLE_FIELDS, limit=100, threshold=100)
        assert len(result) == 0


# ---------------------------------------------------------------
# Rapidfuzz path
# ---------------------------------------------------------------


class TestFuzzyMatchRapidfuzz:
    """Tests for the rapidfuzz-based matching."""

    @patch("oci_logan_mcp.fuzzy_match.RAPIDFUZZ_AVAILABLE", True)
    @patch("oci_logan_mcp.fuzzy_match.process")
    def test_uses_rapidfuzz_when_available(self, mock_process):
        """Uses rapidfuzz when available."""
        mock_process.extract.return_value = [("Severity", 95.0, 0)]
        result = find_similar_fields("Severity", SAMPLE_FIELDS)
        mock_process.extract.assert_called_once()
        assert result == ["Severity"]

    @patch("oci_logan_mcp.fuzzy_match.RAPIDFUZZ_AVAILABLE", True)
    @patch("oci_logan_mcp.fuzzy_match.process")
    def test_filters_by_threshold(self, mock_process):
        """Filters results below threshold."""
        mock_process.extract.return_value = [
            ("Severity", 95.0, 0),
            ("Content", 30.0, 3),
        ]
        result = find_similar_fields("Sev", SAMPLE_FIELDS, threshold=50)
        assert result == ["Severity"]

    @patch("oci_logan_mcp.fuzzy_match.RAPIDFUZZ_AVAILABLE", True)
    @patch("oci_logan_mcp.fuzzy_match.process")
    def test_returns_names_only(self, mock_process):
        """Returns list of strings, not tuples."""
        mock_process.extract.return_value = [("Field1", 80.0, 0), ("Field2", 70.0, 1)]
        result = find_similar_fields("test", ["Field1", "Field2"], threshold=50)
        assert all(isinstance(r, str) for r in result)


# ---------------------------------------------------------------
# _simple_similarity
# ---------------------------------------------------------------


class TestSimpleSimilarity:
    """Tests for simple similarity scoring."""

    def test_identical_strings(self):
        """Identical strings -> 1.0."""
        assert _simple_similarity("test", "test") == 1.0

    def test_substring_match(self):
        """Substring match -> high score (>= 0.7)."""
        score = _simple_similarity("host", "hostname")
        assert score >= 0.7

    def test_contains_reverse(self):
        """Longer string contains shorter -> high score."""
        score = _simple_similarity("hostname", "host")
        assert score >= 0.7

    def test_completely_different(self):
        """Completely different strings -> low score."""
        score = _simple_similarity("abc", "xyz")
        assert score <= 0.3

    def test_partial_character_overlap(self):
        """Some shared chars -> medium score."""
        score = _simple_similarity("abcde", "acdex")
        assert 0.0 < score < 1.0

    def test_empty_strings(self):
        """Both empty -> score 1.0 (identical)."""
        assert _simple_similarity("", "") == 1.0

    def test_one_empty_one_not(self):
        """One empty, one not -> substring match (empty in anything)."""
        # empty string is "in" every string, so substring branch
        score = _simple_similarity("", "abc")
        assert score >= 0.7


# ---------------------------------------------------------------
# _common_substring_score
# ---------------------------------------------------------------


class TestCommonSubstringScore:
    """Tests for common substring scoring."""

    def test_no_common_substring(self):
        """No common chars -> 0."""
        assert _common_substring_score("abc", "xyz") == 0

    def test_full_match(self):
        """Identical strings -> score of 1.0."""
        score = _common_substring_score("hello", "hello")
        assert score == 1.0

    def test_partial_substring(self):
        """Partial overlap -> between 0 and 1."""
        score = _common_substring_score("abcdef", "cdefgh")
        assert 0 < score < 1

    def test_empty_first_string(self):
        """Empty first string -> 0."""
        assert _common_substring_score("", "abc") == 0

    def test_empty_second_string(self):
        """Empty second string -> 0."""
        assert _common_substring_score("abc", "") == 0

    def test_both_empty(self):
        """Both empty -> 0."""
        assert _common_substring_score("", "") == 0


# ---------------------------------------------------------------
# normalize_field_name
# ---------------------------------------------------------------


class TestNormalizeFieldName:
    """Tests for field name normalization."""

    def test_strips_single_quotes(self):
        assert normalize_field_name("'field'") == "field"

    def test_strips_double_quotes(self):
        assert normalize_field_name('"field"') == "field"

    def test_lowercases(self):
        assert normalize_field_name("FIELD") == "field"

    def test_replaces_underscores(self):
        assert normalize_field_name("my_field") == "my field"

    def test_replaces_hyphens(self):
        assert normalize_field_name("my-field") == "my field"

    def test_replaces_dots(self):
        assert normalize_field_name("my.field") == "my field"

    def test_collapses_whitespace(self):
        assert normalize_field_name("my   field") == "my field"

    def test_combined_normalization(self):
        assert normalize_field_name("'My_Complex-Field.Name'") == "my complex field name"
