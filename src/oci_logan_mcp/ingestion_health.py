"""ingestion_health — freshness/stoppage detection for log sources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string to a UTC-aware datetime.

    Accepts trailing `Z` (RFC 3339). Returns None on any parse failure so the
    classifier can treat missing/malformed timestamps as `unknown` without
    raising into the handler.
    """
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _classify(
    last_log_ts: Optional[datetime],
    checked_at: datetime,
    threshold_s: int,
) -> Tuple[str, str, Optional[int], str]:
    """Classify a source as healthy/stopped/unknown based on last-seen age.

    Returns (status, severity, age_seconds, message).
    """
    if last_log_ts is None:
        return (
            "unknown",
            "warn",
            None,
            "No records in freshness probe window.",
        )
    age = int((checked_at - last_log_ts).total_seconds())
    if age >= threshold_s:
        return (
            "stopped",
            "critical",
            age,
            f"Ingestion stopped — last record {age}s ago (threshold {threshold_s}s).",
        )
    return (
        "healthy",
        "info",
        age,
        f"Healthy — last record {age}s ago.",
    )


class IngestionHealthTool:
    """Probe log-source freshness and classify stoppages."""

    def __init__(self, query_engine, schema_manager, settings):
        self._engine = query_engine
        self._schema = schema_manager
        self._settings = settings

    async def run(
        self,
        compartment_id: Optional[str] = None,
        sources: Optional[List[str]] = None,
        severity_filter: str = "warn",
    ) -> Dict[str, Any]:
        raise NotImplementedError  # wired in Task 3
