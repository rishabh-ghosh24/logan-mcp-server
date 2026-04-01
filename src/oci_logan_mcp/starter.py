# src/oci_logan_mcp/starter.py
"""Load curated starter query examples from packaged YAML data."""
from __future__ import annotations

import importlib.resources
import logging
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"basic", "security", "errors", "performance", "statistics"}

# Module-level cache — loaded once per process, never re-read.
_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None
_cache_loaded: bool = False


def load_starter_queries() -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Load starter queries from the packaged YAML file.

    Returns a dict of {category: [examples]} or None on any failure,
    so the caller can fall back to hardcoded examples. Result is cached
    after first call.
    """
    global _cache, _cache_loaded
    if _cache_loaded:
        return _cache

    _cache_loaded = True
    try:
        data_files = importlib.resources.files("oci_logan_mcp") / "data" / "starter_queries.yaml"
        raw = data_files.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)

        if not isinstance(data, dict) or "queries" not in data:
            logger.warning("starter_queries.yaml: missing 'queries' key")
            return None

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for entry in data["queries"]:
            if not isinstance(entry, dict):
                logger.warning(f"starter_queries.yaml: skipping non-dict entry: {entry!r}")
                continue
            cat = entry.get("category", "")
            if cat not in VALID_CATEGORIES:
                logger.warning(f"starter_queries.yaml: skipping entry with unknown category '{cat}'")
                continue
            if not all(k in entry for k in ("name", "query", "description")):
                logger.warning(f"starter_queries.yaml: skipping entry missing required keys: {entry}")
                continue
            grouped.setdefault(cat, []).append({
                "name": entry["name"],
                "query": entry["query"],
                "description": entry["description"],
            })

        if not grouped:
            logger.warning("starter_queries.yaml: no valid entries found")
            return None

        _cache = grouped
        return _cache

    except Exception as e:
        logger.warning(f"Failed to load starter queries: {e}")
        return None
