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
        field = self._resolve_field(entity_type, field_name)

        if sources is None:
            sources = await self._discover_sources(field, entity_value, time_range)

        if not sources:
            return self._empty_result(entity_type, entity_value, field)

        by_source, partial = await self._query_sources(
            sources, field, entity_value, time_range, max_rows_per_source
        )

        timeline = self._build_timeline(by_source)
        total_events = sum(len(s["rows"]) for s in by_source)
        sources_matched = sum(1 for s in by_source if s["rows"])

        return {
            "entity": {"type": entity_type, "value": entity_value, "field": field},
            "by_source": by_source,
            "cross_source_timeline": timeline,
            "stats": {"total_events": total_events, "sources_matched": sources_matched},
            "partial": partial,
            "metadata": {
                "time_range": time_range,
                "sources_queried": [s["source"] for s in by_source],
            },
        }

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

    @staticmethod
    def _build_timeline(by_source: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        events = []
        for src_result in by_source:
            source = src_result["source"]
            for row in src_result["rows"]:
                ts = row.get("Time") or row.get("time") or row.get("timestamp")
                events.append({**row, "timestamp": ts, "source": source})
        events.sort(key=lambda e: (e["timestamp"] is None, e["timestamp"] or ""))
        return events

    @staticmethod
    def _empty_result(
        entity_type: str, entity_value: str, field: str
    ) -> Dict[str, Any]:
        return {
            "entity": {"type": entity_type, "value": entity_value, "field": field},
            "by_source": [],
            "cross_source_timeline": [],
            "stats": {"total_events": 0, "sources_matched": 0},
            "partial": False,
            "metadata": {},
        }
