"""ingestion_health — freshness/stoppage detection for log sources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _utcnow() -> datetime:
    """Seam for tests — monkeypatch this in unit tests to freeze time."""
    return datetime.now(timezone.utc)


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
    """Classify a source as healthy/stopped/unknown based on last-seen age."""
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


_SEVERITY_RANK = {"info": 0, "warn": 1, "critical": 2}


def _passes_severity_filter(severity: str, filter_level: str) -> bool:
    if filter_level == "all":
        return True
    required = _SEVERITY_RANK.get(filter_level, 1)  # default: warn
    return _SEVERITY_RANK.get(severity, 0) >= required


def _compose_probe_query(sources: Optional[List[str]]) -> str:
    """Build the `max(Time) by 'Log Source'` probe query, optionally filtered."""
    base = "* | stats max('Time') as last_log_ts by 'Log Source'"
    if not sources:
        return base
    escaped = ", ".join(f"'{s}'" for s in sources)
    return f"'Log Source' in ({escaped}) | stats max('Time') as last_log_ts by 'Log Source'"


def _extract_last_seen_map(response: Dict[str, Any]) -> Dict[str, Optional[datetime]]:
    """Map source name → parsed last_log_ts from a probe response."""
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", [])]
    rows = data.get("rows", [])
    if "Log Source" not in columns or "last_log_ts" not in columns:
        return {}
    src_idx = columns.index("Log Source")
    ts_idx = columns.index("last_log_ts")
    out: Dict[str, Optional[datetime]] = {}
    for row in rows:
        if not row:
            continue
        name = str(row[src_idx])
        out[name] = _parse_ts(row[ts_idx] if ts_idx < len(row) else None)
    return out


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
        ih_cfg = self._settings.ingestion_health
        checked_at = _utcnow()

        # 1. Target set: caller-provided or enumerated via schema_manager.
        if sources is None:
            discovered = await self._schema.get_log_sources(
                compartment_id=compartment_id
            )
            target_sources = [s.get("name") for s in discovered if s.get("name")]
        else:
            target_sources = list(sources)

        # 2. Run the probe query.
        query = _compose_probe_query(sources)
        response = await self._engine.execute(
            query=query,
            time_range=ih_cfg.freshness_probe_window,
            compartment_id=compartment_id,
        )
        last_seen = _extract_last_seen_map(response)

        # 3. Classify every target source.
        findings_all: List[Dict[str, Any]] = []
        summary = {"sources_healthy": 0, "sources_stopped": 0, "sources_unknown": 0}
        for name in target_sources:
            last_dt = last_seen.get(name)
            status, severity, age, message = _classify(
                last_dt, checked_at, ih_cfg.stoppage_threshold_seconds
            )
            summary[f"sources_{status}"] += 1
            findings_all.append({
                "source": name,
                "status": status,
                "last_log_ts": last_dt.isoformat() if last_dt else None,
                "age_seconds": age,
                "severity": severity,
                "message": message,
            })

        # 4. Apply severity filter to findings (summary counts stay global).
        findings = [
            f for f in findings_all
            if _passes_severity_filter(f["severity"], severity_filter)
        ]

        return {
            "summary": summary,
            "checked_at": checked_at.isoformat(),
            "findings": findings,
            "metadata": {
                "probe_query": query,
                "freshness_probe_window": ih_cfg.freshness_probe_window,
                "stoppage_threshold_seconds": ih_cfg.stoppage_threshold_seconds,
                "severity_filter": severity_filter,
                "sources_queried": target_sources,
            },
        }
