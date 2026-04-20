"""pivot_on_entity — pull everything about an entity across log sources."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

ENTITY_FIELD_MAP: Dict[str, str] = {
    "host": "Host",
    "user": "User",
    "request_id": "Request ID",
    "ip": "IP Address",
}


class PivotTool:
    """Query all matching log sources for a given entity and merge into a timeline."""

    def __init__(self, query_engine):
        self._engine = query_engine

    async def run(
        self,
        entity_type: str,
        entity_value: str,
        time_range: Dict[str, str],
        sources: Optional[List[str]] = None,
        max_rows_per_source: int = 100,
        field_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _resolve_field(entity_type: str, field_name: Optional[str]) -> str:
        if entity_type == "custom":
            if not field_name:
                raise ValueError("field_name is required when entity_type='custom'")
            return field_name
        field = ENTITY_FIELD_MAP.get(entity_type)
        if field is None:
            valid = list(ENTITY_FIELD_MAP.keys()) + ["custom"]
            raise ValueError(f"Unknown entity_type {entity_type!r}. Valid: {valid}")
        return field
