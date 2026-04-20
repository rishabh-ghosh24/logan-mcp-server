"""pivot_on_entity — pull everything about an entity across log sources."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .budget_tracker import BudgetExceededError

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

    async def _discover_sources(
        self, field: str, value: str, time_range: Dict[str, str]
    ) -> List[str]:
        query = f"'{field}' = '{value}' | stats count by 'Log Source'"
        res = await self._engine.execute(query=query, **time_range)

        data = res.get("data", {}) or {}
        columns = [c.get("name") for c in data.get("columns", [])]
        rows = data.get("rows", [])

        if "Log Source" not in columns:
            return []

        src_idx = columns.index("Log Source")
        cnt_idx = columns.index("count") if "count" in columns else len(columns) - 1

        return [
            str(row[src_idx])
            for row in rows
            if row and int(row[cnt_idx] or 0) > 0
        ]

    @staticmethod
    def _extract_rows(response: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = response.get("data", {}) or {}
        columns = [c.get("name") for c in data.get("columns", [])]
        return [dict(zip(columns, row)) for row in data.get("rows", [])]

    async def _query_sources(
        self,
        sources: List[str],
        field: str,
        value: str,
        time_range: Dict[str, str],
        max_rows: int,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        by_source = []
        partial = False

        for source in sources:
            try:
                query = f"'Log Source' = '{source}' and '{field}' = '{value}'"
                res = await self._engine.execute(
                    query=query, max_results=max_rows, **time_range
                )
                rows = self._extract_rows(res)
                truncated = len(rows) >= max_rows
                by_source.append({"source": source, "rows": rows, "truncated": truncated})
            except BudgetExceededError:
                partial = True
                break

        return by_source, partial
