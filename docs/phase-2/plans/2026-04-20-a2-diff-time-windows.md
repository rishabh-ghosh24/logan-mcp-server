# A2 — `diff_time_windows` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `diff_time_windows(query, current_window, comparison_window, dimensions)` — one MCP call that runs the same Logan query against two time windows and returns a per-dimension delta plus a one-line human summary. Cheapest high-signal triage primitive and a hard dependency of A1 `investigate_incident`.

**Architecture:**
- **New module `src/oci_logan_mcp/diff_tool.py`** with a single `DiffTool` class that takes an existing `QueryEngine`. No new OCI client code; we reuse `query_engine.execute(...)` for both windows so caching, estimator, and budget tracking all flow through unchanged.
- **Window normalization.** Each window is a dict that mirrors the `run_query` shape: `{"time_range": "last_1_hour"}` OR `{"time_start": "...", "time_end": "..."}`. Normalized to `(start_dt, end_dt)` via the existing `time_parser.parse_time_range`.
- **Query composition, not query parsing.** The tool **appends** `| stats count as count by <dims>` when `dimensions` is provided. When `dimensions` is omitted, we look at the incoming query for an existing `by <fields>` clause (regex) and **reuse** those fields — no source-side field discovery, no top-K categorical ranking. A1 supplies explicit `dimensions` when it wants a breakout (spec note on A1 step 3). We do not build a general Logan AST parser.
- **Scalar fallback.** When no dimensions are provided and no `by` clause is found, we append `| stats count as count` to get a single-row total; delta is then a single scalar row keyed by `"__total__"`. This keeps tests 1 and 2 honest without forcing callers to pick fields.
- **Missing-dimension handling.** Rows present in only one window contribute a delta with `comparison=0` or `current=0`; `pct_change` is `+inf` for new values and `-100.0` for disappeared values, marked by an explicit `"new"` / `"disappeared"` tag in the delta row so the agent doesn't trip on `inf` serialization.
- **Summary heuristic (pure).** No LLM call. Rule: if every row's `abs(pct_change) < significance_threshold` (default 10%), summary is `"no significant change"`. Else, name the top-K (default 3) rows by `abs(pct_change) * max(current, comparison)` — that weighting avoids naming low-volume noise as "the spike".
- **Delta schema extension vs. spec.** The spec lists `{dimension, current, comparison, pct_change}`. This plan adds a `tag` field (`spike | drop | new | disappeared | stable`) and emits `pct_change: null` for "new" rows (instead of `+inf`). Rationale: JSON-safe serialization + pre-computed classification that A1 can consume without re-deriving. Same row count, same keys, additive fields — not scope creep.
- **Budget.** Two queries per call, launched via `asyncio.gather`. Both go through `QueryEngine.execute`, which calls `BudgetTracker.reserve(...)` — an atomic check-and-commit under a single lock (`budget_tracker.py:76`, post-commit `28c59ed`). Concurrent callers cannot both pass a check that only one of them would; one will raise `BudgetExceededError` inside the lock. The `_diff_time_windows` handler catches `BudgetExceededError` and returns a structured payload `{"status": "budget_exceeded", "error": ..., "partial": null}` rather than letting the generic handler stringify it (Task 6 Step 4).

**Tech Stack:** Python 3, pytest, `asyncio.gather` for concurrent window execution, standard `re` for the `by`-clause extractor. No new runtime dependencies.

**Spec:** [../specs/triage-toolkit.md#a2--diff_time_windows](../specs/triage-toolkit.md) · feature A2.

---

## File Structure

**Create:**
- `src/oci_logan_mcp/diff_tool.py` — `DiffTool` class + pure helpers (`_extract_by_clause`, `_compose_query`, `_compute_delta`, `_build_summary`).
- `tests/test_diff_tool.py` — unit tests for the four spec scenarios plus helper coverage.

**Modify:**
- `src/oci_logan_mcp/tools.py` — register `diff_time_windows` schema (after `run_batch_queries`, ~line 209).
- `src/oci_logan_mcp/handlers.py` — wire `_diff_time_windows` handler; add to the `handlers` dict in `handle_tool_call` (~line 127); construct a shared `DiffTool(self.query_engine)` in `__init__` (~line 92).
- `tests/test_handlers.py` — one smoke test that verifies the tool routes and returns a JSON payload.
- `tests/test_read_only_guard.py` — add `diff_time_windows` to `KNOWN_READERS` (read-only by construction; only runs queries).
- `README.md` — add a short "Investigation toolkit → diff_time_windows" section under existing tool docs.

**Do NOT modify:**
- `query_engine.py` — A2 is a pure consumer.
- `time_parser.py` — reused as-is.
- `budget_tracker.py`, `query_estimator.py` — reused via `QueryEngine`.

**Out of scope (deferred, with rationale):**
- **Full-query AST parsing** — we only detect a top-level `by <fields>` via regex. Nested `eventstats`, `timestats`, or commas-inside-function-calls are not handled; if a real user query trips the regex, they can pass `dimensions` explicitly. Proper parsing lands with whatever tool needs it first.
- **Source-side field discovery** (probing `list_fields` for top-K categorical fields when the query has no `by` clause) — explicitly not in A2's contract (see updated spec). Pushed up to A1, which already has field knowledge from `list_fields` / learned queries and will pass explicit `dimensions` when it wants a breakout.
- **Ratio or rate-based deltas** (rows/sec normalization when windows have different lengths) — P0 compares raw counts. If windows are mismatched in length, the summary is allowed to be misleading; callers should pass equal-length windows. A1 will.
- **Cross-dimension interaction analysis** (e.g., "the spike is concentrated in `host=foo AND status=500`") — P0 reports one row per distinct dimension-tuple value; higher-order analysis is A1's job.

---

## Task 1: `DiffTool` skeleton + scalar count delta

**Files:**
- Create: `src/oci_logan_mcp/diff_tool.py`
- Create: `tests/test_diff_tool.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_diff_tool.py`:

```python
"""Tests for diff_time_windows tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.diff_tool import DiffTool


def _make_engine(results):
    """Create a mock QueryEngine whose execute() returns `results` items in order."""
    engine = MagicMock()
    engine.execute = AsyncMock(side_effect=list(results))
    return engine


def _count_result(count: int) -> dict:
    """Shape a QueryEngine response around a single scalar count."""
    return {
        "source": "live",
        "data": {
            "columns": [{"name": "count"}],
            "rows": [[count]],
        },
        "metadata": {},
    }


class TestScalarDelta:
    @pytest.mark.asyncio
    async def test_identical_windows_produce_zero_delta(self):
        engine = _make_engine([_count_result(100), _count_result(100)])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},  # same label → same data (mocked)
        )

        assert result["current"]["total"] == 100
        assert result["comparison"]["total"] == 100
        # Spec: identical windows yield an empty delta (only significant rows surface).
        assert result["delta"] == []
        assert "no significant change" in result["summary"].lower()

    @pytest.mark.asyncio
    async def test_double_volume_yields_100_pct_delta(self):
        engine = _make_engine([_count_result(200), _count_result(100)])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_start": "2026-04-19T10:00:00Z", "time_end": "2026-04-19T11:00:00Z"},
        )

        assert result["current"]["total"] == 200
        assert result["comparison"]["total"] == 100
        assert result["delta"][0]["pct_change"] == pytest.approx(100.0)
        assert result["delta"][0]["tag"] == "spike"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_diff_tool.py -v`
Expected: FAIL with `ModuleNotFoundError: oci_logan_mcp.diff_tool`.

- [ ] **Step 3: Write minimal implementation**

Create `src/oci_logan_mcp/diff_tool.py`:

```python
"""diff_time_windows — before/after delta across two time windows."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

SIGNIFICANCE_THRESHOLD_PCT = 10.0
TOP_K_SUMMARY = 3


class DiffTool:
    """Run a query in two time windows and return a per-dimension delta."""

    def __init__(self, query_engine):
        self._engine = query_engine

    async def run(
        self,
        query: str,
        current_window: Dict[str, str],
        comparison_window: Dict[str, str],
        dimensions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        effective_dims = dimensions or []
        composed = self._compose_query(query, effective_dims)

        current_task = self._engine.execute(query=composed, **current_window)
        comparison_task = self._engine.execute(query=composed, **comparison_window)
        current_res, comparison_res = await asyncio.gather(current_task, comparison_task)

        current_rows = self._extract_rows(current_res, effective_dims)
        comparison_rows = self._extract_rows(comparison_res, effective_dims)

        delta = self._compute_delta(current_rows, comparison_rows, effective_dims)
        summary = self._build_summary(delta)

        return {
            "current": {"total": sum(r["count"] for r in current_rows), "rows": current_rows},
            "comparison": {"total": sum(r["count"] for r in comparison_rows), "rows": comparison_rows},
            "delta": delta,
            "summary": summary,
            "metadata": {
                "query": query,
                "composed_query": composed,
                "dimensions": effective_dims,
                "current_window": current_window,
                "comparison_window": comparison_window,
            },
        }

    @staticmethod
    def _compose_query(query: str, dimensions: List[str]) -> str:
        if not dimensions:
            return f"{query} | stats count as count"
        dim_list = ", ".join(f"'{d}'" for d in dimensions)
        return f"{query} | stats count as count by {dim_list}"

    @staticmethod
    def _extract_rows(response: Dict[str, Any], dimensions: List[str]) -> List[Dict[str, Any]]:
        data = response.get("data", {}) or {}
        columns = [c.get("name") for c in data.get("columns", [])]
        rows_raw = data.get("rows", [])

        if not dimensions:
            count = 0
            if rows_raw and rows_raw[0]:
                count = int(rows_raw[0][0] or 0)
            return [{"key": ("__total__",), "count": count}]

        # Index by column name for safety (OCI can re-order columns).
        count_idx = columns.index("count") if "count" in columns else len(columns) - 1
        dim_idx = [columns.index(d) for d in dimensions if d in columns]

        extracted: List[Dict[str, Any]] = []
        for row in rows_raw:
            key = tuple(str(row[i]) for i in dim_idx)
            try:
                count = int(row[count_idx] or 0)
            except (TypeError, ValueError):
                count = 0
            extracted.append({"key": key, "count": count})
        return extracted

    @staticmethod
    def _compute_delta(
        current_rows: List[Dict[str, Any]],
        comparison_rows: List[Dict[str, Any]],
        dimensions: List[str],
    ) -> List[Dict[str, Any]]:
        curr = {r["key"]: r["count"] for r in current_rows}
        comp = {r["key"]: r["count"] for r in comparison_rows}
        all_keys = set(curr) | set(comp)

        def _label(key):
            if not dimensions:
                return "__total__"
            return ", ".join(f"{d}={v}" for d, v in zip(dimensions, key))

        rows = []
        for key in all_keys:
            c, p = curr.get(key, 0), comp.get(key, 0)
            if p == 0 and c == 0:
                pct, tag = 0.0, "stable"
            elif p == 0:
                pct, tag = float("inf"), "new"
            elif c == 0:
                pct, tag = -100.0, "disappeared"
            else:
                pct = (c - p) / p * 100.0
                if abs(pct) < SIGNIFICANCE_THRESHOLD_PCT:
                    tag = "stable"
                elif pct > 0:
                    tag = "spike"
                else:
                    tag = "drop"
            rows.append({
                "dimension": _label(key),
                "current": c,
                "comparison": p,
                "pct_change": pct if pct != float("inf") else None,  # JSON-safe
                "tag": tag,
            })
        # Spec contract: `delta` lists only *significant* changes. Stable rows
        # are omitted — callers can still see per-row totals via `current.rows`
        # / `comparison.rows`.
        significant = [r for r in rows if r["tag"] != "stable"]
        significant.sort(key=lambda r: (r["current"] + r["comparison"]), reverse=True)
        return significant

    @staticmethod
    def _build_summary(delta: List[Dict[str, Any]]) -> str:
        # `delta` is already stable-filtered by `_compute_delta`.
        if not delta:
            return "No significant change between windows."
        significant = delta
        # Rank by absolute pct_change weighted by volume; None (new) ranks highest.
        def _rank(r):
            pct = r["pct_change"]
            vol = max(r["current"], r["comparison"])
            if pct is None:
                return (1, vol)
            return (0, abs(pct) * vol)

        top = sorted(significant, key=_rank, reverse=True)[:TOP_K_SUMMARY]
        parts = []
        for r in top:
            if r["tag"] == "new":
                parts.append(f"{r['dimension']} is new ({r['current']} events)")
            elif r["tag"] == "disappeared":
                parts.append(f"{r['dimension']} disappeared (was {r['comparison']})")
            else:
                sign = "+" if r["pct_change"] > 0 else ""
                parts.append(f"{r['dimension']} {sign}{r['pct_change']:.0f}%")
        return "Significant change: " + "; ".join(parts) + "."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_diff_tool.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/diff_tool.py tests/test_diff_tool.py
git commit -m "feat(a2): DiffTool scalar count delta + summary heuristic"
```

---

## Task 2: Dimensioned breakout

**Files:**
- Modify: `src/oci_logan_mcp/diff_tool.py` (already handles it — add tests)
- Modify: `tests/test_diff_tool.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diff_tool.py`:

```python
def _grouped_result(rows_by_dim):
    """Shape a QueryEngine response around grouped rows (dim_val, count)."""
    return {
        "source": "live",
        "data": {
            "columns": [{"name": "host"}, {"name": "count"}],
            "rows": [[k, v] for k, v in rows_by_dim],
        },
        "metadata": {},
    }


class TestDimensionedDelta:
    @pytest.mark.asyncio
    async def test_dimensions_join_on_key(self):
        current = _grouped_result([("web-01", 400), ("web-02", 100)])
        comparison = _grouped_result([("web-01", 200), ("web-02", 100)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        composed = engine.execute.call_args_list[0].kwargs["query"]
        assert "stats count as count by 'host'" in composed

        by_key = {r["dimension"]: r for r in result["delta"]}
        # web-01 is a spike (present). web-02 is stable, therefore filtered out of `delta`.
        assert by_key["host=web-01"]["pct_change"] == pytest.approx(100.0)
        assert by_key["host=web-01"]["tag"] == "spike"
        assert "host=web-02" not in by_key
```

- [ ] **Step 2: Run test to verify it passes (implementation covers it)**

Run: `pytest tests/test_diff_tool.py::TestDimensionedDelta -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_diff_tool.py
git commit -m "test(a2): cover dimensioned breakout join semantics"
```

---

## Task 3: Reuse breakout from query's `by` clause

**Files:**
- Modify: `src/oci_logan_mcp/diff_tool.py`
- Modify: `tests/test_diff_tool.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diff_tool.py`:

```python
class TestReuseBreakout:
    def test_extract_by_clause_single_field(self):
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause("* | stats count by 'Host'") == ["Host"]

    def test_extract_by_clause_multiple_fields(self):
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause("* | stats count by 'Host', 'Status'") == ["Host", "Status"]

    def test_extract_by_clause_unquoted(self):
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause("* | stats count by Host") == ["Host"]

    def test_extract_by_clause_none(self):
        from oci_logan_mcp.diff_tool import _extract_by_clause
        assert _extract_by_clause("* | stats count") == []

    @pytest.mark.asyncio
    async def test_reuses_breakout_from_query_by_clause(self):
        current = _grouped_result([("web-01", 400)])
        comparison = _grouped_result([("web-01", 200)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs' | stats count by 'host'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            # dimensions omitted → reuse `by 'host'` from query
        )

        assert result["metadata"]["dimensions"] == ["host"]
        # The user-provided stats is preserved; no double-append.
        composed = engine.execute.call_args_list[0].kwargs["query"]
        assert composed.count("stats count") == 1
        assert result["delta"][0]["dimension"] == "host=web-01"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_diff_tool.py::TestReuseBreakout -v`
Expected: FAIL — `_extract_by_clause` not exported; breakout-reuse not wired.

- [ ] **Step 3: Implement `_extract_by_clause` and wire into `run()`**

Add to `src/oci_logan_mcp/diff_tool.py` (top-level function, not a method — it's pure):

```python
import re

_BY_CLAUSE_RE = re.compile(r"\bby\s+(.+?)(?:\s*\|\s*|\s*$)", re.IGNORECASE)


def _extract_by_clause(query: str) -> List[str]:
    """Extract dimension field names from a trailing `by <fields>` clause.

    Handles single/multiple fields, quoted or unquoted. Returns [] if none.
    Strips surrounding single quotes and whitespace.
    """
    m = _BY_CLAUSE_RE.search(query)
    if not m:
        return []
    raw = m.group(1).strip()
    return [p.strip().strip("'").strip('"') for p in raw.split(",") if p.strip()]
```

Then change `DiffTool.run()` to:

```python
    async def run(self, query, current_window, comparison_window, dimensions=None):
        reused_breakout = False
        if dimensions is None:
            dimensions = _extract_by_clause(query)
            reused_breakout = bool(dimensions)

        # If we reused the query's own `by` clause, don't re-append stats —
        # the query already aggregates. Otherwise compose the stats pipe.
        composed = query if reused_breakout else self._compose_query(query, dimensions)
        # … rest unchanged; pass `dimensions` into _extract_rows.
```

And in the response `metadata`, set `"dimensions": dimensions` (so the agent can see what we picked) and add `"reused_breakout": reused_breakout` — a boolean telling the caller whether A2 added the breakout or just reused the caller's own.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_diff_tool.py -v`
Expected: PASS (all Task-1/2/3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/diff_tool.py tests/test_diff_tool.py
git commit -m "feat(a2): reuse breakout from query's 'by' clause when dimensions omitted"
```

---

## Task 4: Graceful missing-dimension handling

**Files:**
- Modify: `tests/test_diff_tool.py`

The behavior is already in `_compute_delta` (all_keys = union). This task just pins it with a test so a future refactor can't silently drop asymmetric rows.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diff_tool.py`:

```python
class TestAsymmetricWindows:
    @pytest.mark.asyncio
    async def test_new_value_in_current_only(self):
        current = _grouped_result([("web-01", 100), ("web-99-new", 50)])
        comparison = _grouped_result([("web-01", 100)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        by_key = {r["dimension"]: r for r in result["delta"]}
        assert by_key["host=web-99-new"] == {
            "dimension": "host=web-99-new",
            "current": 50,
            "comparison": 0,
            "pct_change": None,
            "tag": "new",
        }

    @pytest.mark.asyncio
    async def test_disappeared_value_in_current_only(self):
        current = _grouped_result([("web-01", 100)])
        comparison = _grouped_result([("web-01", 100), ("web-99-gone", 200)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        by_key = {r["dimension"]: r for r in result["delta"]}
        assert by_key["host=web-99-gone"]["tag"] == "disappeared"
        assert by_key["host=web-99-gone"]["pct_change"] == -100.0
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_diff_tool.py::TestAsymmetricWindows -v`
Expected: PASS (already implemented in Task 1).

- [ ] **Step 3: Commit**

```bash
git add tests/test_diff_tool.py
git commit -m "test(a2): pin asymmetric-window new/disappeared semantics"
```

---

## Task 5: Summary ranking sanity

**Files:**
- Modify: `tests/test_diff_tool.py`

Pin the weighted-rank heuristic so future changes don't let low-volume noise dominate the summary.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diff_tool.py`:

```python
class TestSummary:
    @pytest.mark.asyncio
    async def test_summary_prefers_high_volume_change(self):
        # web-01: 10000 → 12000 (+20%, high volume) — should be named
        # web-02: 1 → 5 (+400%, tiny volume) — should NOT dominate
        current = _grouped_result([("web-01", 12000), ("web-02", 5)])
        comparison = _grouped_result([("web-01", 10000), ("web-02", 1)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        # Summary names web-01 before web-02 despite web-02's larger pct.
        idx_01 = result["summary"].find("web-01")
        idx_02 = result["summary"].find("web-02")
        assert idx_01 != -1
        assert idx_01 < idx_02 or idx_02 == -1

    @pytest.mark.asyncio
    async def test_summary_quiet_when_all_stable(self):
        current = _grouped_result([("web-01", 100), ("web-02", 105)])
        comparison = _grouped_result([("web-01", 100), ("web-02", 100)])
        engine = _make_engine([current, comparison])
        tool = DiffTool(engine)

        result = await tool.run(
            query="'Log Source' = 'Audit Logs'",
            current_window={"time_range": "last_1_hour"},
            comparison_window={"time_range": "last_1_hour"},
            dimensions=["host"],
        )

        assert "no significant change" in result["summary"].lower()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_diff_tool.py::TestSummary -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_diff_tool.py
git commit -m "test(a2): pin summary ranking (volume-weighted)"
```

---

## Task 6: Register `diff_time_windows` MCP tool

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_read_only_guard.py`

- [ ] **Step 1: Write the failing handler test**

Append to `tests/test_handlers.py`. The file already exposes a `handlers` fixture (tests/test_handlers.py:111) and already imports `AsyncMock` — no new imports needed.

```python
class TestDiffTimeWindows:
    @pytest.mark.asyncio
    async def test_diff_time_windows_routes_through_handler(self, handlers):
        """diff_time_windows tool routes to DiffTool and returns JSON payload."""
        # Stub DiffTool.run to avoid calling QueryEngine in unit tests.
        handlers.diff_tool.run = AsyncMock(return_value={
            "current": {"total": 100, "rows": []},
            "comparison": {"total": 100, "rows": []},
            "delta": [],
            "summary": "No significant change between windows.",
            "metadata": {},
        })

        result = await handlers.handle_tool_call(
            "diff_time_windows",
            {
                "query": "'Log Source' = 'Audit Logs'",
                "current_window": {"time_range": "last_1_hour"},
                "comparison_window": {"time_range": "last_1_hour"},
            },
        )

        assert result[0]["type"] == "text"
        payload = json.loads(result[0]["text"])
        assert payload["summary"] == "No significant change between windows."
        handlers.diff_tool.run.assert_awaited_once()
```

- [ ] **Step 2: Run the handler test to verify it fails**

Run: `pytest tests/test_handlers.py::TestDiffTimeWindows -v`
Expected: FAIL with "Unknown tool: diff_time_windows".

- [ ] **Step 3: Register schema in `tools.py`**

Insert after `run_batch_queries` (~line 209) in `src/oci_logan_mcp/tools.py`:

```python
        {
            "name": "diff_time_windows",
            "description": (
                "Compare the same query across two time windows. Returns per-dimension "
                "deltas (spike/drop/new/disappeared) plus a one-line summary. Cheapest "
                "triage primitive: 'what's different about this hour vs. yesterday?'"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Base Log Analytics query run in both windows",
                    },
                    "current_window": {
                        "type": "object",
                        "description": "Current window: {time_range: '...'} OR {time_start, time_end} (ISO 8601)",
                    },
                    "comparison_window": {
                        "type": "object",
                        "description": "Comparison window, same shape as current_window",
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional. Fields to break out by. If omitted, extracted from the query's 'by' clause; else a scalar total delta.",
                    },
                },
                "required": ["query", "current_window", "comparison_window"],
            },
        },
```

- [ ] **Step 4: Wire the handler**

In `src/oci_logan_mcp/handlers.py`:

1. Import at top: `from .diff_tool import DiffTool` and `from .budget_tracker import BudgetExceededError`.
2. In `__init__` (after `self.query_engine = QueryEngine(...)` around line 92), add: `self.diff_tool = DiffTool(self.query_engine)`.
3. In `handle_tool_call`'s `handlers` dict (~line 127), add: `"diff_time_windows": self._diff_time_windows,`.
4. Add the handler method near `_run_query`:

```python
    async def _diff_time_windows(self, args: Dict) -> List[Dict]:
        """Run diff_time_windows. Catches budget breaches and returns them as a
        structured payload instead of letting the generic exception path
        stringify them — A1 relies on this shape."""
        try:
            result = await self.diff_tool.run(
                query=args["query"],
                current_window=args["current_window"],
                comparison_window=args["comparison_window"],
                dimensions=args.get("dimensions"),
            )
        except BudgetExceededError as e:
            payload = {
                "status": "budget_exceeded",
                "error": str(e),
                "partial": None,
                "budget": self._budget_tracker.snapshot().to_dict(),
            }
            return [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]
        return [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
```

Add a test for the budget-exceeded shape in `tests/test_handlers.py`:

```python
    @pytest.mark.asyncio
    async def test_diff_time_windows_budget_exceeded_structured(self, handlers):
        """BudgetExceededError surfaces as a structured payload, not plain text."""
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        handlers.diff_tool.run = AsyncMock(side_effect=BudgetExceededError("bytes limit hit"))

        result = await handlers.handle_tool_call(
            "diff_time_windows",
            {
                "query": "*",
                "current_window": {"time_range": "last_1_hour"},
                "comparison_window": {"time_range": "last_1_hour"},
            },
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "bytes limit hit" in payload["error"]
```

- [ ] **Step 5: Keep the read-only drift test happy**

`src/oci_logan_mcp/read_only_guard.py` uses a **denylist** (`MUTATING_TOOLS`). `diff_time_windows` is a pure reader, so it does **not** belong in `MUTATING_TOOLS` — do not modify `read_only_guard.py`.

The drift-catching test in `tests/test_read_only_guard.py` asserts every registered tool is classified, i.e. `registered - MUTATING_TOOLS - KNOWN_READERS == set()`. Add `"diff_time_windows"` to the `KNOWN_READERS` set in that test file (only). Nothing else.

- [ ] **Step 6: Run all affected tests**

Run: `pytest tests/test_handlers.py tests/test_read_only_guard.py tests/test_diff_tool.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite — nothing else should regress**

Run: `pytest tests/ -x -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/oci_logan_mcp/diff_tool.py src/oci_logan_mcp/tools.py \
        src/oci_logan_mcp/handlers.py \
        tests/test_diff_tool.py tests/test_handlers.py tests/test_read_only_guard.py
git commit -m "feat(a2): register diff_time_windows MCP tool"
```

---

## Task 7: README + smoke entry

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a docs section**

Under the existing tool-listing section, add:

```markdown
### `diff_time_windows` — before/after triage

Compare the same Log Analytics query across two time windows and get a per-dimension delta plus a one-line summary. Cheapest high-signal investigation primitive.

```json
{
  "tool": "diff_time_windows",
  "query": "'Log Source' = 'Audit Logs' | stats count by 'User Name'",
  "current_window": {"time_range": "last_1_hour"},
  "comparison_window": {"time_start": "2026-04-19T10:00:00Z", "time_end": "2026-04-19T11:00:00Z"}
}
```

Returns `{current, comparison, delta: [...], summary}` where each delta row is tagged `spike | drop | new | disappeared | stable`.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(a2): document diff_time_windows tool"
```

---

## Verification checklist

Before marking the plan done:

- [ ] `pytest tests/test_diff_tool.py -v` — all green, covers the 4 spec scenarios (identical, 2×, breakout-reuse, asymmetric) plus summary ranking.
- [ ] `pytest tests/ -x -q` — full suite still green.
- [ ] `diff_time_windows` appears in `get_tools()` output (`python -c "from oci_logan_mcp.tools import get_tools; print([t['name'] for t in get_tools()])"`).
- [ ] Read-only guard allows `diff_time_windows` (reader — not in `MUTATING_TOOLS`; present in test-file `KNOWN_READERS`).
- [ ] No changes to `query_engine.py`, `time_parser.py`, `budget_tracker.py`, `read_only_guard.py` — A2 is a pure consumer.
- [ ] README has the new section.

## Post-landing follow-ups (do not do in this plan)

- A4 `pivot_on_entity` is a sibling primitive; implement via a separate plan (parallel subagent track).
- Once A4 ships, A1 `investigate_incident` orchestrator can consume both A2 and A4.
- If query-parsing needs grow (e.g., A3 rare-events), replace the regex-based `_extract_by_clause` with a shared Logan parser utility.
