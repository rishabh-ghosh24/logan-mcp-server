# src/oci_logan_mcp/audit.py
"""Shared audit logger – appends JSON-lines with file-locking and rotation."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .file_lock import locked_file

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
_MAX_BACKUPS = 5
_AUDIT_FILENAME = "audit.log"
_STRIP_KEYS = frozenset({
    "confirmation_token",
    "confirmation_secret",
    "confirmation_secret_confirm",
})


class AuditLogger:
    """Append-only JSON-lines audit log with rotation and file locking."""

    def __init__(self, log_dir: Path, session_id: str = "unknown") -> None:
        self._log_dir = log_dir
        self._session_id = session_id
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
            "session_id": self._session_id,
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

    def export_transcript(
        self,
        session_id: str,
        out_dir: Path,
        include_results: bool = True,
        redact: bool = False,
    ) -> Dict[str, Any]:
        """Write matching audit entries to a timestamped JSONL file.

        Returns {path: str, event_count: int}.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"transcript-{session_id}-{timestamp}.jsonl"

        if redact:
            from .sanitize import redact_dict as _redact
        else:
            _redact = None

        count = 0
        with self._thread_lock:
            candidates = self._transcript_source_files()
            with open(out_path, "w", encoding="utf-8") as out:
                for src in candidates:
                    try:
                        with open(src, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    entry = json.loads(line)
                                except Exception:
                                    continue
                                if entry.get("session_id") != session_id:
                                    continue
                                if not include_results:
                                    entry.pop("result_summary", None)
                                if _redact is not None:
                                    entry = _redact(entry)
                                out.write(json.dumps(entry, separators=(",", ":")) + "\n")
                                count += 1
                    except FileNotFoundError:
                        continue
        return {"path": str(out_path), "event_count": count}

    def _transcript_source_files(self) -> List[Path]:
        """Return current log plus rotated backups, oldest first."""
        files = []
        if self._log_path.is_file():
            files.append(self._log_path)
        for i in range(1, _MAX_BACKUPS + 1):
            p = self._log_dir / f"{_AUDIT_FILENAME}.{i}"
            if p.is_file():
                files.append(p)
        files.reverse()  # oldest first
        return files
