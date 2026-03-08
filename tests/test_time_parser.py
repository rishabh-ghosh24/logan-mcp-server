"""Tests for time parser module."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from oci_logan_mcp.time_parser import (
    parse_time_range,
    _parse_datetime,
    format_time_range,
    get_time_range_options,
    TIME_RANGES,
)


# Fixed reference time for deterministic tests
FIXED_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------
# parse_time_range
# ---------------------------------------------------------------


class TestParseTimeRange:
    """Tests for parse_time_range function."""

    def test_both_absolute_times(self):
        """Both start and end provided -> use them directly."""
        start, end = parse_time_range(
            time_start="2025-01-01T00:00:00Z",
            time_end="2025-01-02T00:00:00Z",
        )
        assert start == datetime(2025, 1, 1, tzinfo=timezone.utc)
        assert end == datetime(2025, 1, 2, tzinfo=timezone.utc)

    @patch("oci_logan_mcp.time_parser.datetime")
    def test_start_only_uses_now_as_end(self, mock_dt):
        """Start only -> end = now."""
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.strptime = datetime.strptime

        start, end = parse_time_range(time_start="2025-06-15T10:00:00Z")
        assert start == datetime(2025, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        assert end == FIXED_NOW

    @patch("oci_logan_mcp.time_parser.datetime")
    def test_end_only_uses_default_range_before(self, mock_dt):
        """End only -> start = end - default_range."""
        mock_dt.now.return_value = FIXED_NOW
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.strptime = datetime.strptime

        start, end = parse_time_range(time_end="2025-06-15T12:00:00Z")
        assert end == datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert start == end - timedelta(hours=1)  # default_range = last_1_hour

    @patch("oci_logan_mcp.time_parser.datetime")
    def test_relative_range_last_1_hour(self, mock_dt):
        """Relative range: last_1_hour."""
        mock_dt.now.return_value = FIXED_NOW
        start, end = parse_time_range(time_range="last_1_hour")
        assert end == FIXED_NOW
        assert start == FIXED_NOW - timedelta(hours=1)

    @patch("oci_logan_mcp.time_parser.datetime")
    def test_relative_range_last_7_days(self, mock_dt):
        """Relative range: last_7_days."""
        mock_dt.now.return_value = FIXED_NOW
        start, end = parse_time_range(time_range="last_7_days")
        assert end == FIXED_NOW
        assert start == FIXED_NOW - timedelta(days=7)

    @patch("oci_logan_mcp.time_parser.datetime")
    def test_default_range_when_nothing_provided(self, mock_dt):
        """No args -> uses default_range (last_1_hour)."""
        mock_dt.now.return_value = FIXED_NOW
        start, end = parse_time_range()
        assert end == FIXED_NOW
        assert start == FIXED_NOW - timedelta(hours=1)

    @patch("oci_logan_mcp.time_parser.datetime")
    def test_custom_default_range(self, mock_dt):
        """Custom default_range parameter."""
        mock_dt.now.return_value = FIXED_NOW
        start, end = parse_time_range(default_range="last_24_hours")
        assert start == FIXED_NOW - timedelta(hours=24)

    def test_invalid_range_raises_valueerror(self):
        """Invalid range name -> ValueError."""
        with pytest.raises(ValueError, match="Invalid time range"):
            parse_time_range(time_range="last_99_years")

    def test_invalid_range_error_lists_valid_options(self):
        """Error message lists valid options."""
        with pytest.raises(ValueError) as exc_info:
            parse_time_range(time_range="bad_range")
        assert "last_1_hour" in str(exc_info.value)
        assert "last_7_days" in str(exc_info.value)


# ---------------------------------------------------------------
# _parse_datetime
# ---------------------------------------------------------------


class TestParseDatetime:
    """Tests for _parse_datetime helper."""

    def test_iso_with_timezone(self):
        """ISO 8601 with timezone offset."""
        dt = _parse_datetime("2025-01-15T10:30:00+05:30")
        assert dt.tzinfo is not None

    def test_iso_with_z_suffix(self):
        """Z suffix -> UTC."""
        dt = _parse_datetime("2025-01-15T10:30:00Z")
        assert dt.tzinfo is not None
        assert dt == datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)

    def test_naive_gets_utc(self):
        """Naive ISO datetime -> gets UTC timezone."""
        dt = _parse_datetime("2025-01-15T10:30:00")
        assert dt.tzinfo == timezone.utc

    def test_space_separated_format(self):
        """Space-separated datetime format."""
        dt = _parse_datetime("2025-01-15 10:30:00")
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 10

    def test_date_only_format(self):
        """Date-only format."""
        dt = _parse_datetime("2025-01-15")
        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 15
        assert dt.tzinfo == timezone.utc

    def test_unparseable_raises_valueerror(self):
        """Unparseable string -> ValueError."""
        with pytest.raises(ValueError, match="Cannot parse time string"):
            _parse_datetime("not-a-date")


# ---------------------------------------------------------------
# format_time_range
# ---------------------------------------------------------------


class TestFormatTimeRange:
    """Tests for format_time_range function."""

    def test_format_days(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 1, 4, tzinfo=timezone.utc)
        assert format_time_range(start, end) == "3 day(s)"

    def test_format_hours(self):
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 1, 1, 3, 0, 0, tzinfo=timezone.utc)
        assert format_time_range(start, end) == "3 hour(s)"

    def test_format_minutes(self):
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 1, 1, 0, 45, 0, tzinfo=timezone.utc)
        assert format_time_range(start, end) == "45 minute(s)"

    def test_format_seconds(self):
        start = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2025, 1, 1, 0, 0, 30, tzinfo=timezone.utc)
        assert format_time_range(start, end) == "30 second(s)"


# ---------------------------------------------------------------
# get_time_range_options
# ---------------------------------------------------------------


class TestGetTimeRangeOptions:
    """Tests for get_time_range_options function."""

    def test_returns_all_options(self):
        options = get_time_range_options()
        assert len(options) == 11
        assert "last_15_min" in options
        assert "last_30_days" in options

    def test_keys_match_time_ranges_dict(self):
        options = get_time_range_options()
        assert set(options.keys()) == set(TIME_RANGES.keys())
