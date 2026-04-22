"""investigate_incident (A1) — orchestrator that composes triage primitives
into a structured first-cut investigation report.

Design: docs/phase-2/specs/2026-04-22-a1-investigate-incident-design.md
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

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
