# N2 — Suggested Next Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After every `run_query` / `run_batch_queries` call, attach a `next_steps: list[NextStep]` field to the response so an LLM caller gets cheap, shape-based pivot suggestions instead of blind-looping.

**Architecture:** A pure, side-effect-free heuristics module (`next_steps.py`) inspects the **result shape** — not the query semantics — and emits zero or more `NextStep` suggestions keyed by shape signals (has-error-rows, has-time-buckets-with-spike, has-id-field, large-row-count, zero-rows). `QueryEngine.execute` calls `suggest(query, result)` after a successful execution and returns the list unchanged on the response dict. `_run_query` / `_run_batch_queries` propagate the field verbatim — no handler-layer logic. Heuristics reference downstream tool names (`pivot_on_entity`, `diff_time_windows`, `trace_request_id`, `validate_query`) that may not all exist yet; that is intentional and documented — N2 ships stubbed hints, the tools light up when later features land.

**Tech Stack:** Python 3, pytest, dataclasses. No new runtime dependencies. No network, no file I/O.

**Spec:** [../specs/agent-guardrails.md](../specs/agent-guardrails.md) · feature N2.

---

## File Structure

**Create:**
- `src/oci_logan_mcp/next_steps.py` — `NextStep` dataclass, `suggest(query, result)` entrypoint, one function per heuristic.
- `tests/test_next_steps.py` — one test per heuristic + end-to-end shape tests.

**Modify:**
- `src/oci_logan_mcp/query_engine.py` — call `suggest(...)` after successful execution, attach `next_steps` to the returned dict.
- `tests/test_query_engine.py` — **only if it exists** and already exercises `execute`; otherwise skip. (Grep first; do not create.)

**Do NOT modify:**
- `src/oci_logan_mcp/handlers.py` — no handler-layer changes. `_run_query` already returns `json.dumps(result, ...)`; the new field flows through automatically.
- `src/oci_logan_mcp/tools.py` — the tool's `inputSchema` doesn't change; output shape changes are permissive (callers ignore unknown fields).

**Out of scope** (deferred):
- ML/semantic reasoning about query intent (heuristics only).
- Suggestions for `run_saved_search` — its output flows through `query_engine.execute` too, so it gets suggestions "for free"; no extra work.
- Suggestions for `visualize` — returns an image, not tabular data. Skipped on purpose.
- Configurable heuristic thresholds via `config.py` — defaults only in P0; revisit if a user asks.

---

## Heuristic rationale

Each heuristic is a single function that takes `(query: str, result: dict)` and returns `list[NextStep]`. The top-level `suggest()` runs all of them and concatenates. Keep each function ≤ 15 lines — if a heuristic grows, it's probably conflating signals.

| # | Signal | Output suggestion |
|---|---|---|
| 1 | Row set contains a field whose values look like HTTP error codes (4xx/5xx) or contain `"error"` / `"fail"` / `"exception"` | `pivot_on_entity(<first_entity_field>)` + `run_query(... | stats count by <status_field>)` |
| 2 | Result has time-bucket column (Logan returns `Time`-like column for `timestats`) **and** max bucket value ≥ 3× median of non-max buckets | `diff_time_windows(query, spike_start, spike_end, baseline='same_hour_last_week')` |
| 3 | Result has a field whose name matches `r'(request|trace|correlation|x.request)[-_]?id'` (case-insensitive) and that field is populated in ≥ 1 row | `trace_request_id(<id_value>)` |
| 4 | Row count ≥ `LARGE_RESULT_THRESHOLD` (1000) | `run_query` with tighter time range |
| 5 | Row count == 0 | `validate_query(query)` + a loosen-filter hint |

**Shape-only, not semantics.** We look at column names and row values — never parse the query text beyond trivial regex for "looks like a stats command". A heuristic that needs real query parsing is over-scoped.

**Conservative defaults.** When in doubt (missing columns, unexpected shape), return `[]`. An empty list is always safe; a wrong suggestion wastes the agent's context window.

---

## Task 1: Create `next_steps.py` with `NextStep` dataclass and empty `suggest()`

**Files:**
- Create: `src/oci_logan_mcp/next_steps.py`
- Test: `tests/test_next_steps.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_next_steps.py`:

```python
"""Tests for the next-step suggestion engine."""

from oci_logan_mcp.next_steps import NextStep, suggest


def test_next_step_is_dataclass():
    step = NextStep(tool_name="pivot_on_entity", suggested_args={"entity": "host"}, reason="test")
    assert step.tool_name == "pivot_on_entity"
    assert step.suggested_args == {"entity": "host"}
    assert step.reason == "test"


def test_suggest_returns_list():
    result = {"data": {"rows": [], "columns": []}, "metadata": {}}
    out = suggest("* | head 1", result)
    assert isinstance(out, list)


def test_suggest_never_raises_on_malformed_result():
    # Empty dict, None rows, missing columns, etc.
    assert suggest("*", {}) == []
    assert suggest("*", {"data": None}) == []
    assert suggest("*", {"data": {"rows": None}}) == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_next_steps.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'oci_logan_mcp.next_steps'`.

- [ ] **Step 3: Create the module skeleton**

Create `src/oci_logan_mcp/next_steps.py`:

```python
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

    # Heuristics will be appended below in subsequent tasks.
    return []
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_next_steps.py -v
```

Expected: PASS — 3/3.

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/next_steps.py tests/test_next_steps.py
git commit -m "feat(n2): scaffold next-step suggestion engine"
```

---

## Task 2: Heuristic 5 — empty result → validate_query hint

Ship the simplest heuristic first to lock the wiring before adding noisier logic.

**Files:**
- Modify: `src/oci_logan_mcp/next_steps.py`
- Test: `tests/test_next_steps.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_next_steps.py`:

```python
def test_empty_result_suggests_validate_query():
    result = {"data": {"rows": [], "columns": [{"name": "Time"}]}, "metadata": {}}
    steps = suggest("'Log Source' = 'nonexistent'", result)
    tools = [s.tool_name for s in steps]
    assert "validate_query" in tools
```

- [ ] **Step 2: Run test, verify fail**

```
pytest tests/test_next_steps.py::test_empty_result_suggests_validate_query -v
```

Expected: FAIL — `assert 'validate_query' in []`.

- [ ] **Step 3: Implement heuristic**

In `src/oci_logan_mcp/next_steps.py`, add above `suggest()`:

```python
def _h_empty_result(query: str, rows: list, columns: list) -> List[NextStep]:
    if len(rows) == 0:
        return [NextStep(
            tool_name="validate_query",
            suggested_args={"query": query},
            reason="Query returned zero rows — validate syntax or loosen filters.",
        )]
    return []
```

In `suggest()`, replace the `return []` at the bottom with:

```python
    out: List[NextStep] = []
    out.extend(_h_empty_result(query, rows, columns))
    return out
```

- [ ] **Step 4: Run test, verify pass**

```
pytest tests/test_next_steps.py -v
```

Expected: PASS — all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(n2): empty-result → validate_query hint"
```

---

## Task 3: Heuristic 4 — large result → narrower-window hint

**Files:**
- Modify: `src/oci_logan_mcp/next_steps.py`
- Test: `tests/test_next_steps.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_next_steps.py`:

```python
def test_large_result_suggests_narrower_window():
    rows = [[i, f"msg-{i}"] for i in range(1500)]  # > 1000
    result = {
        "data": {"rows": rows, "columns": [{"name": "Time"}, {"name": "Message"}]},
        "metadata": {},
    }
    steps = suggest("*", result)
    tools = [s.tool_name for s in steps]
    assert "run_query" in tools
    narrower = [s for s in steps if s.tool_name == "run_query"]
    assert any("narrow" in s.reason.lower() or "tighter" in s.reason.lower() for s in narrower)


def test_small_result_does_not_suggest_narrower_window():
    rows = [[i] for i in range(50)]
    result = {"data": {"rows": rows, "columns": [{"name": "Time"}]}, "metadata": {}}
    steps = suggest("*", result)
    tools = [s.tool_name for s in steps]
    assert "run_query" not in tools
```

- [ ] **Step 2: Run test, verify fail**

```
pytest tests/test_next_steps.py -v
```

Expected: FAIL — first new test fails.

- [ ] **Step 3: Implement heuristic**

In `src/oci_logan_mcp/next_steps.py`, add constant at top:

```python
LARGE_RESULT_THRESHOLD = 1000
```

Add heuristic:

```python
def _h_large_result(query: str, rows: list, columns: list) -> List[NextStep]:
    if len(rows) >= LARGE_RESULT_THRESHOLD:
        return [NextStep(
            tool_name="run_query",
            suggested_args={"query": query, "time_range": "last_15_min"},
            reason=f"Result has {len(rows)} rows — try a tighter/narrower time window.",
        )]
    return []
```

Register in `suggest()`:

```python
    out.extend(_h_large_result(query, rows, columns))
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_next_steps.py -v
```

Expected: PASS — 6/6.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(n2): large-result → narrower-window hint"
```

---

## Task 4: Heuristic 3 — request-id field → trace_request_id

**Files:**
- Modify: `src/oci_logan_mcp/next_steps.py`
- Test: `tests/test_next_steps.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_request_id_field_suggests_trace():
    result = {
        "data": {
            "rows": [["2026-04-20T10:00:00Z", "abc-123-def"]],
            "columns": [{"name": "Time"}, {"name": "Request ID"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    tools = [s.tool_name for s in steps]
    assert "trace_request_id" in tools


def test_populated_trace_id_field_suggests_trace():
    result = {
        "data": {
            "rows": [["2026-04-20", "my-trace-42"]],
            "columns": [{"name": "Time"}, {"name": "Trace-ID"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert any(s.tool_name == "trace_request_id" for s in steps)


def test_empty_id_field_does_not_suggest_trace():
    # Field exists but all values null/blank → no suggestion
    result = {
        "data": {
            "rows": [["2026-04-20", None], ["2026-04-20", ""]],
            "columns": [{"name": "Time"}, {"name": "Request ID"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert not any(s.tool_name == "trace_request_id" for s in steps)


def test_no_id_field_no_suggestion():
    result = {
        "data": {
            "rows": [["a", "b"]],
            "columns": [{"name": "Host"}, {"name": "Message"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert not any(s.tool_name == "trace_request_id" for s in steps)
```

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_next_steps.py -v
```

Expected: FAIL on first and second new tests.

- [ ] **Step 3: Implement heuristic**

Add to `src/oci_logan_mcp/next_steps.py`:

```python
import re

_ID_FIELD_RE = re.compile(r"(request|trace|correlation|x[-_ ]?request)[-_ ]?id", re.IGNORECASE)


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
```

Register:

```python
    out.extend(_h_request_id(query, rows, columns))
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_next_steps.py -v
```

Expected: PASS — 10/10.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(n2): request-id field → trace_request_id hint"
```

---

## Task 5: Heuristic 1 — error-like rows → pivot_on_entity + stats by status

**Files:**
- Modify: `src/oci_logan_mcp/next_steps.py`
- Test: `tests/test_next_steps.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_http_5xx_status_suggests_pivot_and_stats():
    result = {
        "data": {
            "rows": [["2026-04-20", "web-01", 500], ["2026-04-20", "web-02", 503]],
            "columns": [{"name": "Time"}, {"name": "Host"}, {"name": "Status"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    tools = [s.tool_name for s in steps]
    # Both pivot hint and stats-by hint should appear.
    assert "pivot_on_entity" in tools
    assert "run_query" in tools
    stats_step = next(s for s in steps if s.tool_name == "run_query" and "stats" in s.reason.lower())
    assert stats_step is not None


def test_severity_field_with_error_value_suggests_pivot():
    result = {
        "data": {
            "rows": [["host-a", "ERROR"], ["host-b", "error"]],
            "columns": [{"name": "Host"}, {"name": "Severity"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert any(s.tool_name == "pivot_on_entity" for s in steps)


def test_successful_rows_do_not_suggest_error_pivot():
    result = {
        "data": {
            "rows": [["host-a", 200], ["host-b", 201]],
            "columns": [{"name": "Host"}, {"name": "Status"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert not any(s.tool_name == "pivot_on_entity" for s in steps)
```

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_next_steps.py -v
```

Expected: FAIL on the first two new tests.

- [ ] **Step 3: Implement heuristic**

Add helpers + heuristic to `src/oci_logan_mcp/next_steps.py`:

```python
_STATUS_FIELD_NAMES = {"status", "status code", "statuscode", "http status", "response status"}
_SEVERITY_FIELD_NAMES = {"severity", "level", "log level"}
_ENTITY_FIELD_NAMES = {"host", "hostname", "entity", "instance", "service", "pod"}
_ERROR_STRINGS = {"error", "fail", "failed", "failure", "exception", "critical", "fatal"}


def _find_col(columns: list, candidates: set) -> tuple[int | None, str | None]:
    for i, col in enumerate(columns):
        name = col.get("name") if isinstance(col, dict) else None
        if name and name.strip().lower() in candidates:
            return i, name
    return None, None


def _is_error_row(row: list, status_idx: int | None, severity_idx: int | None) -> bool:
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
```

Register:

```python
    out.extend(_h_error_rows(query, rows, columns))
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_next_steps.py -v
```

Expected: PASS — 13/13.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(n2): error-row → pivot + stats-by-status hints"
```

---

## Task 6: Heuristic 2 — time-bucket spike → diff_time_windows

This is the most fiddly heuristic and is saved for last so we can keep it modest — if the spike-detection math proves noisy on real queries we can loosen/tighten without affecting others.

**Files:**
- Modify: `src/oci_logan_mcp/next_steps.py`
- Test: `tests/test_next_steps.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_time_bucket_spike_suggests_diff_windows():
    # Flat baseline of 10, one bucket at 100 → clear spike (10x median).
    rows = [
        ["2026-04-20T09:00:00Z", 10],
        ["2026-04-20T09:05:00Z", 12],
        ["2026-04-20T09:10:00Z", 9],
        ["2026-04-20T09:15:00Z", 11],
        ["2026-04-20T09:20:00Z", 100],
        ["2026-04-20T09:25:00Z", 10],
    ]
    result = {
        "data": {"rows": rows, "columns": [{"name": "Time"}, {"name": "Count"}]},
        "metadata": {},
    }
    steps = suggest("* | timestats count as Count span=5m", result)
    assert any(s.tool_name == "diff_time_windows" for s in steps)


def test_flat_time_series_no_spike_suggestion():
    rows = [["t" + str(i), 10 + (i % 3)] for i in range(10)]
    result = {
        "data": {"rows": rows, "columns": [{"name": "Time"}, {"name": "Count"}]},
        "metadata": {},
    }
    steps = suggest("* | timestats count", result)
    assert not any(s.tool_name == "diff_time_windows" for s in steps)


def test_non_timeseries_ignored_by_spike_heuristic():
    result = {
        "data": {"rows": [["a", 1], ["b", 9999]], "columns": [{"name": "Host"}, {"name": "Count"}]},
        "metadata": {},
    }
    steps = suggest("*", result)
    assert not any(s.tool_name == "diff_time_windows" for s in steps)
```

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_next_steps.py -v
```

Expected: FAIL on the spike-detection test.

- [ ] **Step 3: Implement heuristic**

Add to `src/oci_logan_mcp/next_steps.py`:

```python
import statistics

SPIKE_RATIO = 3.0  # max bucket must be ≥ 3× median of others to count as a spike


def _is_time_col(col: dict | Any) -> bool:
    if not isinstance(col, dict):
        return False
    name = (col.get("name") or "").strip().lower()
    return name in {"time", "start time", "timestamp", "_time"}


def _find_numeric_col(columns: list, skip_idx: int) -> int | None:
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
        # Fallback: try column index 1 if it's numeric-looking
        count_idx = 1 if len(columns) > 1 else None
    if count_idx is None:
        return []

    values: list[tuple[Any, float]] = []
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
```

Register in `suggest()`:

```python
    out.extend(_h_time_spike(query, rows, columns))
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_next_steps.py -v
```

Expected: PASS — 16/16.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(n2): time-bucket spike → diff_time_windows hint"
```

---

## Task 7: Wire `suggest()` into `QueryEngine.execute`

**Files:**
- Modify: `src/oci_logan_mcp/query_engine.py:87-99` (the return-live block)
- Modify: `src/oci_logan_mcp/query_engine.py:49-60` (the return-cache block)
- Test: new unit test

- [ ] **Step 1: Write the failing test**

Append to `tests/test_next_steps.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_query_engine_attaches_next_steps():
    """Smoke test: execute() output carries next_steps list."""
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine

    oci_client = MagicMock()
    oci_client.compartment_id = "comp-1"
    oci_client.query = AsyncMock(return_value={
        "rows": [], "columns": [{"name": "Time"}],
    })
    cache = MagicMock()
    cache.get = MagicMock(return_value=None)
    cache.set = MagicMock()
    qlog = MagicMock()

    engine = QueryEngine(oci_client, cache, qlog)
    result = await engine.execute(
        query="* | head 1",
        time_range="last_1_hour",
        use_cache=False,
    )
    assert "next_steps" in result
    assert isinstance(result["next_steps"], list)
    # Empty result → validate_query suggestion should fire.
    assert any(s["tool_name"] == "validate_query" for s in result["next_steps"])
```

- [ ] **Step 2: Run test, verify fail**

```
pytest tests/test_next_steps.py::test_query_engine_attaches_next_steps -v
```

Expected: FAIL — `KeyError: 'next_steps'`.

- [ ] **Step 3: Wire it into `query_engine.py`**

In `src/oci_logan_mcp/query_engine.py`, add at top:

```python
from .next_steps import suggest as _suggest_next_steps
```

Modify the **live-result** return (around line 88) to attach `next_steps`:

```python
            response = {
                "source": "live",
                "data": result,
                "metadata": {
                    "query": query,
                    "compartment_id": effective_compartment,
                    "time_start": start.isoformat(),
                    "time_end": end.isoformat(),
                    "include_subcompartments": include_subcompartments,
                    "execution_time_seconds": execution_time,
                },
            }
            response["next_steps"] = [s.to_dict() for s in _suggest_next_steps(query, response)]
            return response
```

Modify the **cache-hit** return (around line 49) similarly:

```python
            if cached:
                response = {
                    "source": "cache",
                    "data": cached,
                    "metadata": {
                        "query": query,
                        "compartment_id": effective_compartment,
                        "time_start": start.isoformat(),
                        "time_end": end.isoformat(),
                        "include_subcompartments": include_subcompartments,
                    },
                }
                response["next_steps"] = [s.to_dict() for s in _suggest_next_steps(query, response)]
                return response
```

> **Why two sites:** the agent deserves suggestions whether the result came from cache or live. Suggestions depend on the result shape, not the source.

- [ ] **Step 4: Run tests**

```
pytest tests/test_next_steps.py -v
```

Expected: PASS — 17/17.

- [ ] **Step 5: Run the full suite to make sure nothing regressed**

```
pytest tests/ -q
```

Expected: all existing tests pass. The `next_steps` field is additive; existing callers ignore it.

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(n2): attach next_steps to QueryEngine results"
```

---

## Task 8: Manual verification

- [ ] **Step 1: Start the server against your tenancy**

```
oci-logan-mcp
```

- [ ] **Step 2: From an MCP client, run a query that should return zero rows**

```
run_query(query="'Log Source' = 'Nope-Does-Not-Exist'", time_range="last_1_hour")
```

Expected: response JSON has `"next_steps": [{"tool_name": "validate_query", ...}]`.

- [ ] **Step 3: Run a large-result query**

```
run_query(query="*", time_range="last_1_hour", max_results=2000)
```

Expected: response includes a `run_query` narrower-window suggestion.

- [ ] **Step 4: Manually confirm no regressions on `_run_query` JSON serialization**

The response is passed through `json.dumps(result, indent=2, default=str)` in `handlers._run_query` — `NextStep` never reaches that code path (we `.to_dict()` before returning), but dataclass-in-dict slip-ups are a known hazard. If `json.dumps` explodes, we've regressed.

---

## Branch acceptance checklist for N2

- [ ] `next_steps.py` module present with 5 heuristics + `NextStep` dataclass.
- [ ] All heuristic tests passing (at least 16 unit + 1 integration).
- [ ] `run_query` / `run_saved_search` / `run_batch_queries` responses carry `next_steps: [...]`.
- [ ] Full test suite green.
- [ ] No changes to `tools.py` input schemas.
- [ ] Manual smoke against real tenancy confirms the field is present and well-formed.
