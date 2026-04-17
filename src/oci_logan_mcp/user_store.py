# src/oci_logan_mcp/user_store.py
"""Per-user query storage with shared template merging."""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .file_lock import atomic_yaml_read, atomic_yaml_write, locked_file

logger = logging.getLogger(__name__)

MAX_QUERIES_PER_USER = 200
SHARED_CATALOG_LOCK_NAME = "catalog.lock"


class UserStore:
    """Manages per-user learned queries with shared overlay."""

    _VALID_USER_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

    def __init__(self, base_dir: Path, user_id: Optional[str] = None) -> None:
        self.base_dir = base_dir
        raw_id = user_id or os.environ.get("LOGAN_USER") or os.environ.get("USER", "default")
        if not self._VALID_USER_RE.match(raw_id):
            raise ValueError(f"Invalid user_id '{raw_id}': must be alphanumeric, _, ., or -")
        self.user_id = raw_id
        self._user_dir = base_dir / "users" / self.user_id
        self._user_dir.mkdir(parents=True, exist_ok=True)
        self._queries_path = self._user_dir / "learned_queries.yaml"
        self._migrate_legacy(base_dir)
        self._lock_path = self._user_dir / "queries.lock"
        self._thread_lock = threading.RLock()
        self._shared_dir = base_dir / "shared"
        self._shared_lock_path = self._shared_dir / SHARED_CATALOG_LOCK_NAME
        # Ensure shared_dir exists so lock file can be created
        self._shared_dir.mkdir(parents=True, exist_ok=True)

    def _migrate_legacy(self, base_dir: Path) -> None:
        """One-time migration: copy legacy context/learned_queries.yaml to user dir."""
        legacy = base_dir / "context" / "learned_queries.yaml"
        target = self._queries_path
        if legacy.exists() and not target.exists():
            shutil.copy2(legacy, target)
            logger.info(f"Migrated legacy learned queries to user store for '{self.user_id}'")

    def save_query(
        self,
        name: str,
        query: str,
        description: str,
        category: str = "general",
        tags: Optional[List[str]] = None,
        interest_score: int = 0,
        force: bool = False,
        rename_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save or update a learned query.

        Returns the saved entry dict, or a dict with 'collision_warning' key if
        the name (case-insensitive) collides with a builtin or shared entry and
        force=False was not specified.
        """
        effective_name = rename_to if rename_to else name
        now = datetime.now(timezone.utc).isoformat()
        with locked_file(self._lock_path, self._thread_lock):
            data = self._load()
            queries = data.get("queries", [])

            # Personal ↔ personal: update in place by name (bypass collision check)
            for q in queries:
                if q["name"] == effective_name:
                    q["query"] = query
                    q["description"] = description
                    q["category"] = category
                    q["tags"] = tags or q.get("tags", [])
                    q["last_used"] = now
                    q["use_count"] = q.get("use_count", 0) + 1
                    q["interest_score"] = max(q.get("interest_score", 0), interest_score)
                    self._save(data)
                    return deepcopy(q)

            # Personal ↔ personal: update in place by query text (bypass collision check)
            for q in queries:
                if q["query"].strip() == query.strip():
                    q["name"] = effective_name
                    q["description"] = description
                    q["category"] = category
                    q["tags"] = tags or q.get("tags", [])
                    q["last_used"] = now
                    q["use_count"] = q.get("use_count", 0) + 1
                    q["interest_score"] = max(q.get("interest_score", 0), interest_score)
                    self._save(data)
                    return deepcopy(q)

            # New entry — run collision check against builtin + shared under shared lock
            if not force:
                collision = self._check_collision(effective_name)
                if collision:
                    return {
                        "collision_warning": {
                            "conflicts_with": collision,
                            "name": effective_name,
                            "message": (
                                f"A {collision} query with name '{effective_name}' already exists. "
                                f"Pass force=True to override, or rename_to='<new_name>' to use a different name."
                            ),
                        }
                    }

            # Create new entry
            entry = {
                "entry_id": uuid.uuid4().hex,
                "name": effective_name,
                "query": query.strip(),
                "description": description,
                "category": category,
                "tags": tags or [],
                "created_at": now,
                "last_used": now,
                "use_count": 1,
                "success_count": 0,
                "failure_count": 0,
                "interest_score": interest_score,
            }
            queries.append(entry)

            # Enforce max limit
            if len(queries) > MAX_QUERIES_PER_USER:
                queries.sort(key=lambda q: (q.get("use_count", 0), q.get("last_used", "")))
                queries = queries[-MAX_QUERIES_PER_USER:]

            data["queries"] = queries
            self._save(data)
            return deepcopy(entry)

    def _check_collision(self, name: str) -> Optional[str]:
        """Check if name (case-insensitive) collides with a builtin or shared entry.

        Returns 'builtin' or 'shared' if collision found, None otherwise.
        Acquires shared/catalog.lock to serialize against promote_all writes.

        Caller must already hold self._lock_path (user's queries.lock).
        Lock order: queries.lock → shared/catalog.lock (never reversed).
        """
        name_lower = name.lower()
        with locked_file(self._shared_lock_path, self._thread_lock):
            # Check shared promoted_queries.yaml (after any in-flight promotion)
            shared_data = atomic_yaml_read(
                self._shared_dir / "promoted_queries.yaml",
                default={"queries": []},
            )
            for q in shared_data.get("queries", []):
                if q.get("name", "").lower() == name_lower:
                    return "shared"

            # Check builtin (immutable at runtime, no lock needed)
            try:
                import importlib.resources
                data_file = importlib.resources.files("oci_logan_mcp") / "data" / "builtin_queries.yaml"
                raw = data_file.read_text(encoding="utf-8")
                builtin_data = yaml.safe_load(raw) or {}
                for q in builtin_data.get("queries", []):
                    if q.get("name", "").lower() == name_lower:
                        return "builtin"
            except Exception:
                pass  # If builtins fail to load, don't block saves
        return None

    def list_queries(
        self, category: Optional[str] = None, tag: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List personal queries with optional filtering."""
        data = self._load()
        queries = data.get("queries", [])
        if category and category != "all":
            queries = [q for q in queries if q.get("category") == category]
        if tag:
            queries = [q for q in queries if tag in q.get("tags", [])]
        return deepcopy(queries)

    def delete_query(self, name: str) -> bool:
        """Delete a query by name."""
        with locked_file(self._lock_path, self._thread_lock):
            data = self._load()
            queries = data.get("queries", [])
            before = len(queries)
            data["queries"] = [q for q in queries if q["name"] != name]
            if len(data["queries"]) < before:
                self._save(data)
                return True
            return False

    def record_usage(self, query: str) -> bool:
        """Bump use_count for a matching query. Returns True if found."""
        with locked_file(self._lock_path, self._thread_lock):
            data = self._load()
            for q in data.get("queries", []):
                if q["query"].strip() == query.strip():
                    q["use_count"] = q.get("use_count", 0) + 1
                    q["last_used"] = datetime.now(timezone.utc).isoformat()
                    self._save(data)
                    return True
            return False

    def record_success(self, query: str) -> None:
        """Record a successful execution for a query."""
        with locked_file(self._lock_path, self._thread_lock):
            data = self._load()
            for q in data.get("queries", []):
                if q["query"].strip() == query.strip():
                    q["success_count"] = q.get("success_count", 0) + 1
                    self._save(data)
                    return

    def record_failure(self, query: str) -> None:
        """Record a failed execution for a query."""
        with locked_file(self._lock_path, self._thread_lock):
            data = self._load()
            for q in data.get("queries", []):
                if q["query"].strip() == query.strip():
                    q["failure_count"] = q.get("failure_count", 0) + 1
                    self._save(data)
                    return

    def list_merged_queries(
        self, category: Optional[str] = None, tag: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Merge shared promoted + personal queries. Personal wins on name duplicates."""
        shared = self._load_shared_queries()
        personal = self.list_queries()

        merged: Dict[str, Dict[str, Any]] = {}
        for q in shared:
            q["source"] = "shared"
            merged[q["name"]] = q
        for q in personal:
            q["source"] = "personal"
            merged[q["name"]] = q  # Personal overrides shared

        result = list(merged.values())
        if category and category != "all":
            result = [q for q in result if q.get("category") == category]
        if tag:
            result = [q for q in result if tag in q.get("tags", [])]
        return result

    def _load_shared_queries(self) -> List[Dict[str, Any]]:
        """Load promoted shared queries."""
        path = self._shared_dir / "promoted_queries.yaml"
        data = atomic_yaml_read(path, default={"queries": []})
        return deepcopy(data.get("queries", []))

    def _backfill_entry_ids(self, data: Dict[str, Any]) -> bool:
        """Assign UUIDs to any entry missing entry_id. Returns True if any entry was modified.
        Idempotent: entries already having entry_id are untouched."""
        modified = False
        for q in data.get("queries", []):
            if not q.get("entry_id"):
                q["entry_id"] = uuid.uuid4().hex
                modified = True
        return modified

    def _load(self) -> Dict[str, Any]:
        data = atomic_yaml_read(self._queries_path, default={"version": 1, "queries": []})
        if self._backfill_entry_ids(data):
            # Acquire lock in case caller doesn't hold it (re-entrant safe for those that do).
            # Double-check pattern: re-read under lock to avoid racing with another process
            # that already backfilled, then re-check and write.
            with locked_file(self._lock_path, self._thread_lock):
                data = atomic_yaml_read(self._queries_path, default={"version": 1, "queries": []})
                if self._backfill_entry_ids(data):
                    atomic_yaml_write(self._queries_path, data)
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        atomic_yaml_write(self._queries_path, data)
