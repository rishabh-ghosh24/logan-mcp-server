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
    def multi_series_data(self):
        """Create multi-series data (e.g., timestats output with by clause)."""
        return {
            "columns": [
                {"name": "Time"},
                {"name": "Error"},
                {"name": "Warning"},
                {"name": "Info"},
            ],
            "rows": [
                ["2024-01-01 00:00", 10, 20, 100],
                ["2024-01-01 01:00", 15, 25, 90],
                ["2024-01-01 02:00", 5, 10, 120],
                ["2024-01-01 03:00", 20, 30, 80],
            ],
        }

    @pytest.fixture
    def numeric_data(self):
        """Create numeric distribution data for histogram testing."""
        return {
            "columns": [
                {"name": "Response Time"},
            ],
            "rows": [
                [120], [250], [180], [90], [310],
                [150], [200], [170], [220], [280],
                [130], [160], [190], [240], [300],
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

    def test_generate_vertical_bar_chart(self, engine, sample_data):
        """Test vertical bar chart generation."""
        result = engine.generate(sample_data, ChartType.VERTICAL_BAR, title="Test Vertical Bar")

        assert "image_base64" in result
        assert result["chart_type"] == "vertical_bar"

        # Verify valid PNG
        decoded = base64.b64decode(result["image_base64"])
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"

    def test_generate_vertical_bar_stacked(self, engine, multi_series_data):
        """Test stacked vertical bar chart with multi-series data."""
        result = engine.generate(multi_series_data, ChartType.VERTICAL_BAR, title="Stacked Vertical")

        assert "image_base64" in result
        assert result["chart_type"] == "vertical_bar"
        assert len(result["raw_data"]) == 4

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

    def test_generate_heatmap(self, engine, multi_series_data):
        """Test heatmap generation with multi-series data."""
        result = engine.generate(multi_series_data, ChartType.HEATMAP, title="Test Heatmap")

        assert "image_base64" in result
        assert result["chart_type"] == "heatmap"

        # Verify valid PNG
        decoded = base64.b64decode(result["image_base64"])
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"

    def test_generate_heatmap_single_series(self, engine, sample_data):
        """Test heatmap with single value column (1D heatmap)."""
        result = engine.generate(sample_data, ChartType.HEATMAP, title="1D Heatmap")

        assert "image_base64" in result
        assert result["chart_type"] == "heatmap"

    def test_generate_histogram(self, engine, numeric_data):
        """Test histogram generation."""
        result = engine.generate(numeric_data, ChartType.HISTOGRAM, title="Response Time Distribution")

        assert "image_base64" in result
        assert result["chart_type"] == "histogram"

        # Verify valid PNG
        decoded = base64.b64decode(result["image_base64"])
        assert decoded[:8] == b"\x89PNG\r\n\x1a\n"

    def test_generate_histogram_with_labels(self, engine, sample_data):
        """Test histogram with label + count data (uses count column)."""
        result = engine.generate(sample_data, ChartType.HISTOGRAM, title="Count Distribution")

        assert "image_base64" in result
        assert result["chart_type"] == "histogram"

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
        """Test error handling for invalid/unsupported chart type string."""
        # ChartType enum no longer has unsupported values,
        # so test that passing a string that isn't in the enum raises ValueError
        with pytest.raises(ValueError):
            ChartType("nonexistent_chart")

    def test_to_dataframe(self, engine, sample_data):
        """Test conversion to DataFrame."""
        df = engine._to_dataframe(sample_data)

        assert len(df) == 3
        assert "Log Source" in df.columns
        assert "count" in df.columns

    def test_chart_type_enum_values(self):
        """Test that all expected chart types exist in the enum."""
        expected = [
            "pie", "bar", "vertical_bar", "line", "area",
            "table", "tile", "treemap", "heatmap", "histogram",
        ]
        actual = [ct.value for ct in ChartType]
        for val in expected:
            assert val in actual, f"ChartType missing: {val}"
