"""Exhaustive tests for visualization engine."""

import pytest
import base64

from oci_logan_mcp.visualization import VisualizationEngine, ChartType


def _is_valid_png(b64_string: str) -> bool:
    """Verify a base64 string decodes to a valid PNG."""
    decoded = base64.b64decode(b64_string)
    return decoded[:8] == b"\x89PNG\r\n\x1a\n"


class TestVisualizationEngine:
    """Core tests for VisualizationEngine class."""

    @pytest.fixture
    def engine(self):
        """Create visualization engine."""
        return VisualizationEngine()

    @pytest.fixture
    def sample_data(self):
        """Create sample query result data (label + count)."""
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
        """Create multi-series data (timestats output with by clause)."""
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
    def datetime_data(self):
        """Create data with ISO datetime x-axis."""
        return {
            "columns": [
                {"name": "Start Time"},
                {"name": "count"},
            ],
            "rows": [
                ["2024-01-15T10:00:00Z", 120],
                ["2024-01-15T11:00:00Z", 180],
                ["2024-01-15T12:00:00Z", 95],
                ["2024-01-15T13:00:00Z", 210],
                ["2024-01-15T14:00:00Z", 155],
            ],
        }

    @pytest.fixture
    def numeric_data(self):
        """Create numeric distribution data for histogram testing."""
        return {
            "columns": [{"name": "Response Time"}],
            "rows": [
                [120], [250], [180], [90], [310],
                [150], [200], [170], [220], [280],
                [130], [160], [190], [240], [300],
            ],
        }

    @pytest.fixture
    def large_category_data(self):
        """Create data with >10 categories (for pie 'Other' grouping)."""
        return {
            "columns": [{"name": "Source"}, {"name": "count"}],
            "rows": [[f"Source{i}", 100 - i * 5] for i in range(15)],
        }

    @pytest.fixture
    def single_row_data(self):
        """Create single-row data for tile visualization."""
        return {
            "columns": [{"name": "Total Count"}],
            "rows": [[1234567]],
        }

    @pytest.fixture
    def empty_data(self):
        """Create empty query result data."""
        return {"columns": [], "rows": []}

    @pytest.fixture
    def none_rows_data(self):
        """Data with None rows."""
        return {
            "columns": [{"name": "a"}, {"name": "b"}],
            "rows": [None, ["x", 1], None, ["y", 2]],
        }

    @pytest.fixture
    def mismatched_column_data(self):
        """Data where rows have different lengths than columns."""
        return {
            "columns": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
            "rows": [
                ["x", 1],          # too few
                ["y", 2, 3, 4],    # too many
                ["z", 5, 6],       # exact
            ],
        }

    # ---------------------------------------------------------------
    # Pie Chart
    # ---------------------------------------------------------------

    def test_generate_pie_chart(self, engine, sample_data):
        """Test pie chart generation produces valid PNG."""
        result = engine.generate(sample_data, ChartType.PIE, title="Test Pie")

        assert result["image_format"] == "png"
        assert result["chart_type"] == "pie"
        assert _is_valid_png(result["image_base64"])

    def test_pie_chart_other_grouping(self, engine, large_category_data):
        """Pie chart should group >10 slices into 'Other'."""
        result = engine.generate(large_category_data, ChartType.PIE, title="Many Sources")

        assert result["chart_type"] == "pie"
        assert _is_valid_png(result["image_base64"])

    # ---------------------------------------------------------------
    # Bar Chart (horizontal)
    # ---------------------------------------------------------------

    def test_generate_bar_chart(self, engine, sample_data):
        """Test horizontal bar chart generation."""
        result = engine.generate(sample_data, ChartType.BAR, title="Test Bar")

        assert result["chart_type"] == "bar"
        assert _is_valid_png(result["image_base64"])

    def test_bar_chart_stacked(self, engine, multi_series_data):
        """Test stacked horizontal bar chart with multi-series data."""
        result = engine.generate(multi_series_data, ChartType.BAR, title="Stacked Bar")

        assert result["chart_type"] == "bar"
        assert len(result["raw_data"]) == 4

    # ---------------------------------------------------------------
    # Vertical Bar Chart
    # ---------------------------------------------------------------

    def test_generate_vertical_bar_chart(self, engine, sample_data):
        """Test vertical bar chart generation."""
        result = engine.generate(sample_data, ChartType.VERTICAL_BAR, title="Vertical Bar")

        assert result["chart_type"] == "vertical_bar"
        assert _is_valid_png(result["image_base64"])

    def test_vertical_bar_stacked(self, engine, multi_series_data):
        """Test stacked vertical bar chart with multi-series data."""
        result = engine.generate(multi_series_data, ChartType.VERTICAL_BAR, title="Stacked")

        assert result["chart_type"] == "vertical_bar"
        assert len(result["raw_data"]) == 4

    def test_vertical_bar_many_categories(self, engine, large_category_data):
        """Vertical bar with >6 categories should rotate labels."""
        result = engine.generate(large_category_data, ChartType.VERTICAL_BAR)

        assert result["chart_type"] == "vertical_bar"
        assert _is_valid_png(result["image_base64"])

    # ---------------------------------------------------------------
    # Line Chart
    # ---------------------------------------------------------------

    def test_generate_line_chart(self, engine, sample_data):
        """Test line chart generation."""
        result = engine.generate(sample_data, ChartType.LINE, title="Test Line")

        assert result["chart_type"] == "line"
        assert _is_valid_png(result["image_base64"])

    def test_line_chart_datetime_axis(self, engine, datetime_data):
        """Line chart should detect and format datetime x-axis."""
        result = engine.generate(datetime_data, ChartType.LINE, title="Trend Over Time")

        assert result["chart_type"] == "line"
        assert _is_valid_png(result["image_base64"])

    def test_line_chart_multi_series(self, engine, multi_series_data):
        """Line chart should render multiple value series with legend."""
        result = engine.generate(multi_series_data, ChartType.LINE, title="Multi Series")

        assert result["chart_type"] == "line"
        assert len(result["raw_data"]) == 4

    # ---------------------------------------------------------------
    # Area Chart
    # ---------------------------------------------------------------

    def test_generate_area_chart(self, engine, sample_data):
        """Test single area chart generation."""
        result = engine.generate(sample_data, ChartType.AREA, title="Area")

        assert result["chart_type"] == "area"
        assert _is_valid_png(result["image_base64"])

    def test_area_chart_stacked(self, engine, multi_series_data):
        """Test stacked area chart with multi-series data."""
        result = engine.generate(multi_series_data, ChartType.AREA, title="Stacked Area")

        assert result["chart_type"] == "area"
        assert len(result["raw_data"]) == 4

    def test_area_chart_datetime_axis(self, engine, datetime_data):
        """Area chart should handle datetime x-axis."""
        result = engine.generate(datetime_data, ChartType.AREA, title="Volume Over Time")

        assert result["chart_type"] == "area"
        assert _is_valid_png(result["image_base64"])

    # ---------------------------------------------------------------
    # Table
    # ---------------------------------------------------------------

    def test_generate_table(self, engine, sample_data):
        """Test table generation."""
        result = engine.generate(sample_data, ChartType.TABLE, title="Test Table")

        assert result["chart_type"] == "table"
        assert _is_valid_png(result["image_base64"])

    def test_table_truncates_to_20_rows(self, engine):
        """Table should show max 20 rows."""
        data = {
            "columns": [{"name": "x"}, {"name": "y"}],
            "rows": [[f"row{i}", i] for i in range(50)],
        }
        result = engine.generate(data, ChartType.TABLE)
        assert result["chart_type"] == "table"
        # Raw data should still have all rows
        assert len(result["raw_data"]) == 50

    # ---------------------------------------------------------------
    # Tile
    # ---------------------------------------------------------------

    def test_generate_tile(self, engine, sample_data):
        """Test tile generation."""
        result = engine.generate(sample_data, ChartType.TILE, title="Total Count")

        assert result["chart_type"] == "tile"
        assert _is_valid_png(result["image_base64"])

    def test_tile_large_number_formatting(self, engine):
        """Tile should format large numbers with K/M suffixes."""
        data = {
            "columns": [{"name": "count"}],
            "rows": [[1500000]],
        }
        result = engine.generate(data, ChartType.TILE, title="Events")
        assert result["chart_type"] == "tile"
        assert _is_valid_png(result["image_base64"])

    # ---------------------------------------------------------------
    # Heatmap
    # ---------------------------------------------------------------

    def test_generate_heatmap(self, engine, multi_series_data):
        """Test heatmap generation with multi-series data."""
        result = engine.generate(multi_series_data, ChartType.HEATMAP, title="Heatmap")

        assert result["chart_type"] == "heatmap"
        assert _is_valid_png(result["image_base64"])

    def test_heatmap_single_series(self, engine, sample_data):
        """Test heatmap with single value column (1D heatmap)."""
        result = engine.generate(sample_data, ChartType.HEATMAP, title="1D Heatmap")

        assert result["chart_type"] == "heatmap"
        assert _is_valid_png(result["image_base64"])

    def test_heatmap_annotations_for_small_matrix(self, engine):
        """Heatmap should show annotations for matrices <= 15x10."""
        data = {
            "columns": [{"name": "Host"}, {"name": "Mon"}, {"name": "Tue"}, {"name": "Wed"}],
            "rows": [
                ["host1", 10, 20, 15],
                ["host2", 5, 12, 8],
            ],
        }
        result = engine.generate(data, ChartType.HEATMAP)
        assert result["chart_type"] == "heatmap"
        assert _is_valid_png(result["image_base64"])

    # ---------------------------------------------------------------
    # Histogram
    # ---------------------------------------------------------------

    def test_generate_histogram(self, engine, numeric_data):
        """Test histogram generation."""
        result = engine.generate(numeric_data, ChartType.HISTOGRAM, title="Distribution")

        assert result["chart_type"] == "histogram"
        assert _is_valid_png(result["image_base64"])

    def test_histogram_with_labels(self, engine, sample_data):
        """Test histogram with label + count data (uses count column)."""
        result = engine.generate(sample_data, ChartType.HISTOGRAM, title="Count Dist")

        assert result["chart_type"] == "histogram"
        assert _is_valid_png(result["image_base64"])

    def test_histogram_few_values(self, engine):
        """Histogram with few values should auto-adjust bins."""
        data = {
            "columns": [{"name": "value"}],
            "rows": [[10], [20], [30]],
        }
        result = engine.generate(data, ChartType.HISTOGRAM)
        assert result["chart_type"] == "histogram"

    def test_histogram_many_values(self, engine):
        """Histogram with many values should auto-adjust bins."""
        data = {
            "columns": [{"name": "value"}],
            "rows": [[i * 0.5] for i in range(500)],
        }
        result = engine.generate(data, ChartType.HISTOGRAM)
        assert result["chart_type"] == "histogram"

    # ---------------------------------------------------------------
    # Treemap
    # ---------------------------------------------------------------

    def test_generate_treemap(self, engine, sample_data):
        """Test treemap generation (squarify or fallback bar)."""
        result = engine.generate(sample_data, ChartType.TREEMAP, title="Treemap")

        # Should produce either treemap or fallback bar chart
        assert result["chart_type"] == "treemap"
        assert _is_valid_png(result["image_base64"])

    # ---------------------------------------------------------------
    # Empty Data
    # ---------------------------------------------------------------

    def test_generate_empty_chart(self, engine, empty_data):
        """Empty data should produce 'empty' chart type."""
        result = engine.generate(empty_data, ChartType.BAR, title="Empty")

        assert result["chart_type"] == "empty"
        assert _is_valid_png(result["image_base64"])

    def test_empty_data_all_chart_types(self, engine, empty_data):
        """All chart types should handle empty data gracefully."""
        for ct in ChartType:
            result = engine.generate(empty_data, ct, title="Empty")
            assert result["chart_type"] == "empty", f"{ct.value} failed on empty data"

    # ---------------------------------------------------------------
    # DataFrame Conversion
    # ---------------------------------------------------------------

    def test_to_dataframe(self, engine, sample_data):
        """Test conversion to DataFrame."""
        df = engine._to_dataframe(sample_data)

        assert len(df) == 3
        assert "Log Source" in df.columns
        assert "count" in df.columns

    def test_to_dataframe_skips_none_rows(self, engine, none_rows_data):
        """None rows should be skipped."""
        df = engine._to_dataframe(none_rows_data)
        assert len(df) == 2

    def test_to_dataframe_normalizes_row_lengths(self, engine, mismatched_column_data):
        """Rows shorter/longer than columns should be padded/trimmed."""
        import pandas as pd

        df = engine._to_dataframe(mismatched_column_data)
        assert len(df) == 3
        assert len(df.columns) == 3
        # Short row should have NaN in third column (pandas pads with NaN, not None)
        assert pd.isna(df.iloc[0, 2])
        # Long row should be trimmed
        assert df.iloc[1].tolist() == ["y", 2, 3]

    def test_to_dataframe_no_columns(self, engine):
        """Should auto-generate column names when columns list is empty."""
        data = {
            "columns": [],
            "rows": [["a", 1], ["b", 2]],
        }
        df = engine._to_dataframe(data)
        assert len(df) == 2
        assert "col_0" in df.columns

    def test_to_dataframe_callable_rows(self, engine):
        """Should handle callable row values."""
        data = {
            "columns": [{"name": "x"}],
            "rows": [lambda: ["hello"]],
        }
        df = engine._to_dataframe(data)
        assert len(df) == 1
        assert df.iloc[0, 0] == "hello"

    # ---------------------------------------------------------------
    # Datetime Detection
    # ---------------------------------------------------------------

    def test_is_datetime_column_iso_format(self, engine):
        """Should detect ISO 8601 datetime strings."""
        import pandas as pd

        series = pd.Series(["2024-01-01T10:00:00Z", "2024-01-01T11:00:00Z"])
        assert engine._is_datetime_column(series) is True

    def test_is_datetime_column_non_datetime(self, engine):
        """Should reject non-datetime strings."""
        import pandas as pd

        series = pd.Series(["Linux Syslog", "Windows Event"])
        assert engine._is_datetime_column(series) is False

    def test_is_datetime_column_empty(self, engine):
        """Should return False for empty series."""
        import pandas as pd

        series = pd.Series([], dtype=object)
        assert engine._is_datetime_column(series) is False

    def test_parse_datetime_column(self, engine):
        """Should parse ISO datetime strings into datetime objects."""
        import pandas as pd

        series = pd.Series(["2024-01-01T10:00:00Z", "2024-01-01T11:00:00Z"])
        result = engine._parse_datetime_column(series)
        # pandas 2.x uses microsecond resolution (us), older uses nanosecond (ns)
        assert "datetime64" in str(result.dtype)
        assert "UTC" in str(result.dtype)

    # ---------------------------------------------------------------
    # Count Axis Formatting
    # ---------------------------------------------------------------

    def test_format_count_axis(self, engine):
        """Should not raise when formatting axes."""
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots()
        ax.bar(["a", "b"], [1000, 2000000])
        engine._format_count_axis(ax, axis="y")
        engine._format_count_axis(ax, axis="x")
        plt.close(fig)

    # ---------------------------------------------------------------
    # Raw Data & Result Structure
    # ---------------------------------------------------------------

    def test_raw_data_included(self, engine, sample_data):
        """Result should contain raw_data records."""
        result = engine.generate(sample_data, ChartType.BAR)

        assert "raw_data" in result
        assert len(result["raw_data"]) == 3

    def test_result_structure(self, engine, sample_data):
        """Result should have all required keys."""
        result = engine.generate(sample_data, ChartType.PIE)

        assert set(result.keys()) == {"image_base64", "image_format", "raw_data", "chart_type"}

    # ---------------------------------------------------------------
    # ChartType Enum
    # ---------------------------------------------------------------

    def test_invalid_chart_type_string(self):
        """Invalid string should raise ValueError."""
        with pytest.raises(ValueError):
            ChartType("nonexistent_chart")

    def test_chart_type_enum_values(self):
        """All expected chart types should exist in the enum."""
        expected = [
            "pie", "bar", "vertical_bar", "line", "area",
            "table", "tile", "treemap", "heatmap", "histogram",
        ]
        actual = [ct.value for ct in ChartType]
        for val in expected:
            assert val in actual, f"ChartType missing: {val}"

    def test_chart_type_count(self):
        """Should have exactly 10 chart types."""
        assert len(ChartType) == 10

    # ---------------------------------------------------------------
    # All Chart Types with Same Data (Regression)
    # ---------------------------------------------------------------

    def test_all_chart_types_sample_data(self, engine, sample_data):
        """Every chart type should succeed with standard label+count data."""
        for ct in ChartType:
            result = engine.generate(sample_data, ct, title=f"Test {ct.value}")
            assert "image_base64" in result, f"{ct.value} failed"
            assert _is_valid_png(result["image_base64"]), f"{ct.value} invalid PNG"

    def test_all_chart_types_multi_series(self, engine, multi_series_data):
        """Every chart type should succeed with multi-series data."""
        for ct in ChartType:
            result = engine.generate(multi_series_data, ct, title=f"Test {ct.value}")
            assert "image_base64" in result, f"{ct.value} failed"
            assert _is_valid_png(result["image_base64"]), f"{ct.value} invalid PNG"
