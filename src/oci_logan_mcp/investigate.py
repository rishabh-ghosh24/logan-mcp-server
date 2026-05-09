"""investigate_incident (A1) — orchestrator that composes triage primitives
into a structured first-cut investigation report.

Design: docs/phase-2/specs/2026-04-22-a1-investigate-incident-design.md
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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


def _compose_chronic_baseline_query(
    seed_filter: str,
    terms: Tuple[str, ...],
    top_k: int,
    focus_sources: Optional[List[str]],
) -> str:
    """Compose the chronic-baseline ranking query.

    Counts events per source matching any error-like substring over the
    current investigation window. Per spec §3.2:
      - terms are validated upstream (config load) and rendered literally
      - source names are escaped via single-quote-doubling
      - wildcard seed omits the seed clause entirely
    """
    error_clause = "(" + " or ".join(
        f"'Original Log Content' like '%{t}%'" for t in terms
    ) + ")"

    if seed_filter == "*":
        head = error_clause
    else:
        head = f"({seed_filter}) and {error_clause}"

    if focus_sources:
        escaped = ", ".join(
            f"'{name.replace(chr(39), chr(39) * 2)}'" for name in focus_sources
        )
        head = f"{head} and 'Log Source' in ({escaped})"

    return f"{head} | stats count as n by 'Log Source' | sort -n | head {top_k}"


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


def _normalize_focus_sources(sources: Optional[List[Any]], top_k: int) -> Optional[List[str]]:
    """Normalize caller-provided source focus list, preserving order."""
    if sources is None:
        return None
    if not isinstance(sources, list):
        raise ValueError("focus_sources must be a list of log source names")
    normalized: List[str] = []
    seen: Set[str] = set()
    for source in sources:
        if not isinstance(source, str):
            raise ValueError("focus_sources must contain only strings")
        clean = source.strip()
        if not clean or clean in seen:
            continue
        normalized.append(clean)
        seen.add(clean)
        if len(normalized) >= top_k:
            break
    return normalized


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
TOP_K_MAX = 10
TIMELINE_CAP = 50

# Entity discovery uses the same field names as pivot_tool's ENTITY_FIELD_MAP,
# but A1 P0 uses only three (ip deferred to P1 to bound per-source query count).
A1_ENTITY_FIELDS = [
    ("host", "Host Name (Server)"),
    ("user", "User Name"),
    ("request_id", "Request ID"),
]
PER_SOURCE_CONCURRENCY = 5
CLUSTER_HEAD = 3
ENTITY_HEAD = 5
TIMELINE_HEAD = 20
DEFAULT_TRACK_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class _InvestigationModeConfig:
    name: str
    run_ingestion_health: bool
    run_parser_failures: bool
    run_entities: bool
    run_timeline: bool
    cluster_head: int
    entity_head: int
    timeline_head: int
    per_source_concurrency: int
    timeout_seconds: float
    run_chronic_baseline: bool


_MODE_CONFIGS: Dict[str, _InvestigationModeConfig] = {
    "quick": _InvestigationModeConfig(
        name="quick",
        run_ingestion_health=False,
        run_parser_failures=False,
        run_entities=False,
        run_timeline=False,
        cluster_head=3,
        entity_head=0,
        timeline_head=0,
        per_source_concurrency=PER_SOURCE_CONCURRENCY,
        timeout_seconds=60.0,
        run_chronic_baseline=False,
    ),
    "standard": _InvestigationModeConfig(
        name="standard",
        run_ingestion_health=True,
        run_parser_failures=True,
        run_entities=True,
        run_timeline=True,
        cluster_head=CLUSTER_HEAD,
        entity_head=ENTITY_HEAD,
        timeline_head=TIMELINE_HEAD,
        per_source_concurrency=PER_SOURCE_CONCURRENCY,
        timeout_seconds=DEFAULT_TRACK_TIMEOUT_SECONDS,
        run_chronic_baseline=True,
    ),
    "deep": _InvestigationModeConfig(
        name="deep",
        run_ingestion_health=True,
        run_parser_failures=True,
        run_entities=True,
        run_timeline=True,
        cluster_head=5,
        entity_head=10,
        timeline_head=50,
        per_source_concurrency=PER_SOURCE_CONCURRENCY,
        timeout_seconds=180.0,
        run_chronic_baseline=True,
    ),
}


def _mode_config(mode: str) -> _InvestigationModeConfig:
    if mode not in _MODE_CONFIGS:
        raise ValueError(
            f"mode must be one of {sorted(_MODE_CONFIGS)}; got {mode!r}"
        )
    return _MODE_CONFIGS[mode]


async def _await_with_timeout(awaitable, timeout_seconds: Optional[float]):
    if timeout_seconds is None:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout=timeout_seconds)


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
        problem_priority = None
        if prio is not None:
            problem_priority = int(prio) if isinstance(prio, (int, float)) else str(prio)
        out.append({
            "pattern": str(row[sample_idx]) if row[sample_idx] is not None else "",
            "count": int(cnt) if cnt is not None else 0,
            "problem_priority": problem_priority,
        })
    return out


def _parse_chronic_response(
    response: Dict[str, Any],
    threshold: int,
    focus_sources: Optional[List[str]] = None,
    seed_total_events: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Parse a `| stats count as n by 'Log Source'` response for chronic baseline.

    Defensive in two ways (spec §3.3):
      1. Drops rows where `n < threshold` even though the query may already cap.
      2. Drops sources not in `focus_sources` if provided, even if the query
         already filtered.

    `seed_total_events`, if provided and > 0, populates `error_like_share_of_seed`
    on each entry as `n / seed_total_events`. Otherwise the field is None.
    """
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", []) or []]
    rows = data.get("rows", []) or []
    if "Log Source" not in columns or "n" not in columns:
        return []
    src_idx = columns.index("Log Source")
    cnt_idx = columns.index("n")
    max_idx = max(src_idx, cnt_idx)
    focus_set = set(focus_sources) if focus_sources else None
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not row or len(row) <= max_idx:
            continue
        src = row[src_idx]
        cnt = row[cnt_idx]
        if src is None or cnt is None:
            continue
        try:
            n = int(cnt)
        except (TypeError, ValueError):
            continue
        if n < threshold:
            continue
        src_str = str(src)
        if focus_set is not None and src_str not in focus_set:
            continue
        if seed_total_events and seed_total_events > 0:
            share: Optional[float] = n / seed_total_events
        else:
            share = None
        out.append({
            "source": src_str,
            "error_like_count": n,
            "error_like_share_of_seed": share,
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
    *,
    run_entities: bool = True,
    run_timeline: bool = True,
    cluster_head: int = CLUSTER_HEAD,
    entity_head: int = ENTITY_HEAD,
    timeline_head: int = TIMELINE_HEAD,
    query_timeout_seconds: Optional[float] = DEFAULT_TRACK_TIMEOUT_SECONDS,
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
        seed_filter, source, f"cluster | sort -Count | head {cluster_head}",
    )
    try:
        cluster_resp = await _await_with_timeout(
            engine.execute(
                query=cluster_query, time_range=time_range, compartment_id=compartment_id,
            ),
            query_timeout_seconds,
        )
        result["top_error_clusters"] = _parse_cluster_response(cluster_resp)
    except BudgetExceededError:
        raise
    except Exception as e:
        result["errors"].append(f"cluster: {type(e).__name__}: {e}")
        result["infra_error"] = True

    # Entity discovery (3 fields, concurrent). Distinguish field-variance
    # (soft) from infrastructure failures (hard, but non-fatal to branch).
    async def run_entity(entity_type: str, field_name: str) -> tuple[str, str, Dict[str, Any]]:
        entity_query = _compose_source_scoped_query(
            seed_filter, source,
            f"stats count as n by '{field_name}' | sort -n | head {entity_head}",
        )
        entity_resp = await _await_with_timeout(
            engine.execute(
                query=entity_query, time_range=time_range, compartment_id=compartment_id,
            ),
            query_timeout_seconds,
        )
        return entity_type, field_name, entity_resp

    if run_entities:
        entity_results = await asyncio.gather(
            *(run_entity(entity_type, field_name) for entity_type, field_name in A1_ENTITY_FIELDS),
            return_exceptions=True,
        )
        for entity_spec, entity_result in zip(A1_ENTITY_FIELDS, entity_results):
            entity_type, field_name = entity_spec
            if isinstance(entity_result, BudgetExceededError):
                raise entity_result
            if isinstance(entity_result, Exception):
                e = entity_result
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
                continue

            _, _, entity_resp = entity_result
            try:
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
        f"fields Time, Severity, 'Original Log Content' | sort -Time | head {timeline_head}",
    )
    if run_timeline:
        try:
            tl_resp = await _await_with_timeout(
                engine.execute(
                    query=timeline_query, time_range=time_range, compartment_id=compartment_id,
                ),
                query_timeout_seconds,
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
        focus_sources: Optional[List[Any]] = None,
        mode: str = "standard",
        track_timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        if top_k < TOP_K_MIN or top_k > TOP_K_MAX:
            raise ValueError(
                f"top_k must be in [{TOP_K_MIN}, {TOP_K_MAX}]; got {top_k}"
            )
        normalized_focus_sources = _normalize_focus_sources(focus_sources, top_k)
        config = _mode_config(mode)
        timeout_seconds = (
            config.timeout_seconds
            if track_timeout_seconds is None
            else track_timeout_seconds
        )

        seed_filter = _extract_seed_filter(query)
        acc: Dict[str, Any] = {
            "mode": config.name,
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
            await self._run_core_tracks(
                acc=acc,
                query=query,
                seed_filter=seed_filter,
                time_range=time_range,
                compartment_id=compartment_id,
                config=config,
                timeout_seconds=timeout_seconds,
            )
            diff_result = acc.get("diff") or {}

            # Identify stopped sources from J1 to exclude from ranking.
            stopped: Set[str] = set()
            ih_section = acc.get("ingestion_health") or {}
            for finding in ((ih_section.get("snapshot") or {}).get("findings") or []):
                if finding.get("status") == "stopped":
                    stopped.add(str(finding.get("source")))

            # DiffTool's _label(key) formats single-dimension keys as
            # f"Log Source=<value>", not the bare value. Strip the prefix so
            # downstream code sees the raw Log Source name.
            normalized_delta = []
            for entry in diff_result.get("delta") or []:
                dim = entry.get("dimension")
                if isinstance(dim, str) and dim.startswith("Log Source="):
                    entry = dict(entry)
                    entry["dimension"] = dim[len("Log Source="):]
                normalized_delta.append(entry)
            acc["anomalous_sources"] = _rank_anomalous_sources(
                normalized_delta, stopped, top_k,
            )
            if normalized_focus_sources is not None:
                acc["anomalous_sources"] = [
                    {
                        "source": source,
                        "current_count": None,
                        "comparison_count": None,
                        "pct_change": None,
                    }
                    for source in normalized_focus_sources
                ]
            # Seed per_source entries for drill-down phases.
            for s in acc["anomalous_sources"]:
                acc["per_source"][s["source"]] = {
                    "top_error_clusters": [],
                    "top_entities": [],
                    "timeline": None,
                    "errors": [],
                }

            # Phases 5+6 — per-source drill-down under bounded semaphore.
            sem = asyncio.Semaphore(config.per_source_concurrency)
            sources_list = [s["source"] for s in acc["anomalous_sources"]]

            async def bounded(source_name: str):
                async with sem:
                    return await _drill_down_one_source(
                        self._engine,
                        source_name,
                        seed_filter,
                        time_range,
                        compartment_id,
                        run_entities=config.run_entities,
                        run_timeline=config.run_timeline,
                        cluster_head=config.cluster_head,
                        entity_head=config.entity_head,
                        timeline_head=config.timeline_head,
                        query_timeout_seconds=timeout_seconds,
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
            acc["recommended_parallel_tasks"] = _recommended_parallel_tasks(acc, config)
        except BudgetExceededError:
            acc["partial_reasons"].add("budget_exceeded")
        return _finalize(acc, self._budget)

    async def _run_core_tracks(
        self,
        *,
        acc: Dict[str, Any],
        query: str,
        seed_filter: str,
        time_range: str,
        compartment_id: Optional[str],
        config: _InvestigationModeConfig,
        timeout_seconds: Optional[float],
    ) -> None:
        track_specs = [
            (
                "seed",
                "seed_result",
                self._run_seed_track(query, time_range, compartment_id, timeout_seconds),
            ),
            (
                "diff",
                "diff",
                self._run_diff_track(seed_filter, time_range, compartment_id, timeout_seconds),
            ),
        ]
        if config.run_ingestion_health:
            track_specs.append((
                "ingestion_health",
                "ingestion_health",
                self._run_ingestion_health_track(compartment_id, time_range, timeout_seconds),
            ))
        else:
            acc["ingestion_health"] = _not_run_track(
                "ingestion_health",
                config.name,
                "disabled_by_investigation_mode",
            )
        if config.run_parser_failures:
            track_specs.append((
                "parser_failures",
                "parser_failures",
                self._run_parser_failures_track(time_range, compartment_id, timeout_seconds),
            ))
        else:
            acc["parser_failures"] = _not_run_track(
                "parser_failures",
                config.name,
                "disabled_by_investigation_mode",
            )

        results = await asyncio.gather(
            *(spec[2] for spec in track_specs),
            return_exceptions=True,
        )
        for (track_name, acc_key, _), result in zip(track_specs, results):
            if isinstance(result, BudgetExceededError):
                acc["partial_reasons"].add("budget_exceeded")
                acc[acc_key] = _track_error_payload(track_name, result)
                continue
            if isinstance(result, asyncio.TimeoutError):
                acc["partial_reasons"].add(f"{track_name}_timeout")
                acc[acc_key] = _track_error_payload(track_name, result)
                continue
            if isinstance(result, Exception):
                acc["partial_reasons"].add(f"{track_name}_errors")
                acc[acc_key] = _track_error_payload(track_name, result)
                continue
            acc[acc_key] = result

    async def _run_seed_track(
        self,
        query: str,
        time_range: str,
        compartment_id: Optional[str],
        timeout_seconds: Optional[float],
    ) -> Dict[str, Any]:
        return await _await_with_timeout(
            self._engine.execute(
                query=query,
                time_range=time_range,
                compartment_id=compartment_id,
            ),
            timeout_seconds,
        )

    async def _run_ingestion_health_track(
        self,
        compartment_id: Optional[str],
        time_range: str,
        timeout_seconds: Optional[float],
    ) -> Dict[str, Any]:
        j1_snapshot = await _await_with_timeout(
            self._ih_tool.run(
                compartment_id=compartment_id,
                severity_filter="all",
            ),
            timeout_seconds,
        )
        probe_window = self._settings.ingestion_health.freshness_probe_window
        return {
            "snapshot": j1_snapshot,
            "probe_window": probe_window,
            "note": (
                f"Freshness is evaluated over J1's configured probe window "
                f"({probe_window}), which may differ from the investigation "
                f"time_range ({time_range}). A source marked healthy here "
                f"could have been stopped during the investigation window."
            ),
        }

    async def _run_parser_failures_track(
        self,
        time_range: str,
        compartment_id: Optional[str],
        timeout_seconds: Optional[float],
    ) -> Dict[str, Any]:
        return await _await_with_timeout(
            self._j2_tool.run(
                time_range=time_range,
                top_n=10,
                compartment_id=compartment_id,
            ),
            timeout_seconds,
        )

    async def _run_diff_track(
        self,
        seed_filter: str,
        time_range: str,
        compartment_id: Optional[str],
        timeout_seconds: Optional[float],
    ) -> Dict[str, Any]:
        anchor = _utcnow()
        current_w, comparison_w = _compute_windows(time_range, anchor)
        # Thread compartment_id through the window dicts so DiffTool's
        # **current_window / **comparison_window splat forwards it to
        # each engine.execute call. Keeps _compute_windows signature pure.
        if compartment_id is not None:
            current_w["compartment_id"] = compartment_id
            comparison_w["compartment_id"] = compartment_id
        if seed_filter == "*":
            ranking_query = "* | stats count as n by 'Log Source'"
        else:
            ranking_query = f"{seed_filter} | stats count as n by 'Log Source'"
        return await _await_with_timeout(
            self._diff_tool.run(
                query=ranking_query,
                current_window=current_w,
                comparison_window=comparison_w,
            ),
            timeout_seconds,
        )


def _not_run_track(track_name: str, mode: str, reason: str) -> Dict[str, Any]:
    return {
        "status": "not_run",
        "track": track_name,
        "mode": mode,
        "reason": reason,
    }


def _track_error_payload(track_name: str, exc: Exception) -> Dict[str, Any]:
    return {
        "status": "error",
        "track": track_name,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "delta": [],
        "summary": "",
    }


def _recommended_parallel_tasks(
    acc: Dict[str, Any],
    config: _InvestigationModeConfig,
) -> List[Dict[str, Any]]:
    seed = acc.get("seed") or {}
    seed_filter = seed.get("seed_filter") or "*"
    time_range = seed.get("time_range") or "last_1_hour"
    compartment_id = seed.get("compartment_id")
    tasks: List[Dict[str, Any]] = []

    if (acc.get("ingestion_health") or {}).get("status") == "not_run":
        tasks.append({
            "task_id": "track.ingestion_health",
            "type": "investigation_track",
            "tool_name": "ingestion_health",
            "suggested_args": {
                "severity_filter": "all",
                **({"compartment_id": compartment_id} if compartment_id else {}),
            },
            "reason": "Quick mode skipped ingestion-health context.",
            "can_run_in_parallel": True,
        })
    if (acc.get("parser_failures") or {}).get("status") == "not_run":
        tasks.append({
            "task_id": "track.parser_failures",
            "type": "investigation_track",
            "tool_name": "parser_failure_triage",
            "suggested_args": {
                "time_range": time_range,
                "top_n": 10,
                **({"compartment_id": compartment_id} if compartment_id else {}),
            },
            "reason": "Quick mode skipped parser-failure context.",
            "can_run_in_parallel": True,
        })

    for source_entry in acc.get("anomalous_sources") or []:
        source = source_entry.get("source")
        if not source:
            continue
        source_key = str(source).lower().replace(" ", "_")
        tasks.append({
            "task_id": f"source.{source_key}.clusters",
            "type": "source_drilldown",
            "source": source,
            "tool_name": "run_query",
            "suggested_args": {
                "query": _compose_source_scoped_query(
                    seed_filter,
                    source,
                    f"cluster | sort -Count | head {config.cluster_head}",
                ),
                "time_range": time_range,
                **({"compartment_id": compartment_id} if compartment_id else {}),
            },
            "reason": "Cluster dominant error patterns for this source.",
            "can_run_in_parallel": True,
        })
        for entity_type, field_name in A1_ENTITY_FIELDS:
            tasks.append({
                "task_id": f"source.{source_key}.entity.{entity_type}",
                "type": "source_drilldown",
                "source": source,
                "tool_name": "run_query",
                "suggested_args": {
                    "query": _compose_source_scoped_query(
                        seed_filter,
                        source,
                        f"stats count as n by '{field_name}' | sort -n | head {max(config.entity_head, ENTITY_HEAD)}",
                    ),
                    "time_range": time_range,
                    **({"compartment_id": compartment_id} if compartment_id else {}),
                },
                "reason": f"Find top {entity_type} values for this source.",
                "can_run_in_parallel": True,
            })
        tasks.append({
            "task_id": f"source.{source_key}.timeline",
            "type": "source_drilldown",
            "source": source,
            "tool_name": "run_query",
            "suggested_args": {
                "query": _compose_source_scoped_query(
                    seed_filter,
                    source,
                    f"fields Time, Severity, 'Original Log Content' | sort -Time | head {max(config.timeline_head, TIMELINE_HEAD)}",
                ),
                "time_range": time_range,
                **({"compartment_id": compartment_id} if compartment_id else {}),
            },
            "reason": "Build a recent event timeline for this source.",
            "can_run_in_parallel": True,
        })

    return tasks


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
        "investigation_mode": acc.get("mode", "standard"),
        "seed": acc["seed"],
        "ingestion_health": acc["ingestion_health"],
        "parser_failures": acc["parser_failures"],
        "anomalous_sources": anomalous_list,
        "cross_source_timeline": cross_source,
        "next_steps": acc.get("next_steps") or [],
        "recommended_parallel_tasks": acc.get("recommended_parallel_tasks") or [],
        "budget": budget_snap,
        "partial": bool(reasons),
        "partial_reasons": reasons,
        "elapsed_seconds": round(elapsed, 3),
    }
