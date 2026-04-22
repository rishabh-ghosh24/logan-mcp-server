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
            # Phases 3-7 are added by subsequent tasks.
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
        "next_steps": [],  # filled in Task 14
        "budget": budget_snap,
        "partial": bool(reasons),
        "partial_reasons": reasons,
        "elapsed_seconds": round(elapsed, 3),
    }
