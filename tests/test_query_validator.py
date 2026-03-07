"""Tests for query validator."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.validator import QueryValidator, ValidationResult


class TestQueryValidator:
    """Tests for QueryValidator class."""

    @pytest.fixture
    def mock_schema_manager(self):
        """Create mock schema manager."""
        manager = MagicMock()
        manager.get_all_field_names = AsyncMock(
            return_value=["Severity", "Host Name", "Log Source", "Message"]
        )
        return manager

    @pytest.fixture
    def validator(self, mock_schema_manager):
        """Create validator with mock schema manager."""
        return QueryValidator(mock_schema_manager)

    @pytest.mark.asyncio
    async def test_validate_empty_query(self, validator):
        """Test validation of empty query."""
        result = await validator.validate("")

        assert result.valid is False
        assert "empty" in result.errors[0].lower()

    @pytest.mark.asyncio
    async def test_validate_valid_query(self, validator):
        """Test validation of valid query."""
        result = await validator.validate("* | stats count by 'Severity'")

        assert result.valid is True
        assert len(result.errors) == 0

    @pytest.mark.asyncio
    async def test_unbalanced_quotes(self, validator):
        """Test detection of unbalanced quotes."""
        result = await validator.validate("'Error | stats count")

        assert result.valid is False
        assert any("quote" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_unbalanced_parentheses(self, validator):
        """Test detection of unbalanced parentheses."""
        result = await validator.validate("* | where (a = 'b'")

        assert result.valid is False
        assert any("parenthes" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_empty_pipe_segment(self, validator):
        """Test detection of empty pipe segment."""
        result = await validator.validate("* | | stats count")

        assert result.valid is False
        assert any("pipe" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_field_not_found_suggestion(self, validator):
        """Test suggestion for invalid field name."""
        result = await validator.validate("'Serverity' = 'Error'")

        assert result.valid is False
        assert any("Severity" in s for s in result.suggestions)

    @pytest.mark.asyncio
    async def test_estimate_cost_wildcard(self, validator):
        """Test cost estimation for wildcard query."""
        result = await validator.validate("*")

        assert result.estimated_cost == "high"

    @pytest.mark.asyncio
    async def test_estimate_cost_filtered(self, validator):
        """Test cost estimation for filtered query."""
        result = await validator.validate("'Severity' = 'Error' | stats count")

        assert result.estimated_cost == "low"

    def test_get_query_suggestions(self, validator):
        """Test query suggestions."""
        suggestions = validator.get_query_suggestions("|")

        assert len(suggestions) > 0
        assert all("|" in s for s in suggestions)
