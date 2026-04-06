# src/oci_logan_mcp/audit.py
"""Shared audit logger – appends JSON-lines with file-locking and rotation."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .file_lock import locked_file

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
_MAX_BACKUPS = 5
_AUDIT_FILENAME = "audit.log"
_STRIP_KEYS = frozenset({"confirmation_token", "confirmation_secret"})


class AuditLogger:
    """Append-only JSON-lines audit log with rotation and file locking."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._log_dir, 0o750)
        except OSError:
            pass
        self._log_path = self._log_dir / _AUDIT_FILENAME
        self._lock_path = self._log_dir / "audit.lock"
        self._thread_lock = threading.RLock()

    def log(
        self,
        user: str,
        tool: str,
        args: Dict[str, Any],
        outcome: str,
        result_summary: str = "",
        error: str = "",
    ) -> None:
        """Append one audit entry as a JSON line."""
        clean_args = {k: v for k, v in args.items() if k not in _STRIP_KEYS}
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user": user,
            "pid": os.getpid(),
            "tool": tool,
            "args": clean_args,
            "outcome": outcome,
        }
        if result_summary:
            entry["result_summary"] = result_summary
        if error:
            entry["error"] = error

        line = json.dumps(entry, separators=(",", ":")) + "\n"

        with locked_file(self._lock_path, self._thread_lock):
            self._rotate_if_needed()
            new_file = not self._log_path.is_file()
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            if new_file:
                try:
                    os.chmod(self._log_path, 0o640)
                except OSError:
                    pass

    def _rotate_if_needed(self) -> None:
        """Rotate audit.log when it reaches the size threshold."""
        if not self._log_path.is_file():
            return
        try:
            if self._log_path.stat().st_size < _MAX_FILE_SIZE:
                return
        except OSError:
            return

        # Shift existing backups: .5 is deleted, .4->.5, .3->.4, ...
        for i in range(_MAX_BACKUPS, 0, -1):
            src = self._log_dir / f"{_AUDIT_FILENAME}.{i}"
            dst = self._log_dir / f"{_AUDIT_FILENAME}.{i + 1}"
            if src.is_file():
                if i == _MAX_BACKUPS:
                    src.unlink()
                else:
                    src.rename(dst)

        # Current log becomes .1
        self._log_path.rename(self._log_dir / f"{_AUDIT_FILENAME}.1")
