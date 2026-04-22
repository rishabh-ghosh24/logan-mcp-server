"""why_did_this_fire (A6) — historical replay for Logan-managed alarms."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from .ingestion_health import _parse_ts
from .investigate import _extract_seed_filter

_PENDING_DURATION_RE = re.compile(
    r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def _parse_pending_duration_seconds(value: Optional[str]) -> Optional[int]:
    """Parse OCI Monitoring pending_duration (ISO-8601) into seconds."""
    if not value or not isinstance(value, str):
        return None
    match = _PENDING_DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total or None


def _coerce_fire_time(fire_time: Union[str, datetime]) -> datetime:
    """Normalize fire_time into a UTC-aware datetime."""
    if isinstance(fire_time, datetime):
        dt = fire_time
    elif isinstance(fire_time, str):
        raw = fire_time.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("fire_time must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError("fire_time must be an ISO-8601 timestamp")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_top_contributing_rows(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize raw-row query output into stable postmortem rows."""
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", []) or []]
    rows = data.get("rows", []) or []
    required = {"Time", "Log Source", "Original Log Content"}
    if not required <= set(columns):
        return []

    time_idx = columns.index("Time")
    source_idx = columns.index("Log Source")
    severity_idx = columns.index("Severity") if "Severity" in columns else None
    message_idx = columns.index("Original Log Content")
    max_idx = max(
        idx for idx in (time_idx, source_idx, severity_idx, message_idx) if idx is not None
    )

    normalized: List[Dict[str, Any]] = []
    for row in rows:
        if not row or len(row) <= max_idx:
            continue
        ts = _parse_ts(row[time_idx])
        if ts is None:
            continue
        severity = row[severity_idx] if severity_idx is not None else None
        normalized.append({
            "time": ts.isoformat(),
            "source": str(row[source_idx]) if row[source_idx] is not None else "",
            "severity": str(severity) if severity is not None else None,
            "message": str(row[message_idx]) if row[message_idx] is not None else "",
        })
    return normalized


class WhyDidThisFireTool:
    """Replay Logan-managed alarm context around a historical fire time."""

    def __init__(self, oci_client, query_engine):
        self._client = oci_client
        self._engine = query_engine

    @staticmethod
    def _error(error_code: str, error: str, *, alarm_ocid: str, **extra: Any) -> Dict[str, Any]:
        payload = {
            "status": "error",
            "error_code": error_code,
            "error": error,
            "alarm_ocid": alarm_ocid,
        }
        payload.update(extra)
        return payload

    async def run(
        self,
        alarm_ocid: str,
        fire_time: Union[str, datetime],
        window_before_seconds: Optional[int] = None,
        window_after_seconds: int = 60,
    ) -> Dict[str, Any]:
        alarm = await self._client.get_alarm(alarm_ocid)
        tags = alarm.get("freeform_tags", {}) or {}

        if tags.get("logan_managed") != "true":
            return self._error(
                "alarm_not_logan_managed",
                "Alarm is not Logan-managed.",
                alarm_ocid=alarm_ocid,
                observed_logan_managed=tags.get("logan_managed"),
            )
        if tags.get("logan_kind") != "monitoring_alarm":
            return self._error(
                "alarm_kind_mismatch",
                "Alarm is Logan-managed but is not a monitoring alarm.",
                alarm_ocid=alarm_ocid,
                observed_logan_kind=tags.get("logan_kind"),
            )

        stored_query = tags.get("logan_query")
        if not stored_query:
            return self._error(
                "alarm_missing_query_metadata",
                "Alarm is missing stored Logan query metadata.",
                alarm_ocid=alarm_ocid,
            )

        fire_dt = _coerce_fire_time(fire_time)
        parsed_pending_duration = _parse_pending_duration_seconds(alarm.get("pending_duration"))
        effective_before_seconds = (
            window_before_seconds
            if window_before_seconds is not None
            else (parsed_pending_duration or 300)
        )
        window_start = fire_dt - timedelta(seconds=effective_before_seconds)
        window_end = fire_dt + timedelta(seconds=window_after_seconds)
        compartment_id = alarm.get("compartment_id")

        trigger_query_result = await self._engine.execute(
            query=stored_query,
            time_start=window_start.isoformat(),
            time_end=window_end.isoformat(),
            compartment_id=compartment_id,
        )

        seed_filter = _extract_seed_filter(stored_query)
        seed_filter_degraded = seed_filter == "*"

        top_contributing_rows: List[Dict[str, Any]] = []
        top_contributing_rows_omitted_reason = None
        if seed_filter_degraded:
            top_contributing_rows_omitted_reason = "unscoped_seed_filter"
        else:
            rows_query = (
                f"({seed_filter}) | fields Time, 'Log Source', Severity, "
                f"'Original Log Content' | sort -Time | head 50"
            )
            rows_result = await self._engine.execute(
                query=rows_query,
                time_start=window_start.isoformat(),
                time_end=window_end.isoformat(),
                compartment_id=compartment_id,
            )
            top_contributing_rows = _normalize_top_contributing_rows(rows_result)

        result = {
            "alarm": {
                "alarm_id": alarm.get("id"),
                "display_name": alarm.get("display_name"),
                "severity": alarm.get("severity"),
                "is_enabled": alarm.get("is_enabled"),
                "mql_query": alarm.get("query"),
                "stored_logan_query": stored_query,
                "stored_schedule": tags.get("logan_schedule"),
                "backing_saved_search_id": tags.get("logan_backing_saved_search_id"),
                "backing_metric_task_id": tags.get("logan_backing_metric_task_id"),
                "compartment_id": compartment_id,
            },
            "evaluation": {
                "pending_duration": alarm.get("pending_duration"),
                "pending_duration_seconds": parsed_pending_duration,
                "schedule_cron": tags.get("logan_schedule"),
                "mql_query": alarm.get("query"),
            },
            "window": {
                "time_start": window_start.isoformat(),
                "time_end": window_end.isoformat(),
                "window_before_seconds": effective_before_seconds,
                "window_after_seconds": window_after_seconds,
            },
            "seed": {
                "query": stored_query,
                "seed_filter": seed_filter,
                "seed_filter_degraded": seed_filter_degraded,
            },
            "trigger_query_result": trigger_query_result,
            "top_contributing_rows": top_contributing_rows,
            "related_saved_search_id": tags.get("logan_backing_saved_search_id"),
            "dashboard_id": None,
        }
        if top_contributing_rows_omitted_reason is not None:
            result["top_contributing_rows_omitted_reason"] = top_contributing_rows_omitted_reason
        return result
