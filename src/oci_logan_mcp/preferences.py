# src/oci_logan_mcp/preferences.py
"""Structured preference learning — per-user disambiguation, field affinity, time defaults."""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .file_lock import atomic_yaml_read, atomic_yaml_write, locked_file


class PreferenceStore:
    """Per-user preference storage with structured learning."""

    def __init__(self, user_dir: Path) -> None:
        self._path = user_dir / "preferences.yaml"
        self._lock_path = user_dir / "preferences.lock"
        self._thread_lock = threading.RLock()

    def remember(self, intent_key: str, resolved_value: str, confidence: float = 0.9) -> None:
        """Save or update a disambiguation preference."""
        now = datetime.now(timezone.utc).isoformat()
        with locked_file(self._lock_path, self._thread_lock):
            data = self._load()
            prefs = data.setdefault("preferences", [])
            for p in prefs:
                if p.get("intent_key") == intent_key:
                    p["resolved_value"] = resolved_value
                    p["confidence"] = confidence
                    p["usage_count"] = p.get("usage_count", 0) + 1
                    p["last_used"] = now
                    self._save(data)
                    return
            prefs.append({
                "intent_key": intent_key,
                "resolved_value": resolved_value,
                "confidence": confidence,
                "usage_count": 1,
                "last_used": now,
            })
            self._save(data)

    def get(self, intent_key: str) -> Optional[Dict[str, Any]]:
        """Look up a preference by intent key."""
        data = self._load()
        for p in data.get("preferences", []):
            if p.get("intent_key") == intent_key:
                return deepcopy(p)
        return None

    def list_all(self) -> List[Dict[str, Any]]:
        """Return all preferences."""
        data = self._load()
        return deepcopy(data.get("preferences", []))

    def track_field_usage(self, log_source: str, field_name: str) -> None:
        """Track which fields are used with which log sources."""
        with locked_file(self._lock_path, self._thread_lock):
            data = self._load()
            affinity = data.setdefault("field_affinity", {})
            source_fields = affinity.setdefault(log_source, {})
            source_fields[field_name] = source_fields.get(field_name, 0) + 1
            self._save(data)

    def get_common_fields(self, log_source: str, limit: int = 10) -> List[str]:
        """Get most commonly used fields for a log source, sorted by frequency."""
        data = self._load()
        source_fields = data.get("field_affinity", {}).get(log_source, {})
        sorted_fields = sorted(source_fields.items(), key=lambda x: x[1], reverse=True)
        return [f[0] for f in sorted_fields[:limit]]

    def track_time_range(self, log_source: str, time_range: str) -> None:
        """Track commonly used time ranges per log source."""
        with locked_file(self._lock_path, self._thread_lock):
            data = self._load()
            ranges = data.setdefault("time_ranges", {})
            source_ranges = ranges.setdefault(log_source, {})
            source_ranges[time_range] = source_ranges.get(time_range, 0) + 1
            self._save(data)

    def suggest_time_range(self, log_source: str) -> Optional[str]:
        """Suggest the most common time range for a log source."""
        data = self._load()
        source_ranges = data.get("time_ranges", {}).get(log_source, {})
        if not source_ranges:
            return None
        return max(source_ranges, key=source_ranges.get)

    def _load(self) -> Dict[str, Any]:
        return atomic_yaml_read(self._path, default={})

    def _save(self, data: Dict[str, Any]) -> None:
        atomic_yaml_write(self._path, data)
