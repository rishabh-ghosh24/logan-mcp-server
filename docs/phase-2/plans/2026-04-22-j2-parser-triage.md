# J2 — Parser Failure Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `parser_failure_triage` MCP tool that runs two Logan queries — an aggregate stats query and a per-parser samples query — and returns the top N parsers by failure count with up to 3 sample raw lines each.

**Architecture:** A standalone `parser_triage.py` module follows the same structure as `ingestion_health.py`: pure query-builder functions, pure response-parser functions, and a `ParserTriageTool` class that orchestrates two sequential `query_engine.execute()` calls. Stats are fetched first; if empty the samples query is skipped entirely. The handler in `handlers.py` catches `BudgetExceededError` like all other triage tools.

**Tech Stack:** Python asyncio, OCI Log Analytics (Logan query engine), pytest + pytest-asyncio, existing `QueryEngine` and `MCPHandlers` patterns.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/oci_logan_mcp/parser_triage.py` | Create | Query builders, response parsers, `ParserTriageTool` |
| `src/oci_logan_mcp/tools.py` | Modify | Add `parser_failure_triage` schema after `ingestion_health` |
| `src/oci_logan_mcp/handlers.py` | Modify | Import, instantiate, route, handler method |
| `tests/test_parser_triage.py` | Create | Unit + orchestration tests |
| `tests/test_handlers.py` | Modify | `TestParserFailureTriage` routing + budget tests |
| `tests/test_read_only_guard.py` | Modify | Add `"parser_failure_triage"` to `KNOWN_READERS` |
| `README.md` | Modify | Add `### parser_failure_triage` under Investigation Toolkit |

---

### Task 1: Query builders

**Files:**
- Create: `src/oci_logan_mcp/parser_triage.py`
- Create: `tests/test_parser_triage.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_parser_triage.py
"""Tests for parser_failure_triage tool."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.parser_triage import _build_stats_query, _build_samples_query


class TestQueryBuilders:
    def test_stats_query_includes_failure_source_filter(self):
        q = _build_stats_query(top_n=10)
        assert "'Log Source' = 'Parser Failure'" in q

    def test_stats_query_aggregates_count_first_last(self):
        q = _build_stats_query(top_n=10)
        assert "stats count as failure_count" in q
        assert "earliest('Time') as first_seen" in q
        assert "latest('Time') as last_seen" in q
        assert "by 'Parser Name', 'Log Source'" in q

    def test_stats_query_sorts_and_limits_to_top_n(self):
        q = _build_stats_query(top_n=7)
        assert "sort -failure_count" in q
        assert "| head 7" in q

    def test_samples_query_filters_to_given_parser_names(self):
        q = _build_samples_query(["Apache Parser", "Syslog"])
        assert "'Log Source' = 'Parser Failure'" in q
        assert "'Parser Name' in ('Apache Parser', 'Syslog')" in q
        assert "'Original Log Content'" in q

    def test_samples_query_limits_to_3x_parser_count(self):
        q = _build_samples_query(["A", "B", "C"])  # 3 parsers × 3 = 9
        assert "| head 9" in q

    def test_samples_query_escapes_embedded_single_quotes(self):
        q = _build_samples_query(["Bob's Parser"])
        assert "'Bob''s Parser'" in q
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_parser_triage.py::TestQueryBuilders -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'oci_logan_mcp.parser_triage'`

- [ ] **Step 3: Create `parser_triage.py` with the two query builders**

```python
# src/oci_logan_mcp/parser_triage.py
"""parser_failure_triage — surface top parser failures with sample raw lines."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _build_stats_query(top_n: int) -> str:
    return (
        "'Log Source' = 'Parser Failure' | "
        "stats count as failure_count, "
        "earliest('Time') as first_seen, "
        "latest('Time') as last_seen "
        "by 'Parser Name', 'Log Source' | "
        f"sort -failure_count | head {top_n}"
    )


def _build_samples_query(parser_names: List[str]) -> str:
    escaped = ", ".join(
        f"'{n.replace(chr(39), chr(39) * 2)}'" for n in parser_names
    )
    return (
        f"'Log Source' = 'Parser Failure' AND 'Parser Name' in ({escaped}) | "
        f"fields 'Parser Name', 'Original Log Content' | "
        f"head {len(parser_names) * 3}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_parser_triage.py::TestQueryBuilders -v
```
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/parser_triage.py tests/test_parser_triage.py
git commit -m "feat(j2): query builders for parser_failure_triage"
```

---

### Task 2: Response parsers

**Files:**
- Modify: `src/oci_logan_mcp/parser_triage.py`
- Modify: `tests/test_parser_triage.py`

The stats query response has columns `["Parser Name", "Log Source", "failure_count", "first_seen", "last_seen"]`.
The samples query response has columns `["Parser Name", "Original Log Content"]`.
Both follow the same `{"data": {"columns": [...], "rows": [...]}}` shape as every other Logan response.

- [ ] **Step 1: Write the failing tests**

Add after `TestQueryBuilders` in `tests/test_parser_triage.py`:

```python
# ---------------------------------------------------------------------------
# Helpers shared by parser tests
# ---------------------------------------------------------------------------

def _stats_resp(rows):
    return {
        "data": {
            "columns": [
                {"name": "Parser Name"}, {"name": "Log Source"},
                {"name": "failure_count"}, {"name": "first_seen"}, {"name": "last_seen"},
            ],
            "rows": rows,
        }
    }


def _samples_resp(rows):
    return {
        "data": {
            "columns": [
                {"name": "Parser Name"}, {"name": "Original Log Content"},
            ],
            "rows": rows,
        }
    }


class TestResponseParsers:
    def test_parse_stats_basic(self):
        from oci_logan_mcp.parser_triage import _parse_stats_response
        resp = _stats_resp([
            ["Apache Parser", "Apache Access", 42,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        result = _parse_stats_response(resp)
        assert len(result) == 1
        r = result[0]
        assert r["parser_name"] == "Apache Parser"
        assert r["source"] == "Apache Access"
        assert r["failure_count"] == 42
        assert r["first_seen"] == "2026-04-22T00:00:00Z"
        assert r["last_seen"] == "2026-04-22T09:00:00Z"

    def test_parse_stats_empty_response(self):
        from oci_logan_mcp.parser_triage import _parse_stats_response
        result = _parse_stats_response({"data": {"columns": [], "rows": []}})
        assert result == []

    def test_parse_stats_missing_data_key(self):
        from oci_logan_mcp.parser_triage import _parse_stats_response
        result = _parse_stats_response({})
        assert result == []

    def test_parse_samples_groups_by_parser(self):
        from oci_logan_mcp.parser_triage import _parse_samples_response
        resp = _samples_resp([
            ["Apache Parser", "raw1"],
            ["Apache Parser", "raw2"],
            ["Apache Parser", "raw3"],
            ["Apache Parser", "raw4"],  # 4th sample → capped at 3
            ["Syslog Parser", "syslog_raw"],
        ])
        samples = _parse_samples_response(resp)
        assert samples["Apache Parser"] == ["raw1", "raw2", "raw3"]
        assert samples["Syslog Parser"] == ["syslog_raw"]

    def test_parse_samples_empty_response(self):
        from oci_logan_mcp.parser_triage import _parse_samples_response
        result = _parse_samples_response({"data": {"columns": [], "rows": []}})
        assert result == {}

    def test_merge_results_attaches_samples(self):
        from oci_logan_mcp.parser_triage import _merge_results
        stats = [{
            "parser_name": "Apache Parser",
            "source": "Apache Access",
            "failure_count": 10,
            "first_seen": "2026-04-22T00:00:00Z",
            "last_seen": "2026-04-22T09:00:00Z",
        }]
        samples = {"Apache Parser": ["line1", "line2"]}
        result = _merge_results(stats, samples)
        assert result[0]["sample_raw_lines"] == ["line1", "line2"]

    def test_merge_results_empty_samples_for_parser_with_no_raw_data(self):
        from oci_logan_mcp.parser_triage import _merge_results
        stats = [{
            "parser_name": "Silent Parser",
            "source": "X",
            "failure_count": 5,
            "first_seen": "2026-04-22T00:00:00Z",
            "last_seen": "2026-04-22T09:00:00Z",
        }]
        result = _merge_results(stats, {})
        assert result[0]["sample_raw_lines"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_parser_triage.py::TestResponseParsers -v
```
Expected: FAIL with `ImportError: cannot import name '_parse_stats_response'`

- [ ] **Step 3: Add the three parser functions to `parser_triage.py`**

Append after `_build_samples_query`:

```python
def _parse_stats_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", [])]
    rows = data.get("rows", [])
    required = {"Parser Name", "Log Source", "failure_count", "first_seen", "last_seen"}
    if not required.issubset(set(columns)):
        return []
    pn_idx = columns.index("Parser Name")
    src_idx = columns.index("Log Source")
    cnt_idx = columns.index("failure_count")
    fs_idx = columns.index("first_seen")
    ls_idx = columns.index("last_seen")
    out = []
    for row in rows:
        if not row:
            continue
        out.append({
            "parser_name": str(row[pn_idx]),
            "source": str(row[src_idx]),
            "failure_count": int(row[cnt_idx]),
            "first_seen": str(row[fs_idx]) if row[fs_idx] is not None else None,
            "last_seen": str(row[ls_idx]) if row[ls_idx] is not None else None,
        })
    return out


def _parse_samples_response(response: Dict[str, Any]) -> Dict[str, List[str]]:
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", [])]
    rows = data.get("rows", [])
    if "Parser Name" not in columns or "Original Log Content" not in columns:
        return {}
    pn_idx = columns.index("Parser Name")
    raw_idx = columns.index("Original Log Content")
    out: Dict[str, List[str]] = {}
    for row in rows:
        if not row:
            continue
        name = str(row[pn_idx])
        raw = str(row[raw_idx]) if row[raw_idx] is not None else ""
        bucket = out.setdefault(name, [])
        if len(bucket) < 3:
            bucket.append(raw)
    return out


def _merge_results(
    stats: List[Dict[str, Any]],
    samples: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    out = []
    for entry in stats:
        out.append({
            **entry,
            "sample_raw_lines": samples.get(entry["parser_name"], []),
        })
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_parser_triage.py::TestResponseParsers -v
```
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/parser_triage.py tests/test_parser_triage.py
git commit -m "feat(j2): response parsers and merge for parser_failure_triage"
```

---

### Task 3: ParserTriageTool orchestrator

**Files:**
- Modify: `src/oci_logan_mcp/parser_triage.py`
- Modify: `tests/test_parser_triage.py`

The `run()` method:
1. Runs the stats query to get the top N parsers.
2. If stats is empty, returns `{failures: [], total_failure_count: 0}` without running the samples query.
3. Otherwise, runs the samples query filtered to those parser names.
4. Merges and returns the final result.

- [ ] **Step 1: Write the failing tests**

Add a fixture and `TestOrchestration` class to `tests/test_parser_triage.py`:

```python
# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_engine(*responses):
    """Mock engine whose execute() returns responses in order."""
    engine = MagicMock()
    engine.execute = AsyncMock(side_effect=list(responses))
    return engine


def _make_settings():
    from oci_logan_mcp.config import Settings
    return Settings()


class TestOrchestration:
    @pytest.mark.asyncio
    async def test_run_aggregates_failures_and_attaches_samples(self):
        from oci_logan_mcp.parser_triage import ParserTriageTool
        stats_resp = _stats_resp([
            ["Apache Parser", "Apache Access", 42,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
            ["Syslog Parser", "Linux Syslog", 7,
             "2026-04-22T01:00:00Z", "2026-04-22T08:00:00Z"],
        ])
        samples_resp = _samples_resp([
            ["Apache Parser", "malformed line 1"],
            ["Apache Parser", "malformed line 2"],
            ["Syslog Parser", "bad syslog line"],
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run(time_range="last_24h", top_n=20)

        assert result["total_failure_count"] == 49  # 42 + 7
        assert len(result["failures"]) == 2
        by_parser = {f["parser_name"]: f for f in result["failures"]}
        assert by_parser["Apache Parser"]["failure_count"] == 42
        assert by_parser["Apache Parser"]["sample_raw_lines"] == [
            "malformed line 1", "malformed line 2",
        ]
        assert by_parser["Syslog Parser"]["sample_raw_lines"] == ["bad syslog line"]

    @pytest.mark.asyncio
    async def test_run_empty_result_skips_samples_query(self):
        from oci_logan_mcp.parser_triage import ParserTriageTool
        engine = _make_engine(_stats_resp([]))
        tool = ParserTriageTool(engine)

        result = await tool.run()

        assert result["total_failure_count"] == 0
        assert result["failures"] == []
        # samples query must NOT be called when stats are empty
        assert engine.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_run_passes_time_range_and_top_n_to_engine(self):
        from oci_logan_mcp.parser_triage import ParserTriageTool
        engine = _make_engine(_stats_resp([]))
        tool = ParserTriageTool(engine)

        await tool.run(time_range="last_4_hours", top_n=5)

        kwargs = engine.execute.call_args.kwargs
        assert kwargs["time_range"] == "last_4_hours"
        assert "| head 5" in kwargs["query"]

    @pytest.mark.asyncio
    async def test_run_samples_capped_at_three_per_parser(self):
        from oci_logan_mcp.parser_triage import ParserTriageTool
        stats_resp = _stats_resp([
            ["Noisy Parser", "Source A", 100,
             "2026-04-22T00:00:00Z", "2026-04-22T09:00:00Z"],
        ])
        # Engine returns 5 sample lines for the one parser.
        samples_resp = _samples_resp([
            ["Noisy Parser", f"raw line {i}"] for i in range(5)
        ])
        engine = _make_engine(stats_resp, samples_resp)
        tool = ParserTriageTool(engine)

        result = await tool.run()

        assert len(result["failures"][0]["sample_raw_lines"]) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_parser_triage.py::TestOrchestration -v
```
Expected: FAIL with `ImportError: cannot import name 'ParserTriageTool'`

- [ ] **Step 3: Add `ParserTriageTool` to `parser_triage.py`**

Append at the bottom of the file:

```python
class ParserTriageTool:
    """Run two Logan queries to surface top parser failures with sample lines."""

    def __init__(self, query_engine):
        self._engine = query_engine

    async def run(
        self,
        time_range: str = "last_24h",
        top_n: int = 20,
    ) -> Dict[str, Any]:
        stats_query = _build_stats_query(top_n)
        stats_resp = await self._engine.execute(
            query=stats_query,
            time_range=time_range,
        )
        stats = _parse_stats_response(stats_resp)

        if not stats:
            return {"failures": [], "total_failure_count": 0}

        parser_names = [s["parser_name"] for s in stats]
        samples_query = _build_samples_query(parser_names)
        samples_resp = await self._engine.execute(
            query=samples_query,
            time_range=time_range,
        )
        samples = _parse_samples_response(samples_resp)

        failures = _merge_results(stats, samples)
        total = sum(f["failure_count"] for f in failures)
        return {"failures": failures, "total_failure_count": total}
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_parser_triage.py -v
```
Expected: all tests PASS

- [ ] **Step 5: Run the full suite to confirm no regressions**

```
pytest --tb=short -q
```
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/parser_triage.py tests/test_parser_triage.py
git commit -m "feat(j2): ParserTriageTool orchestrator with two-query flow"
```

---

### Task 4: Tool schema

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `tests/test_parser_triage.py`

The `ingestion_health` entry ends at line ~318 with `},` (closing the inputSchema dict) followed by `# Visualization Tools`. Add the new schema between those two.

- [ ] **Step 1: Write a failing test**

Add to `tests/test_parser_triage.py`:

```python
class TestToolSchema:
    def test_parser_failure_triage_schema_present(self):
        from oci_logan_mcp.tools import get_tools
        names = [t["name"] for t in get_tools()]
        assert "parser_failure_triage" in names

    def test_parser_failure_triage_schema_properties(self):
        from oci_logan_mcp.tools import get_tools
        tool = next(t for t in get_tools() if t["name"] == "parser_failure_triage")
        props = tool["inputSchema"]["properties"]
        assert "time_range" in props
        assert "top_n" in props
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_parser_triage.py::TestToolSchema -v
```
Expected: FAIL with `AssertionError` (tool not in list)

- [ ] **Step 3: Add the schema to `tools.py`**

Find the closing brace of the `ingestion_health` entry and the `# Visualization Tools` comment. Insert after the `ingestion_health` closing `},`:

```python
        {
            "name": "parser_failure_triage",
            "description": (
                "Surface the top parser failures ranked by failure count. "
                "Returns up to top_n parsers, each with failure_count, "
                "the source they belong to, first/last seen timestamps, "
                "and up to 3 sample raw lines that failed to parse. "
                "Use this to identify which parsers need fixing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "time_range": {
                        "type": "string",
                        "description": (
                            "Time window to scan for parser failures. "
                            "Default: 'last_24h'. Accepts Logan time strings "
                            "such as 'last_1_hour', 'last_7_days'."
                        ),
                    },
                    "top_n": {
                        "type": "integer",
                        "description": (
                            "Maximum number of parsers to return, ranked by "
                            "failure count descending. Default: 20."
                        ),
                    },
                },
            },
        },
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_parser_triage.py::TestToolSchema -v
```
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/tools.py tests/test_parser_triage.py
git commit -m "feat(j2): register parser_failure_triage tool schema"
```

---

### Task 5: Handler, routing, and read-only guard

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_read_only_guard.py`

- [ ] **Step 1: Write failing handler tests**

Add at the end of `tests/test_handlers.py`:

```python
class TestParserFailureTriage:
    @pytest.mark.asyncio
    async def test_routes_to_parser_triage_tool(self, handlers):
        """parser_failure_triage routes to ParserTriageTool and returns JSON."""
        handlers.parser_triage_tool.run = AsyncMock(return_value={
            "failures": [],
            "total_failure_count": 0,
        })

        result = await handlers.handle_tool_call(
            "parser_failure_triage",
            {"time_range": "last_24h", "top_n": 5},
        )

        assert result[0]["type"] == "text"
        payload = json.loads(result[0]["text"])
        assert "failures" in payload
        assert payload["total_failure_count"] == 0
        handlers.parser_triage_tool.run.assert_awaited_once_with(
            time_range="last_24h",
            top_n=5,
        )

    @pytest.mark.asyncio
    async def test_budget_exceeded_returns_structured_payload(self, handlers):
        """BudgetExceededError surfaces as structured payload, not plain text."""
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        handlers.parser_triage_tool.run = AsyncMock(
            side_effect=BudgetExceededError("query limit hit")
        )

        result = await handlers.handle_tool_call(
            "parser_failure_triage",
            {},
        )

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "query limit hit" in payload["error"]
        assert "budget" in payload
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_handlers.py::TestParserFailureTriage -v
```
Expected: FAIL (tool not in dispatch dict)

- [ ] **Step 3: Wire the handler into `handlers.py`**

**3a.** Add the import after the `IngestionHealthTool` import line (~line 31):

```python
from .parser_triage import ParserTriageTool
```

**3b.** Add instantiation after `self.ingestion_health_tool = IngestionHealthTool(...)` (~line 101):

```python
        self.parser_triage_tool = ParserTriageTool(self.query_engine)
```

**3c.** Add the route after `"ingestion_health": self._ingestion_health,` in `handle_tool_call` (~line 138):

```python
            "parser_failure_triage": self._parser_failure_triage,
```

**3d.** Add the handler method after `_ingestion_health` (~line 709):

```python
    async def _parser_failure_triage(self, args: Dict) -> List[Dict]:
        try:
            result = await self.parser_triage_tool.run(
                time_range=args.get("time_range", "last_24h"),
                top_n=int(args.get("top_n", 20)),
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

- [ ] **Step 4: Add `"parser_failure_triage"` to `KNOWN_READERS` in `tests/test_read_only_guard.py`**

Find the `KNOWN_READERS` set (~line 114). Add `"parser_failure_triage"` alongside `"ingestion_health"`:

```python
    KNOWN_READERS = {
        "list_log_sources", "list_fields", "list_entities", "list_parsers",
        "list_labels", "list_saved_searches", "list_log_groups",
        "validate_query", "run_query", "run_saved_search", "run_batch_queries",
        "diff_time_windows",
        "pivot_on_entity",
        "ingestion_health",
        "parser_failure_triage",
        "visualize", "export_results",
        "get_current_context", "list_compartments",
        "test_connection", "find_compartment",
        "get_query_examples", "get_log_summary",
        "get_preferences", "list_alerts", "list_dashboards",
        "explain_query", "get_session_budget",
        "export_transcript",
    }
```

- [ ] **Step 5: Run all affected tests**

```
pytest tests/test_handlers.py::TestParserFailureTriage tests/test_read_only_guard.py -v
```
Expected: all PASS

- [ ] **Step 6: Run full suite**

```
pytest --tb=short -q
```
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add src/oci_logan_mcp/handlers.py tests/test_handlers.py tests/test_read_only_guard.py
git commit -m "feat(j2): wire parser_failure_triage handler and read-only guard"
```

---

### Task 6: README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the `parser_failure_triage` entry under `## Investigation Toolkit`**

In `README.md`, find the `## Investigation Toolkit` section and append after the `### ingestion_health` block (before `## Multi-User Learning`):

```markdown
### `parser_failure_triage` — which parsers are broken?

Surface the top parser failures ranked by volume. Returns up to 20 parsers, each with failure count, first/last seen timestamps, and up to 3 sample raw lines that failed to parse. Use this to identify which parsers need fixing before investigating an incident.

```json
{
  "tool": "parser_failure_triage",
  "time_range": "last_24h",
  "top_n": 10
}
```

Returns `{failures: [...], total_failure_count: N}` where each entry carries `parser_name`, `source`, `failure_count`, `first_seen`, `last_seen`, and `sample_raw_lines` (up to 3).
```

- [ ] **Step 2: Update the capability table row**

In `README.md`, find the `| **Triage diffs** |` row in the `## What You Can Do` table and add `parser_failure_triage` to the tools cell:

```markdown
| **Triage diffs** | `diff_time_windows`, `pivot_on_entity`, `ingestion_health`, `parser_failure_triage` | Compare a query across two time windows; pull all events for an entity across sources; probe per-source ingestion freshness; surface top parser failures with sample lines |
```

- [ ] **Step 3: Verify tests still pass**

```
pytest --tb=short -q
```
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(j2): add parser_failure_triage to README Investigation Toolkit"
```

---

## Self-Review

### Spec coverage
- ✅ `parser_failure_triage(time_range, top_n)` tool interface implemented
- ✅ Returns `{failures: [...], total_failure_count}` shape
- ✅ Each failure has `parser_name`, `source`, `failure_count`, `sample_raw_lines` (≤3), `first_seen`, `last_seen`
- ✅ Uses `Log Source = 'Parser Failure'` query pattern
- ✅ Aggregate + rank + sample — pure query + post-processing
- ✅ Test 1: N failure events aggregated correctly (`test_run_aggregates_failures_and_attaches_samples`)
- ✅ Test 2: samples limited to 3 per parser (`test_run_samples_capped_at_three_per_parser`)
- ✅ Test 3: empty result → empty list, `total_failure_count=0` (`test_run_empty_result_skips_samples_query`)

### No placeholders
All steps contain complete code. No "TBD" or "add error handling" filler.

### Type consistency
- `_build_stats_query(top_n: int) -> str` — used in `ParserTriageTool.run()`
- `_build_samples_query(parser_names: List[str]) -> str` — used with `[s["parser_name"] for s in stats]`
- `_parse_stats_response(response) -> List[Dict]` — each dict has keys: `parser_name`, `source`, `failure_count`, `first_seen`, `last_seen`
- `_parse_samples_response(response) -> Dict[str, List[str]]` — maps `parser_name → [raw_line, ...]`
- `_merge_results(stats, samples) -> List[Dict]` — adds `sample_raw_lines` key from samples dict, consistent with spec output
- `ParserTriageTool.run()` returns `{"failures": [...], "total_failure_count": int}` — matches spec and handler test assertions
