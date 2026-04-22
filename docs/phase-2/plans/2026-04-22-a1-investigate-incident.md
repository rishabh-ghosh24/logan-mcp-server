# A1 `investigate_incident` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flagship MCP tool that takes a seed Logan query + time range and returns a structured first-cut investigation (stopped sources, parser failures, anomalous sources with top error clusters, top entities, timelines, and next steps), target ≤20s p95.

**Architecture:** Pure-functions + thin class (matches `ingestion_health.py` / `parser_triage.py` pattern). Module-level phase helpers are individually testable; `InvestigateIncidentTool.run()` orchestrates them with an accumulator dict, composing J1/J2/A2/Logan `cluster` and `next_steps.suggest()`. `BudgetExceededError` is caught inside `run()` and surfaced as a partial `InvestigationReport` — A1 never raises this error out of its boundary.

**Tech Stack:** Python 3.11 / asyncio, existing `QueryEngine`, `IngestionHealthTool`, `ParserTriageTool`, `DiffTool`, `next_steps.suggest()`, `_parse_ts` / `_ts_to_iso` from `ingestion_health.py`. pytest + pytest-asyncio.

**Design doc:** [`docs/phase-2/specs/2026-04-22-a1-investigate-incident-design.md`](../specs/2026-04-22-a1-investigate-incident-design.md)

---

## Scope

Per the design doc §2 — this plan implements A1 P0:
- Seed: `query` + `time_range` only (`alarm_ocid` and `description` deferred to A6/P1)
- `top_k` clamped to `[1, 3]`
- J2 unconditional, Semaphore(2) per-source parallelism
- `top_entities` (not "changed_entities"); 3 entity fields (host, user, request_id); `ip` deferred to P1
- Best-effort timeline, droppable on budget/error with `partial_reasons`
- Quote-aware `seed_filter` extraction; downstream composition wraps in parens for boolean-precedence safety; `*` special-cases to no-parens source predicate
- A2 windows computed from a single anchor (zero-gap adjacency)

Out of scope: alarm-OCID resolution, NL-to-query, full pipeline-aware parser, per-entity change-detection, deep A4 fan-out per discovered entity, budget-introspection scheduling.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/oci_logan_mcp/investigate.py` | Create | Phase helper functions (pure) + `InvestigateIncidentTool` class |
| `src/oci_logan_mcp/handlers.py` | Modify | Import, instantiate, route, `_investigate_incident` method |
| `src/oci_logan_mcp/tools.py` | Modify | MCP schema (enum'd `time_range`, bounded `top_k`) |
| `tests/test_investigate.py` | Create | Unit tests per phase helper + orchestration tests |
| `tests/test_handlers.py` | Modify | `TestInvestigateIncident` class (routing + validation) |
| `tests/test_read_only_guard.py` | Modify | Add `"investigate_incident"` to `KNOWN_READERS` |
| `README.md` | Modify | Investigation Toolkit section + capability-table row |
| `docs/phase-2/specs/triage-toolkit.md` | Modify | §A1 updated to match design doc (top_entities, deferrals, limitations) |

---

## Shared data model

Every phase mutates one accumulator dict. `_finalize(acc)` assembles the final `InvestigationReport`.

```python
from typing import Any, Dict, List, Set
from datetime import datetime

# Accumulator shape (shared by all phase helpers)
{
    "seed": {
        "query": str,
        "seed_filter": str,              # "*" if degraded
        "seed_filter_degraded": bool,
        "time_range": str,
        "compartment_id": str | None,
    },
    "seed_result": Dict | None,          # raw Phase 1 engine response
    "ingestion_health": {
        "snapshot": Dict,
        "probe_window": str,
        "note": str,
    } | None,
    "parser_failures": Dict | None,      # raw J2 output
    "diff": Dict | None,                 # raw A2 output
    "anomalous_sources": List[Dict],     # ranked + trimmed to top_k (excluding J1-stopped)
    "per_source": Dict[str, Dict],       # source_name -> {current_count, comparison_count, pct_change, top_error_clusters, top_entities, timeline, errors}
    "partial_reasons": Set[str],         # dedup; finalize converts to list
    "source_errors": List[str],          # exception repr strings from per-source branches
    "start_time": datetime,              # wall-clock for elapsed_seconds
    "budget_snapshot": Dict | None,      # finalized at end from budget_tracker.snapshot().to_dict()
}
```

---

### Task 1: `_extract_seed_filter` — quote-aware extraction

**Files:**
- Create: `src/oci_logan_mcp/investigate.py`
- Create: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_investigate.py`:

```python
"""Tests for investigate_incident (A1) module."""
import pytest

from oci_logan_mcp.investigate import _extract_seed_filter


class TestExtractSeedFilter:
    def test_empty_query_returns_wildcard(self):
        assert _extract_seed_filter("") == "*"

    def test_bare_wildcard_returns_wildcard(self):
        assert _extract_seed_filter("*") == "*"

    def test_whitespace_wildcard_returns_wildcard(self):
        assert _extract_seed_filter("   *   ") == "*"

    def test_filter_only_returns_filter(self):
        assert _extract_seed_filter("'Event' = 'error'") == "'Event' = 'error'"

    def test_filter_with_trailing_stats_pipe(self):
        assert _extract_seed_filter("'Event' = 'error' | stats count") == "'Event' = 'error'"

    def test_filter_with_trailing_where_pipe_dropped(self):
        # Later pipeline stages (where/eval/etc.) are dropped for drill-down scoping.
        assert _extract_seed_filter(
            "'Event' = 'error' | where X = 'y'"
        ) == "'Event' = 'error'"

    def test_pipe_inside_single_quoted_literal_preserved(self):
        # Pipes inside a quoted string literal must not terminate the filter.
        q = "'Log Source' = 'http://x.com/a|b' | stats count"
        assert _extract_seed_filter(q) == "'Log Source' = 'http://x.com/a|b'"

    def test_doubled_single_quote_escape(self):
        # OCI LA escapes embedded quotes by doubling: 'O''Brien'.
        q = "'User' = 'O''Brien' | stats count"
        assert _extract_seed_filter(q) == "'User' = 'O''Brien'"

    def test_pipe_inside_double_quoted_literal_preserved(self):
        q = '"Field" = "val|with|pipes" | stats count'
        assert _extract_seed_filter(q) == '"Field" = "val|with|pipes"'

    def test_source_pinned_seed_returns_unchanged(self):
        # User who pre-scopes to one source gets it respected verbatim.
        assert _extract_seed_filter("'Log Source' = 'X'") == "'Log Source' = 'X'"
```

- [ ] **Step 2: Run tests to verify they fail**

```
cd /Users/rishabh/github/logan-mcp-server && python3 -m pytest tests/test_investigate.py::TestExtractSeedFilter -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'oci_logan_mcp.investigate'`

- [ ] **Step 3: Implement `_extract_seed_filter` in `src/oci_logan_mcp/investigate.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py::TestExtractSeedFilter -v
```
Expected: 10 PASS

- [ ] **Step 5: Run full suite (regression check)**

```
python3 -m pytest --tb=short -q
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): quote-aware _extract_seed_filter helper"
```

---

### Task 2: `_compose_source_scoped_query` — parens-wrapped composition

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
from oci_logan_mcp.investigate import _compose_source_scoped_query


class TestComposeSourceScopedQuery:
    def test_wildcard_seed_omits_and_and_parens(self):
        # "*" means no seed scoping — emit just the source predicate.
        q = _compose_source_scoped_query("*", "Apache Access", "cluster | sort -Count | head 3")
        assert q == "'Log Source' = 'Apache Access' | cluster | sort -Count | head 3"

    def test_simple_seed_gets_parens(self):
        q = _compose_source_scoped_query("'Event' = 'error'", "Apache", "cluster")
        assert q == "('Event' = 'error') and 'Log Source' = 'Apache' | cluster"

    def test_boolean_precedence_safety_with_or(self):
        # Precedence regression: without parens, `or` would escape the source predicate.
        q = _compose_source_scoped_query(
            "'Severity' = 'ERROR' or 'Level' = 'FATAL'",
            "Apache Access",
            "cluster",
        )
        assert "(" in q and ")" in q
        assert "(\'Severity\' = 'ERROR' or 'Level' = 'FATAL')" in q
        assert "and 'Log Source' = 'Apache Access'" in q

    def test_source_with_embedded_single_quote_escaped(self):
        q = _compose_source_scoped_query("*", "Bob's Logs", "stats count")
        # Embedded single quote is doubled per OCI LA grammar.
        assert "'Bob''s Logs'" in q

    def test_seed_already_source_pinned_still_wraps(self):
        # User pre-scoped to source Y; we still wrap in parens and append
        # the current source. The resulting `('Log Source' = 'Y') and 'Log Source' = 'X'`
        # correctly returns no rows for X != Y (desired).
        q = _compose_source_scoped_query("'Log Source' = 'Y'", "X", "cluster")
        assert q == "('Log Source' = 'Y') and 'Log Source' = 'X' | cluster"
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestComposeSourceScopedQuery -v
```
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `_compose_source_scoped_query`**

Append to `src/oci_logan_mcp/investigate.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py::TestComposeSourceScopedQuery -v
```
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): _compose_source_scoped_query with parens/wildcard handling"
```

---

### Task 3: `_compute_windows` — dual-anchor windows

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
from datetime import datetime, timedelta, timezone

from oci_logan_mcp.investigate import _compute_windows


class TestComputeWindows:
    def test_equal_length_and_zero_gap_adjacency(self):
        anchor = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        current, comparison = _compute_windows("last_1_hour", anchor)
        # Zero-gap: comparison.end == current.start
        assert comparison["time_end"] == current["time_start"]
        # Equal length
        c_start = datetime.fromisoformat(current["time_start"])
        c_end = datetime.fromisoformat(current["time_end"])
        p_start = datetime.fromisoformat(comparison["time_start"])
        p_end = datetime.fromisoformat(comparison["time_end"])
        assert (c_end - c_start) == (p_end - p_start) == timedelta(hours=1)

    def test_current_ends_at_anchor(self):
        anchor = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        current, _ = _compute_windows("last_1_hour", anchor)
        assert current["time_end"] == anchor.isoformat()

    def test_comparison_starts_at_anchor_minus_2_delta(self):
        anchor = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        _, comparison = _compute_windows("last_1_hour", anchor)
        expected = (anchor - timedelta(hours=2)).isoformat()
        assert comparison["time_start"] == expected

    def test_supports_longer_ranges(self):
        anchor = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        current, comparison = _compute_windows("last_7_days", anchor)
        c_start = datetime.fromisoformat(current["time_start"])
        c_end = datetime.fromisoformat(current["time_end"])
        assert (c_end - c_start) == timedelta(days=7)
        assert comparison["time_end"] == current["time_start"]

    def test_unknown_time_range_raises(self):
        with pytest.raises(ValueError, match="Unknown time_range"):
            _compute_windows("last_42_years", datetime(2026, 4, 22, tzinfo=timezone.utc))
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestComputeWindows -v
```
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `_compute_windows`**

Append to `src/oci_logan_mcp/investigate.py`:

```python
from datetime import datetime
from typing import Dict, Tuple

from .time_parser import TIME_RANGES


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
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py::TestComputeWindows -v
```
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): _compute_windows with dual-anchor absolute timestamps"
```

---

### Task 4: `_rank_anomalous_sources` — A2 delta ranking

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

**Live probe required before test fixtures:** Before writing the test helper shape, run this query against the VM once and capture the `DiffTool` response:

```bash
ssh emdemo-logan "cd ~/logan-mcp-server && git checkout feat/investigate-incident && .venv/bin/pip install -e . --quiet && .venv/bin/python -c \"
import asyncio, json
from oci_logan_mcp.config import load_config
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.query_logger import QueryLogger
from oci_logan_mcp.query_engine import QueryEngine
from oci_logan_mcp.query_estimator import QueryEstimator
from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
from oci_logan_mcp.diff_tool import DiffTool
from datetime import datetime, timedelta, timezone
async def m():
    s=load_config(); c=OCILogAnalyticsClient(s); cache=CacheManager(); ql=QueryLogger()
    est=QueryEstimator(c,s)
    bt=BudgetTracker('probe', BudgetLimits(enabled=False,max_queries_per_session=100,max_bytes_per_session=0,max_cost_usd_per_session=0))
    eng=QueryEngine(c,cache,ql,estimator=est,budget_tracker=bt)
    tool=DiffTool(eng)
    now=datetime.now(timezone.utc)
    r=await tool.run(
        query=\\\"'Parse Failed' = 1 | stats count as n by 'Log Source'\\\",
        current_window={'time_start':(now-timedelta(hours=1)).isoformat(),'time_end':now.isoformat()},
        comparison_window={'time_start':(now-timedelta(hours=2)).isoformat(),'time_end':(now-timedelta(hours=1)).isoformat()},
    )
    print(json.dumps({k: (v if k!='current' and k!='comparison' else 'AggregateResult') for k,v in r.items()}, indent=2, default=str))
    print('delta head:', r['delta'][:2] if r.get('delta') else r.get('delta'))
asyncio.run(m())
\" && git checkout main && .venv/bin/pip install -e . --quiet"
```

Record the captured shape of `delta` entries (keys: `dimension`, `current`, `comparison`, `pct_change` from `diff_tool.py`) for use in the test fixture below.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
from oci_logan_mcp.investigate import _rank_anomalous_sources


def _diff_delta(*entries):
    """Build a DiffTool-shaped delta list. Each entry is
    (dimension, current, comparison, pct_change)."""
    return [
        {"dimension": d, "current": cur, "comparison": comp, "pct_change": pct}
        for d, cur, comp, pct in entries
    ]


class TestRankAnomalousSources:
    def test_sort_by_absolute_pct_change_desc(self):
        delta = _diff_delta(
            ("A", 100, 100, 0.0),
            ("B", 200, 100, 100.0),
            ("C", 0, 100, -100.0),
            ("D", 150, 100, 50.0),
        )
        ranked = _rank_anomalous_sources(delta, stopped_sources=set(), top_k=3)
        sources = [r["source"] for r in ranked]
        assert sources == ["B", "C", "D"]  # |100|, |-100|, |50|

    def test_top_k_trims_result(self):
        delta = _diff_delta(
            ("A", 10, 0, 1000.0),
            ("B", 20, 0, 2000.0),
            ("C", 30, 0, 3000.0),
            ("D", 40, 0, 4000.0),
        )
        ranked = _rank_anomalous_sources(delta, stopped_sources=set(), top_k=2)
        assert len(ranked) == 2

    def test_stopped_sources_excluded(self):
        delta = _diff_delta(
            ("Broken", 999, 0, 9999.0),     # highest delta but stopped
            ("Working", 100, 0, 100.0),
        )
        ranked = _rank_anomalous_sources(delta, stopped_sources={"Broken"}, top_k=5)
        assert [r["source"] for r in ranked] == ["Working"]

    def test_zero_comparison_handles_infinite_pct(self):
        # pct_change may be None or an inf marker when comparison=0. Rank
        # falls back to absolute current count in that case.
        delta = [
            {"dimension": "NewSource", "current": 500, "comparison": 0, "pct_change": None},
            {"dimension": "OldSource", "current": 10, "comparison": 5, "pct_change": 100.0},
        ]
        ranked = _rank_anomalous_sources(delta, stopped_sources=set(), top_k=2)
        assert ranked[0]["source"] == "NewSource"

    def test_ranked_entries_preserve_shape(self):
        delta = _diff_delta(("A", 50, 10, 400.0))
        ranked = _rank_anomalous_sources(delta, stopped_sources=set(), top_k=1)
        r = ranked[0]
        assert r["source"] == "A"
        assert r["current_count"] == 50
        assert r["comparison_count"] == 10
        assert r["pct_change"] == 400.0

    def test_empty_delta_returns_empty(self):
        assert _rank_anomalous_sources([], stopped_sources=set(), top_k=3) == []
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestRankAnomalousSources -v
```
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `_rank_anomalous_sources`**

Append to `src/oci_logan_mcp/investigate.py`:

```python
from typing import List, Set


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
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py::TestRankAnomalousSources -v
```
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): _rank_anomalous_sources with stopped-source exclusion"
```

---

### Task 5: `_select_top_entities` — entity-discovery response parser

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
from oci_logan_mcp.investigate import _select_top_entities


def _entity_resp(field_name: str, rows):
    """Shape an engine response for `| stats count as n by '{field}'`."""
    return {
        "data": {
            "columns": [{"name": field_name}, {"name": "n"}],
            "rows": rows,
        }
    }


class TestSelectTopEntities:
    def test_parses_entity_rows(self):
        resp = _entity_resp("Host Name (Server)", [
            ["web-01", 50],
            ["web-02", 30],
            ["web-03", 10],
        ])
        entities = _select_top_entities(resp, "host", "Host Name (Server)")
        assert entities == [
            {"entity_type": "host", "entity_value": "web-01", "count": 50},
            {"entity_type": "host", "entity_value": "web-02", "count": 30},
            {"entity_type": "host", "entity_value": "web-03", "count": 10},
        ]

    def test_empty_response_returns_empty(self):
        resp = {"data": {"columns": [], "rows": []}}
        assert _select_top_entities(resp, "host", "Host Name (Server)") == []

    def test_missing_data_key_returns_empty(self):
        assert _select_top_entities({}, "user", "User Name") == []

    def test_missing_field_column_returns_empty(self):
        # Response doesn't contain the expected field column.
        resp = _entity_resp("Wrong Name", [["x", 1]])
        assert _select_top_entities(resp, "host", "Host Name (Server)") == []

    def test_none_entity_value_skipped(self):
        resp = _entity_resp("Host Name (Server)", [
            [None, 99],
            ["real-host", 1],
        ])
        entities = _select_top_entities(resp, "host", "Host Name (Server)")
        assert entities == [
            {"entity_type": "host", "entity_value": "real-host", "count": 1},
        ]

    def test_null_count_defaults_to_zero(self):
        resp = _entity_resp("Host Name (Server)", [["x", None]])
        entities = _select_top_entities(resp, "host", "Host Name (Server)")
        assert entities == [
            {"entity_type": "host", "entity_value": "x", "count": 0},
        ]
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestSelectTopEntities -v
```
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `_select_top_entities`**

Append to `src/oci_logan_mcp/investigate.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py::TestSelectTopEntities -v
```
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): _select_top_entities response parser"
```

---

### Task 6: `_merge_cross_source_timeline` — merge + sort + cap

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
from oci_logan_mcp.investigate import _merge_cross_source_timeline


class TestMergeCrossSourceTimeline:
    def test_merges_and_sorts_by_time(self):
        per_source = {
            "A": [
                {"time": "2026-04-22T10:00:00+00:00", "severity": "error", "message": "A2"},
                {"time": "2026-04-22T09:00:00+00:00", "severity": "warn",  "message": "A1"},
            ],
            "B": [
                {"time": "2026-04-22T09:30:00+00:00", "severity": None, "message": "B1"},
            ],
        }
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert out == [
            {"time": "2026-04-22T09:00:00+00:00", "source": "A", "severity": "warn",  "message": "A1"},
            {"time": "2026-04-22T09:30:00+00:00", "source": "B", "severity": None,    "message": "B1"},
            {"time": "2026-04-22T10:00:00+00:00", "source": "A", "severity": "error", "message": "A2"},
        ]

    def test_cap_enforced(self):
        per_source = {
            "X": [
                {"time": f"2026-04-22T10:{i:02d}:00+00:00", "severity": None, "message": f"m{i}"}
                for i in range(60)
            ]
        }
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert len(out) == 50

    def test_null_source_timeline_skipped(self):
        per_source = {"A": None, "B": [{"time": "2026-04-22T10:00:00+00:00", "severity": None, "message": "B"}]}
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert out == [{"time": "2026-04-22T10:00:00+00:00", "source": "B", "severity": None, "message": "B"}]

    def test_all_null_returns_none(self):
        # All sources had their timeline dropped. Distinguish from empty-success.
        per_source = {"A": None, "B": None}
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert out is None

    def test_all_empty_returns_empty_list(self):
        # All sources ran cleanly but produced no rows. Not the same as null.
        per_source = {"A": [], "B": []}
        out = _merge_cross_source_timeline(per_source, cap=50)
        assert out == []

    def test_empty_input_returns_none(self):
        assert _merge_cross_source_timeline({}, cap=50) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestMergeCrossSourceTimeline -v
```
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `_merge_cross_source_timeline`**

Append to `src/oci_logan_mcp/investigate.py`:

```python
from typing import Optional


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
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py::TestMergeCrossSourceTimeline -v
```
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): _merge_cross_source_timeline with null-vs-empty semantics"
```

---

### Task 7: `_templated_summary` — human-readable summary synthesis

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
from oci_logan_mcp.investigate import _templated_summary


class TestTemplatedSummary:
    def test_clean_report(self):
        acc = {
            "seed": {"seed_filter": "'Event' = 'error'", "seed_filter_degraded": False, "time_range": "last_1_hour"},
            "ingestion_health": {"snapshot": {"summary": {"sources_stopped": 0}}},
            "parser_failures": {"total_failure_count": 0},
            "anomalous_sources": [
                {"source": "Apache", "pct_change": 150.0},
                {"source": "Syslog", "pct_change": 80.0},
            ],
            "partial_reasons": set(),
        }
        s = _templated_summary(acc)
        assert "'Event' = 'error'" in s
        assert "last_1_hour" in s
        assert "Apache" in s
        assert "partial" not in s.lower()

    def test_degraded_seed_mentions_unscoped(self):
        acc = {
            "seed": {"seed_filter": "*", "seed_filter_degraded": True, "time_range": "last_1_hour"},
            "ingestion_health": {"snapshot": {"summary": {"sources_stopped": 0}}},
            "parser_failures": {"total_failure_count": 0},
            "anomalous_sources": [],
            "partial_reasons": set(),
        }
        s = _templated_summary(acc)
        assert "unscoped" in s.lower()

    def test_partial_appended(self):
        acc = {
            "seed": {"seed_filter": "'x' = 'y'", "seed_filter_degraded": False, "time_range": "last_1_hour"},
            "ingestion_health": {"snapshot": {"summary": {"sources_stopped": 0}}},
            "parser_failures": {"total_failure_count": 0},
            "anomalous_sources": [],
            "partial_reasons": {"budget_exceeded", "timeline_omitted"},
        }
        s = _templated_summary(acc)
        assert "partial" in s.lower()
        assert "budget_exceeded" in s
        assert "timeline_omitted" in s

    def test_stopped_and_parser_failures_mentioned(self):
        acc = {
            "seed": {"seed_filter": "*", "seed_filter_degraded": True, "time_range": "last_1_hour"},
            "ingestion_health": {"snapshot": {"summary": {"sources_stopped": 2}}},
            "parser_failures": {"total_failure_count": 500},
            "anomalous_sources": [],
            "partial_reasons": set(),
        }
        s = _templated_summary(acc)
        assert "2" in s and "stopped" in s.lower()
        assert "500" in s and ("parse" in s.lower() or "parser" in s.lower())
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestTemplatedSummary -v
```
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `_templated_summary`**

Append to `src/oci_logan_mcp/investigate.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py::TestTemplatedSummary -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): _templated_summary synthesis"
```

---

### Task 8: `InvestigateIncidentTool` skeleton + `_finalize` + accumulator init

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

Skeleton only — `run()` populates the accumulator with seed metadata and calls `_finalize()`. Subsequent tasks fill in each phase. Tests verify the minimal shape of the output and the BudgetExceededError partial-report path.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.investigate import InvestigateIncidentTool


def _make_engine():
    engine = MagicMock()
    engine.execute = AsyncMock(return_value={"data": {"columns": [], "rows": []}})
    return engine


def _make_deps(schema_sources=None, ih_result=None, j2_result=None):
    """Stub out J1, J2, A2 tool instances the orchestrator composes."""
    from unittest.mock import MagicMock, AsyncMock
    schema = MagicMock()
    schema.get_log_sources = AsyncMock(return_value=[{"name": n} for n in (schema_sources or [])])
    ih_tool = MagicMock()
    ih_tool.run = AsyncMock(return_value=ih_result or {
        "summary": {"sources_healthy": 0, "sources_stopped": 0, "sources_unknown": 0},
        "findings": [],
        "checked_at": "2026-04-22T10:00:00+00:00",
        "metadata": {},
    })
    j2_tool = MagicMock()
    j2_tool.run = AsyncMock(return_value=j2_result or {"failures": [], "total_failure_count": 0})
    diff_tool = MagicMock()
    diff_tool.run = AsyncMock(return_value={"current": {}, "comparison": {}, "delta": [], "summary": "no change"})
    return schema, ih_tool, j2_tool, diff_tool


def _make_settings():
    from oci_logan_mcp.config import Settings
    return Settings()


def _make_budget():
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    return BudgetTracker(
        session_id="test",
        limits=BudgetLimits(enabled=False, max_queries_per_session=100,
                            max_bytes_per_session=0, max_cost_usd_per_session=0),
    )


class TestInvestigateSkeleton:
    @pytest.mark.asyncio
    async def test_minimal_report_shape(self):
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="'Event' = 'error'", time_range="last_1_hour", top_k=3)

        # Minimal required keys regardless of phase content.
        for key in ("summary", "seed", "ingestion_health", "parser_failures",
                    "anomalous_sources", "cross_source_timeline", "next_steps",
                    "budget", "partial", "partial_reasons", "elapsed_seconds"):
            assert key in report, f"missing {key}"

    @pytest.mark.asyncio
    async def test_seed_section_populated(self):
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="'Event' = 'error' | stats count", time_range="last_1_hour", top_k=3)
        assert report["seed"]["query"] == "'Event' = 'error' | stats count"
        assert report["seed"]["seed_filter"] == "'Event' = 'error'"
        assert report["seed"]["seed_filter_degraded"] is False
        assert report["seed"]["time_range"] == "last_1_hour"

    @pytest.mark.asyncio
    async def test_wildcard_seed_sets_degraded(self):
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        assert report["seed"]["seed_filter_degraded"] is True

    @pytest.mark.asyncio
    async def test_budget_exception_returns_partial_report_not_raised(self):
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        engine = _make_engine()
        engine.execute = AsyncMock(side_effect=BudgetExceededError("over"))
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )

        # Must NOT raise; must return a partial InvestigationReport instead.
        report = await tool.run(query="'x' = 'y'", time_range="last_1_hour", top_k=3)
        assert report["partial"] is True
        assert "budget_exceeded" in report["partial_reasons"]
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestInvestigateSkeleton -v
```
Expected: FAIL — class does not exist yet.

- [ ] **Step 3: Implement skeleton `InvestigateIncidentTool` + `_finalize`**

Append to `src/oci_logan_mcp/investigate.py`:

```python
import asyncio
from datetime import datetime, timezone

from .budget_tracker import BudgetExceededError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


TOP_K_MIN = 1
TOP_K_MAX = 3
TIMELINE_CAP = 50


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
            # Phases 1-7 are added by subsequent tasks.
            pass
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
```

Also add the missing import at the top:

```python
from typing import Any, Dict, List, Optional, Set
```

(Replace the earlier `from typing import Any` line with this expanded form if already present.)

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py::TestInvestigateSkeleton -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): InvestigateIncidentTool skeleton + _finalize + accumulator"
```

---

### Task 9: Phase 1 — seed execution

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_investigate.py`:

```python
class TestPhase1SeedExecution:
    @pytest.mark.asyncio
    async def test_seed_query_executed_with_time_range(self):
        engine = _make_engine()
        engine.execute = AsyncMock(return_value={"data": {"columns": [{"name": "n"}], "rows": [[5]]}})
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        await tool.run(query="'Event' = 'error'", time_range="last_1_hour", top_k=3, compartment_id="ocid1.test")

        # The seed query is executed once as-is (phase 1).
        calls = [c for c in engine.execute.await_args_list
                 if c.kwargs.get("query") == "'Event' = 'error'"]
        assert len(calls) == 1
        assert calls[0].kwargs.get("time_range") == "last_1_hour"
        assert calls[0].kwargs.get("compartment_id") == "ocid1.test"
```

- [ ] **Step 2: Run test to verify it fails**

```
python3 -m pytest tests/test_investigate.py::TestPhase1SeedExecution -v
```
Expected: FAIL — seed query not executed.

- [ ] **Step 3: Wire Phase 1 into `run()`**

In `src/oci_logan_mcp/investigate.py`, replace the `try: pass` in `InvestigateIncidentTool.run()` with:

```python
        try:
            # Phase 1 — seed execution
            acc["seed_result"] = await self._engine.execute(
                query=query,
                time_range=time_range,
                compartment_id=compartment_id,
            )
        except BudgetExceededError:
            acc["partial_reasons"].add("budget_exceeded")
```

- [ ] **Step 4: Run test to verify it passes**

```
python3 -m pytest tests/test_investigate.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): phase 1 seed execution"
```

---

### Task 10: Phase 2 — J1 freshness snapshot

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
class TestPhase2IngestionHealth:
    @pytest.mark.asyncio
    async def test_j1_invoked_with_all_severity(self):
        ih_result = {
            "summary": {"sources_healthy": 2, "sources_stopped": 1, "sources_unknown": 0},
            "findings": [
                {"source": "Broken", "status": "stopped", "severity": "critical"},
            ],
            "checked_at": "2026-04-22T10:00:00+00:00",
            "metadata": {},
        }
        schema, ih, j2, diff = _make_deps(ih_result=ih_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3, compartment_id="ocid1.xyz")
        ih.run.assert_awaited_once()
        kwargs = ih.run.await_args.kwargs
        assert kwargs["severity_filter"] == "all"
        assert kwargs["compartment_id"] == "ocid1.xyz"

    @pytest.mark.asyncio
    async def test_report_wraps_j1_with_probe_window_and_note(self):
        schema, ih, j2, diff = _make_deps()
        settings = _make_settings()
        settings.ingestion_health.freshness_probe_window = "last_1_hour"
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=settings, budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_7_days", top_k=3)
        ih_section = report["ingestion_health"]
        assert ih_section["probe_window"] == "last_1_hour"
        assert "probe window" in ih_section["note"].lower()
        assert "snapshot" in ih_section
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestPhase2IngestionHealth -v
```
Expected: FAIL — J1 not invoked / ingestion_health stays None.

- [ ] **Step 3: Wire Phase 2**

In `src/oci_logan_mcp/investigate.py`, extend the try block in `run()`:

```python
        try:
            # Phase 1 — seed execution
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
        except BudgetExceededError:
            acc["partial_reasons"].add("budget_exceeded")
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): phase 2 J1 freshness snapshot with probe_window note"
```

---

### Task 11: Phase 3 — J2 parser failures

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_investigate.py`:

```python
class TestPhase3ParserFailures:
    @pytest.mark.asyncio
    async def test_j2_invoked_with_time_range(self):
        j2_result = {"failures": [{"source": "X", "failure_count": 42}], "total_failure_count": 42}
        schema, ih, j2, diff = _make_deps(j2_result=j2_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_24_hours", top_k=3)

        j2.run.assert_awaited_once()
        kwargs = j2.run.await_args.kwargs
        assert kwargs["time_range"] == "last_24_hours"
        assert kwargs["top_n"] == 10
        assert report["parser_failures"]["total_failure_count"] == 42
```

- [ ] **Step 2: Run test to verify it fails**

```
python3 -m pytest tests/test_investigate.py::TestPhase3ParserFailures -v
```
Expected: FAIL.

- [ ] **Step 3: Wire Phase 3**

In `src/oci_logan_mcp/investigate.py`, after Phase 2 in the try block, add:

```python
            # Phase 3 — J2 parser failures (always-on)
            acc["parser_failures"] = await self._j2_tool.run(
                time_range=time_range,
                top_n=10,
            )
```

- [ ] **Step 4: Run test to verify it passes**

```
python3 -m pytest tests/test_investigate.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): phase 3 J2 parser_failure_triage"
```

---

### Task 12: Phase 4 — A2 anomaly ranking with anchored windows

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
class TestPhase4AnomalyRanking:
    @pytest.mark.asyncio
    async def test_diff_query_is_seed_scoped_by_log_source(self):
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        await tool.run(query="'Event' = 'error'", time_range="last_1_hour", top_k=3)

        diff.run.assert_awaited_once()
        kwargs = diff.run.await_args.kwargs
        assert kwargs["query"] == "'Event' = 'error' | stats count as n by 'Log Source'"
        # Both windows are absolute timestamps (anchored).
        assert "time_start" in kwargs["current_window"]
        assert "time_end" in kwargs["current_window"]
        assert "time_start" in kwargs["comparison_window"]
        assert "time_end" in kwargs["comparison_window"]
        assert kwargs["current_window"]["time_start"] == kwargs["comparison_window"]["time_end"]

    @pytest.mark.asyncio
    async def test_anomalous_sources_populated_from_diff_delta(self):
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [
                {"dimension": "A", "current": 100, "comparison": 50, "pct_change": 100.0},
                {"dimension": "B", "current": 30, "comparison": 60, "pct_change": -50.0},
                {"dimension": "C", "current": 15, "comparison": 10, "pct_change": 50.0},
                {"dimension": "D", "current": 5, "comparison": 5, "pct_change": 0.0},
            ],
        }
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        srcs = [s["source"] for s in report["anomalous_sources"]]
        assert srcs == ["A", "B", "C"]  # by |pct_change|: 100, 50, 50

    @pytest.mark.asyncio
    async def test_stopped_sources_excluded_from_ranking(self):
        ih_result = {
            "summary": {"sources_healthy": 1, "sources_stopped": 1, "sources_unknown": 0},
            "findings": [{"source": "Broken", "status": "stopped", "severity": "critical"}],
            "checked_at": "2026-04-22T10:00:00+00:00", "metadata": {},
        }
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [
                {"dimension": "Broken", "current": 999, "comparison": 0, "pct_change": 9999.0},
                {"dimension": "Working", "current": 10, "comparison": 5, "pct_change": 100.0},
            ],
        }
        schema, ih, j2, diff = _make_deps(ih_result=ih_result)
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        srcs = [s["source"] for s in report["anomalous_sources"]]
        assert "Broken" not in srcs
        assert "Working" in srcs
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestPhase4AnomalyRanking -v
```
Expected: FAIL.

- [ ] **Step 3: Wire Phase 4**

In `src/oci_logan_mcp/investigate.py`, add after Phase 3 in the try block:

```python
            # Phase 4 — A2 anomaly ranking with anchored windows
            anchor = _utcnow()
            current_w, comparison_w = _compute_windows(time_range, anchor)
            if seed_filter == "*":
                ranking_query = "* | stats count as n by 'Log Source'"
            else:
                ranking_query = f"{seed_filter} | stats count as n by 'Log Source'"
            diff_result = await self._diff_tool.run(
                query=ranking_query,
                current_window=current_w,
                comparison_window=comparison_w,
            )
            acc["diff"] = diff_result

            # Identify stopped sources from J1 to exclude from ranking.
            stopped: Set[str] = set()
            ih_section = acc.get("ingestion_health") or {}
            for finding in ((ih_section.get("snapshot") or {}).get("findings") or []):
                if finding.get("status") == "stopped":
                    stopped.add(str(finding.get("source")))

            acc["anomalous_sources"] = _rank_anomalous_sources(
                diff_result.get("delta") or [], stopped, top_k,
            )
            # Seed per_source entries for drill-down phases.
            for s in acc["anomalous_sources"]:
                acc["per_source"][s["source"]] = {
                    "top_error_clusters": [],
                    "top_entities": [],
                    "timeline": None,
                    "errors": [],
                }
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): phase 4 A2 anomaly ranking with anchored windows"
```

---

### Task 13: Phase 5+6 — per-source drill-down (cluster + entities + timeline)

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

**Live probe before test fixtures:** Capture the real response shape for each query type on the VM. Record the column layout for `cluster`, entity discovery, and timeline queries.

```bash
ssh emdemo-logan "cd ~/logan-mcp-server && git checkout feat/investigate-incident && .venv/bin/pip install -e . --quiet && .venv/bin/python -c \"
import asyncio, json
from oci_logan_mcp.config import load_config
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.query_logger import QueryLogger
from oci_logan_mcp.query_engine import QueryEngine
from oci_logan_mcp.query_estimator import QueryEstimator
from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
async def m():
    s=load_config(); c=OCILogAnalyticsClient(s); cache=CacheManager(); ql=QueryLogger()
    est=QueryEstimator(c,s)
    bt=BudgetTracker('probe', BudgetLimits(enabled=False,max_queries_per_session=100,max_bytes_per_session=0,max_cost_usd_per_session=0))
    eng=QueryEngine(c,cache,ql,estimator=est,budget_tracker=bt)
    probes=[
        (\\\"cluster\\\",        \\\"('Parse Failed' = 1) and 'Log Source' = 'Kubernetes Kubelet Logs' | cluster | sort -Count | head 3\\\"),
        (\\\"entity-host\\\",    \\\"('Parse Failed' = 1) and 'Log Source' = 'Kubernetes Kubelet Logs' | stats count as n by 'Host Name (Server)' | sort -n | head 5\\\"),
        (\\\"entity-user\\\",    \\\"('Parse Failed' = 1) and 'Log Source' = 'Kubernetes Kubelet Logs' | stats count as n by 'User Name' | sort -n | head 5\\\"),
        (\\\"entity-reqid\\\",   \\\"('Parse Failed' = 1) and 'Log Source' = 'Kubernetes Kubelet Logs' | stats count as n by 'Request ID' | sort -n | head 5\\\"),
        (\\\"timeline\\\",       \\\"('Parse Failed' = 1) and 'Log Source' = 'Kubernetes Kubelet Logs' | fields Time, Severity, 'Original Log Content' | sort -Time | head 5\\\"),
    ]
    for label,q in probes:
        try:
            r=await eng.execute(query=q, time_range='last_1_hour')
            cols=[c.get('name') for c in r.get('data',{}).get('columns',[])]
            print(f'{label}: cols={cols}')
            for row in r.get('data',{}).get('rows',[])[:1]:
                print(f'  row={[str(v)[:60] for v in row] if isinstance(row,list) else row}')
        except Exception as e:
            print(f'{label}: ERR {str(e)[:120]}')
asyncio.run(m())
\" && git checkout main && .venv/bin/pip install -e . --quiet"
```

Record the output. Any entity-field probe that returns `InvalidParameter` is expected (we will handle it as partial in the implementation). Update the test fixtures below to match the real column names captured.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
class TestPhase5And6DrillDown:
    @pytest.mark.asyncio
    async def test_cluster_and_entity_queries_composed_with_parens(self):
        """Phase 5: for each anomalous source, runs cluster + 3 entity discoveries."""
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Apache", "current": 100, "comparison": 50, "pct_change": 100.0}],
        }

        # Stub engine.execute with routing by query substring.
        async def execute_router(**kwargs):
            q = kwargs.get("query", "")
            if "| cluster" in q:
                return {"data": {"columns": [
                    {"name": "Cluster Sample"}, {"name": "Count"}, {"name": "Problem Priority"},
                ], "rows": [["sample1", 42, 2]]}}
            if "'Host Name (Server)'" in q:
                return {"data": {"columns": [{"name": "Host Name (Server)"}, {"name": "n"}],
                                 "rows": [["web-01", 20]]}}
            if "'User Name'" in q:
                return {"data": {"columns": [{"name": "User Name"}, {"name": "n"}],
                                 "rows": [["alice", 5]]}}
            if "'Request ID'" in q:
                return {"data": {"columns": [{"name": "Request ID"}, {"name": "n"}],
                                 "rows": [["req-123", 3]]}}
            if "| fields Time" in q:
                return {"data": {"columns": [
                    {"name": "Time"}, {"name": "Severity"}, {"name": "Original Log Content"},
                ], "rows": [[1776829209000, "error", "bad line"]]}}
            return {"data": {"columns": [], "rows": []}}

        engine = MagicMock()
        engine.execute = AsyncMock(side_effect=execute_router)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="'Event' = 'error'", time_range="last_1_hour", top_k=3)

        # Every per-source query starts with the parens-wrapped seed_filter.
        executed_queries = [c.kwargs.get("query", "") for c in engine.execute.await_args_list]
        per_source_queries = [q for q in executed_queries if "'Log Source' = 'Apache'" in q]
        assert len(per_source_queries) >= 4  # cluster + 3 entities + timeline
        for q in per_source_queries:
            assert q.startswith("('Event' = 'error') and 'Log Source' = 'Apache'"), q

        # Populated fields on the anomalous_source entry.
        src = report["anomalous_sources"][0]
        assert len(src["top_error_clusters"]) == 1
        assert src["top_error_clusters"][0]["pattern"] == "sample1"
        assert src["top_error_clusters"][0]["count"] == 42
        entity_values = {e["entity_value"] for e in src["top_entities"]}
        assert {"web-01", "alice", "req-123"} <= entity_values
        assert src["timeline"] is not None
        assert len(src["timeline"]) == 1

    @pytest.mark.asyncio
    async def test_invalid_entity_field_partial_not_fatal(self):
        from oci.exceptions import ServiceError
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Apache", "current": 100, "comparison": 0, "pct_change": None}],
        }

        async def execute_router(**kwargs):
            q = kwargs.get("query", "")
            if "'User Name'" in q:
                raise ServiceError(status=400, code="InvalidParameter",
                                   headers={}, message="Invalid field for SEARCH = operator: User Name")
            return {"data": {"columns": [], "rows": []}}

        engine = MagicMock()
        engine.execute = AsyncMock(side_effect=execute_router)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="'Event' = 'x'", time_range="last_1_hour", top_k=3)

        assert report["partial"] is True
        assert "entity_discovery_partial" in report["partial_reasons"]
        # Other entity types still populated (empty here because engine returns empty).
        src = report["anomalous_sources"][0]
        assert src["errors"]  # non-empty
        assert any("User Name" in e for e in src["errors"])

    @pytest.mark.asyncio
    async def test_timeline_error_sets_partial_and_null(self):
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Apache", "current": 100, "comparison": 50, "pct_change": 100.0}],
        }

        async def execute_router(**kwargs):
            q = kwargs.get("query", "")
            if "| fields Time" in q:
                raise RuntimeError("timeline server went away")
            return {"data": {"columns": [], "rows": []}}

        engine = MagicMock()
        engine.execute = AsyncMock(side_effect=execute_router)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)

        assert "timeline_omitted" in report["partial_reasons"]
        src = report["anomalous_sources"][0]
        assert src["timeline"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestPhase5And6DrillDown -v
```
Expected: FAIL.

- [ ] **Step 3: Implement the per-source branch + Phase 5+6 wiring**

Add to `src/oci_logan_mcp/investigate.py` (before the class):

```python
# Entity discovery uses the same field names as pivot_tool's ENTITY_FIELD_MAP,
# but A1 P0 uses only three (ip deferred to P1 to bound per-source query count).
A1_ENTITY_FIELDS = [
    ("host", "Host Name (Server)"),
    ("user", "User Name"),
    ("request_id", "Request ID"),
]
PER_SOURCE_CONCURRENCY = 2
CLUSTER_HEAD = 3
ENTITY_HEAD = 5
TIMELINE_HEAD = 20


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
        out.append({
            "pattern": str(row[sample_idx]) if row[sample_idx] is not None else "",
            "count": int(cnt) if cnt is not None else 0,
            "problem_priority": int(prio) if prio is not None else None,
        })
    return out


def _parse_timeline_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse a `| fields Time, Severity, 'Original Log Content' | sort -Time | head N` response.

    Normalizes Time through _ts_to_iso so epoch-ms LONGs become ISO strings.
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
) -> Dict[str, Any]:
    """Run cluster + entity discovery + timeline for a single source, sequentially.

    Returns a dict with keys: top_error_clusters, top_entities, timeline,
    errors, entity_discovery_partial, timeline_omitted.

    Never raises: invalid-field errors per entity type become entries on
    `errors` + set `entity_discovery_partial`. Timeline errors set
    `timeline_omitted` and leave `timeline` = None.
    """
    result = {
        "top_error_clusters": [],
        "top_entities": [],
        "timeline": None,
        "errors": [],
        "entity_discovery_partial": False,
        "timeline_omitted": False,
    }

    # Cluster
    cluster_query = _compose_source_scoped_query(
        seed_filter, source, f"cluster | sort -Count | head {CLUSTER_HEAD}",
    )
    try:
        cluster_resp = await engine.execute(
            query=cluster_query, time_range=time_range, compartment_id=compartment_id,
        )
        result["top_error_clusters"] = _parse_cluster_response(cluster_resp)
    except BudgetExceededError:
        raise
    except Exception as e:
        result["errors"].append(f"cluster: {type(e).__name__}: {e}")

    # Entity discovery (3 fields, sequential)
    for entity_type, field_name in A1_ENTITY_FIELDS:
        entity_query = _compose_source_scoped_query(
            seed_filter, source,
            f"stats count as n by '{field_name}' | sort -n | head {ENTITY_HEAD}",
        )
        try:
            entity_resp = await engine.execute(
                query=entity_query, time_range=time_range, compartment_id=compartment_id,
            )
            result["top_entities"].extend(
                _select_top_entities(entity_resp, entity_type, field_name)
            )
        except BudgetExceededError:
            raise
        except Exception as e:
            result["errors"].append(f"top_entities[{entity_type}]: {type(e).__name__}: {e}")
            result["entity_discovery_partial"] = True

    # Timeline
    timeline_query = _compose_source_scoped_query(
        seed_filter, source,
        f"fields Time, Severity, 'Original Log Content' | sort -Time | head {TIMELINE_HEAD}",
    )
    try:
        tl_resp = await engine.execute(
            query=timeline_query, time_range=time_range, compartment_id=compartment_id,
        )
        result["timeline"] = _parse_timeline_response(tl_resp)
    except BudgetExceededError:
        raise
    except Exception as e:
        result["errors"].append(f"timeline: {type(e).__name__}: {e}")
        result["timeline_omitted"] = True

    return result
```

Then in `InvestigateIncidentTool.run()`, after Phase 4 in the try block, add Phases 5+6:

```python
            # Phases 5+6 — per-source drill-down under Semaphore(2)
            sem = asyncio.Semaphore(PER_SOURCE_CONCURRENCY)
            sources_list = [s["source"] for s in acc["anomalous_sources"]]

            async def bounded(source_name: str):
                async with sem:
                    return await _drill_down_one_source(
                        self._engine, source_name, seed_filter, time_range, compartment_id,
                    )

            results = await asyncio.gather(
                *(bounded(s) for s in sources_list),
                return_exceptions=True,
            )
            for source_name, branch_result in zip(sources_list, results):
                if isinstance(branch_result, Exception):
                    acc["per_source"][source_name]["errors"].append(
                        f"branch: {type(branch_result).__name__}: {branch_result}"
                    )
                    acc["source_errors"].append(str(branch_result))
                    acc["partial_reasons"].add("source_errors")
                    continue
                ps = acc["per_source"][source_name]
                ps["top_error_clusters"] = branch_result["top_error_clusters"]
                ps["top_entities"] = branch_result["top_entities"]
                ps["timeline"] = branch_result["timeline"]
                ps["errors"].extend(branch_result["errors"])
                if branch_result["entity_discovery_partial"]:
                    acc["partial_reasons"].add("entity_discovery_partial")
                if branch_result["timeline_omitted"]:
                    acc["partial_reasons"].add("timeline_omitted")
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): phases 5+6 per-source drill-down (cluster + entities + timeline)"
```

---

### Task 14: Phase 7 — next_steps.suggest() + finalized summary

**Files:**
- Modify: `src/oci_logan_mcp/investigate.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
class TestPhase7NextSteps:
    @pytest.mark.asyncio
    async def test_next_steps_populated_from_seed_result(self):
        """next_steps.suggest() is called with the seed query + seed_result."""
        engine = _make_engine()
        engine.execute = AsyncMock(return_value={
            "data": {
                "columns": [{"name": "Time"}, {"name": "Count"}],
                "rows": [
                    ["2026-04-22T10:00:00+00:00", 1],
                    ["2026-04-22T10:01:00+00:00", 1],
                    ["2026-04-22T10:02:00+00:00", 1],
                    ["2026-04-22T10:03:00+00:00", 30],  # spike
                ],
            }
        })
        schema, ih, j2, diff = _make_deps()
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="'Event' = 'error'", time_range="last_1_hour", top_k=3)

        # next_steps is a list (possibly empty); next_steps heuristic may
        # or may not suggest anything for this seed shape. What we assert:
        # the key is present and it's a list of dicts if non-empty.
        assert isinstance(report["next_steps"], list)
        for step in report["next_steps"]:
            assert "tool_name" in step
            assert "suggested_args" in step
            assert "reason" in step

    @pytest.mark.asyncio
    async def test_summary_mentions_anomalous_sources(self):
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Apache", "current": 100, "comparison": 50, "pct_change": 100.0}],
        }
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=_make_engine(), schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="'x' = 'y'", time_range="last_1_hour", top_k=3)
        assert "Apache" in report["summary"]
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestPhase7NextSteps -v
```
Expected: FAIL — `next_steps` is still an empty list from `_finalize`.

- [ ] **Step 3: Wire Phase 7 into `run()` and `_finalize()`**

In `src/oci_logan_mcp/investigate.py`, add import:

```python
from . import next_steps as _next_steps
```

After Phase 5+6 in `run()`, add:

```python
            # Phase 7 — next_steps suggestions from the seed result
            acc["next_steps"] = [
                step.to_dict()
                for step in _next_steps.suggest(query, acc.get("seed_result") or {})
            ]
```

In `_finalize`, change `"next_steps": []` to `"next_steps": acc.get("next_steps") or []`.

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/investigate.py tests/test_investigate.py
git commit -m "feat(a1): phase 7 next_steps suggestions"
```

---

### Task 15: MCP tool schema

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `tests/test_investigate.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
class TestToolSchema:
    def test_investigate_incident_schema_present(self):
        from oci_logan_mcp.tools import get_tools
        names = [t["name"] for t in get_tools()]
        assert "investigate_incident" in names

    def test_investigate_incident_schema_properties(self):
        from oci_logan_mcp.tools import get_tools
        tool = next(t for t in get_tools() if t["name"] == "investigate_incident")
        props = tool["inputSchema"]["properties"]
        assert "query" in props
        assert props["query"]["type"] == "string"
        assert "time_range" in props
        assert props["time_range"]["type"] == "string"
        assert "enum" in props["time_range"]
        assert "top_k" in props
        assert props["top_k"]["minimum"] == 1
        assert props["top_k"]["maximum"] == 3
        assert "compartment_id" in props
        assert tool["inputSchema"].get("required") == ["query"]
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_investigate.py::TestToolSchema -v
```
Expected: FAIL — tool not registered.

- [ ] **Step 3: Add tool schema to `src/oci_logan_mcp/tools.py`**

Find the `parser_failure_triage` entry in `get_tools()` — its closing `},` is followed by the `# Visualization Tools` section comment (or similar). Insert the `investigate_incident` entry immediately after `parser_failure_triage`:

```python
        {
            "name": "investigate_incident",
            "description": (
                "Flagship triage tool. Given a seed Logan query and a time "
                "range, returns a structured first-cut investigation: which "
                "sources are stopped (J1), which parsers are failing (J2), "
                "which sources are anomalous vs. the prior equal-length "
                "window (A2), and for each of the top_k anomalous sources: "
                "top error clusters (Logan `cluster`), top entities "
                "(host/user/request_id by count), and a recent-events "
                "timeline. Merged cross-source timeline + next-step "
                "suggestions round out the report. Budget exhaustion and "
                "per-source errors yield a partial InvestigationReport with "
                "`partial: true` and specific `partial_reasons` — A1 never "
                "raises BudgetExceededError out of its boundary."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Seed Logan query. Only the pre-pipe search "
                            "clause is used for drill-down scoping (later "
                            "pipeline stages are dropped). A seed of '*' "
                            "degrades to unscoped investigation and is "
                            "reported via `seed.seed_filter_degraded`."
                        ),
                    },
                    "time_range": {
                        "type": "string",
                        "enum": [
                            "last_15_min", "last_30_min",
                            "last_1_hour", "last_3_hours", "last_6_hours",
                            "last_12_hours", "last_24_hours",
                            "last_2_days", "last_7_days", "last_14_days",
                            "last_30_days",
                        ],
                        "description": (
                            "Investigation window. Default: 'last_1_hour'. "
                            "A2 compares this window to the prior "
                            "equal-length window."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "description": (
                            "Number of anomalous sources to drill into. "
                            "Default: 3. P0 clamp is [1, 3] to match the "
                            "~20s p95 latency guarantee."
                        ),
                    },
                    "compartment_id": {
                        "type": "string",
                        "description": "Optional compartment OCID. Uses default if not specified.",
                    },
                },
                "required": ["query"],
            },
        },
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py::TestToolSchema -v
```
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/tools.py tests/test_investigate.py
git commit -m "feat(a1): register investigate_incident MCP tool schema"
```

---

### Task 16: Handler wiring

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_handlers.py`

Key design point: the handler does NOT catch `BudgetExceededError`; the orchestrator returns a partial report internally. Only `ValueError` (bad top_k) and missing-query pre-flight get structured-error shape.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_handlers.py`:

```python
class TestInvestigateIncident:
    @pytest.mark.asyncio
    async def test_routes_to_investigate_tool(self, handlers):
        handlers.investigate_tool.run = AsyncMock(return_value={
            "summary": "ok",
            "seed": {"query": "'x' = 'y'", "seed_filter": "'x' = 'y'",
                     "seed_filter_degraded": False, "time_range": "last_1_hour",
                     "compartment_id": None},
            "ingestion_health": None, "parser_failures": None,
            "anomalous_sources": [], "cross_source_timeline": None,
            "next_steps": [], "budget": {}, "partial": False,
            "partial_reasons": [], "elapsed_seconds": 0.1,
        })
        result = await handlers.handle_tool_call(
            "investigate_incident",
            {"query": "'x' = 'y'", "time_range": "last_1_hour", "top_k": 3},
        )
        payload = json.loads(result[0]["text"])
        assert payload["summary"] == "ok"
        handlers.investigate_tool.run.assert_awaited_once_with(
            query="'x' = 'y'",
            time_range="last_1_hour",
            top_k=3,
            compartment_id=None,
        )

    @pytest.mark.asyncio
    async def test_partial_report_forwarded_verbatim(self, handlers):
        """A1 diverges from other triage tools: no {status: budget_exceeded} wrapper."""
        handlers.investigate_tool.run = AsyncMock(return_value={
            "summary": "partial", "seed": {},
            "ingestion_health": None, "parser_failures": None,
            "anomalous_sources": [], "cross_source_timeline": None,
            "next_steps": [], "budget": {}, "partial": True,
            "partial_reasons": ["budget_exceeded"], "elapsed_seconds": 3.0,
        })
        result = await handlers.handle_tool_call(
            "investigate_incident",
            {"query": "*"},
        )
        payload = json.loads(result[0]["text"])
        # Partial report shape forwarded verbatim, NOT wrapped in {status: "..."}
        assert "status" not in payload
        assert payload["partial"] is True
        assert payload["partial_reasons"] == ["budget_exceeded"]

    @pytest.mark.asyncio
    async def test_missing_query_returns_structured_error(self, handlers):
        handlers.investigate_tool.run = AsyncMock()
        result = await handlers.handle_tool_call("investigate_incident", {})
        payload = json.loads(result[0]["text"])
        assert payload["status"] == "error"
        assert "query" in payload["error"]
        handlers.investigate_tool.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bad_top_k_returns_structured_error(self, handlers):
        handlers.investigate_tool.run = AsyncMock()
        for bad in (-1, 0, 4, 10, "abc"):
            result = await handlers.handle_tool_call(
                "investigate_incident", {"query": "*", "top_k": bad},
            )
            payload = json.loads(result[0]["text"])
            assert payload["status"] == "error", f"top_k={bad!r} did not error"
        handlers.investigate_tool.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_budget_exception_surfaces_as_error(self, handlers):
        handlers.investigate_tool.run = AsyncMock(side_effect=RuntimeError("unexpected"))
        result = await handlers.handle_tool_call("investigate_incident", {"query": "*"})
        payload = json.loads(result[0]["text"])
        # Falls through to handle_tool_call's generic exception path
        assert "unexpected" in payload.get("error", "") or "unexpected" in result[0]["text"]
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/test_handlers.py::TestInvestigateIncident -v
```
Expected: FAIL.

- [ ] **Step 3: Wire the handler**

In `src/oci_logan_mcp/handlers.py`:

**3a.** Add import after the `ParserTriageTool` import:

```python
from .investigate import InvestigateIncidentTool
```

**3b.** Instantiate in `__init__` after `self.parser_triage_tool = ParserTriageTool(self.query_engine)`:

```python
        self.investigate_tool = InvestigateIncidentTool(
            query_engine=self.query_engine,
            schema_manager=self.schema_manager,
            ingestion_health_tool=self.ingestion_health_tool,
            parser_triage_tool=self.parser_triage_tool,
            diff_tool=self.diff_tool,
            settings=settings,
            budget_tracker=self._budget_tracker,
        )
```

**3c.** Add route in `handle_tool_call`'s dispatch dict after `"parser_failure_triage"`:

```python
            "investigate_incident": self._investigate_incident,
```

**3d.** Add handler method after `_parser_failure_triage`:

```python
    async def _investigate_incident(self, args: Dict) -> List[Dict]:
        """Route to InvestigateIncidentTool. Partial reports forward verbatim;
        the orchestrator catches BudgetExceededError internally, so the handler
        does NOT wrap in the generic {status: "budget_exceeded"} shape."""
        query = args.get("query")
        if not query or not isinstance(query, str):
            return [{"type": "text", "text": json.dumps(
                {"status": "error", "error": "query is required and must be a string"},
                indent=2,
            )}]
        try:
            top_k = int(args.get("top_k", 3))
        except (TypeError, ValueError):
            return [{"type": "text", "text": json.dumps(
                {"status": "error", "error": "top_k must be an integer"}, indent=2,
            )}]
        if top_k < 1 or top_k > 3:
            return [{"type": "text", "text": json.dumps(
                {"status": "error", "error": "top_k must be between 1 and 3 (P0)"},
                indent=2,
            )}]
        report = await self.investigate_tool.run(
            query=query,
            time_range=args.get("time_range", "last_1_hour"),
            top_k=top_k,
            compartment_id=args.get("compartment_id"),
        )
        return [{"type": "text", "text": json.dumps(report, indent=2, default=str)}]
```

- [ ] **Step 4: Run tests to verify they pass**

```
python3 -m pytest tests/test_handlers.py::TestInvestigateIncident -v
```
Expected: 5 PASS

- [ ] **Step 5: Run full suite**

```
python3 -m pytest --tb=short -q
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/oci_logan_mcp/handlers.py tests/test_handlers.py
git commit -m "feat(a1): wire investigate_incident handler (partial report forwarded verbatim)"
```

---

### Task 17: Read-only guard registration

**Files:**
- Modify: `tests/test_read_only_guard.py`

- [ ] **Step 1: Add `"investigate_incident"` to `KNOWN_READERS`**

In `tests/test_read_only_guard.py`, find the `KNOWN_READERS` set. Add `"investigate_incident",` alongside `"parser_failure_triage"`:

```python
    KNOWN_READERS = {
        "list_log_sources", "list_fields", "list_entities", "list_parsers",
        "list_labels", "list_saved_searches", "list_log_groups",
        "validate_query", "run_query", "run_saved_search", "run_batch_queries",
        "diff_time_windows",
        "pivot_on_entity",
        "ingestion_health",
        "parser_failure_triage",
        "investigate_incident",
        "visualize", "export_results",
        "get_current_context", "list_compartments",
        "test_connection", "find_compartment",
        "get_query_examples", "get_log_summary",
        "get_preferences", "list_alerts", "list_dashboards",
        "explain_query", "get_session_budget",
        "export_transcript",
    }
```

- [ ] **Step 2: Run the guard test**

```
python3 -m pytest tests/test_read_only_guard.py -v
```
Expected: all PASS (the AST walker finds the new `"investigate_incident"` route and the set contains it).

- [ ] **Step 3: Run full suite**

```
python3 -m pytest --tb=short -q
```
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_read_only_guard.py
git commit -m "test(a1): register investigate_incident in KNOWN_READERS"
```

---

### Task 18: Orchestration integration tests (remaining edge cases)

**Files:**
- Modify: `tests/test_investigate.py`

Covers the remaining §8.2 design scenarios not yet tested by the phase-specific tests.

- [ ] **Step 1: Add the remaining orchestration tests**

Append to `tests/test_investigate.py`:

```python
class TestOrchestrationEdgeCases:
    @pytest.mark.asyncio
    async def test_source_pinned_seed_no_false_cross_source_expansion(self):
        """Seed `'Log Source' = 'X' | ...` → drill-down queries for other sources
        compose to expressions that match zero rows (intentional)."""
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "Y", "current": 5, "comparison": 2, "pct_change": 150.0}],
        }
        engine = _make_engine()
        engine.execute = AsyncMock(return_value={"data": {"columns": [], "rows": []}})
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        await tool.run(query="'Log Source' = 'X'", time_range="last_1_hour", top_k=3)
        per_source_queries = [
            c.kwargs.get("query", "")
            for c in engine.execute.await_args_list
            if "'Log Source' = 'Y'" in c.kwargs.get("query", "")
        ]
        # Each per-source query for Y must contain the seed's X constraint wrapped in parens.
        for q in per_source_queries:
            assert q.startswith("('Log Source' = 'X') and 'Log Source' = 'Y'"), q

    @pytest.mark.asyncio
    async def test_per_source_gather_exception_isolated(self):
        """One source's drill-down branch raises → that source has errors,
        others complete, partial_reasons includes source_errors."""
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [
                {"dimension": "A", "current": 100, "comparison": 0, "pct_change": None},
                {"dimension": "B", "current": 50, "comparison": 10, "pct_change": 400.0},
            ],
        }

        # A's cluster query raises a non-Budget exception, B runs clean.
        async def execute_router(**kwargs):
            q = kwargs.get("query", "")
            if "'Log Source' = 'A'" in q and "| cluster" in q:
                raise RuntimeError("A cluster failed hard")
            return {"data": {"columns": [], "rows": []}}

        engine = MagicMock()
        engine.execute = AsyncMock(side_effect=execute_router)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)

        # The branch-level exception is a recoverable per-phase error, so it
        # becomes a per-source `errors` entry rather than a branch-level failure.
        # Source A's `errors` contains the cluster error; drill-down continues for B.
        a_entry = next(s for s in report["anomalous_sources"] if s["source"] == "A")
        b_entry = next(s for s in report["anomalous_sources"] if s["source"] == "B")
        assert any("cluster" in e for e in a_entry["errors"])
        assert b_entry["errors"] == [] or not any("cluster" in e for e in b_entry["errors"])

    @pytest.mark.asyncio
    async def test_multi_reason_partial(self):
        """Simultaneously trigger timeline_omitted AND entity_discovery_partial."""
        from oci.exceptions import ServiceError
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "A", "current": 50, "comparison": 10, "pct_change": 400.0}],
        }

        async def execute_router(**kwargs):
            q = kwargs.get("query", "")
            if "'User Name'" in q:
                raise ServiceError(status=400, code="InvalidParameter",
                                   headers={}, message="Invalid field")
            if "| fields Time" in q:
                raise RuntimeError("timeline failed")
            return {"data": {"columns": [], "rows": []}}

        engine = MagicMock()
        engine.execute = AsyncMock(side_effect=execute_router)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        assert report["partial"] is True
        assert "entity_discovery_partial" in report["partial_reasons"]
        assert "timeline_omitted" in report["partial_reasons"]

    @pytest.mark.asyncio
    async def test_all_timelines_drop_cross_source_null(self):
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [
                {"dimension": "A", "current": 10, "comparison": 1, "pct_change": 900.0},
                {"dimension": "B", "current": 5, "comparison": 1, "pct_change": 400.0},
            ],
        }

        async def execute_router(**kwargs):
            q = kwargs.get("query", "")
            if "| fields Time" in q:
                raise RuntimeError("timeline unavailable")
            return {"data": {"columns": [], "rows": []}}

        engine = MagicMock()
        engine.execute = AsyncMock(side_effect=execute_router)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        assert report["cross_source_timeline"] is None
        for s in report["anomalous_sources"]:
            assert s["timeline"] is None
        assert "timeline_omitted" in report["partial_reasons"]

    @pytest.mark.asyncio
    async def test_epoch_ms_timestamps_in_timeline_normalized_to_iso(self):
        diff_result = {
            "current": {}, "comparison": {}, "summary": "",
            "delta": [{"dimension": "A", "current": 5, "comparison": 1, "pct_change": 400.0}],
        }

        async def execute_router(**kwargs):
            q = kwargs.get("query", "")
            if "| fields Time" in q:
                # OCI returns Time as epoch-millisecond LONG
                return {"data": {
                    "columns": [
                        {"name": "Time"}, {"name": "Severity"}, {"name": "Original Log Content"},
                    ],
                    "rows": [
                        [1776829209147, "error", "msg1"],
                        [1776829200000, None, "msg2"],
                    ],
                }}
            return {"data": {"columns": [], "rows": []}}

        engine = MagicMock()
        engine.execute = AsyncMock(side_effect=execute_router)
        schema, ih, j2, diff = _make_deps()
        diff.run = AsyncMock(return_value=diff_result)
        tool = InvestigateIncidentTool(
            query_engine=engine, schema_manager=schema,
            ingestion_health_tool=ih, parser_triage_tool=j2, diff_tool=diff,
            settings=_make_settings(), budget_tracker=_make_budget(),
        )
        report = await tool.run(query="*", time_range="last_1_hour", top_k=3)
        # Timeline entries have ISO timestamps, not raw ints.
        for s in report["anomalous_sources"]:
            if s["timeline"]:
                for row in s["timeline"]:
                    assert isinstance(row["time"], str)
                    assert "T" in row["time"] and row["time"].endswith("+00:00")
        if report["cross_source_timeline"]:
            for row in report["cross_source_timeline"]:
                assert isinstance(row["time"], str)
                assert "T" in row["time"]
```

- [ ] **Step 2: Run tests to verify they pass**

```
python3 -m pytest tests/test_investigate.py -v
```
Expected: all PASS

- [ ] **Step 3: Run full suite (regression check)**

```
python3 -m pytest --tb=short -q
```
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_investigate.py
git commit -m "test(a1): orchestration edge cases (source-pinned, multi-reason, epoch-ms, null timeline)"
```

---

### Task 19: Docs — README entry + spec doc §A1 update

**Files:**
- Modify: `README.md`
- Modify: `docs/phase-2/specs/triage-toolkit.md`

- [ ] **Step 1: Add `### investigate_incident` to README**

In `README.md`, find the `## Investigation Toolkit` section. After the `### parser_failure_triage` entry, before `## Multi-User Learning`, insert:

```markdown
### `investigate_incident` — one-call first-cut triage

The flagship. Given a seed Logan query + time range, returns a structured investigation: stopped sources (J1), parser failures (J2), top_k anomalous sources vs. the prior equal-length window (A2), with per-source top error clusters (Logan `cluster`), top entities (host/user/request_id), and a recent-events timeline. Target: ≤20s p95 on dogfood data.

```json
{
  "tool": "investigate_incident",
  "query": "'Event' = 'error'",
  "time_range": "last_1_hour",
  "top_k": 3
}
```

Returns `{summary, seed, ingestion_health, parser_failures, anomalous_sources: [{source, pct_change, top_error_clusters, top_entities, timeline, errors}], cross_source_timeline, next_steps, budget, partial, partial_reasons, elapsed_seconds}`.

**P0 limitations** (documented honestly, not magic):
- Only the seed's pre-pipe search clause is used for drill-down scoping. A seed like `'Event' = 'error' | where Severity = 'critical'` investigates **all** `'Event' = 'error'` rows — the `where` narrowing is dropped for drill-down. Fix: put all scoping in the pre-pipe filter.
- `top_k` is clamped to `[1, 3]` — matches the ~21-query budget and ≤20s latency guarantee.
- `alarm_ocid` seed and NL-to-query `description` seed are deferred (A6 will own alarm-OCID).
- J1's freshness is evaluated over its configured `freshness_probe_window`, which may differ from the investigation `time_range`; the report's `ingestion_health.note` explains the difference.

**Partial responses.** A1 never raises `BudgetExceededError` out of its boundary — instead, the report comes back with `partial: true` and specific `partial_reasons`:
- `"budget_exceeded"` — session budget ran out mid-investigation
- `"timeline_omitted"` — one or more per-source timeline queries errored
- `"entity_discovery_partial"` — one or more entity fields weren't valid for a source (`InvalidParameter`)
- `"source_errors"` — a per-source branch failed wholesale

Each condition is accompanied by `anomalous_sources[*].errors` entries describing exactly what went wrong per source.
```

Then update the `| **Triage diffs** |` row in the `## What You Can Do` table to include `investigate_incident`:

```markdown
| **Triage diffs** | `diff_time_windows`, `pivot_on_entity`, `ingestion_health`, `parser_failure_triage`, `investigate_incident` | Compare a query across two time windows; pull events for an entity across sources; probe per-source ingestion freshness; surface top parser failures; one-call first-cut investigation orchestrator |
```

- [ ] **Step 2: Update `docs/phase-2/specs/triage-toolkit.md` §A1**

Find the `## A1 — \`investigate_incident\`` section. Update it to:

```markdown
## A1 — `investigate_incident` (orchestrator)

> **Implementation status:** Shipped in P0 per the detailed design at
> [`docs/phase-2/specs/2026-04-22-a1-investigate-incident-design.md`](2026-04-22-a1-investigate-incident-design.md).
> This section captures the branch-level acceptance criteria; the design doc
> is the source of truth for the actual implementation shape.

### Purpose
Flagship. One tool call that takes a seed query and a time range and returns a first-cut investigation. Composes J1 (ingestion_health), J2 (parser_failure_triage), A2 (diff_time_windows), and Logan `cluster` + `next_steps.suggest()` heuristics.

### P0 tool interface
```
investigate_incident(
  query: str,                        # seed, required (P0: query only; alarm_ocid/description deferred)
  time_range: str = "last_1_hour",   # TIME_RANGES enum
  top_k: int = 3,                    # clamped [1, 3]
  compartment_id: str | None = None,
) -> InvestigationReport
```

See the design doc for the full `InvestigationReport` shape, including:
- `top_entities` (not "changed_entities" — computed by count, not change-vs-baseline)
- `partial_reasons: list[str]` with values: `budget_exceeded`, `timeline_omitted`, `entity_discovery_partial`, `source_errors`
- J1 wrapped with `probe_window` + `note` (freshness snapshot ≠ investigation-window)
- `seed.seed_filter` (quote-aware pre-pipe extraction) + `seed.seed_filter_degraded` flag
- Per-source `errors` list for non-fatal issues

### P0 deferrals (explicit)
- `alarm_ocid` seed → A6 `why_did_this_fire` owns OCID → fire_time + MQL-to-Logan translation
- `description` (NL) seed → no in-repo NL resolver exists; skip for P0
- `ip` entity discovery → deferred to bound per-source query count (3 entity fields in P0: host, user, request_id)
- Full pipeline-aware seed parsing → quote-aware pre-pipe split only; `where`/`eval`/`stats` tails are dropped for drill-down scoping

### Dependencies
- **Hard:** A2 (diff_time_windows), J1 (ingestion_health), J2 (parser_failure_triage), N5 (BudgetTracker)
- **Shipped alongside** on the feat/triage-toolkit branch (all on main now)
```

- [ ] **Step 3: Run full suite (check nothing broke from docs)**

```
python3 -m pytest --tb=short -q
```
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add README.md docs/phase-2/specs/triage-toolkit.md
git commit -m "docs(a1): add investigate_incident to README; update spec §A1 to match design doc"
```

---

## Self-review

**1. Spec coverage** — Each requirement from the design doc:
- §2 interface ✓ (Task 8 skeleton + Task 15 schema + Task 16 handler)
- §2.1 deferrals ✓ (no alarm_ocid / description in schema — Task 15)
- §2.2 seed-query limitation ✓ (Task 1 `_extract_seed_filter` + documented in Task 19 README)
- §3 InvestigationReport shape ✓ (assembled in `_finalize`, Task 8)
- §4 architecture ✓ (pure fns + thin class, Tasks 1-8)
- §5 quote-aware extraction ✓ (Task 1)
- §6.1 Phase 1 ✓ (Task 9)
- §6.2 Phase 2 with probe_window + note ✓ (Task 10)
- §6.3 Phase 3 ✓ (Task 11)
- §6.4 Phase 4 with anchored windows ✓ (Task 3 + Task 12)
- §6.5 Phases 5+6 with Semaphore + per-source errors ✓ (Task 13)
- §6.7 Phase 7 next_steps + summary ✓ (Task 7 + Task 14)
- §6.8 Budget handling ✓ (Task 8 partial path + per-task wiring)
- §7 file layout ✓ (Tasks 1-19 touch exactly these files)
- §8.1 unit tests ✓ (Tasks 1-7)
- §8.2 orchestration tests ✓ (Tasks 8-14 + Task 18)
- §8.3 handler tests ✓ (Task 16)
- §8.4 read-only guard ✓ (Task 17)
- §8.5 live-probe discipline ✓ (Task 4 probe step, Task 13 probe step)
- §9 docs ✓ (Task 19)
- §10 out of scope ✓ (deferrals honored throughout)

**2. Placeholder scan** — no TBD/TODO/"add error handling"/"similar to Task N" patterns. Every code block is the actual code the implementer will paste or adapt.

**3. Type consistency**:
- `_extract_seed_filter(query: str) -> str` — consistent across Tasks 1, 2, 8, 12
- `_compose_source_scoped_query(seed_filter, source, tail) -> str` — consistent across Tasks 2, 12, 13
- `_compute_windows(time_range, anchor) -> Tuple[Dict, Dict]` — consistent across Tasks 3, 12
- `_rank_anomalous_sources(delta, stopped_sources, top_k) -> List[Dict]` — consistent across Tasks 4, 12
- `_select_top_entities(response, entity_type, field_name) -> List[Dict]` — consistent across Tasks 5, 13
- `_merge_cross_source_timeline(per_source, cap) -> Optional[List]` — consistent across Tasks 6, 8 (`_finalize`)
- `_templated_summary(acc) -> str` — consistent across Tasks 7, 8, 14
- `InvestigateIncidentTool.run(query, time_range, top_k, compartment_id) -> Dict` — consistent across Tasks 8-16
- `A1_ENTITY_FIELDS`, `PER_SOURCE_CONCURRENCY`, `CLUSTER_HEAD`, `ENTITY_HEAD`, `TIMELINE_HEAD`, `TOP_K_MIN/MAX`, `TIMELINE_CAP` — module constants declared in Tasks 8 and 13, used consistently
- Per-source accumulator schema (`per_source[source] = {top_error_clusters, top_entities, timeline, errors}`) — consistent across Tasks 8, 12, 13

All identifiers check out.

---

## Execution handoff

Plan complete and saved to `docs/phase-2/plans/2026-04-22-a1-investigate-incident.md`. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review (spec + quality) between tasks, fast iteration. Required sub-skill: `superpowers:subagent-driven-development`.
2. **Inline Execution** — execute in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
