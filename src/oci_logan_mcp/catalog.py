"""Unified query catalog with provenance-tagged entries."""

from __future__ import annotations

import importlib.resources
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

import yaml

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
        except Exception:
            return []

        out = []
        for q in data.get("queries", []):
            if not isinstance(q, dict):
                continue
            if not {"name", "query", "description"} <= q.keys():
                continue

            # Defensively coerce tags to list if present
            tags = q.get("tags", [])
            if not isinstance(tags, list):
                logger.warning(
                    f"{filename}: Entry '{q.get('name')}' has non-list tags, coercing to []"
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
                )
            )

        return out
