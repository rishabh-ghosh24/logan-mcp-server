"""File-backed incident report persistence."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


REPORT_ID_RE = re.compile(r"^rpt_[0-9a-f]{32}$")


class ReportStoreError(ValueError):
    """Raised when a report cannot be persisted or retrieved safely."""


class ReportStore:
    def __init__(self, store_dir: Path) -> None:
        self.store_dir = Path(store_dir)

    def save(self, report: Dict[str, Any]) -> Dict[str, Any]:
        report_id = self._validate_report_id(str(report.get("report_id", "")))
        markdown = report.get("markdown")
        html = report.get("html")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ReportStoreError("report.markdown is required")
        if not isinstance(html, str) or not html.strip():
            raise ReportStoreError("report.html is required")

        report_dir = self._report_dir(report_id)
        report_dir.mkdir(parents=True, exist_ok=True)

        metadata = dict(report.get("metadata") or {})
        metadata["report_id"] = report_id
        metadata["stored_at"] = datetime.now(timezone.utc).isoformat()

        artifacts = [
            self._write_artifact(report_dir, "report.md", markdown),
            self._write_artifact(report_dir, "report.html", html),
            self._write_artifact(
                report_dir,
                "metadata.json",
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                artifact_type="metadata",
            ),
        ]
        return {**report, "metadata": metadata, "artifacts": artifacts}

    def get(self, report_id: str) -> Dict[str, Any]:
        report_id = self._validate_report_id(report_id)
        report_dir = self._report_dir(report_id)
        metadata_path = report_dir / "metadata.json"
        markdown_path = report_dir / "report.md"
        html_path = report_dir / "report.html"
        if not metadata_path.is_file() or not markdown_path.is_file():
            raise ReportStoreError(f"report not found: {report_id}")

        metadata = json.loads(metadata_path.read_text())
        html = html_path.read_text() if html_path.is_file() else None
        artifacts = self._artifacts_for(report_dir)
        return {
            "report_id": report_id,
            "markdown": markdown_path.read_text(),
            "html": html,
            "metadata": metadata,
            "artifacts": artifacts,
        }

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        if limit < 1:
            limit = 1
        if not self.store_dir.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for path in self.store_dir.iterdir():
            if not path.is_dir() or not REPORT_ID_RE.match(path.name):
                continue
            metadata_path = path / "metadata.json"
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text())
            except Exception:
                continue
            rows.append({
                "report_id": path.name,
                "stored_at": metadata.get("stored_at", ""),
                "generated_at": metadata.get("generated_at", ""),
                "source_type": metadata.get("source_type", ""),
                "artifacts": self._artifacts_for(path),
            })
        rows.sort(key=lambda row: row.get("stored_at") or row.get("generated_at") or "", reverse=True)
        return rows[:limit]

    def _validate_report_id(self, report_id: str) -> str:
        if not REPORT_ID_RE.match(report_id or ""):
            raise ReportStoreError("invalid report_id")
        return report_id

    def _report_dir(self, report_id: str) -> Path:
        report_dir = (self.store_dir / report_id).resolve()
        store_root = self.store_dir.resolve()
        if os.path.commonpath([str(store_root), str(report_dir)]) != str(store_root):
            raise ReportStoreError("invalid report path")
        return report_dir

    def _write_artifact(
        self,
        report_dir: Path,
        name: str,
        content: str,
        artifact_type: str = "text",
    ) -> Dict[str, Any]:
        path = report_dir / name
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(content)
        tmp_path.replace(path)
        return {
            "name": name,
            "type": artifact_type,
            "path": str(path),
        }

    def _artifacts_for(self, report_dir: Path) -> List[Dict[str, Any]]:
        artifacts: List[Dict[str, Any]] = []
        for name, artifact_type in (
            ("report.md", "text"),
            ("report.html", "text"),
            ("metadata.json", "metadata"),
        ):
            path = report_dir / name
            if path.is_file():
                artifacts.append({
                    "name": name,
                    "type": artifact_type,
                    "path": str(path),
                })
        return artifacts
