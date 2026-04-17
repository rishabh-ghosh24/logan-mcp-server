# src/oci_logan_mcp/promote.py
"""Promotion pipeline: scan user dirs, promote high-quality queries to shared."""
from __future__ import annotations

import logging
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .file_lock import atomic_yaml_read, atomic_yaml_write, locked_file
from .sanitize import sanitize_query_text, normalize_query_text
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


def _write_back_status(user_dir: Path, status_updates: Dict[str, Dict[str, Any]]) -> None:
    """Write promotion status/reason back to a user's learned_queries.yaml.

    status_updates: {entry_id: {"promotion_status": "...", "promotion_reason": "...", "promoted_at": "..."}}

    Re-reads the YAML under the user's queries.lock, patches only the status
    fields for matching entry_ids, writes back atomically.
    Entries not present in the YAML are silently skipped.
    """
    queries_path = user_dir / "learned_queries.yaml"
    lock_path = user_dir / "queries.lock"
    if not queries_path.exists() or not status_updates:
        return

    thread_lock = threading.RLock()
    with locked_file(lock_path, thread_lock):
        data = atomic_yaml_read(queries_path, default={"queries": []})
        modified = False
        for q in data.get("queries", []):
            eid = q.get("entry_id")
            if eid and eid in status_updates:
                for field, value in status_updates[eid].items():
                    q[field] = value
                modified = True
        if modified:
            atomic_yaml_write(queries_path, data)


def promote_all(base_dir: Path) -> Dict[str, Any]:
    """Scan all user dirs, promote qualifying queries to shared, write status back."""
    users_dir = base_dir / "users"
    shared_dir = base_dir / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)

    if not users_dir.exists():
        return {"promoted": 0, "scanned_users": 0}

    # Phase 1: scan (unlocked) — build promotion map
    # canonical_key -> {"entries_by_user": {user_id: entry}, "users": set, "best": entry}
    promotion_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    user_count = 0

    for user_dir in sorted(users_dir.iterdir()):
        if not user_dir.is_dir():
            continue
        user_count += 1
        user_id = user_dir.name
        queries_path = user_dir / "learned_queries.yaml"
        data = atomic_yaml_read(queries_path, default={"queries": []})

        for q in data.get("queries", []):
            if not q.get("name") or not q.get("query") or not q.get("entry_id"):
                continue  # legacy entries without entry_id skip

            canonical_key = (q["name"].lower(), normalize_query_text(q["query"]))
            bucket = promotion_map.setdefault(canonical_key, {
                "entries_by_user": {},
                "users": set(),
                "best": None,
            })
            bucket["entries_by_user"][user_id] = q
            bucket["users"].add(user_id)
            # Track best representative (highest interest_score across all users)
            if bucket["best"] is None or q.get("interest_score", 0) > bucket["best"].get("interest_score", 0):
                bucket["best"] = q

    # Phase 1.5: name-level collision detection across different canonical keys
    name_to_keys: Dict[str, List[Tuple[str, str]]] = {}
    for ck in promotion_map.keys():
        name_lower = ck[0]
        name_to_keys.setdefault(name_lower, []).append(ck)

    name_collision_losers: Dict[str, Dict[str, Dict[str, Any]]] = {}  # user_id -> entry_id -> update

    for name_lower, keys in name_to_keys.items():
        if len(keys) <= 1:
            continue
        # Multiple different queries share this name — pick winner by (interest_score, success_rate)
        def score_key(ck: Tuple[str, str]) -> Tuple[float, float]:
            b = promotion_map[ck]
            total = sum(e.get("success_count", 0) + e.get("failure_count", 0) for e in b["entries_by_user"].values())
            successes = sum(e.get("success_count", 0) for e in b["entries_by_user"].values())
            rate = successes / total if total > 0 else 0.0
            return (b["best"].get("interest_score", 0), rate)

        winner_key = max(keys, key=score_key)
        for ck in keys:
            if ck == winner_key:
                continue
            winner_best = promotion_map[winner_key]["best"]
            for uid, entry in promotion_map[ck]["entries_by_user"].items():
                name_collision_losers.setdefault(uid, {})[entry["entry_id"]] = {
                    "promotion_status": "rejected: name_collision_cross_user",
                    "promotion_reason": (
                        f"Another user's query with name '{name_lower}' scored higher "
                        f"(interest_score={winner_best.get('interest_score', 0)}) and was promoted instead."
                    ),
                }
            del promotion_map[ck]

    # Phase 2: decide promotion outcomes, build per-user status updates
    now = datetime.now(timezone.utc).isoformat()
    promoted = []
    status_updates_by_user: Dict[str, Dict[str, Dict[str, Any]]] = {}

    # Seed with name-collision losers
    for uid, entries in name_collision_losers.items():
        status_updates_by_user.setdefault(uid, {}).update(entries)

    for canonical_key, bucket in promotion_map.items():
        best = bucket["best"]
        uc = len(bucket["users"])

        # Aggregate metrics across users
        total_success = sum(e.get("success_count", 0) for e in bucket["entries_by_user"].values())
        total_failure = sum(e.get("failure_count", 0) for e in bucket["entries_by_user"].values())
        total = total_success + total_failure
        success_rate = total_success / total if total > 0 else 0.0
        interest = best.get("interest_score", 0)

        decision_reason = None
        promoted_flag = False

        if total == 0:
            decision_reason = "No execution data yet"
        elif uc >= 2:
            if interest >= MULTI_USER_MIN_INTEREST and success_rate >= MULTI_USER_MIN_SUCCESS_RATE:
                promoted_flag = True
            else:
                decision_reason = (
                    f"Multi-user threshold not met: interest_score={interest} "
                    f"(need {MULTI_USER_MIN_INTEREST}), success_rate={success_rate:.2f} "
                    f"(need {MULTI_USER_MIN_SUCCESS_RATE})"
                )
        else:  # single user
            if interest >= SINGLE_USER_MIN_INTEREST and success_rate >= SINGLE_USER_MIN_SUCCESS_RATE:
                promoted_flag = True
            else:
                decision_reason = (
                    f"Single-user threshold not met: interest_score={interest} "
                    f"(need {SINGLE_USER_MIN_INTEREST}), success_rate={success_rate:.2f} "
                    f"(need {SINGLE_USER_MIN_SUCCESS_RATE})"
                )

        if promoted_flag:
            sanitized = sanitize_for_sharing(best)
            if sanitized is None:
                status = "rejected: sanitization_failed"
                reason = "Query contained content that could not be sanitized for sharing"
                for uid, entry in bucket["entries_by_user"].items():
                    status_updates_by_user.setdefault(uid, {})[entry["entry_id"]] = {
                        "promotion_status": status,
                        "promotion_reason": reason,
                    }
                continue
            sanitized["success_count"] = total_success
            sanitized["failure_count"] = total_failure
            sanitized["interest_score"] = interest
            promoted.append(sanitized)
            for uid, entry in bucket["entries_by_user"].items():
                status_updates_by_user.setdefault(uid, {})[entry["entry_id"]] = {
                    "promotion_status": "promoted",
                    "promotion_reason": f"Promoted to shared catalog (users={uc}, success_rate={success_rate:.2f})",
                    "promoted_at": now,
                }
        else:
            if decision_reason and decision_reason.startswith("No execution"):
                status = "pending"
            else:
                min_sr = MULTI_USER_MIN_SUCCESS_RATE if uc >= 2 else SINGLE_USER_MIN_SUCCESS_RATE
                if success_rate < min_sr:
                    status = "rejected: low_success_rate"
                else:
                    status = "rejected: low_interest_score"
            for uid, entry in bucket["entries_by_user"].items():
                status_updates_by_user.setdefault(uid, {})[entry["entry_id"]] = {
                    "promotion_status": status,
                    "promotion_reason": decision_reason,
                }

    # Phase 3: write shared file (under shared lock)
    promoted.sort(key=lambda q: q.get("interest_score", 0), reverse=True)
    promoted = promoted[:MAX_PROMOTED]

    shared_lock_path = shared_dir / SHARED_CATALOG_LOCK_NAME
    shared_lock_path.touch(exist_ok=True)
    thread_lock = threading.RLock()

    with locked_file(shared_lock_path, thread_lock):
        existing_shared = atomic_yaml_read(shared_dir / "promoted_queries.yaml", default={"queries": []})
        existing_by_canonical = {
            (q.get("name", "").lower(), normalize_query_text(q.get("query", ""))): q
            for q in existing_shared.get("queries", [])
        }
        for p in promoted:
            ck = (p["name"].lower(), normalize_query_text(p["query"]))
            existing_by_canonical[ck] = p
        final = list(existing_by_canonical.values())
        final.sort(key=lambda q: q.get("interest_score", 0), reverse=True)
        final = final[:MAX_PROMOTED]

        atomic_yaml_write(shared_dir / "promoted_queries.yaml", {
            "version": 1,
            "queries": final,
            "promoted_at": now,
        })

    # Phase 4: per-user status write-back (serial, one lock at a time)
    for user_id, updates in status_updates_by_user.items():
        user_dir = users_dir / user_id
        _write_back_status(user_dir, updates)

    logger.info(f"Promoted {len(promoted)} queries from {user_count} users")
    return {
        "promoted": len(promoted),
        "scanned_users": user_count,
        "status_updated_users": len(status_updates_by_user),
    }
