"""Unified query catalog with provenance-tagged entries."""

from __future__ import annotations

import importlib.resources
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .file_lock import atomic_yaml_read

logger = logging.getLogger(__name__)


class SourceType(str, Enum):
    """Source type for catalog entries."""

    BUILTIN = "builtin"
    STARTER = "starter"
    PERSONAL = "personal"
    SHARED = "shared"


@dataclass
class CatalogEntry:
    """A queryable entry in the unified catalog with provenance tracking."""

    entry_id: str
    name: str
    query: str
    description: str
    source: SourceType
    category: str = "general"
    tags: List[str] = field(default_factory=list)
    interest_score: int = 0
    success_count: int = 0
    failure_count: int = 0
    use_count: int = 0
    created_at: Optional[str] = None
    last_used: Optional[str] = None
    promoted_at: Optional[str] = None
    promotion_status: Optional[str] = None
    promotion_reason: Optional[str] = None


class UnifiedCatalog:
    """Unified catalog loader for builtin, starter, personal, and shared queries."""

    def __init__(self, base_dir: Path):
        """Initialize catalog with a base directory for personal/shared sources.

        Args:
            base_dir: Base directory path (currently used for future personal/shared loaders).
        """
        self.base_dir = base_dir

    def load_builtins(self) -> List[CatalogEntry]:
        """Load builtin query templates from packaged YAML.

        Returns:
            List of CatalogEntry objects with source=BUILTIN. Empty list on any failure.
        """
        return self._load_packaged_yaml("builtin_queries.yaml", SourceType.BUILTIN)

    def load_starters(self) -> List[CatalogEntry]:
        """Load starter query examples from packaged YAML.

        Returns:
            List of CatalogEntry objects with source=STARTER. Empty list on any failure.
        """
        return self._load_packaged_yaml("starter_queries.yaml", SourceType.STARTER)

    def load_personal(self, user_id: str) -> List[CatalogEntry]:
        """Load a user's personal learned queries.

        Args:
            user_id: User ID to load queries for.

        Returns:
            List of CatalogEntry objects with source=PERSONAL. Empty list if user has no queries.
        """
        path = self.base_dir / "users" / user_id / "learned_queries.yaml"
        return self._load_yaml_file(path, SourceType.PERSONAL)

    def load_shared(self) -> List[CatalogEntry]:
        """Load shared promoted queries.

        Returns:
            List of CatalogEntry objects with source=SHARED. Empty list if no shared queries exist.
        """
        path = self.base_dir / "shared" / "promoted_queries.yaml"
        return self._load_yaml_file(path, SourceType.SHARED)

    def _parse_queries(
        self, data: dict, source: SourceType, origin: str
    ) -> List[CatalogEntry]:
        """Parse a loaded YAML dict into CatalogEntry list, skipping malformed entries.

        Args:
            data: Parsed YAML data dict (expected to have "queries" key).
            source: SourceType to assign to entries.
            origin: Origin string for logging (filename or description).

        Returns:
            List of CatalogEntry objects. Malformed entries are logged and skipped.
        """
        out = []
        for q in data.get("queries", []):
            if not isinstance(q, dict):
                logger.warning(f"{origin}: skipping non-dict entry: {q!r}")
                continue
            if not {"name", "query", "description"} <= q.keys():
                logger.warning(f"{origin}: skipping entry missing required keys: {q!r}")
                continue

            # Defensively coerce tags to list if present
            tags = q.get("tags", [])
            if not isinstance(tags, list):
                logger.warning(
                    f"{origin}: non-list tags coerced to [] for entry {q['name']}"
                )
                tags = []

            out.append(
                CatalogEntry(
                    entry_id=q.get("entry_id") or f"{source.value}:{q['name']}",
                    name=q["name"],
                    query=q["query"],
                    description=q["description"],
                    source=source,
                    category=q.get("category", "general"),
                    tags=tags,
                    interest_score=q.get("interest_score", 0),
                    success_count=q.get("success_count", 0),
                    failure_count=q.get("failure_count", 0),
                    use_count=q.get("use_count", 0),
                    created_at=q.get("created_at"),
                    last_used=q.get("last_used"),
                    promoted_at=q.get("promoted_at"),
                    promotion_status=q.get("promotion_status"),
                    promotion_reason=q.get("promotion_reason"),
                )
            )

        return out

    def _load_yaml_file(self, path: Path, source: SourceType) -> List[CatalogEntry]:
        """Load queries from a YAML file in the filesystem.

        Args:
            path: Full path to YAML file.
            source: SourceType to assign to loaded entries.

        Returns:
            List of CatalogEntry objects. Returns [] if file missing or corrupt.
        """
        data = atomic_yaml_read(path, default={"queries": []})
        return self._parse_queries(data, source, origin=str(path))

    def for_my_queries_view(self, user_id: str) -> List[CatalogEntry]:
        """Personal > shared > builtin > starter by name."""
        return self._merge_by_name([
            self.load_personal(user_id),
            self.load_shared(),
            self.load_builtins(),
            self.load_starters(),
        ])

    def for_templates_resource(self) -> List[CatalogEntry]:
        """builtin > shared. Personal and starter excluded."""
        return self._merge_by_name([self.load_builtins(), self.load_shared()])

    def for_onboarding(self) -> List[CatalogEntry]:
        """Starter only (Task 15 will augment with top-N shared community favorites)."""
        return self.load_starters()

    def _merge_by_name(self, buckets: List[List[CatalogEntry]]) -> List[CatalogEntry]:
        """Merge entries across sources. Buckets are in priority order (highest first).
        Name comparison is case-insensitive. On collision, the higher-priority entry
        wins AND KEEPS ITS OWN METRICS — losing entries are dropped entirely, not merged.
        """
        seen: Dict[str, CatalogEntry] = {}
        for bucket in buckets:
            for e in bucket:
                key = e.name.lower()
                if key not in seen:
                    seen[key] = e
        return list(seen.values())

    def _load_packaged_yaml(self, filename: str, source: SourceType) -> List[CatalogEntry]:
        """Load queries from a packaged YAML file.

        Args:
            filename: Name of YAML file in src/oci_logan_mcp/data/ directory.
            source: SourceType to assign to loaded entries.

        Returns:
            List of CatalogEntry objects. Returns [] on any failure.
        """
        try:
            data_file = importlib.resources.files("oci_logan_mcp") / "data" / filename
            raw = data_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
        except (FileNotFoundError, yaml.YAMLError, UnicodeDecodeError) as e:
            logger.error(f"Failed to load packaged YAML {filename}: {e}")
            return []

        return self._parse_queries(data, source, origin=filename)
