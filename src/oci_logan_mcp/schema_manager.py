"""Schema management service for Log Analytics."""

from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from .client import OCILogAnalyticsClient
from .cache import CacheManager


@dataclass
class FieldInfo:
    """Information about a Log Analytics field."""

    name: str
    data_type: str
    description: Optional[str] = None
    possible_values: Optional[List[str]] = None
    hint: Optional[str] = None
    display_name: Optional[str] = None


class SchemaManager:
    """Manages schema information for Log Analytics."""

    FIELD_HINTS = {
        "Severity": "Use for filtering log severity levels. Values: Critical, Error, Warning, Info, Debug",
        "Entity": "The source system/host that generated the log",
        "Log Source": "The type/category of log data",
        "Message": "The raw log message content",
        "Time": "Timestamp when the log was generated",
        "Host Name": "The hostname of the system that generated the log",
        "Host Name (Server)": "The server hostname",
        "Error Id": "Unique identifier for an error event",
    }

    KNOWN_VALUES = {
        "Severity": ["Critical", "Error", "Warning", "Info", "Debug"],
    }

    def __init__(self, oci_client: OCILogAnalyticsClient, cache: CacheManager):
        """Initialize schema manager."""
        self.oci_client = oci_client
        self.cache = cache

    async def get_log_sources(
        self, compartment_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get all log sources with metadata."""
        cache_key = f"sources:{compartment_id or 'default'}"
        cached = self.cache.get(cache_key, category="schema")
        if cached:
            return cached

        sources = await self.oci_client.list_log_sources(compartment_id)

        self.cache.set(cache_key, sources, category="schema")
        return sources

    async def get_fields(self, source_name: Optional[str] = None) -> List[FieldInfo]:
        """Get fields with semantic hints."""
        cache_key = f"fields:{source_name or 'all'}"
        cached = self.cache.get(cache_key, category="schema")
        if cached:
            return cached

        fields = await self.oci_client.list_fields(source_name)

        result = []
        for f in fields:
            field_name = f.get("name", "")
            hint = self._generate_semantic_hint(field_name, f.get("description", ""))
            result.append(
                FieldInfo(
                    name=field_name,
                    data_type=f.get("data_type", "STRING"),
                    description=f.get("description"),
                    possible_values=self.KNOWN_VALUES.get(field_name),
                    hint=hint,
                    display_name=f.get("display_name") or None,
                )
            )

        self.cache.set(cache_key, result, category="schema")
        return result

    async def get_all_field_names(self) -> List[str]:
        """Get list of all field names."""
        fields = await self.get_fields()
        return [f.name for f in fields]

    async def get_entities(self, entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get monitored entities."""
        cache_key = f"entities:{entity_type or 'all'}"
        cached = self.cache.get(cache_key, category="schema")
        if cached:
            return cached

        entities = await self.oci_client.list_entities(entity_type)

        self.cache.set(cache_key, entities, category="schema")
        return entities

    async def get_parsers(self) -> List[Dict[str, Any]]:
        """Get available parsers."""
        cache_key = "parsers"
        cached = self.cache.get(cache_key, category="schema")
        if cached:
            return cached

        parsers = await self.oci_client.list_parsers()

        self.cache.set(cache_key, parsers, category="schema")
        return parsers

    async def get_labels(self) -> List[Dict[str, Any]]:
        """Get label definitions."""
        cache_key = "labels"
        cached = self.cache.get(cache_key, category="schema")
        if cached:
            return cached

        labels = await self.oci_client.list_labels()

        self.cache.set(cache_key, labels, category="schema")
        return labels

    DIGEST_WINDOW = "last_24_hours"
    DIGEST_TOP = 20
    VOLUME_QUERY = "* | stats count by 'Log Source' | sort -count"

    async def get_volume_by_source(self, query_engine, compartment_id: Optional[str],
                                   include_subcompartments: bool = True,
                                   window: str = DIGEST_WINDOW) -> Dict[str, int]:
        """Records per source over the window, in volume order. One query, kept for the query
        cache's five minutes so the digest and every ranked listing share it."""
        cache_key = f"volume:{compartment_id or 'default'}:{include_subcompartments}:{window}"
        cached = self.cache.get(cache_key, category="query")
        if cached is not None:
            return dict(cached)
        result = await query_engine.execute(
            query=self.VOLUME_QUERY, time_range=window, compartment_id=compartment_id,
            include_subcompartments=include_subcompartments, max_results=500, use_cache=False,
        )
        data = result.get("data", {}) or {}
        columns = [c.get("name") for c in data.get("columns", [])]
        source_idx = columns.index("Log Source") if "Log Source" in columns else 0
        count_idx = columns.index("count") if "count" in columns else 1
        volume: Dict[str, int] = {}
        for row in data.get("rows", []):
            if len(row) > max(source_idx, count_idx) and row[source_idx]:
                volume[str(row[source_idx])] = int(row[count_idx] or 0)
        self.cache.set(cache_key, volume, category="query")
        return volume

    async def get_schema_digest(self, query_engine, scope: Dict[str, Any]) -> Dict[str, Any]:
        """A read-time digest for the schema resource: counts, the top sources by volume and where
        to find the rest. Nothing is stored; a failing part is reported under errors."""
        compartment_id = scope.get("compartment_id")
        include_subs = bool(scope.get("include_subcompartments", True))
        errors: Dict[str, str] = {}
        counts: Dict[str, int] = {}
        sources: List[Dict[str, Any]] = []
        volume: Dict[str, int] = {}
        try:
            sources = await self.get_log_sources(compartment_id)
            counts["log_sources_defined"] = len(sources)
        except Exception as error:  # fail soft, part by part
            errors["log_sources"] = f"{type(error).__name__}: {error}"[:200]
        try:
            volume = await self.get_volume_by_source(query_engine, compartment_id, include_subs)
            counts["log_sources_with_data_in_window"] = len(volume)
        except Exception as error:
            errors["volume_by_source"] = f"{type(error).__name__}: {error}"[:200]
        try:
            counts.update({k: v for k, v in (await self.oci_client.get_entities_summary(compartment_id)).items()
                           if k in ("active_entities", "entities_with_logs_collected")})
        except Exception as error:
            errors["entities_summary"] = f"{type(error).__name__}: {error}"[:200]
        try:
            counts["entity_types"] = len(await self.oci_client.list_entity_types())
        except Exception as error:
            errors["entity_types"] = f"{type(error).__name__}: {error}"[:200]

        internal = {s.get("display_name") or s.get("name"): s.get("name") for s in sources}
        top = [{"display_name": name, "name": internal.get(name), "records": records}
               for name, records in list(volume.items())[:self.DIGEST_TOP]]
        digest: Dict[str, Any] = {
            "scope": {"namespace": scope.get("namespace"), "compartment_id": compartment_id,
                      "include_subcompartments": include_subs, "window": self.DIGEST_WINDOW},
            "counts": counts,
            "top_sources_by_volume": top,
            "more_sources": {
                "with_data_not_shown": max(0, len(volume) - len(top)),
                "defined_without_data_in_window": max(0, len(sources) - len(volume)) if sources else None,
                "note": ("Only sources with records in the window are listed. A source absent here may still "
                         "exist and hold older data: use list_log_sources(keyword) to find it by name, or "
                         "get_log_summary with a wider time_range."),
            },
            "how_to_get_more": {
                "fields_of_a_source": "list_fields(source_name, keyword)",
                "entities": "list_entities(entity_type, keyword)",
                "sources_by_name": "list_log_sources(keyword)",
                "saved_searches": "list_saved_searches(keyword)",
                "parsers": "list_parsers", "labels": "list_labels",
                "volume_by_source_over_any_window": "get_log_summary or run_query",
            },
            "freshness": "Built at read time; the volume query is reused for five minutes.",
        }
        if errors:
            digest["errors"] = errors
        return digest

    def _generate_semantic_hint(self, field_name: str, description: str) -> str:
        """Generate a semantic hint for a field."""
        if field_name in self.FIELD_HINTS:
            return self.FIELD_HINTS[field_name]
        return description or ""
