"""Private query history and in-memory recent queries."""

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from .config import LoggingConfig
from .file_lock import locked_file

_HISTORY_MAX_BYTES = 10 * 1024 * 1024
_HISTORY_BACKUPS = 5


class QueryLogger:
    """Keep private query history.

    Maintains a log file with query history and provides
    access to recent queries for the MCP resource.
    """

    def __init__(self, config: Optional[LoggingConfig] = None, user_dir: Optional[Path] = None):
        """Initialize query logger."""
        self.config = config or LoggingConfig()
        self._enabled = self.config.query_logging
        self._recent_queries: List[Dict[str, Any]] = []
        self._max_recent = 100

        if self._enabled:
            self._setup_history(user_dir)

    @staticmethod
    def _ensure_private_file(path: Path) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.chmod(path, 0o600)
        finally:
            os.close(descriptor)

    def _setup_history(self, user_dir: Optional[Path]) -> None:
        """Use an independent private sink, with no logging propagation."""
        directory = user_dir / "history" if user_dir is not None else Path(self.config.log_path)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._history_path = directory / "queries.log"
        self._lock_path = directory / "history.lock"
        self._thread_lock = threading.RLock()
        self._ensure_private_file(self._lock_path)
        with locked_file(self._lock_path, self._thread_lock):
            self._ensure_private_file(self._history_path)

    def _append_history(self, line: str) -> None:
        with locked_file(self._lock_path, self._thread_lock):
            if self._history_path.stat().st_size >= _HISTORY_MAX_BYTES:
                for index in range(_HISTORY_BACKUPS - 1, 0, -1):
                    source = self._history_path.with_name(f"queries.log.{index}")
                    if source.exists():
                        os.replace(source, self._history_path.with_name(f"queries.log.{index + 1}"))
                os.replace(self._history_path, self._history_path.with_name("queries.log.1"))
            descriptor = os.open(self._history_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as output:
                os.chmod(self._history_path, 0o600)
                output.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {line}\n")

    def log_query(
        self,
        query: str,
        time_start: datetime,
        time_end: datetime,
        execution_time: float,
        result_count: int,
        success: bool,
        error: Optional[str] = None,
        compartment_id: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> None:
        """Log a query execution."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "query": query,
            "time_range": {
                "start": time_start.isoformat(),
                "end": time_end.isoformat(),
            },
            "execution_time_seconds": round(execution_time, 3),
            "result_count": result_count,
            "success": success,
        }

        if error:
            entry["error"] = error
        if compartment_id:
            entry["compartment_id"] = compartment_id
        if namespace:
            entry["namespace"] = namespace

        self._recent_queries.insert(0, entry)
        if len(self._recent_queries) > self._max_recent:
            self._recent_queries = self._recent_queries[: self._max_recent]

        if self._enabled:
            log_line = self._format_log_line(entry)
            self._append_history(log_line)

    def get_recent_queries(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent successful queries."""
        successful = [q for q in self._recent_queries if q.get("success", False)]
        return successful[:limit]

    def get_all_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get all recent queries including failures."""
        return self._recent_queries[:limit]

    def get_stats(self) -> Dict[str, Any]:
        """Get query statistics."""
        total = len(self._recent_queries)
        successful = sum(1 for q in self._recent_queries if q.get("success", False))
        failed = total - successful

        if total > 0:
            avg_time = sum(
                q.get("execution_time_seconds", 0) for q in self._recent_queries
            ) / total
        else:
            avg_time = 0

        return {
            "total_queries": total,
            "successful": successful,
            "failed": failed,
            "success_rate": round(successful / total * 100, 1) if total > 0 else 0,
            "avg_execution_time_seconds": round(avg_time, 3),
        }

    def _format_log_line(self, entry: Dict[str, Any]) -> str:
        """Format a log entry for file output."""
        status = "SUCCESS" if entry["success"] else "FAILED"
        query_preview = entry["query"][:100] + "..." if len(entry["query"]) > 100 else entry["query"]
        query_preview = query_preview.replace("\n", " ")

        parts = [
            status,
            f"{entry['execution_time_seconds']:.3f}s",
            f"{entry['result_count']} results",
            f"Query: {query_preview}",
        ]

        if entry.get("error"):
            parts.append(f"Error: {entry['error'][:200]}")

        return " | ".join(parts)
