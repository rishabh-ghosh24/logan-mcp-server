"""Time range parsing utilities."""

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple


# Mapping of relative time range names to timedelta
TIME_RANGES = {
    "last_15_min": timedelta(minutes=15),
    "last_30_min": timedelta(minutes=30),
    "last_1_hour": timedelta(hours=1),
    "last_3_hours": timedelta(hours=3),
    "last_6_hours": timedelta(hours=6),
    "last_12_hours": timedelta(hours=12),
    "last_24_hours": timedelta(hours=24),
    "last_2_days": timedelta(days=2),
    "last_7_days": timedelta(days=7),
    "last_14_days": timedelta(days=14),
    "last_30_days": timedelta(days=30),
}


def parse_time_range(
    time_start: Optional[str] = None,
    time_end: Optional[str] = None,
    time_range: Optional[str] = None,
    default_range: str = "last_1_hour",
) -> Tuple[datetime, datetime]:
    """Parse time range parameters into datetime objects.

    Args:
        time_start: Absolute start time (ISO 8601 format).
        time_end: Absolute end time (ISO 8601 format).
        time_range: Relative time range name (e.g., 'last_1_hour').
        default_range: Default relative range if no parameters provided.

    Returns:
        Tuple of (start_datetime, end_datetime) in UTC.

    Raises:
        ValueError: If invalid time parameters provided.
    """
    # Use current time as end time reference
    now = datetime.now(timezone.utc)

    # If absolute times provided, use them
    if time_start and time_end:
        start = _parse_datetime(time_start)
        end = _parse_datetime(time_end)
        return start, end

    # If only start time provided, use now as end
    if time_start:
        start = _parse_datetime(time_start)
        return start, now

    # If only end time provided, use default range before it
    if time_end:
        end = _parse_datetime(time_end)
        delta = TIME_RANGES.get(default_range, timedelta(hours=1))
        return end - delta, end

    # Use relative time range
    range_name = time_range or default_range
    delta = TIME_RANGES.get(range_name)

    if delta is None:
        raise ValueError(
            f"Invalid time range: {range_name}. "
            f"Valid options: {', '.join(TIME_RANGES.keys())}"
        )

    return now - delta, now


def _parse_datetime(time_str: str) -> datetime:
    """Parse a datetime string to datetime object.

    Supports ISO 8601 format with optional timezone.

    Args:
        time_str: Time string to parse.

    Returns:
        Datetime object (with timezone info).

    Raises:
        ValueError: If time string cannot be parsed.
    """
    # Handle Z suffix
    time_str = time_str.replace("Z", "+00:00")

    try:
        # Try ISO format with timezone
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # Try common formats
    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(time_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    raise ValueError(f"Cannot parse time string: {time_str}")


def format_time_range(start: datetime, end: datetime) -> str:
    """Format a time range for display.

    Args:
        start: Start datetime.
        end: End datetime.

    Returns:
        Human-readable time range string.
    """
    delta = end - start

    if delta.days > 0:
        return f"{delta.days} day(s)"
    elif delta.seconds >= 3600:
        hours = delta.seconds // 3600
        return f"{hours} hour(s)"
    elif delta.seconds >= 60:
        minutes = delta.seconds // 60
        return f"{minutes} minute(s)"
    else:
        return f"{delta.seconds} second(s)"


def get_time_range_options() -> dict:
    """Get available time range options.

    Returns:
        Dictionary of time range name to description.
    """
    return {
        "last_15_min": "Last 15 minutes",
        "last_30_min": "Last 30 minutes",
        "last_1_hour": "Last 1 hour",
        "last_3_hours": "Last 3 hours",
        "last_6_hours": "Last 6 hours",
        "last_12_hours": "Last 12 hours",
        "last_24_hours": "Last 24 hours",
        "last_2_days": "Last 2 days",
        "last_7_days": "Last 7 days",
        "last_14_days": "Last 14 days",
        "last_30_days": "Last 30 days",
    }
