"""investigate_incident (A1) — orchestrator that composes triage primitives
into a structured first-cut investigation report.

Design: docs/phase-2/specs/2026-04-22-a1-investigate-incident-design.md
"""

from __future__ import annotations

from typing import Any


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
