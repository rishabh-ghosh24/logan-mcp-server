"""pivot_on_entity — pull everything about an entity across log sources."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .budget_tracker import BudgetExceededError

# Live-probed against the dogfood tenancy: `'Host'` and `'User'` are not
# valid OCI Log Analytics field names for the SEARCH `=` operator —
# `'Host Name (Server)'` and `'User Name'` are. Field-name availability
# can vary per tenancy; callers whose tenancy uses different names should
# pass entity_type="custom" with an explicit field_name.
ENTITY_FIELD_MAP: Dict[str, str] = {
    "host": "Host Name (Server)",
    "user": "User Name",
    "request_id": "Request ID",
    "ip": "Source IP",
}
IP_FIELDS = ("Source IP", "Destination IP", "Host IP Address (Client)")


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
        fields = IP_FIELDS if entity_type == "ip" else None

        if sources is None:
            sources = await self._discover_sources(field, entity_value, time_range, fields=fields)

        if not sources:
            result = self._empty_result(entity_type, entity_value, field, time_range)
            if fields:
                result["entity"]["fields"] = list(fields)
            return result

        by_source, partial = await self._query_sources(
            sources, field, entity_value, time_range, max_rows_per_source, fields=fields
        )

        timeline = self._build_timeline(by_source)
        total_events = sum(len(s["rows"]) for s in by_source)
        sources_matched = sum(1 for s in by_source if s["rows"])

        return {
            "entity": {"type": entity_type, "value": entity_value, "field": field,
                       **({"fields": list(fields)} if fields else {})},
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

    @staticmethod
    def _condition(field: str, value: str, fields: Optional[Sequence[str]] = None) -> str:
        """Keep caller values literal and group the supported IP alternatives."""
        def escape(text: str) -> str:
            return text.replace("\\", "\\\\").replace("'", "\\'")

        conditions = [f"'{escape(name)}' = '{escape(value)}'" for name in (fields or (field,))]
        return "(" + " or ".join(conditions) + ")" if fields else conditions[0]

    async def _discover_sources(
        self, field: str, value: str, time_range: Dict[str, str],
        fields: Optional[Sequence[str]] = None,
    ) -> List[str]:
        query = f"{self._condition(field, value, fields)} | stats count by 'Log Source'"
        res = await self._engine.execute(query=query, **time_range)

        data = res.get("data", {}) or {}
        columns = [c.get("name") for c in data.get("columns", [])]
        rows = data.get("rows", [])

        if "Log Source" not in columns:
            return []

        src_idx = columns.index("Log Source")
        cnt_idx = columns.index("count") if "count" in columns else len(columns) - 1

        sources = []
        for row in rows:
            if not row:
                continue
            try:
                cnt = int(row[cnt_idx] or 0)
            except (TypeError, ValueError):
                cnt = 0
            if cnt > 0:
                sources.append(str(row[src_idx]))
        return sources

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
        fields: Optional[Sequence[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        by_source = []
        partial = False

        for source in sources:
            try:
                source_condition = self._condition("Log Source", source)
                query = f"{source_condition} and {self._condition(field, value, fields)}"
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
        entity_type: str,
        entity_value: str,
        field: str,
        time_range: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return {
            "entity": {"type": entity_type, "value": entity_value, "field": field},
            "by_source": [],
            "cross_source_timeline": [],
            "stats": {"total_events": 0, "sources_matched": 0},
            "partial": False,
            "metadata": {
                "time_range": time_range or {},
                "sources_queried": [],
            },
        }
