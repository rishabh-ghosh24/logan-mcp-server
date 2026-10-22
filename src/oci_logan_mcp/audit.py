# src/oci_logan_mcp/audit.py
"""Shared audit logger – appends JSON-lines with file-locking and rotation."""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List

from .file_lock import locked_file
from .sanitize import redact_dict

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
_MAX_BACKUPS = 5
_AUDIT_FILENAME = "audit.log"
_CLIENT_METADATA: ContextVar[dict | None] = ContextVar("mcp_audit_client", default=None)
_STRIP_KEYS = frozenset({
    "confirmation_token",
    "confirmation_secret",
    "confirmation_secret_confirm",
})
_TEXT_ARGUMENT_KEYS = frozenset({
    "query", "queries", "sample_logs", "markdown", "description", "notes", "regex_pattern",
})
_SUMMARY_KEYS = frozenset({
    "success", "status", "execution_ms", "error_type", "row_count", "column_count",
})


@contextmanager
def audit_client_context(metadata: dict | None) -> Iterator[None]:
    """Carry client claims into nested audit events for this call only."""
    token = _CLIENT_METADATA.set(metadata)
    try:
        yield
    finally:
        _CLIENT_METADATA.reset(token)


def _fingerprint(text: str) -> Dict[str, Any]:
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "length": len(text)}


def _omit_argument_text(value: Any, text_field: bool = False) -> Any:
    if isinstance(value, dict):
        return {key: _omit_argument_text(item, text_field or key.lower() in _TEXT_ARGUMENT_KEYS)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_omit_argument_text(item, text_field) for item in value]
    if isinstance(value, str) and (text_field or len(value) > 64):
        return _fingerprint(value)
    return value


class AuditLogger:
    """Append-only JSON-lines audit log with rotation and file locking."""

    def __init__(self, log_dir: Path, session_id: str = "unknown", store_arguments: bool = True) -> None:
        self._log_dir = log_dir
        self._session_id = session_id
        self._store_arguments = store_arguments
        self._log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._log_dir, 0o750)
        except OSError:
            pass
        self._log_path = self._log_dir / _AUDIT_FILENAME
        self._lock_path = self._log_dir / "audit.lock"
        self._thread_lock = threading.RLock()

    @property
    def session_id(self) -> str:
        """Public accessor for the session id stamped on every audit entry."""
        return self._session_id

    def log(
        self,
        user: str,
        tool: str,
        args: Dict[str, Any],
        outcome: str,
        result_summary: Any = "",
        error: str = "",
        trace_id: str | None = None,
        audit_ref: str | None = None,
        blocked: bool | None = None,
        block_reason: str | None = None,
        audit_strictness: str | None = None,
        event_type: str = "mcp_call",
        actor_source: str = "logan_user",
        client_metadata: Dict[str, Any] | None = None,
        error_type: str | None = None,
    ) -> None:
        """Append one audit entry as a JSON line."""
        clean_args = {k: v for k, v in args.items() if k not in _STRIP_KEYS}
        redacted_args = redact_dict(clean_args)
        sample_digest = clean_args.get("sample_sha256")
        if isinstance(sample_digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sample_digest):
            redacted_args["sample_sha256"] = sample_digest
        if not self._store_arguments:
            redacted_args = _omit_argument_text(redacted_args)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if blocked is None:
            blocked = outcome in {
                "read_only_blocked",
                "confirmation_unavailable",
                "confirmation_failed",
                "unknown_tool",
                "audit_blocked",
            }
        entry: Dict[str, Any] = {
            # Legacy fields consumed by playbook/transcript code.
            "timestamp": timestamp,
            "session_id": self._session_id,
            "user": user,
            "pid": os.getpid(),
            "tool": tool,
            "outcome": outcome,
            # Enriched MCP audit fields used by Argus POC audit review.
            "ts": timestamp,
            "event_id": f"evt_{uuid.uuid4().hex}",
            "trace_id": trace_id or f"trace_{uuid.uuid4().hex}",
            "audit_ref": audit_ref,
            "actor": user,
            "actor_source": actor_source,
            "client_metadata": client_metadata or _CLIENT_METADATA.get() or {
                "claimed_client": "unknown",
                "claimed_version": "unknown",
                "source_ip_truncated": None,
            },
            "event_type": event_type,
            "args_redacted": redacted_args,
            "blocked": blocked,
            "block_reason": block_reason,
            "audit_strictness": audit_strictness or "best_effort",
        }
        if result_summary != "":
            if not self._store_arguments:
                result_summary = {
                    key: value for key, value in (result_summary.items() if isinstance(result_summary, dict) else [])
                    if key in _SUMMARY_KEYS and isinstance(value, (bool, int, float))
                }
            entry["result_summary"] = result_summary
        if error or error_type:
            if not self._store_arguments:
                entry["error"] = {
                    "error_type": error_type or "unknown", **_fingerprint(error),
                }
            elif error:
                entry["error"] = error
        if not self._store_arguments:
            entry["text_retained"] = False

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

    def iter_entries(self, session_id: str | None = None) -> Iterator[Dict[str, Any]]:
        """Return audit entries from current and rotated logs, oldest first."""
        entries: List[Dict[str, Any]] = []
        with self._thread_lock:
            for src in self._transcript_source_files():
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
                            if (
                                session_id is not None
                                and entry.get("session_id") != session_id
                            ):
                                continue
                            entries.append(entry)
                except FileNotFoundError:
                    continue
        return iter(entries)

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
