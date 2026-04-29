from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

REPORT_ID_RE = re.compile(r"^rpt_[0-9a-f]{32}$")


class ReportStoreError(Exception):
    """Base class for report store failures."""


class InvalidReportIdError(ReportStoreError):
    """Raised when a report id is unsafe or malformed."""


class ReportNotFoundError(ReportStoreError):
    """Raised when a report id does not exist in the store."""


class ReportStoreCorruptError(ReportStoreError):
    """Raised when a stored report cannot be read safely."""


class ReportStore:
    def __init__(self, artifact_dir: Path | str) -> None:
        self.artifact_dir = Path(artifact_dir).expanduser()
        self.root = self.artifact_dir / "store"

    def save(self, report: dict[str, Any]) -> dict[str, Any]:
        report_id = self._validate_report_id(str(report.get("report_id", "")))
        markdown = report.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ReportStoreError("report.markdown is required")

        report_dir = self._report_dir_for_save(report_id)

        markdown_path = report_dir / "report.md"
        html = report.get("html")
        html_path = report_dir / "report.html" if isinstance(html, str) and html.strip() else None
        metadata_path = report_dir / "metadata.json"

        raw_metadata = {} if "metadata" not in report or report.get("metadata") is None else report["metadata"]
        if not isinstance(raw_metadata, dict):
            raise ReportStoreError("report.metadata must be an object")
        metadata = dict(raw_metadata)
        metadata.setdefault("title", "Incident Report")
        metadata["report_id"] = report_id
        metadata["markdown_path"] = str(markdown_path)
        metadata["html_path"] = str(html_path) if html_path else None
        metadata["metadata_path"] = str(metadata_path)

        artifacts = [
            {"name": "markdown", "type": "markdown", "path": str(markdown_path)},
        ]
        if html_path:
            artifacts.append({"name": "html", "type": "html", "path": str(html_path)})
        artifacts.append({"name": "metadata", "type": "json", "path": str(metadata_path)})

        try:
            self._atomic_write_text(markdown_path, markdown)
            if html_path:
                self._atomic_write_text(html_path, html)
            else:
                self._remove_stale_html(report_dir / "report.html")
            self._atomic_write_text(
                metadata_path,
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            )
        except ReportStoreError:
            raise
        except OSError as exc:
            raise ReportStoreError(f"Could not persist report: {exc}") from exc

        return {
            "report_id": report_id,
            "markdown_path": str(markdown_path),
            "html_path": str(html_path) if html_path else None,
            "metadata_path": str(metadata_path),
            "metadata": metadata,
            "artifacts": artifacts,
        }

    def get(self, report_id: str) -> dict[str, Any]:
        report_id = self._validate_report_id(report_id)
        self._ensure_existing_root_for_read(report_id)
        report_dir = self._existing_report_dir(report_id)
        metadata_path = report_dir / "metadata.json"
        markdown_path = report_dir / "report.md"
        html_path = report_dir / "report.html"

        if metadata_path.is_symlink() or markdown_path.is_symlink():
            raise ReportStoreCorruptError(f"Report is unsafe: {report_id}")
        if not metadata_path.exists() or not markdown_path.exists():
            raise ReportNotFoundError(f"Report not found: {report_id}")
        self._ensure_file_within_root(metadata_path, report_id)
        self._ensure_file_within_root(markdown_path, report_id)
        if html_path.is_symlink():
            raise ReportStoreCorruptError(f"Report is unsafe: {report_id}")
        if html_path.exists():
            self._ensure_file_within_root(html_path, report_id)

        try:
            metadata = self._read_metadata(metadata_path, report_id)
            markdown = markdown_path.read_text(encoding="utf-8")
            html = html_path.read_text(encoding="utf-8") if html_path.exists() else None
        except OSError as exc:
            raise ReportStoreCorruptError(f"Report is corrupt: {report_id}") from exc

        return {
            "report_id": report_id,
            "markdown": markdown,
            "markdown_path": str(markdown_path),
            "html": html,
            "html_path": str(html_path) if html_path.exists() else None,
            "metadata": metadata,
            "metadata_path": str(metadata_path),
        }

    def list(self, limit: int = 20) -> dict[str, Any]:
        limit = self._clamp_limit(limit)
        reports: list[dict[str, Any]] = []
        corrupt_count = 0

        if self.root.is_symlink():
            return {"reports": [], "warnings": {"corrupt_count": 1}}
        if not self.root.exists():
            return {"reports": [], "warnings": {"corrupt_count": 0}}
        if not self.root.is_dir():
            return {"reports": [], "warnings": {"corrupt_count": 1}}

        for report_dir in self.root.iterdir():
            if not REPORT_ID_RE.fullmatch(report_dir.name):
                continue
            if report_dir.is_symlink():
                corrupt_count += 1
                continue
            if not report_dir.is_dir():
                continue
            try:
                self._ensure_path_within_root(report_dir, ReportStoreCorruptError)
            except ReportStoreCorruptError:
                corrupt_count += 1
                continue

            metadata_path = report_dir / "metadata.json"
            if metadata_path.is_symlink() or not metadata_path.exists():
                corrupt_count += 1
                continue

            try:
                self._ensure_file_within_root(metadata_path, report_dir.name)
                metadata = self._read_metadata(metadata_path, report_dir.name)
                markdown_path = report_dir / "report.md"
                self._ensure_file_within_root(markdown_path, report_dir.name)
                html_path = self._list_html_path(report_dir, report_dir.name)
            except ReportStoreCorruptError:
                corrupt_count += 1
                continue

            reports.append(
                {
                    "report_id": report_dir.name,
                    "title": metadata.get("title", "Incident Report"),
                    "generated_at": metadata.get("generated_at"),
                    "time_range": metadata.get("time_range"),
                    "summary_length": metadata.get("summary_length"),
                    "word_count": metadata.get("word_count"),
                    "markdown_path": str(markdown_path),
                    "html_path": str(html_path) if html_path else None,
                    "metadata_path": str(metadata_path),
                }
            )

        reports.sort(key=lambda entry: entry.get("generated_at") or "", reverse=True)
        return {"reports": reports[:limit], "warnings": {"corrupt_count": corrupt_count}}

    def _validate_report_id(self, report_id: str) -> str:
        if not REPORT_ID_RE.fullmatch(report_id):
            raise InvalidReportIdError(f"Invalid report_id: {report_id}")
        return report_id

    def _report_dir(self, report_id: str) -> Path:
        return self.root / self._validate_report_id(report_id)

    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _remove_stale_html(self, html_path: Path) -> None:
        if not html_path.exists() and not html_path.is_symlink():
            return
        if html_path.is_dir() and not html_path.is_symlink():
            raise ReportStoreError(f"Stale HTML path is not a file: {html_path}")
        try:
            html_path.unlink()
        except OSError as exc:
            raise ReportStoreError(f"Could not remove stale HTML file: {html_path}") from exc

    def _clamp_limit(self, limit: int) -> int:
        try:
            parsed_limit = int(limit)
        except (TypeError, ValueError):
            parsed_limit = 20
        return max(1, min(parsed_limit, 100))

    def _report_dir_for_save(self, report_id: str) -> Path:
        self._ensure_root_directory()
        report_dir = self._report_dir(report_id)
        if report_dir.is_symlink():
            raise ReportStoreError(f"Report path is unsafe: {report_id}")
        if report_dir.exists() and not report_dir.is_dir():
            raise ReportStoreError(f"Report path is not a directory: {report_id}")
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReportStoreError(f"Could not create report directory: {report_id}") from exc
        if report_dir.is_symlink() or not report_dir.is_dir():
            raise ReportStoreError(f"Report path is unsafe: {report_id}")
        self._ensure_path_within_root(report_dir, ReportStoreError)
        return report_dir

    def _existing_report_dir(self, report_id: str) -> Path:
        report_dir = self._report_dir(report_id)
        if report_dir.is_symlink():
            raise ReportStoreCorruptError(f"Report path is unsafe: {report_id}")
        if not report_dir.exists():
            raise ReportNotFoundError(f"Report not found: {report_id}")
        if not report_dir.is_dir():
            raise ReportStoreCorruptError(f"Report path is not a directory: {report_id}")
        self._ensure_path_within_root(report_dir, ReportStoreCorruptError)
        return report_dir

    def _ensure_root_directory(self) -> None:
        if self.root.is_symlink():
            raise ReportStoreError("Report store root is unsafe")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReportStoreError("Could not create report store root") from exc
        if self.root.is_symlink() or not self.root.is_dir():
            raise ReportStoreError("Report store root is unsafe")

    def _ensure_existing_root_for_read(self, report_id: str) -> None:
        if self.root.is_symlink():
            raise ReportStoreCorruptError(f"Report store root is unsafe: {report_id}")
        if not self.root.exists():
            raise ReportNotFoundError(f"Report not found: {report_id}")
        if not self.root.is_dir():
            raise ReportStoreCorruptError(f"Report store root is unsafe: {report_id}")

    def _ensure_path_within_root(self, path: Path, error_type: type[ReportStoreError]) -> None:
        try:
            path.resolve(strict=True).relative_to(self.root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise error_type(f"Report path is outside the store root: {path}") from exc

    def _ensure_file_within_root(self, path: Path, report_id: str) -> None:
        if not path.is_file():
            raise ReportStoreCorruptError(f"Report file is not a file: {report_id}")
        self._ensure_path_within_root(path, ReportStoreCorruptError)

    def _list_html_path(self, report_dir: Path, report_id: str) -> Path | None:
        html_path = report_dir / "report.html"
        if html_path.is_symlink():
            raise ReportStoreCorruptError(f"Report is unsafe: {report_id}")
        if not html_path.exists():
            return None
        self._ensure_file_within_root(html_path, report_id)
        return html_path

    def _read_metadata(self, metadata_path: Path, report_id: str) -> dict[str, Any]:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportStoreCorruptError(f"Report is corrupt: {report_id}") from exc
        if not isinstance(metadata, dict):
            raise ReportStoreCorruptError(f"Report metadata is not an object: {report_id}")
        return metadata
