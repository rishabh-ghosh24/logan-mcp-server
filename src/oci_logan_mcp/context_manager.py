"""Persistent context and memory management for cross-session learning."""

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import Settings

logger = logging.getLogger(__name__)

# Default context directory
CONTEXT_DIR = Path.home() / ".oci-logan-mcp" / "context"

# Maximum number of learned queries to keep
MAX_LEARNED_QUERIES = 200


class ContextManager:
    """Manages persistent tenancy context and learned queries.

    Provides cross-session memory by persisting tenancy metadata,
    discovered schema data, and working queries to YAML files
    under ~/.oci-logan-mcp/context/.
    """

    def __init__(self, settings: Settings, context_dir: Optional[Path] = None):
        self.settings = settings
        self.context_dir = context_dir or CONTEXT_DIR
        self.context_dir.mkdir(parents=True, exist_ok=True)

        self._context_file = self.context_dir / "tenancy_context.yaml"
        self._queries_file = self.context_dir / "learned_queries.yaml"

        # In-memory state loaded from disk
        self._tenancy_context: Dict[str, Any] = {}
        self._learned_queries: List[Dict[str, Any]] = []

        # Load from disk at startup
        self._load_tenancy_context()
        self._load_learned_queries()

    # ----------------------------------------------------------------
    # Loading
    # ----------------------------------------------------------------

    def _load_tenancy_context(self) -> None:
        """Load tenancy context from YAML file."""
        if not self._context_file.exists():
            self._tenancy_context = self._default_context()
            return

        try:
            with open(self._context_file) as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                self._tenancy_context = data
            else:
                logger.warning("Corrupt tenancy context file, using defaults")
                self._tenancy_context = self._default_context()
        except Exception as e:
            logger.warning(f"Failed to load tenancy context: {e}, using defaults")
            self._tenancy_context = self._default_context()

    def _load_learned_queries(self) -> None:
        """Load learned queries from YAML file."""
        if not self._queries_file.exists():
            self._learned_queries = []
            return

        try:
            with open(self._queries_file) as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict) and isinstance(data.get("queries"), list):
                self._learned_queries = data["queries"]
            else:
                logger.warning("Corrupt learned queries file, starting empty")
                self._learned_queries = []
        except Exception as e:
            logger.warning(f"Failed to load learned queries: {e}, starting empty")
            self._learned_queries = []

    def _default_context(self) -> Dict[str, Any]:
        """Return default empty tenancy context."""
        return {
            "namespace": self.settings.log_analytics.namespace,
            "default_compartment_id": self.settings.log_analytics.default_compartment_id,
            "log_sources": [],
            "fields": [],
            "entities": [],
            "parsers": [],
            "labels": [],
            "log_groups": [],
            "saved_searches": [],
            "compartments": [],
            "notes": [],
            "last_updated": None,
            "version": 1,
        }

    # ----------------------------------------------------------------
    # Saving (atomic writes)
    # ----------------------------------------------------------------

    def _save_tenancy_context(self) -> None:
        """Save tenancy context to YAML file atomically."""
        self._tenancy_context["last_updated"] = datetime.utcnow().isoformat()
        self._atomic_yaml_write(self._context_file, self._tenancy_context)

    def _save_learned_queries(self) -> None:
        """Save learned queries to YAML file atomically."""
        data = {
            "version": 1,
            "last_updated": datetime.utcnow().isoformat(),
            "queries": self._learned_queries,
        }
        self._atomic_yaml_write(self._queries_file, data)

    def _atomic_yaml_write(self, filepath: Path, data: Any) -> None:
        """Write YAML data atomically (write to temp file, then rename)."""
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(filepath.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                os.replace(tmp_path, str(filepath))
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception as e:
            logger.error(f"Failed to save {filepath.name}: {e}")

    # ----------------------------------------------------------------
    # Tenancy Context Operations
    # ----------------------------------------------------------------

    def get_tenancy_context(self) -> Dict[str, Any]:
        """Get the full tenancy context."""
        return dict(self._tenancy_context)

    def update_log_sources(self, sources: List[Dict[str, Any]]) -> int:
        """Update log sources in tenancy context. Returns count of new sources."""
        existing = {s.get("name") for s in self._tenancy_context.get("log_sources", [])}
        new_sources = []
        now = datetime.utcnow().isoformat()

        for source in sources:
            name = source.get("name") or source.get("display_name")
            if name and name not in existing:
                new_sources.append(source)
                existing.add(name)

        if new_sources or not self._tenancy_context.get("log_sources"):
            self._tenancy_context["log_sources"] = sources
            self._save_tenancy_context()

        return len(new_sources)

    def update_confirmed_fields(self, fields: List[Dict[str, Any]]) -> int:
        """Update confirmed fields in tenancy context. Returns count of new fields."""
        existing = {f.get("name") for f in self._tenancy_context.get("fields", [])}
        new_count = sum(1 for f in fields if f.get("name") not in existing)

        if new_count > 0 or not self._tenancy_context.get("fields"):
            self._tenancy_context["fields"] = fields
            self._save_tenancy_context()

        return new_count

    def update_entities(self, entities: List[Dict[str, Any]]) -> int:
        """Update entities in tenancy context."""
        existing = {e.get("name") for e in self._tenancy_context.get("entities", [])}
        new_count = sum(1 for e in entities if e.get("name") not in existing)

        if new_count > 0 or not self._tenancy_context.get("entities"):
            self._tenancy_context["entities"] = entities
            self._save_tenancy_context()

        return new_count

    def update_parsers(self, parsers: List[Dict[str, Any]]) -> int:
        """Update parsers in tenancy context."""
        existing = {p.get("name") for p in self._tenancy_context.get("parsers", [])}
        new_count = sum(1 for p in parsers if p.get("name") not in existing)

        if new_count > 0 or not self._tenancy_context.get("parsers"):
            self._tenancy_context["parsers"] = parsers
            self._save_tenancy_context()

        return new_count

    def update_labels(self, labels: List[Dict[str, Any]]) -> int:
        """Update labels in tenancy context."""
        existing = {l.get("name") for l in self._tenancy_context.get("labels", [])}
        new_count = sum(1 for l in labels if l.get("name") not in existing)

        if new_count > 0 or not self._tenancy_context.get("labels"):
            self._tenancy_context["labels"] = labels
            self._save_tenancy_context()

        return new_count

    def update_log_groups(self, log_groups: List[Dict[str, Any]]) -> int:
        """Update log groups in tenancy context."""
        existing = {g.get("id") for g in self._tenancy_context.get("log_groups", [])}
        new_count = sum(1 for g in log_groups if g.get("id") not in existing)

        if new_count > 0 or not self._tenancy_context.get("log_groups"):
            self._tenancy_context["log_groups"] = log_groups
            self._save_tenancy_context()

        return new_count

    def update_saved_searches(self, saved_searches: List[Dict[str, Any]]) -> int:
        """Update saved searches in tenancy context."""
        existing = {s.get("id") for s in self._tenancy_context.get("saved_searches", [])}
        new_count = sum(1 for s in saved_searches if s.get("id") not in existing)

        if new_count > 0 or not self._tenancy_context.get("saved_searches"):
            self._tenancy_context["saved_searches"] = saved_searches
            self._save_tenancy_context()

        return new_count

    def update_compartments(self, compartments: List[Dict[str, Any]]) -> int:
        """Update compartments in tenancy context."""
        existing = {c.get("id") for c in self._tenancy_context.get("compartments", [])}
        new_count = sum(1 for c in compartments if c.get("id") not in existing)

        if new_count > 0 or not self._tenancy_context.get("compartments"):
            self._tenancy_context["compartments"] = compartments
            self._save_tenancy_context()

        return new_count

    def add_note(self, note: str) -> None:
        """Add an environment-specific note."""
        notes = self._tenancy_context.setdefault("notes", [])
        if note not in notes:
            notes.append(note)
            self._save_tenancy_context()

    def remove_note(self, note_index: int) -> bool:
        """Remove a note by index. Returns True if removed."""
        notes = self._tenancy_context.get("notes", [])
        if 0 <= note_index < len(notes):
            notes.pop(note_index)
            self._save_tenancy_context()
            return True
        return False

    # ----------------------------------------------------------------
    # Schema Refresh (called at startup)
    # ----------------------------------------------------------------

    async def refresh_schema(self, oci_client: Any, settings: Settings) -> Dict[str, int]:
        """Fetch all schema data from OCI API and save to context.

        Called at server startup to ensure always-fresh schema data.
        Returns a dict of counts for each schema category.
        """
        counts = {}

        try:
            sources = await oci_client.list_log_sources()
            counts["log_sources"] = self.update_log_sources(sources)
        except Exception as e:
            logger.warning(f"Failed to refresh log sources: {e}")

        try:
            fields = await oci_client.list_fields()
            counts["fields"] = self.update_confirmed_fields(fields)
        except Exception as e:
            logger.warning(f"Failed to refresh fields: {e}")

        try:
            entities = await oci_client.list_entities()
            counts["entities"] = self.update_entities(entities)
        except Exception as e:
            logger.warning(f"Failed to refresh entities: {e}")

        try:
            parsers = await oci_client.list_parsers()
            counts["parsers"] = self.update_parsers(parsers)
        except Exception as e:
            logger.warning(f"Failed to refresh parsers: {e}")

        try:
            labels = await oci_client.list_labels()
            counts["labels"] = self.update_labels(labels)
        except Exception as e:
            logger.warning(f"Failed to refresh labels: {e}")

        try:
            log_groups = await oci_client.list_log_groups()
            counts["log_groups"] = self.update_log_groups(log_groups)
        except Exception as e:
            logger.warning(f"Failed to refresh log groups: {e}")

        try:
            compartments = await oci_client.list_compartments()
            counts["compartments"] = self.update_compartments(compartments)
        except Exception as e:
            logger.warning(f"Failed to refresh compartments: {e}")

        # Update namespace and compartment from settings
        self._tenancy_context["namespace"] = settings.log_analytics.namespace
        self._tenancy_context["default_compartment_id"] = settings.log_analytics.default_compartment_id
        self._save_tenancy_context()

        logger.info(f"Schema refresh complete: {counts}")
        return counts

    # ----------------------------------------------------------------
    # Learned Query Operations
    # ----------------------------------------------------------------

    def save_learned_query(
        self,
        name: str,
        query: str,
        description: str,
        category: str = "general",
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Save a working query for future reference.

        Deduplicates by name (updates existing) and by query text
        (updates if same query exists under a different name).
        """
        now = datetime.utcnow().isoformat()
        normalized_query = query.strip()

        # Check for existing by name
        for existing in self._learned_queries:
            if existing["name"] == name:
                existing["query"] = normalized_query
                existing["description"] = description
                existing["category"] = category
                existing["tags"] = tags or existing.get("tags", [])
                existing["last_used"] = now
                existing["use_count"] = existing.get("use_count", 0) + 1
                self._save_learned_queries()
                return existing

        # Check for existing by query text
        for existing in self._learned_queries:
            if existing["query"].strip() == normalized_query:
                existing["name"] = name
                existing["description"] = description
                existing["category"] = category
                existing["tags"] = tags or existing.get("tags", [])
                existing["last_used"] = now
                existing["use_count"] = existing.get("use_count", 0) + 1
                self._save_learned_queries()
                return existing

        # New query
        entry = {
            "name": name,
            "description": description,
            "query": normalized_query,
            "category": category,
            "tags": tags or [],
            "created_at": now,
            "last_used": now,
            "use_count": 1,
        }
        self._learned_queries.append(entry)

        # Enforce max limit
        if len(self._learned_queries) > MAX_LEARNED_QUERIES:
            self._learned_queries.sort(
                key=lambda q: (q.get("use_count", 0), q.get("last_used", "")),
            )
            self._learned_queries = self._learned_queries[-MAX_LEARNED_QUERIES:]

        self._save_learned_queries()
        return entry

    def list_learned_queries(
        self,
        category: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List learned queries, optionally filtered by category or tag."""
        queries = self._learned_queries

        if category and category != "all":
            queries = [q for q in queries if q.get("category") == category]

        if tag:
            queries = [q for q in queries if tag in q.get("tags", [])]

        return queries

    def delete_learned_query(self, name: str) -> bool:
        """Delete a learned query by name. Returns True if deleted."""
        original_count = len(self._learned_queries)
        self._learned_queries = [
            q for q in self._learned_queries if q["name"] != name
        ]
        if len(self._learned_queries) < original_count:
            self._save_learned_queries()
            return True
        return False

    def record_query_usage(self, query: str) -> None:
        """Bump use_count and last_used for a matching learned query."""
        normalized = query.strip()
        for existing in self._learned_queries:
            if existing["query"].strip() == normalized:
                existing["use_count"] = existing.get("use_count", 0) + 1
                existing["last_used"] = datetime.utcnow().isoformat()
                self._save_learned_queries()
                return

    # ----------------------------------------------------------------
    # Template Merging
    # ----------------------------------------------------------------

    def get_all_templates(
        self, builtin_templates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Merge built-in templates with learned queries.

        Learned queries are appended after built-in templates,
        marked with source='learned'. Built-in templates are
        marked with source='builtin'.
        """
        # Mark built-in templates
        merged = []
        builtin_queries = set()
        for t in builtin_templates:
            entry = dict(t)
            entry["source"] = "builtin"
            merged.append(entry)
            builtin_queries.add(t.get("query", "").strip())

        # Append learned queries (skip duplicates of built-in)
        for q in self._learned_queries:
            if q["query"].strip() not in builtin_queries:
                entry = dict(q)
                entry["source"] = "learned"
                merged.append(entry)

        return merged
