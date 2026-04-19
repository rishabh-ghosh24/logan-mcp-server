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

from dataclasses import dataclass, field
from typing import Any, Dict, List


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
    return out


def _h_empty_result(query: str, rows: list, columns: list) -> List[NextStep]:
    if len(rows) == 0 and len(columns) > 0:
        return [NextStep(
            tool_name="validate_query",
            suggested_args={"query": query},
            reason="Query returned zero rows — validate syntax or loosen filters.",
        )]
    return []
