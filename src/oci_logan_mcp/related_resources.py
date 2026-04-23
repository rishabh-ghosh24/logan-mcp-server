"""related_dashboards_and_searches (A7) - suggest related dashboards and queries."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, List, Optional

from .catalog import CatalogEntry
from .fuzzy_match import find_similar_fields, normalize_field_name


class RelatedDashboardsAndSearchesTool:
    """Suggest dashboards, saved searches, and learned queries for a source/entity/field."""

    def __init__(self, dashboard_service, saved_search_service, catalog):
        self._dashboard_service = dashboard_service
        self._saved_search = saved_search_service
        self._catalog = catalog

    @staticmethod
    def _error(error_code: str, error: str) -> Dict[str, Any]:
        return {
            "status": "error",
            "error_code": error_code,
            "error": error,
        }

    @staticmethod
    def _normalize(value: Optional[str]) -> str:
        return normalize_field_name(value or "")

    def _build_terms(
        self,
        *,
        source: Optional[str],
        entity: Optional[Dict[str, Any]],
        field: Optional[str],
    ) -> List[Dict[str, str]]:
        terms: List[Dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        candidates = [
            ("source", source),
            ("entity_type", entity.get("type") if isinstance(entity, dict) else None),
            ("entity_value", entity.get("value") if isinstance(entity, dict) else None),
            ("field", field),
        ]
        for kind, raw in candidates:
            normalized = self._normalize(raw)
            if not normalized:
                continue
            key = (kind, normalized)
            if key in seen:
                continue
            seen.add(key)
            terms.append({"kind": kind, "raw": str(raw), "normalized": normalized})
        return terms

    def _score_text_fields(
        self,
        *,
        terms: List[Dict[str, str]],
        primary_fields: Dict[str, Optional[str]],
        secondary_fields: Dict[str, Optional[str]],
    ) -> Optional[Dict[str, Any]]:
        best: Optional[Dict[str, Any]] = None
        normalized_primary_fields = {
            field_label: self._normalize(raw_text)
            for field_label, raw_text in primary_fields.items()
        }
        normalized_secondary_fields = {
            field_label: self._normalize(raw_text)
            for field_label, raw_text in secondary_fields.items()
        }
        fuzzy_candidates = {
            normalized_text: field_label
            for field_label, normalized_text in normalized_primary_fields.items()
            if normalized_text
        }

        for term in terms:
            for field_label, normalized_text in normalized_primary_fields.items():
                if not normalized_text:
                    continue
                if term["normalized"] in normalized_text:
                    candidate = {
                        "score": 3,
                        "reason": f"{term['kind'].replace('_', ' ')} matched {field_label}",
                    }
                    if best is None or candidate["score"] > best["score"]:
                        best = candidate

            for field_label, normalized_text in normalized_secondary_fields.items():
                if not normalized_text:
                    continue
                if term["normalized"] in normalized_text:
                    candidate = {
                        "score": 2,
                        "reason": f"{term['kind'].replace('_', ' ')} matched {field_label}",
                    }
                    if best is None or candidate["score"] > best["score"]:
                        best = candidate

            if not fuzzy_candidates:
                continue
            similar = find_similar_fields(
                term["normalized"],
                list(fuzzy_candidates.keys()),
                limit=1,
                threshold=70,
            )
            if similar:
                matched_field = fuzzy_candidates[similar[0]]
                candidate = {
                    "score": 1,
                    "reason": (
                        f"{term['kind'].replace('_', ' ')} "
                        f"fuzzy matched {matched_field}"
                    ),
                }
                if best is None or candidate["score"] > best["score"]:
                    best = candidate

        return best

    def _format_result(
        self,
        *,
        item_id: str,
        name: str,
        score: int,
        reason: str,
    ) -> Dict[str, Any]:
        return {
            "id": item_id,
            "name": name,
            "score": score,
            "reason": reason,
        }

    def _rank_dashboards(
        self,
        dashboards: Iterable[Dict[str, Any]],
        terms: List[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        ranked: List[Dict[str, Any]] = []
        for dashboard in dashboards:
            match = self._score_text_fields(
                terms=terms,
                primary_fields={"display_name": dashboard.get("display_name")},
                secondary_fields={"description": dashboard.get("description")},
            )
            if not match:
                continue
            ranked.append(
                self._format_result(
                    item_id=dashboard.get("id", ""),
                    name=dashboard.get("display_name", ""),
                    score=match["score"],
                    reason=match["reason"],
                )
            )
        return sorted(ranked, key=lambda item: (-item["score"], item["name"].lower()))[:5]

    async def _rank_saved_searches(self, terms: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        listed = await self._saved_search.list_searches()

        shortlisted: List[Dict[str, Any]] = []
        for search in listed:
            match = self._score_text_fields(
                terms=terms,
                primary_fields={"display_name": search.get("display_name")},
                secondary_fields={},
            )
            shortlisted.append(
                {
                    "id": search.get("id", ""),
                    "display_name": search.get("display_name", ""),
                    "score": match["score"] if match else 0,
                    "reason": match["reason"] if match else "metadata inspected",
                }
            )

        shortlisted = sorted(
            shortlisted,
            key=lambda item: (-item["score"], item["display_name"].lower()),
        )[:10]
        if not shortlisted:
            return []

        details = await asyncio.gather(
            *(self._saved_search.get_search_by_id(item["id"]) for item in shortlisted)
        )

        rescored: List[Dict[str, Any]] = []
        for detail, metadata_item in zip(details, shortlisted):
            match = self._score_text_fields(
                terms=terms,
                primary_fields={"display_name": detail.get("display_name")},
                secondary_fields={"query": detail.get("query")},
            )
            if not match:
                match = {"score": metadata_item["score"], "reason": metadata_item["reason"]}
            if match["score"] <= 0:
                continue
            rescored.append(
                self._format_result(
                    item_id=detail.get("id", metadata_item["id"]),
                    name=detail.get("display_name", metadata_item["display_name"]),
                    score=match["score"],
                    reason=match["reason"],
                )
            )
        return sorted(rescored, key=lambda item: (-item["score"], item["name"].lower()))[:5]

    def _rank_catalog_entries(
        self,
        entries: Iterable[CatalogEntry],
        terms: List[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        ranked: List[Dict[str, Any]] = []
        for entry in entries:
            match = self._score_text_fields(
                terms=terms,
                primary_fields={"name": entry.name},
                secondary_fields={
                    "description": entry.description,
                    "query": entry.query,
                },
            )
            if not match:
                continue
            ranked.append(
                {
                    **self._format_result(
                        item_id=entry.entry_id,
                        name=entry.name,
                        score=match["score"],
                        reason=match["reason"],
                    ),
                    "_source_rank": 0 if entry.source.value == "personal" else 1,
                }
            )
        ranked = sorted(
            ranked,
            key=lambda item: (-item["score"], item["_source_rank"], item["name"].lower()),
        )[:5]
        for item in ranked:
            item.pop("_source_rank", None)
        return ranked

    async def run(
        self,
        *,
        source: Optional[str] = None,
        entity: Optional[Dict[str, Any]] = None,
        field: Optional[str] = None,
        user_id: str,
    ) -> Dict[str, Any]:
        if entity is not None:
            if not isinstance(entity, dict):
                return self._error(
                    "invalid_entity",
                    "entity must be an object with string type and value fields.",
                )
            entity_type = entity.get("type")
            entity_value = entity.get("value")
            if not isinstance(entity_type, str) or not entity_type.strip():
                return self._error(
                    "invalid_entity",
                    "entity.type must be a non-empty string.",
                )
            if not isinstance(entity_value, str) or not entity_value.strip():
                return self._error(
                    "invalid_entity",
                    "entity.value must be a non-empty string.",
                )

        terms = self._build_terms(source=source, entity=entity, field=field)
        if not terms:
            return self._error(
                "missing_search_input",
                "Provide at least one of source, entity, or field.",
            )

        dashboards = await self._dashboard_service.list_dashboards()
        learned_queries = self._catalog.load_personal(user_id) + self._catalog.load_shared()

        return {
            "dashboards": self._rank_dashboards(dashboards, terms),
            "saved_searches": await self._rank_saved_searches(terms),
            "learned_queries": self._rank_catalog_entries(learned_queries, terms),
        }
