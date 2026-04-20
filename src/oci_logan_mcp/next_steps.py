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
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List

LARGE_RESULT_THRESHOLD = 1000

_ID_FIELD_RE = re.compile(r"(request|trace|correlation|x[-_ ]?request)[-_ ]?id", re.IGNORECASE)

_STATUS_FIELD_NAMES = {"status", "status code", "statuscode", "http status", "response status"}
_SEVERITY_FIELD_NAMES = {"severity", "level", "log level"}
_ENTITY_FIELD_NAMES = {"host", "hostname", "entity", "instance", "service", "pod"}
_ERROR_STRINGS = {"error", "fail", "failed", "failure", "exception", "critical", "fatal"}

SPIKE_RATIO = 3.0


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
    out.extend(_h_error_rows(query, rows, columns))
    out.extend(_h_time_spike(query, rows, columns))
    return out


def _is_time_col(col) -> bool:
    if not isinstance(col, dict):
        return False
    name = (col.get("name") or "").strip().lower()
    return name in {"time", "start time", "timestamp", "_time"}


def _find_numeric_col(columns: list, skip_idx: int):
    for i, col in enumerate(columns):
        if i == skip_idx or not isinstance(col, dict):
            continue
        dtype = (col.get("dataType") or col.get("type") or "").lower()
        if dtype in {"int", "integer", "long", "double", "float", "number"}:
            return i
    return None


def _h_time_spike(query: str, rows: list, columns: list) -> List[NextStep]:
    if len(rows) < 4:
        return []
    time_idx = next((i for i, c in enumerate(columns) if _is_time_col(c)), None)
    if time_idx is None:
        return []
    count_idx = _find_numeric_col(columns, skip_idx=time_idx)
    if count_idx is None:
        count_idx = 1 if len(columns) > 1 else None
    if count_idx is None:
        return []

    values = []
    for row in rows:
        if not isinstance(row, list) or count_idx >= len(row) or time_idx >= len(row):
            continue
        v = row[count_idx]
        try:
            values.append((row[time_idx], float(v)))
        except (TypeError, ValueError):
            continue
    if len(values) < 4:
        return []

    max_ts, max_val = max(values, key=lambda x: x[1])
    others = [v for ts, v in values if ts != max_ts]
    if not others:
        return []
    median = statistics.median(others)
    if median <= 0:
        return []
    if max_val < SPIKE_RATIO * median:
        return []

    return [NextStep(
        tool_name="diff_time_windows",
        suggested_args={
            "query": query,
            "spike_bucket": str(max_ts),
            "baseline": "same_hour_last_week",
        },
        reason=f"Bucket at {max_ts} is {max_val:.0f} vs. median {median:.0f} — compare to last week.",
    )]


def _find_col(columns: list, candidates: set) -> tuple:
    for i, col in enumerate(columns):
        name = col.get("name") if isinstance(col, dict) else None
        if name and name.strip().lower() in candidates:
            return i, name
    return None, None


def _is_error_row(row: list, status_idx, severity_idx) -> bool:
    if status_idx is not None and status_idx < len(row):
        val = row[status_idx]
        if isinstance(val, (int, float)) and 400 <= int(val) < 600:
            return True
        if isinstance(val, str) and val.strip().isdigit():
            try:
                n = int(val.strip())
                if 400 <= n < 600:
                    return True
            except ValueError:
                pass
    if severity_idx is not None and severity_idx < len(row):
        val = row[severity_idx]
        if isinstance(val, str) and val.strip().lower() in _ERROR_STRINGS:
            return True
    return False


def _h_error_rows(query: str, rows: list, columns: list) -> List[NextStep]:
    status_idx, status_name = _find_col(columns, _STATUS_FIELD_NAMES)
    severity_idx, severity_name = _find_col(columns, _SEVERITY_FIELD_NAMES)
    if status_idx is None and severity_idx is None:
        return []

    has_errors = any(_is_error_row(r, status_idx, severity_idx) for r in rows if isinstance(r, list))
    if not has_errors:
        return []

    entity_idx, entity_name = _find_col(columns, _ENTITY_FIELD_NAMES)
    suggestions: List[NextStep] = []
    if entity_idx is not None:
        suggestions.append(NextStep(
            tool_name="pivot_on_entity",
            suggested_args={"entity_type": entity_name, "time_range": "last_1_hour"},
            reason=f"Result contains error rows — pivot on '{entity_name}' to see everything touching each entity.",
        ))
    group_field = status_name or severity_name
    if group_field:
        suggestions.append(NextStep(
            tool_name="run_query",
            suggested_args={"query": f"{query} | stats count by '{group_field}'"},
            reason=f"Errors present — try stats-by '{group_field}' to see the breakdown.",
        ))
    return suggestions


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
