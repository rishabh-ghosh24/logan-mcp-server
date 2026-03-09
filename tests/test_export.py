"""Tests for export service module."""

import json
import pytest

from oci_logan_mcp.export import ExportService


@pytest.fixture
def svc():
    """Create an ExportService instance."""
    return ExportService()


@pytest.fixture
def sample_data():
    """Standard query result data."""
    return {
        "columns": [{"name": "Severity"}, {"name": "Count"}],
        "rows": [["ERROR", 42], ["WARN", 108], ["INFO", 999]],
        "total_count": 3,
        "is_partial": False,
    }


# ---------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------


class TestExportCSV:
    """Tests for CSV export."""

    def test_csv_basic_export(self, svc, sample_data):
        """CSV contains header and all rows."""
        result = svc.export(sample_data, format="csv")
        lines = result.strip().splitlines()
        assert lines[0] == "Severity,Count"
        assert lines[1] == "ERROR,42"
        assert len(lines) == 4  # header + 3 rows

    def test_csv_no_columns_infers_from_rows(self, svc):
        """Auto-infer column names when columns list is empty."""
        data = {"columns": [], "rows": [["a", 1], ["b", 2]]}
        result = svc.export(data, format="csv")
        lines = result.strip().splitlines()
        assert lines[0] == "col_0,col_1"
        assert lines[1] == "a,1"

    def test_csv_empty_rows(self, svc):
        """CSV with columns but no rows -> header-only."""
        data = {"columns": [{"name": "X"}], "rows": []}
        result = svc.export(data, format="csv")
        lines = result.strip().splitlines()
        assert len(lines) == 1
        assert lines[0] == "X"

    def test_csv_with_dict_rows(self, svc):
        """Rows as dicts -> values extracted."""
        data = {
            "columns": [{"name": "a"}, {"name": "b"}],
            "rows": [{"x": 1, "y": 2}],
        }
        result = svc.export(data, format="csv")
        lines = result.strip().splitlines()
        assert lines[1] == "1,2"

    def test_csv_with_callable_rows(self, svc):
        """Rows as callables -> invoked."""
        data = {
            "columns": [{"name": "a"}],
            "rows": [lambda: [42]],
        }
        result = svc.export(data, format="csv")
        lines = result.strip().splitlines()
        assert lines[1] == "42"

    def test_csv_with_none_row(self, svc):
        """None row -> materialized as empty list, producing an empty CSV row."""
        data = {"columns": [{"name": "a"}], "rows": [None]}
        result = svc.export(data, format="csv")
        # csv.writer writes "a\r\n\r\n" for header + empty row
        # After strip, the empty trailing line disappears, but the row was written
        assert result.count("\r\n") >= 2  # header + at least one data row

    def test_csv_with_scalar_row(self, svc):
        """Single scalar value -> wrapped in list."""
        data = {"columns": [{"name": "val"}], "rows": [99]}
        result = svc.export(data, format="csv")
        lines = result.strip().splitlines()
        assert lines[1] == "99"


# ---------------------------------------------------------------
# JSON Export
# ---------------------------------------------------------------


class TestExportJSON:
    """Tests for JSON export."""

    def test_json_basic_export(self, svc, sample_data):
        """JSON returns array of records by default."""
        result = svc.export(sample_data, format="json")
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 3
        assert parsed[0]["Severity"] == "ERROR"
        assert parsed[0]["Count"] == 42

    def test_json_with_metadata(self, svc, sample_data):
        """include_metadata=True wraps data with metadata."""
        result = svc.export(sample_data, format="json", include_metadata=True)
        parsed = json.loads(result)
        assert "metadata" in parsed
        assert "data" in parsed
        assert parsed["metadata"]["total_count"] == 3
        assert parsed["metadata"]["is_partial"] is False
        assert len(parsed["data"]) == 3

    def test_json_without_metadata(self, svc, sample_data):
        """include_metadata=False returns bare array."""
        result = svc.export(sample_data, format="json", include_metadata=False)
        parsed = json.loads(result)
        assert isinstance(parsed, list)

    def test_json_no_columns_infers_from_rows(self, svc):
        """Auto-infer column names when columns missing."""
        data = {"columns": [], "rows": [["a", 1]]}
        result = svc.export(data, format="json")
        parsed = json.loads(result)
        assert "col_0" in parsed[0]
        assert "col_1" in parsed[0]

    def test_json_empty_rows(self, svc):
        """Empty rows -> empty array."""
        data = {"columns": [{"name": "X"}], "rows": []}
        result = svc.export(data, format="json")
        parsed = json.loads(result)
        assert parsed == []

    def test_json_preserves_total_count(self, svc, sample_data):
        """Metadata total_count comes from data dict."""
        sample_data["total_count"] = 999
        result = svc.export(sample_data, format="json", include_metadata=True)
        parsed = json.loads(result)
        assert parsed["metadata"]["total_count"] == 999

    def test_json_preserves_is_partial(self, svc):
        """Metadata is_partial reflects data dict."""
        data = {
            "columns": [{"name": "a"}],
            "rows": [["x"]],
            "is_partial": True,
        }
        result = svc.export(data, format="json", include_metadata=True)
        parsed = json.loads(result)
        assert parsed["metadata"]["is_partial"] is True


# ---------------------------------------------------------------
# _materialize_row
# ---------------------------------------------------------------


class TestMaterializeRow:
    """Tests for row materialization."""

    def test_none_row(self, svc):
        assert svc._materialize_row(None) == []

    def test_callable_row(self, svc):
        assert svc._materialize_row(lambda: [1, 2, 3]) == [1, 2, 3]

    def test_iterable_row_list(self, svc):
        assert svc._materialize_row([1, 2]) == [1, 2]

    def test_iterable_row_tuple(self, svc):
        assert svc._materialize_row((1, 2)) == [1, 2]

    def test_dict_row(self, svc):
        assert svc._materialize_row({"a": 1, "b": 2}) == [1, 2]

    def test_string_row(self, svc):
        """String is not iterable-expanded; it's wrapped."""
        assert svc._materialize_row("hello") == ["hello"]

    def test_scalar_int(self, svc):
        assert svc._materialize_row(42) == [42]

    def test_scalar_float(self, svc):
        assert svc._materialize_row(3.14) == [3.14]


# ---------------------------------------------------------------
# Dispatch & DataFrame
# ---------------------------------------------------------------


class TestExportDispatch:
    """Tests for format dispatch."""

    def test_unsupported_format_raises(self, svc, sample_data):
        with pytest.raises(ValueError, match="Unsupported export format"):
            svc.export(sample_data, format="xml")


class TestToDataFrame:
    """Tests for DataFrame conversion."""

    def test_basic_dataframe(self, svc, sample_data):
        df = svc.to_dataframe(sample_data)
        assert len(df) == 3
        assert list(df.columns) == ["Severity", "Count"]

    def test_dataframe_no_columns(self, svc):
        data = {"columns": [], "rows": [["a", 1], ["b", 2]]}
        df = svc.to_dataframe(data)
        assert len(df) == 2
        assert "col_0" in df.columns

    def test_dataframe_empty(self, svc):
        data = {"columns": [{"name": "X"}], "rows": []}
        df = svc.to_dataframe(data)
        assert len(df) == 0
        assert list(df.columns) == ["X"]
