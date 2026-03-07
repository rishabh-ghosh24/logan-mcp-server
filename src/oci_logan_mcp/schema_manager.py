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

    async def get_full_schema(self) -> Dict[str, Any]:
        """Get complete schema for AI context."""
        sources = await self.get_log_sources()
        fields = await self.get_fields()
        entities = await self.get_entities()
        parsers = await self.get_parsers()
        labels = await self.get_labels()

        return {
            "log_sources": sources,
            "fields": [
                {
                    "name": f.name,
                    "data_type": f.data_type,
                    "description": f.description,
                    "possible_values": f.possible_values,
                    "hint": f.hint,
                }
                for f in fields
            ],
            "entities": entities,
            "parsers": parsers,
            "labels": labels,
        }

    def _generate_semantic_hint(self, field_name: str, description: str) -> str:
        """Generate a semantic hint for a field."""
        if field_name in self.FIELD_HINTS:
            return self.FIELD_HINTS[field_name]
        return description or ""
