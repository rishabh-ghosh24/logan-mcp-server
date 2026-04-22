"""Persistent context and memory management for cross-session tenancy data."""

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import Settings

logger = logging.getLogger(__name__)

# Default context directory
CONTEXT_DIR = Path.home() / ".oci-logan-mcp" / "context"


class ContextManager:
    """Manages persistent tenancy context.

    Provides cross-session memory by persisting tenancy metadata and
    discovered schema data to YAML files under ~/.oci-logan-mcp/context/.
    Learned query storage is handled by UserStore (v0.5+).
    """

    def __init__(self, settings: Settings, context_dir: Optional[Path] = None):
        self.settings = settings
        self.context_dir = context_dir or CONTEXT_DIR
        self.context_dir.mkdir(parents=True, exist_ok=True)

        self._context_file = self.context_dir / "tenancy_context.yaml"

        # In-memory state loaded from disk
        self._tenancy_context: Dict[str, Any] = {}

        # Load from disk at startup
        self._load_tenancy_context()

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
        self._tenancy_context["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._atomic_yaml_write(self._context_file, self._tenancy_context)

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

    def update_log_sources(self, sources: List[Dict[str, Any]]) -> str:
        """Update log sources in tenancy context. Returns summary string."""
        existing = {s.get("name") for s in self._tenancy_context.get("log_sources", [])}
        new_count = 0

        for source in sources:
            name = source.get("name") or source.get("display_name")
            if name and name not in existing:
                new_count += 1

        if new_count > 0 or not self._tenancy_context.get("log_sources"):
            self._tenancy_context["log_sources"] = sources
            self._save_tenancy_context()

        return f"{len(sources)} total ({new_count} new)"

    def update_confirmed_fields(self, fields: List[Dict[str, Any]]) -> str:
        """Update confirmed fields in tenancy context. Returns summary string."""
        existing = {f.get("name") for f in self._tenancy_context.get("fields", [])}
        new_count = sum(1 for f in fields if f.get("name") not in existing)

        if new_count > 0 or not self._tenancy_context.get("fields"):
            self._tenancy_context["fields"] = fields
            self._save_tenancy_context()

        return f"{len(fields)} total ({new_count} new)"

    def update_entities(self, entities: List[Dict[str, Any]]) -> str:
        """Update entities in tenancy context. Returns summary string."""
        existing = {e.get("name") for e in self._tenancy_context.get("entities", [])}
        new_count = sum(1 for e in entities if e.get("name") not in existing)

        if new_count > 0 or not self._tenancy_context.get("entities"):
            self._tenancy_context["entities"] = entities
            self._save_tenancy_context()

        return f"{len(entities)} total ({new_count} new)"

    def update_parsers(self, parsers: List[Dict[str, Any]]) -> str:
        """Update parsers in tenancy context. Returns summary string."""
        existing = {p.get("name") for p in self._tenancy_context.get("parsers", [])}
        new_count = sum(1 for p in parsers if p.get("name") not in existing)

        if new_count > 0 or not self._tenancy_context.get("parsers"):
            self._tenancy_context["parsers"] = parsers
            self._save_tenancy_context()

        return f"{len(parsers)} total ({new_count} new)"

    def update_labels(self, labels: List[Dict[str, Any]]) -> str:
        """Update labels in tenancy context. Returns summary string."""
        existing = {l.get("name") for l in self._tenancy_context.get("labels", [])}
        new_count = sum(1 for l in labels if l.get("name") not in existing)

        if new_count > 0 or not self._tenancy_context.get("labels"):
            self._tenancy_context["labels"] = labels
            self._save_tenancy_context()

        return f"{len(labels)} total ({new_count} new)"

    def update_log_groups(self, log_groups: List[Dict[str, Any]]) -> str:
        """Update log groups in tenancy context. Returns summary string."""
        existing = {g.get("id") for g in self._tenancy_context.get("log_groups", [])}
        new_count = sum(1 for g in log_groups if g.get("id") not in existing)

        if new_count > 0 or not self._tenancy_context.get("log_groups"):
            self._tenancy_context["log_groups"] = log_groups
            self._save_tenancy_context()

        return f"{len(log_groups)} total ({new_count} new)"

    def update_saved_searches(self, saved_searches: List[Dict[str, Any]]) -> str:
        """Update saved searches in tenancy context. Returns summary string."""
        existing = {s.get("id") for s in self._tenancy_context.get("saved_searches", [])}
        new_count = sum(1 for s in saved_searches if s.get("id") not in existing)

        if new_count > 0 or not self._tenancy_context.get("saved_searches"):
            self._tenancy_context["saved_searches"] = saved_searches
            self._save_tenancy_context()

        return f"{len(saved_searches)} total ({new_count} new)"

    def update_compartments(self, compartments: List[Dict[str, Any]]) -> str:
        """Update compartments in tenancy context. Returns summary string."""
        existing = {c.get("id") for c in self._tenancy_context.get("compartments", [])}
        new_count = sum(1 for c in compartments if c.get("id") not in existing)

        if new_count > 0 or not self._tenancy_context.get("compartments"):
            self._tenancy_context["compartments"] = compartments
            self._save_tenancy_context()

        return f"{len(compartments)} total ({new_count} new)"

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

