"""Tests for visualization engine."""

import pytest
import base64

from oci_logan_mcp.visualization import VisualizationEngine, ChartType


class TestVisualizationEngine:
    """Tests for VisualizationEngine class."""

    @pytest.fixture
    def engine(self):
        """Create visualization engine."""
        return VisualizationEngine()

    @pytest.fixture
    def sample_data(self):
        """Create sample query result data."""
        return {
            "columns": [
                {"name": "Log Source"},
                {"name": "count"},
            ],
            "rows": [
                ["Linux Syslog", 100],
                ["Windows Event", 50],
                ["Database Audit", 25],
            ],
        }

    @pytest.fixture
    def empty_data(self):
        """Create empty query result data."""
        return {"columns": [], "rows": []}

    def test_generate_pie_chart(self, engine, sample_data):
        """Test pie chart generation."""
        result = engine.generate(sample_data, ChartType.PIE, title="Test Pie")

        assert "image_base64" in result
        assert result["image_format"] == "png"
        assert result["chart_type"] == "pie"

        # Verify it's valid base64
        decoded = base64.b64decode(result["image_base64"])
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes

    def test_generate_bar_chart(self, engine, sample_data):
        """Test bar chart generation."""
        result = engine.generate(sample_data, ChartType.BAR, title="Test Bar")

        assert "image_base64" in result
        assert result["chart_type"] == "bar"

    def test_generate_line_chart(self, engine, sample_data):
        """Test line chart generation."""
        result = engine.generate(sample_data, ChartType.LINE, title="Test Line")

        assert "image_base64" in result
        assert result["chart_type"] == "line"

    def test_generate_table(self, engine, sample_data):
        """Test table generation."""
        result = engine.generate(sample_data, ChartType.TABLE, title="Test Table")

        assert "image_base64" in result
        assert result["chart_type"] == "table"

    def test_generate_tile(self, engine, sample_data):
        """Test tile generation."""
        result = engine.generate(sample_data, ChartType.TILE, title="Total Count")

        assert "image_base64" in result
        assert result["chart_type"] == "tile"

    def test_generate_empty_chart(self, engine, empty_data):
        """Test generation with empty data."""
        result = engine.generate(empty_data, ChartType.BAR, title="Empty")

        assert "image_base64" in result
        assert result["chart_type"] == "empty"

    def test_raw_data_included(self, engine, sample_data):
        """Test that raw data is included in result."""
        result = engine.generate(sample_data, ChartType.BAR)

        assert "raw_data" in result
        assert len(result["raw_data"]) == 3

    def test_invalid_chart_type(self, engine, sample_data):
        """Test error handling for invalid chart type."""
        with pytest.raises(ValueError):
            engine.generate(sample_data, ChartType.GEO_MAP)

    def test_to_dataframe(self, engine, sample_data):
        """Test conversion to DataFrame."""
        df = engine._to_dataframe(sample_data)

        assert len(df) == 3
        assert "Log Source" in df.columns
        assert "count" in df.columns
