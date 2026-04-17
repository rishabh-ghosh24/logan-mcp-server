"""Unified query catalog with provenance-tagged entries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


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
