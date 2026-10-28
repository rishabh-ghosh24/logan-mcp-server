# src/oci_logan_mcp/promote.py
"""Promotion pipeline: scan user dirs, promote high-quality queries to shared."""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .file_lock import atomic_yaml_read, atomic_yaml_write, locked_file
from .sanitize import sanitize_query_text, normalize_query_text
from .user_store import SHARED_CATALOG_LOCK_NAME, ensure_entry_ids

logger = logging.getLogger(__name__)

# Promotion thresholds
SINGLE_USER_MIN_INTEREST = 4
SINGLE_USER_MIN_SUCCESS_RATE = 0.80
MULTI_USER_MIN_INTEREST = 3
MULTI_USER_MIN_SUCCESS_RATE = 0.70
MAX_PROMOTED = 100
OVER_CAP_STATUS = "pending: over_cap"
SHARED_SCHEMA_FIELDS = (
    "entry_id", "name", "query", "description", "category", "tags",
    "interest_score", "success_count", "failure_count", "use_count", "user_count",
    "intent_key", "query_shape",
)
_COUNT_FIELDS = ("interest_score", "success_count", "failure_count", "use_count", "user_count")
_CATEGORIES = {"basic", "security", "errors", "performance", "statistics", "network", "audit", "general"}


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return 0


def shared_key(entry: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Identify a shared row by its sanitized name and quote-aware query text."""
    name, query = entry.get("name"), entry.get("query")
    if not isinstance(name, str) or not isinstance(query, str):
        return None
    name, query = sanitize_query_text(name), sanitize_query_text(query)
    if name is None or query is None:
        return None
    return name.lower(), normalize_query_text(query)


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
    if shared_key(query) is None:
        return None
    q = {key: query[key] for key in SHARED_SCHEMA_FIELDS if key in query}
    for key in ("name", "query", "description"):
        value = q.get(key, "")
        q[key] = (sanitize_query_text(value) if isinstance(value, str) else None) or ""
    tags = q.get("tags") or []
    q["tags"] = [clean for tag in (tags if isinstance(tags, list) else [])
                 if isinstance(tag, str) and (clean := sanitize_query_text(tag)) is not None]
    category = q.get("category")
    q["category"] = category if isinstance(category, str) and category in _CATEGORIES else "general"
    for key in ("intent_key", "query_shape"):
        if not isinstance(q.get(key), str) or not re.fullmatch(r"[a-z0-9_]{1,64}", q[key]):
            q.pop(key, None)
    if not isinstance(q.get("entry_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", q["entry_id"]):
        q.pop("entry_id", None)
    for key in _COUNT_FIELDS:
        if key in q:
            q[key] = _integer(q[key])
    q["source"] = "shared"
    q["promoted_at"] = datetime.now(timezone.utc).isoformat()
    return q


_SHARED_CONTENT_FIELDS = (
    "name", "query", "description", "category", "tags",
    "interest_score", "success_count", "failure_count", "user_count",
)


def _shared_entry_content_equal(prior: Dict[str, Any], new: Dict[str, Any]) -> bool:
    """Compare meaningful content fields between a prior shared entry and a
    freshly-sanitized one. Ignores promoted_at, source, and any legacy fields
    not in the current shared schema."""
    key = shared_key(prior)
    return key is not None and key == shared_key(new) and all(
        prior.get(f) == new.get(f) for f in _SHARED_CONTENT_FIELDS
    )


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

    # Phase 1: read one user at a time and build the promotion map.
    # canonical_key -> {"entries_by_user": {user_id: entry}, "users": set, "best": entry}
    promotion_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    user_count = 0
    scan_errors: Dict[str, str] = {}
    scanned_files: Dict[str, Tuple[Path, Optional[int]]] = {}
    keys_by_entry_id: Dict[str, set] = {}
    status_updates_by_user: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for user_dir in sorted(users_dir.iterdir()):
        if not user_dir.is_dir():
            continue
        user_count += 1
        user_id = user_dir.name
        queries_path = user_dir / "learned_queries.yaml"
        lock_path = user_dir / "queries.lock"
        # Backfill entry_id for any legacy (pre-1.2.0) rows before scanning.
        # ensure_entry_ids persists IDs under the user's queries.lock so status
        # write-back in phase 4 can match entries by entry_id deterministically.
        try:
            with locked_file(lock_path, threading.RLock()):
                data = ensure_entry_ids(queries_path, lock_path, strict=True)
                mtime = queries_path.stat().st_mtime_ns if queries_path.exists() else None
                scanned_files[user_id] = (queries_path, mtime)
        except Exception as error:
            scan_errors[user_id] = f"read_failed: {type(error).__name__}"
            logger.warning("Promotion scan failed for user %s (%s)", user_id, type(error).__name__)
            continue

        for q in data.get("queries", []):
            if not q.get("name") or not q.get("query") or not q.get("entry_id"):
                continue  # defensive: entry_id should be present after backfill

            canonical_key = shared_key(q)
            if canonical_key is None:
                status_updates_by_user.setdefault(user_id, {})[q["entry_id"]] = {
                    "promotion_status": "rejected: sanitization_failed",
                    "promotion_reason": "Name or query could not be sanitized for sharing",
                }
                continue
            keys_by_entry_id.setdefault(q["entry_id"], set()).add(canonical_key)
            q = dict(q)
            for field in _COUNT_FIELDS:
                if field in q:
                    q[field] = _integer(q[field])
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

    # Phase 2: evaluate each canonical key for qualification + sanitization.
    # Promotion candidates are held pending the name-collision resolution
    # in phase 2.5 — we intentionally do NOT prune name collisions up front,
    # because the highest-scoring variant can still fail sanitization, in
    # which case a lower-scoring clean variant with the same name must take
    # over instead of being silently dropped.
    now = datetime.now(timezone.utc).isoformat()
    # canonical_key -> {"sanitized": entry, "bucket": bucket, "success_rate": float,
    #                    "interest": int, "uc": int, "total_success": int, "total_failure": int}
    promote_candidates: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for canonical_key, bucket in promotion_map.items():
        best = bucket["best"]
        uc = len(bucket["users"])

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
                # Sanitization failed — this canonical key is out; a name-
                # collision peer may still qualify in phase 2.5.
                for uid, entry in bucket["entries_by_user"].items():
                    status_updates_by_user.setdefault(uid, {})[entry["entry_id"]] = {
                        "promotion_status": "rejected: sanitization_failed",
                        "promotion_reason": "Query contained content that could not be sanitized for sharing",
                    }
                continue
            sanitized["success_count"] = total_success
            sanitized["failure_count"] = total_failure
            sanitized["interest_score"] = interest
            sanitized["user_count"] = uc
            promote_candidates[canonical_key] = {
                "sanitized": sanitized,
                "bucket": bucket,
                "success_rate": success_rate,
                "interest": interest,
                "uc": uc,
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

    # Phase 2.5: resolve cross-user name collisions among *qualified+sanitized*
    # candidates only. Losers that already survived qualification and
    # sanitization are flipped to 'rejected: name_collision_cross_user'.
    # Candidates that failed earlier stages were never here to begin with, so
    # their spot can be reclaimed by a lower-scoring clean peer.
    name_groups: Dict[str, List[Tuple[str, str]]] = {}
    for ck in promote_candidates:
        name_groups.setdefault(ck[0], []).append(ck)

    promoted: List[Dict[str, Any]] = []
    winners = []
    for name_lower, keys in name_groups.items():
        if len(keys) == 1:
            winner_key = keys[0]
        else:
            def score_key(ck: Tuple[str, str]) -> Tuple[int, float]:
                c = promote_candidates[ck]
                return (c["interest"], c["success_rate"])
            winner_key = max(keys, key=score_key)
            winner_interest = promote_candidates[winner_key]["interest"]
            for ck in keys:
                if ck == winner_key:
                    continue
                loser_bucket = promote_candidates[ck]["bucket"]
                for uid, entry in loser_bucket["entries_by_user"].items():
                    status_updates_by_user.setdefault(uid, {})[entry["entry_id"]] = {
                        "promotion_status": "rejected: name_collision_cross_user",
                        "promotion_reason": (
                            f"Another user's query with name '{name_lower}' scored higher "
                            f"(interest_score={winner_interest}) and was promoted instead."
                        ),
                    }

        winner = promote_candidates[winner_key]
        promoted.append(winner["sanitized"])
        winners.append(winner)

    # Phase 3: write shared file (under shared lock)
    promoted.sort(key=lambda q: q.get("interest_score", 0), reverse=True)
    promoted = promoted[:MAX_PROMOTED]

    shared_lock_path = shared_dir / SHARED_CATALOG_LOCK_NAME
    shared_lock_path.touch(exist_ok=True)
    thread_lock = threading.RLock()

    with locked_file(shared_lock_path, thread_lock):
        existing_shared = atomic_yaml_read(
            shared_dir / "promoted_queries.yaml", default={"queries": []}, strict=True,
        )
        existing_by_canonical = {}
        for row in existing_shared.get("queries", []):
            clean = sanitize_for_sharing(row)
            if clean is None:
                continue
            prior_time = row.get("promoted_at")
            if isinstance(prior_time, str):
                try:
                    clean["promoted_at"] = datetime.fromisoformat(prior_time).isoformat()
                except ValueError:
                    pass
            existing_by_canonical[shared_key(clean)] = clean
        for p in promoted:
            ck = shared_key(p)
            name_lower = ck[0]
            # Evict stale same-name entries whose canonical key differs from
            # the new winner. This run's phase 2.5 already chose exactly one
            # winner per name among qualified candidates and flipped the losers'
            # per-user statuses to 'rejected: name_collision_cross_user'; the
            # shared catalog must reflect that decision. Otherwise a previously
            # promoted same-name entry from an earlier run lingers and
            # contradicts the per-user state.
            for stale_key in [k for k in existing_by_canonical if k[0] == name_lower and k != ck]:
                del existing_by_canonical[stale_key]
            # Preserve existing promoted_at when the meaningful content is
            # unchanged, so the field reflects last meaningful update (not
            # last scan). Lets consumers distinguish stagnant shared catalogs
            # from actively-churning ones.
            prior = existing_by_canonical.get(ck)
            if prior is not None and _shared_entry_content_equal(prior, p):
                p["promoted_at"] = prior.get("promoted_at", p["promoted_at"])
            existing_by_canonical[ck] = p

        # Recheck under the shared lock, immediately before withdrawal and write.
        for uid, (path, mtime) in scanned_files.items():
            try:
                current = path.stat().st_mtime_ns if path.exists() else None
                if current != mtime:
                    raise ValueError("changed_during_scan")
            except (OSError, ValueError):
                scan_errors[uid] = "changed_during_scan"
                logger.warning("Promotion store changed during scan for user %s", uid)
        rejected_keys = {
            key for key, bucket in promotion_map.items()
            if any(status_updates_by_user.get(uid, {}).get(entry["entry_id"], {})
                   .get("promotion_status", "").startswith("rejected:")
                   for uid, entry in bucket["entries_by_user"].items())
        }
        for key, row in list(existing_by_canonical.items()):
            personal_keys = keys_by_entry_id.get(row.get("entry_id"), set())
            replaced = bool(personal_keys) and key not in personal_keys
            absent = not scan_errors and key not in promotion_map
            if key in rejected_keys or replaced or absent:
                del existing_by_canonical[key]
        final = list(existing_by_canonical.values())
        final.sort(key=lambda q: q.get("interest_score", 0), reverse=True)
        final = final[:MAX_PROMOTED]

        atomic_yaml_write(shared_dir / "promoted_queries.yaml", {
            "version": 1,
            "queries": final,
            "promoted_at": now,
        })

    final_keys = {shared_key(q) for q in final}
    for winner in winners:
        sanitized = winner["sanitized"]
        key = shared_key(sanitized)
        if key in final_keys:
            update = {
                "promotion_status": "promoted",
                "promotion_reason": (
                    f"Promoted to shared catalog (users={winner['uc']}, "
                    f"success_rate={winner['success_rate']:.2f})"
                ),
                "promoted_at": now,
            }
        else:
            update = {
                "promotion_status": OVER_CAP_STATUS,
                "promotion_reason": (
                    f"Qualified, but the shared catalog cap of {MAX_PROMOTED} was reached; "
                    "retried on the next run"
                ),
            }
        for uid, entry in winner["bucket"]["entries_by_user"].items():
            status_updates_by_user.setdefault(uid, {})[entry["entry_id"]] = update

    # Phase 4: per-user status write-back (serial, one lock at a time)
    for user_id, updates in status_updates_by_user.items():
        user_dir = users_dir / user_id
        _write_back_status(user_dir, updates)

    logger.info(f"Promoted {len(promoted)} queries from {user_count} users")
    result = {
        "promoted": len(promoted),
        "scanned_users": user_count,
        "status_updated_users": len(status_updates_by_user),
        "scan_errors": scan_errors,
    }
    if scan_errors:
        result["withdrawal_skipped"] = "incomplete_scan"
    return result
