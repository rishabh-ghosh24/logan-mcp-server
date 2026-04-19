"""Heuristic pivot suggestions attached to query responses.

Each heuristic function takes (query, result) and returns a list of
NextStep suggestions based on the shape of `result`. The top-level
`suggest()` runs all heuristics and concatenates their output.

Design invariants:
  - Pure; no I/O, no network, no mutation of inputs.
  - Never raise. On malformed input, return [].
  - Shape-only; do not semantically parse the query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

LARGE_RESULT_THRESHOLD = 1000

_ID_FIELD_RE = re.compile(r"(request|trace|correlation|x[-_ ]?request)[-_ ]?id", re.IGNORECASE)


@dataclass
class NextStep:
    tool_name: str
    suggested_args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "suggested_args": self.suggested_args,
            "reason": self.reason,
        }


def suggest(query: str, result: Dict[str, Any]) -> List[NextStep]:
    """Return pivot suggestions based on result shape. Never raises."""
    try:
        data = (result or {}).get("data") or {}
        rows = data.get("rows") or []
        columns = data.get("columns") or []
    except Exception:
        return []

    if not isinstance(rows, list) or not isinstance(columns, list):
        return []

    out: List[NextStep] = []
    out.extend(_h_empty_result(query, rows, columns))
    out.extend(_h_large_result(query, rows, columns))
    out.extend(_h_request_id(query, rows, columns))
    return out


def _h_request_id(query: str, rows: list, columns: list) -> List[NextStep]:
    id_col_idx = None
    id_col_name = None
    for i, col in enumerate(columns):
        name = col.get("name") if isinstance(col, dict) else None
        if name and _ID_FIELD_RE.search(name):
            id_col_idx = i
            id_col_name = name
            break
    if id_col_idx is None:
        return []

    sample = None
    for row in rows:
        if not isinstance(row, list) or id_col_idx >= len(row):
            continue
        val = row[id_col_idx]
        if val not in (None, ""):
            sample = val
            break
    if sample is None:
        return []

    return [NextStep(
        tool_name="trace_request_id",
        suggested_args={"request_id": sample},
        reason=f"Result has a '{id_col_name}' field — trace all events for this id.",
    )]


def _h_large_result(query: str, rows: list, columns: list) -> List[NextStep]:
    if len(rows) >= LARGE_RESULT_THRESHOLD:
        return [NextStep(
            tool_name="run_query",
            suggested_args={"query": query, "time_range": "last_15_min"},
            reason=f"Result has {len(rows)} rows — try a tighter/narrower time window.",
        )]
    return []


def _h_empty_result(query: str, rows: list, columns: list) -> List[NextStep]:
    if len(rows) == 0 and len(columns) > 0:
        return [NextStep(
            tool_name="validate_query",
            suggested_args={"query": query},
            reason="Query returned zero rows — validate syntax or loosen filters.",
        )]
    return []
