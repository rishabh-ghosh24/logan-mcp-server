"""trace_request_id — search request/trace id fields across sources."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Tuple

from .budget_tracker import BudgetExceededError

logger = logging.getLogger(__name__)


class TraceRequestIdTool:
    """Probe common trace/request-id fields and merge matching events."""

    DEFAULT_ID_FIELDS = ["Request ID", "Trace ID", "traceId", "x-request-id"]
    RECORD_ID_FIELDS = ("_id", "id", "Record ID")
    _SOFT_FIELD_MISS_TOKENS = (
        "unknown field",
        "invalid field",
        "invalid field name",
        "not a valid field",
        "unknown field name",
    )

    def __init__(self, pivot_tool):
        self._pivot = pivot_tool

    async def run(
        self,
        request_id: str,
        time_range: Dict[str, Any],
        id_fields: List[str] | None = None,
    ) -> Dict[str, Any]:
        results = []
        for field_name in self._candidate_fields(id_fields):
            try:
                result = await self._pivot.run(
                    entity_type="custom",
                    entity_value=request_id,
                    time_range=time_range,
                    field_name=field_name,
                )
            except BudgetExceededError:
                raise
            except Exception as exc:
                if self._is_soft_field_miss(exc):
                    logger.debug("Skipping trace field %s after soft miss: %s", field_name, exc)
                    continue
                raise
            results.append(result)

        events, sources = self._merge_results(results)
        return {
            "request_id": request_id,
            "events": events,
            "sources_matched": sources,
        }

    @classmethod
    def _candidate_fields(cls, id_fields: List[str] | None) -> List[str]:
        candidates = id_fields if id_fields is not None else cls.DEFAULT_ID_FIELDS
        seen = set()
        out: List[str] = []
        for field_name in candidates:
            normalized = str(field_name).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)
        return out

    @classmethod
    def _is_soft_field_miss(cls, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(token in message for token in cls._SOFT_FIELD_MISS_TOKENS)

    @classmethod
    def _dedup_key(cls, event: Dict[str, Any]) -> Tuple[Any, ...]:
        source = event.get("source")
        for field_name in cls.RECORD_ID_FIELDS:
            value = event.get(field_name)
            if value not in (None, ""):
                return ("record_id", source, field_name, str(value))

        normalized_items = tuple(
            sorted(
                (str(key), cls._normalize_value(value))
                for key, value in event.items()
            )
        )
        return ("row", source, normalized_items)

    @classmethod
    def _normalize_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return tuple(
                sorted((str(key), cls._normalize_value(val)) for key, val in value.items())
            )
        if isinstance(value, list):
            return tuple(cls._normalize_value(item) for item in value)
        return value

    @classmethod
    def _merge_results(
        cls, results: Iterable[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        merged: List[Dict[str, Any]] = []
        sources = set()
        seen = set()

        for result in results:
            for source_result in result.get("by_source", []):
                source_name = source_result.get("source")
                if source_name and source_result.get("rows"):
                    sources.add(str(source_name))
            for event in result.get("cross_source_timeline", []):
                normalized = dict(event)
                timestamp = (
                    normalized.get("timestamp")
                    or normalized.get("Time")
                    or normalized.get("time")
                    or normalized.get("Start Time")
                )
                normalized.setdefault("timestamp", timestamp)
                if normalized.get("source"):
                    sources.add(str(normalized["source"]))
                dedup_key = cls._dedup_key(normalized)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                merged.append(normalized)

        merged.sort(key=lambda event: (event.get("timestamp") is None, event.get("timestamp") or ""))
        return merged, sorted(sources)
