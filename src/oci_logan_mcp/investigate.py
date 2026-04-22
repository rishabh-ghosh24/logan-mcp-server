"""investigate_incident (A1) — orchestrator that composes triage primitives
into a structured first-cut investigation report.

Design: docs/phase-2/specs/2026-04-22-a1-investigate-incident-design.md
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .budget_tracker import BudgetExceededError
from .time_parser import TIME_RANGES
from . import next_steps as _next_steps


def _extract_seed_filter(query: str) -> str:
    """Return the pre-pipe search clause of a seed query, quote-aware.

    Scans char-by-char tracking single-quote and double-quote string context,
    handling OCI LA's doubled-quote escape (e.g. 'O''Brien'). Pipes inside
    quoted literals do not terminate the filter clause.

    Returns '*' for empty or effectively-unscoped inputs.

    P0 limitation (from design §2.2): only the pre-pipe clause is preserved.
    Later pipeline stages (where, eval, stats, etc.) are dropped for
    drill-down scoping.
    """
    if not query:
        return "*"

    in_single = in_double = False
    i, end = 0, len(query)

    while i < end:
        c = query[i]
        if in_single:
            if c == "'":
                if i + 1 < end and query[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if c == '"':
                if i + 1 < end and query[i + 1] == '"':
                    i += 2
                    continue
                in_double = False
        else:
            if c == "'":
                in_single = True
            elif c == '"':
                in_double = True
            elif c == "|":
                break
        i += 1

    f = query[:i].strip()
    return "*" if not f or f == "*" else f


def _compose_source_scoped_query(seed_filter: str, source: str, tail: str) -> str:
    """Compose a per-source query with boolean-precedence safety.

    Wraps `seed_filter` in parens so that seeds containing `or`/mixed
    precedence don't let rows escape the source constraint.

    Special case: `seed_filter == "*"` emits just the source predicate
    (no parens, no `and`). Logan doesn't accept `(*)`.

    `tail` is the pipeline tail appended after `| ` — e.g. `"cluster | sort -Count | head 3"`.
    `source` is quote-escaped via single-quote doubling.
    """
    escaped_source = source.replace("'", "''")
    src_pred = f"'Log Source' = '{escaped_source}'"
    if seed_filter == "*":
        base = src_pred
    else:
        base = f"({seed_filter}) and {src_pred}"
    return f"{base} | {tail}"


def _compute_windows(
    time_range: str, anchor: datetime,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Compute current and comparison windows as absolute ISO timestamps.

    Both windows derive from the single `anchor` so they are guaranteed
    equal-length and zero-gap adjacent (comparison.end == current.start).
    This defends against the drift that would occur if the current window
    were passed as a relative `time_range` token — `parse_time_range()`
    captures its own `now` inside the engine at query time, which differs
    from A1's anchor by the wall-clock latency of intervening phases.

    Raises ValueError if `time_range` isn't in TIME_RANGES.
    """
    if time_range not in TIME_RANGES:
        raise ValueError(
            f"Unknown time_range: {time_range}. "
            f"Valid: {sorted(TIME_RANGES.keys())}"
        )
    delta = TIME_RANGES[time_range]
    current = {
        "time_start": (anchor - delta).isoformat(),
        "time_end":   anchor.isoformat(),
    }
    comparison = {
        "time_start": (anchor - 2 * delta).isoformat(),
        "time_end":   (anchor - delta).isoformat(),
    }
    return current, comparison


def _rank_anomalous_sources(
    delta: List[Dict[str, Any]],
    stopped_sources: Set[str],
    top_k: int,
) -> List[Dict[str, Any]]:
    """Rank DiffTool delta entries by absolute pct_change, excluding stopped sources.

    For rows where `pct_change` is None (comparison was zero), fall back
    to absolute `current` count for ordering.
    """
    def rank_key(entry: Dict[str, Any]) -> float:
        pct = entry.get("pct_change")
        if pct is None:
            return abs(float(entry.get("current") or 0))
        return abs(float(pct))

    filtered = [
        e for e in delta
        if str(e.get("dimension")) not in stopped_sources
    ]
    sorted_entries = sorted(filtered, key=rank_key, reverse=True)
    out = []
    for e in sorted_entries[:top_k]:
        out.append({
            "source": str(e["dimension"]),
            "current_count": int(e.get("current") or 0),
            "comparison_count": int(e.get("comparison") or 0),
            "pct_change": e.get("pct_change"),
        })
    return out


def _select_top_entities(
    response: Dict[str, Any],
    entity_type: str,
    field_name: str,
) -> List[Dict[str, Any]]:
    """Parse a `| stats count as n by '<field>'` response into entity entries.

    Returns an empty list if the response is malformed, missing the
    expected column, or if all rows have null entity values. Skips
    individual rows where the entity value is None; defaults a None
    count to 0.
    """
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", []) or []]
    rows = data.get("rows", []) or []
    if field_name not in columns or "n" not in columns:
        return []
    field_idx = columns.index(field_name)
    count_idx = columns.index("n")
    max_idx = max(field_idx, count_idx)
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not row or len(row) <= max_idx:
            continue
        value = row[field_idx]
        if value is None:
            continue
        count = row[count_idx]
        out.append({
            "entity_type": entity_type,
            "entity_value": str(value),
            "count": int(count) if count is not None else 0,
        })
    return out


def _merge_cross_source_timeline(
    per_source: Dict[str, Optional[List[Dict[str, Any]]]],
    cap: int,
) -> Optional[List[Dict[str, Any]]]:
    """Merge per-source timelines into one time-sorted stream.

    Returns:
      - None if every source's timeline is None (all dropped) OR input is empty
      - [] if every source ran but produced zero rows
      - Sorted list (up to `cap` entries) otherwise

    Distinguishes "timeline was dropped" (None) from "timeline returned
    zero rows" (empty list).
    """
    if not per_source:
        return None

    # All-None → dropped-timeline semantic.
    non_null = {s: t for s, t in per_source.items() if t is not None}
    if not non_null:
        return None

    merged: List[Dict[str, Any]] = []
    for source, rows in non_null.items():
        for row in rows:
            merged.append({
                "time": row["time"],
                "source": source,
                "severity": row.get("severity"),
                "message": row.get("message", ""),
            })
    merged.sort(key=lambda r: r["time"])
    return merged[:cap]


def _utcnow() -> datetime:
    """Return current UTC time. Seam for deterministic testing."""
    return datetime.now(timezone.utc)


TOP_K_MIN = 1
TOP_K_MAX = 3
TIMELINE_CAP = 50

# Entity discovery uses the same field names as pivot_tool's ENTITY_FIELD_MAP,
# but A1 P0 uses only three (ip deferred to P1 to bound per-source query count).
A1_ENTITY_FIELDS = [
    ("host", "Host Name (Server)"),
    ("user", "User Name"),
    ("request_id", "Request ID"),
]
PER_SOURCE_CONCURRENCY = 2
CLUSTER_HEAD = 3
ENTITY_HEAD = 5
TIMELINE_HEAD = 20


def _is_field_variance_error(exc: Exception) -> bool:
    """True iff this exception represents OCI LA rejecting a field that
    doesn't exist on the target source/tenancy (e.g. `'User Name'` in a
    tenancy that only has `'Host Name (Server)'`).

    This is the ONLY exception class the design lets us silently downgrade
    to `entity_discovery_partial`. Transport, auth, and 5xx failures must
    surface as `source_errors`, not masquerade as field variance.
    """
    try:
        from oci.exceptions import ServiceError
    except ImportError:
        return False
    if not isinstance(exc, ServiceError):
        return False
    code = getattr(exc, "code", "") or ""
    message = getattr(exc, "message", "") or ""
    return code == "InvalidParameter" and "Invalid field" in message


def _parse_cluster_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse a `| cluster | sort -Count | head N` response.

    Real OCI LA `cluster` returns 14+ columns (Cluster Sample, Count, Problem
    Priority, Score, etc.). We surface the three most actionable ones.
    """
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", []) or []]
    rows = data.get("rows", []) or []
    if "Cluster Sample" not in columns or "Count" not in columns:
        return []
    sample_idx = columns.index("Cluster Sample")
    count_idx = columns.index("Count")
    prio_idx = columns.index("Problem Priority") if "Problem Priority" in columns else None
    out: List[Dict[str, Any]] = []
    max_idx = max(sample_idx, count_idx, prio_idx if prio_idx is not None else 0)
    for row in rows:
        if not row or len(row) <= max_idx:
            continue
        cnt = row[count_idx]
        prio = row[prio_idx] if prio_idx is not None else None
        out.append({
            "pattern": str(row[sample_idx]) if row[sample_idx] is not None else "",
            "count": int(cnt) if cnt is not None else 0,
            "problem_priority": int(prio) if prio is not None else None,
        })
    return out


def _parse_timeline_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse a `| fields Time, Severity, 'Original Log Content' | sort -Time | head N` response.

    Normalizes Time through _parse_ts so epoch-ms LONGs become ISO strings.
    """
    from .ingestion_health import _parse_ts
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", []) or []]
    rows = data.get("rows", []) or []
    if "Time" not in columns or "Original Log Content" not in columns:
        return []
    t_idx = columns.index("Time")
    sev_idx = columns.index("Severity") if "Severity" in columns else None
    msg_idx = columns.index("Original Log Content")
    max_idx = max(i for i in (t_idx, sev_idx, msg_idx) if i is not None)
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not row or len(row) <= max_idx:
            continue
        t = _parse_ts(row[t_idx])
        if t is None:
            continue
        sev = row[sev_idx] if sev_idx is not None else None
        out.append({
            "time": t.isoformat(),
            "severity": str(sev) if sev is not None else None,
            "message": str(row[msg_idx]) if row[msg_idx] is not None else "",
        })
    return out


async def _drill_down_one_source(
    engine,
    source: str,
    seed_filter: str,
    time_range: str,
    compartment_id: Optional[str],
) -> Dict[str, Any]:
    """Run cluster + entity discovery + timeline for a single source, sequentially.

    Returns a dict with keys:
      - top_error_clusters, top_entities, timeline, errors
      - entity_discovery_partial: True iff at least one entity-field query
        failed with the recognized field-variance shape (ServiceError
        code=InvalidParameter, message contains "Invalid field")
      - timeline_omitted: True iff the timeline query failed (best-effort
        per design)
      - infra_error: True iff any non-field-variance, non-Budget exception
        was caught (cluster error, unexpected entity-query failure).
        Timeline failures do NOT set infra_error — they map to
        timeline_omitted via their own path.

    Re-raises `BudgetExceededError` so the orchestrator can distinguish
    it from generic source failures when handling the gather result.
    """
    result = {
        "top_error_clusters": [],
        "top_entities": [],
        "timeline": None,
        "errors": [],
        "entity_discovery_partial": False,
        "timeline_omitted": False,
        "infra_error": False,
    }

    # Cluster — any non-Budget failure is infrastructure. Record it but
    # don't abort the branch; entity/timeline may still succeed.
    cluster_query = _compose_source_scoped_query(
        seed_filter, source, f"cluster | sort -Count | head {CLUSTER_HEAD}",
    )
    try:
        cluster_resp = await engine.execute(
            query=cluster_query, time_range=time_range, compartment_id=compartment_id,
        )
        result["top_error_clusters"] = _parse_cluster_response(cluster_resp)
    except BudgetExceededError:
        raise
    except Exception as e:
        result["errors"].append(f"cluster: {type(e).__name__}: {e}")
        result["infra_error"] = True

    # Entity discovery (3 fields, sequential). Distinguish field-variance
    # (soft) from infrastructure failures (hard, but non-fatal to branch).
    for entity_type, field_name in A1_ENTITY_FIELDS:
        entity_query = _compose_source_scoped_query(
            seed_filter, source,
            f"stats count as n by '{field_name}' | sort -n | head {ENTITY_HEAD}",
        )
        try:
            entity_resp = await engine.execute(
                query=entity_query, time_range=time_range, compartment_id=compartment_id,
            )
            result["top_entities"].extend(
                _select_top_entities(entity_resp, entity_type, field_name)
            )
        except BudgetExceededError:
            raise
        except Exception as e:
            if _is_field_variance_error(e):
                result["errors"].append(
                    f"top_entities[{entity_type}]: field '{field_name}' "
                    f"not present in this source (InvalidParameter)"
                )
                result["entity_discovery_partial"] = True
            else:
                result["errors"].append(
                    f"top_entities[{entity_type}]: {type(e).__name__}: {e}"
                )
                result["infra_error"] = True

    # Timeline — best-effort per design. Any non-Budget error is non-fatal
    # and maps to timeline_omitted (NOT infra_error; timeline is the
    # lowest-value sub-phase).
    timeline_query = _compose_source_scoped_query(
        seed_filter, source,
        f"fields Time, Severity, 'Original Log Content' | sort -Time | head {TIMELINE_HEAD}",
    )
    try:
        tl_resp = await engine.execute(
            query=timeline_query, time_range=time_range, compartment_id=compartment_id,
        )
        result["timeline"] = _parse_timeline_response(tl_resp)
    except BudgetExceededError:
        raise
    except Exception as e:
        result["errors"].append(f"timeline: {type(e).__name__}: {e}")
        result["timeline_omitted"] = True

    return result


def _templated_summary(acc: Dict[str, Any]) -> str:
    """Render a 1-2 sentence human-readable summary from the accumulator."""
    seed = acc["seed"]
    scope = "unscoped (seed filter degraded to *)" if seed.get("seed_filter_degraded") else seed["seed_filter"]
    time_range = seed["time_range"]

    ih_summary = ((acc.get("ingestion_health") or {}).get("snapshot") or {}).get("summary") or {}
    stopped = int(ih_summary.get("sources_stopped", 0) or 0)
    parse_count = int((acc.get("parser_failures") or {}).get("total_failure_count", 0) or 0)
    anomalous = acc.get("anomalous_sources") or []

    parts = [f"Investigated {scope} over {time_range}."]
    if anomalous:
        top = anomalous[0]
        parts.append(
            f"{len(anomalous)} anomalous source(s) (top: {top['source']} "
            f"pct_change={top.get('pct_change')})."
        )
    else:
        parts.append("No anomalous sources detected.")

    if stopped:
        parts.append(f"J1 flags {stopped} stopped source(s).")
    if parse_count:
        parts.append(f"J2 reports {parse_count} parse failure(s).")

    reasons = acc.get("partial_reasons") or set()
    if reasons:
        parts.append(f"Result is partial: {', '.join(sorted(reasons))}.")

    return " ".join(parts)


class InvestigateIncidentTool:
    """Orchestrator for A1 investigate_incident.

    Composes J1 (ingestion_health), J2 (parser_failure_triage),
    A2 (diff_time_windows), and Logan's native `cluster` command into a
    first-cut structured investigation. See
    `docs/phase-2/specs/2026-04-22-a1-investigate-incident-design.md`.
    """

    def __init__(
        self,
        query_engine,
        schema_manager,
        ingestion_health_tool,
        parser_triage_tool,
        diff_tool,
        settings,
        budget_tracker,
    ):
        self._engine = query_engine
        self._schema = schema_manager
        self._ih_tool = ingestion_health_tool
        self._j2_tool = parser_triage_tool
        self._diff_tool = diff_tool
        self._settings = settings
        self._budget = budget_tracker

    async def run(
        self,
        query: str,
        time_range: str = "last_1_hour",
        top_k: int = 3,
        compartment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if top_k < TOP_K_MIN or top_k > TOP_K_MAX:
            raise ValueError(
                f"top_k must be in [{TOP_K_MIN}, {TOP_K_MAX}] for P0; got {top_k}"
            )

        seed_filter = _extract_seed_filter(query)
        acc: Dict[str, Any] = {
            "seed": {
                "query": query,
                "seed_filter": seed_filter,
                "seed_filter_degraded": seed_filter == "*",
                "time_range": time_range,
                "compartment_id": compartment_id,
            },
            "seed_result": None,
            "ingestion_health": None,
            "parser_failures": None,
            "diff": None,
            "anomalous_sources": [],
            "per_source": {},
            "partial_reasons": set(),
            "source_errors": [],
            "start_time": _utcnow(),
            "budget_snapshot": None,
        }
        try:
            # Phase 1: seed query (result stored for subsequent phases).
            acc["seed_result"] = await self._engine.execute(
                query=query,
                time_range=time_range,
                compartment_id=compartment_id,
            )
            # Phase 2 — J1 freshness snapshot (configured probe window, not investigation window)
            j1_snapshot = await self._ih_tool.run(
                compartment_id=compartment_id,
                severity_filter="all",
            )
            probe_window = self._settings.ingestion_health.freshness_probe_window
            acc["ingestion_health"] = {
                "snapshot": j1_snapshot,
                "probe_window": probe_window,
                "note": (
                    f"Freshness is evaluated over J1's configured probe window "
                    f"({probe_window}), which may differ from the investigation "
                    f"time_range ({time_range}). A source marked healthy here "
                    f"could have been stopped during the investigation window."
                ),
            }
            # Phase 3 — J2 parser failures (always-on)
            acc["parser_failures"] = await self._j2_tool.run(
                time_range=time_range,
                top_n=10,
            )
            # Phase 4 — A2 anomaly ranking with anchored windows
            anchor = _utcnow()
            current_w, comparison_w = _compute_windows(time_range, anchor)
            if seed_filter == "*":
                ranking_query = "* | stats count as n by 'Log Source'"
            else:
                ranking_query = f"{seed_filter} | stats count as n by 'Log Source'"
            diff_result = await self._diff_tool.run(
                query=ranking_query,
                current_window=current_w,
                comparison_window=comparison_w,
            )
            acc["diff"] = diff_result

            # Identify stopped sources from J1 to exclude from ranking.
            stopped: Set[str] = set()
            ih_section = acc.get("ingestion_health") or {}
            for finding in ((ih_section.get("snapshot") or {}).get("findings") or []):
                if finding.get("status") == "stopped":
                    stopped.add(str(finding.get("source")))

            acc["anomalous_sources"] = _rank_anomalous_sources(
                diff_result.get("delta") or [], stopped, top_k,
            )
            # Seed per_source entries for drill-down phases.
            for s in acc["anomalous_sources"]:
                acc["per_source"][s["source"]] = {
                    "top_error_clusters": [],
                    "top_entities": [],
                    "timeline": None,
                    "errors": [],
                }

            # Phases 5+6 — per-source drill-down under Semaphore(2)
            sem = asyncio.Semaphore(PER_SOURCE_CONCURRENCY)
            sources_list = [s["source"] for s in acc["anomalous_sources"]]

            async def bounded(source_name: str):
                async with sem:
                    return await _drill_down_one_source(
                        self._engine, source_name, seed_filter, time_range, compartment_id,
                    )

            results = await asyncio.gather(
                *(bounded(s) for s in sources_list),
                return_exceptions=True,
            )
            for source_name, branch_result in zip(sources_list, results):
                # Budget exhaustion is its own partial_reason; don't downgrade
                # to source_errors.
                if isinstance(branch_result, BudgetExceededError):
                    acc["per_source"][source_name]["errors"].append(
                        f"branch: BudgetExceededError: {branch_result}"
                    )
                    acc["partial_reasons"].add("budget_exceeded")
                    continue
                # Other uncaught branch-level exceptions (should be rare given
                # _drill_down_one_source catches everything non-Budget).
                if isinstance(branch_result, Exception):
                    acc["per_source"][source_name]["errors"].append(
                        f"branch: {type(branch_result).__name__}: {branch_result}"
                    )
                    acc["source_errors"].append(str(branch_result))
                    acc["partial_reasons"].add("source_errors")
                    continue
                # Normal path: merge branch_result flags into partial_reasons.
                ps = acc["per_source"][source_name]
                ps["top_error_clusters"] = branch_result["top_error_clusters"]
                ps["top_entities"] = branch_result["top_entities"]
                ps["timeline"] = branch_result["timeline"]
                ps["errors"].extend(branch_result["errors"])
                if branch_result["entity_discovery_partial"]:
                    acc["partial_reasons"].add("entity_discovery_partial")
                if branch_result["timeline_omitted"]:
                    acc["partial_reasons"].add("timeline_omitted")
                # infra_error = non-Budget, non-field-variance failure seen
                # inside a per-phase try — maps to source_errors.
                if branch_result["infra_error"]:
                    acc["partial_reasons"].add("source_errors")
            # Phase 7 — next_steps suggestions from the seed result
            acc["next_steps"] = [
                step.to_dict()
                for step in _next_steps.suggest(query, acc.get("seed_result") or {})
            ]
        except BudgetExceededError:
            acc["partial_reasons"].add("budget_exceeded")
        return _finalize(acc, self._budget)


def _finalize(acc: Dict[str, Any], budget_tracker) -> Dict[str, Any]:
    """Assemble the final InvestigationReport from the accumulator."""
    reasons = sorted(acc["partial_reasons"]) if acc.get("partial_reasons") else []
    budget_snap = budget_tracker.snapshot().to_dict() if budget_tracker else {}
    elapsed = (_utcnow() - acc["start_time"]).total_seconds()

    # per_source dict → ordered list matching anomalous_sources ranking.
    anomalous_list: List[Dict[str, Any]] = []
    for ranked in acc["anomalous_sources"]:
        src = ranked["source"]
        entry = dict(ranked)
        ps = acc["per_source"].get(src, {})
        entry["top_error_clusters"] = ps.get("top_error_clusters", [])
        entry["top_entities"] = ps.get("top_entities", [])
        entry["timeline"] = ps.get("timeline")
        entry["errors"] = ps.get("errors", [])
        anomalous_list.append(entry)

    # Cross-source timeline built from per-source dict.
    timeline_by_source = {
        src: acc["per_source"].get(src, {}).get("timeline")
        for src in acc["per_source"].keys()
    }
    cross_source = _merge_cross_source_timeline(timeline_by_source, cap=TIMELINE_CAP)

    return {
        "summary": _templated_summary(acc),
        "seed": acc["seed"],
        "ingestion_health": acc["ingestion_health"],
        "parser_failures": acc["parser_failures"],
        "anomalous_sources": anomalous_list,
        "cross_source_timeline": cross_source,
        "next_steps": acc.get("next_steps") or [],
        "budget": budget_snap,
        "partial": bool(reasons),
        "partial_reasons": reasons,
        "elapsed_seconds": round(elapsed, 3),
    }
