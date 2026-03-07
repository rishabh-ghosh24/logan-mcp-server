"""Query audit logging for debugging and compliance."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from logging.handlers import RotatingFileHandler

from .config import LoggingConfig


class QueryLogger:
    """Logs queries for audit and debugging purposes.

    Maintains a log file with query history and provides
    access to recent queries for the MCP resource.
    """

    def __init__(self, config: Optional[LoggingConfig] = None):
        """Initialize query logger."""
        self.config = config or LoggingConfig()
        self._enabled = self.config.query_logging
        self._recent_queries: List[Dict[str, Any]] = []
        self._max_recent = 100

        if self._enabled:
            self._setup_file_logger()

    def _setup_file_logger(self) -> None:
        """Set up the rotating file logger."""
        log_dir = Path(self.config.log_path)
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / "queries.log"

        self._file_logger = logging.getLogger("oci_logan_mcp.queries")
        self._file_logger.setLevel(logging.INFO)

        if not self._file_logger.handlers:
            handler = RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10MB
                backupCount=5,
            )
            formatter = logging.Formatter(
                "%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            )
            handler.setFormatter(formatter)
            self._file_logger.addHandler(handler)

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
            self._file_logger.info(log_line)

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
