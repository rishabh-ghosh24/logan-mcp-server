"""Record N6 audit events into N1 investigation playbooks."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .audit import AuditLogger
from .playbook_store import PlaybookStore


def _audit_precision(value: datetime) -> datetime:
    """Match AuditLogger's whole-second timestamp precision."""
    return value.astimezone(timezone.utc).replace(microsecond=0)


_PROCESS_STARTED_AT = _audit_precision(datetime.now(timezone.utc))


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _audit_precision(parsed)


def _iso(value: datetime) -> str:
    return _audit_precision(value).isoformat()


class PlaybookRecorder:
    """Build and persist playbooks from current-process audit events."""

    def __init__(self, audit_logger: AuditLogger, store: PlaybookStore, owner: str) -> None:
        self._audit_logger = audit_logger
        self._store = store
        self._owner = owner

    def record(
        self,
        name: str,
        description: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> Dict[str, Any]:
        since_dt = _parse_iso_datetime(since) if since else _PROCESS_STARTED_AT
        until_dt = _parse_iso_datetime(until) if until else datetime.now(timezone.utc)
        if since_dt > until_dt:
            raise ValueError("since must be before until")

        steps = []
        for entry in self._audit_logger.iter_entries(session_id=self._audit_logger.session_id):
            if entry.get("tool") == "record_investigation":
                continue
            ts_raw = entry.get("timestamp")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = _parse_iso_datetime(ts_raw)
            except ValueError:
                continue
            if ts < since_dt or ts > until_dt:
                continue
            steps.append(
                {
                    "tool": entry.get("tool", ""),
                    "args": entry.get("args", {}),
                    "ts": _iso(ts),
                    "outcome": entry.get("outcome", ""),
                }
            )
        steps.sort(key=lambda step: step["ts"])

        warning = ""
        if not steps:
            warning = "No audit events matched the requested time window."

        playbook = {
            "id": f"pb_{uuid.uuid4().hex}",
            "name": name,
            "description": description or "",
            "owner": self._owner,
            "created_at": _iso(datetime.now(timezone.utc)),
            "steps": steps,
            "source_process_session_id": self._audit_logger.session_id,
            "window": {"since": _iso(since_dt), "until": _iso(until_dt)},
            "warning": warning,
        }
        return self._store.save(playbook)
