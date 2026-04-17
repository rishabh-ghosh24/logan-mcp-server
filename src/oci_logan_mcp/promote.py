# src/oci_logan_mcp/promote.py
"""Promotion pipeline: scan user dirs, promote high-quality queries to shared."""
from __future__ import annotations

import logging
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .file_lock import atomic_yaml_read, atomic_yaml_write, locked_file
from .sanitize import sanitize_query_text
from .user_store import SHARED_CATALOG_LOCK_NAME

logger = logging.getLogger(__name__)

# Promotion thresholds
SINGLE_USER_MIN_INTEREST = 4
SINGLE_USER_MIN_SUCCESS_RATE = 0.80
MULTI_USER_MIN_INTEREST = 3
MULTI_USER_MIN_SUCCESS_RATE = 0.70
MAX_PROMOTED = 100


def should_promote(query: Dict[str, Any], user_count: int = 1) -> bool:
    """Decide if a query qualifies for promotion to shared."""
    total = query.get("success_count", 0) + query.get("failure_count", 0)
    if total == 0:
        return False

    success_rate = query.get("success_count", 0) / total
    interest = query.get("interest_score", 0)

    if user_count >= 2:
        return interest >= MULTI_USER_MIN_INTEREST and success_rate >= MULTI_USER_MIN_SUCCESS_RATE
    else:
        return interest >= SINGLE_USER_MIN_INTEREST and success_rate >= SINGLE_USER_MIN_SUCCESS_RATE


def sanitize_for_sharing(query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Sanitize a query for shared storage. Returns None if unredeemable."""
    q = deepcopy(query)
    cleaned = sanitize_query_text(q["query"])
    if cleaned is None:
        return None
    q["query"] = cleaned
    q["source"] = "shared"
    # Remove user-specific tags
    q.pop("created_at", None)
    q["promoted_at"] = datetime.now(timezone.utc).isoformat()
    return q


def promote_all(base_dir: Path) -> Dict[str, Any]:
    """Scan all user dirs, promote qualifying queries to shared."""
    users_dir = base_dir / "users"
    shared_dir = base_dir / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)

    # Collect all queries across users, tracking user count per query text
    query_map: Dict[str, Dict[str, Any]] = {}  # query_text -> best entry
    user_counts: Dict[str, int] = {}  # query_text -> distinct user count

    if not users_dir.exists():
        return {"promoted": 0, "scanned_users": 0}

    user_count = 0
    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        user_count += 1
        queries_path = user_dir / "learned_queries.yaml"
        data = atomic_yaml_read(queries_path, default={"queries": []})
        for q in data.get("queries", []):
            key = q["query"].strip()
            user_counts[key] = user_counts.get(key, 0) + 1
            # Keep the entry with highest success count
            existing = query_map.get(key)
            if existing is None or q.get("success_count", 0) > existing.get("success_count", 0):
                query_map[key] = q

    # Apply promotion criteria
    promoted = []
    for query_text, q in query_map.items():
        uc = user_counts.get(query_text, 1)
        if should_promote(q, user_count=uc):
            sanitized = sanitize_for_sharing(q)
            if sanitized:
                promoted.append(sanitized)

    # Sort by interest_score desc, cap at MAX_PROMOTED
    promoted.sort(key=lambda q: q.get("interest_score", 0), reverse=True)
    promoted = promoted[:MAX_PROMOTED]

    # Acquire shared-catalog lock before writing (serializes against _check_collision reads)
    shared_lock_path = shared_dir / SHARED_CATALOG_LOCK_NAME
    shared_lock_path.touch(exist_ok=True)
    thread_lock = threading.RLock()

    with locked_file(shared_lock_path, thread_lock):
        # Load existing shared, merge (keep higher quality version)
        existing_shared = atomic_yaml_read(shared_dir / "promoted_queries.yaml", default={"queries": []})
        existing_map = {q["query"].strip(): q for q in existing_shared.get("queries", [])}
        for q in promoted:
            existing_map[q["query"].strip()] = q  # New promotion overwrites

        final = list(existing_map.values())
        final.sort(key=lambda q: q.get("interest_score", 0), reverse=True)
        final = final[:MAX_PROMOTED]

        atomic_yaml_write(shared_dir / "promoted_queries.yaml", {
            "version": 1,
            "last_promoted": datetime.now(timezone.utc).isoformat(),
            "queries": final,
        })

    logger.info(f"Promoted {len(promoted)} queries from {user_count} users")
    return {"promoted": len(promoted), "scanned_users": user_count}
