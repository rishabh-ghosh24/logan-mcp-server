# J1 — `ingestion_health` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `ingestion_health(compartment_id?, sources?, severity_filter?)` — one MCP call that probes the freshness of log ingestion and classifies each source as `healthy`, `stopped`, or `unknown`. Answers the 2am question "is log ingestion even working right now?" and is a hard dependency of A1 `investigate_incident` (step 2: enumerate stopped sources).

**Architecture:**
- **New module `src/oci_logan_mcp/ingestion_health.py`** with an `IngestionHealthTool` class that takes an existing `QueryEngine`, `SchemaManager`, and the ingestion-health settings. Pure consumer: no new OCI client code.
- **One aggregate probe query.** `'* | stats max(Time) as last_log_ts by 'Log Source'`, bounded by `freshness_probe_window` (config, default `last_1_hour`). A single query classifies every source that emitted in the window. Sources present in the enumeration but absent from the result are `unknown`. This design mirrors how we picked aggregate-query-then-classify for A2 — one query, local post-processing — keeping cost identical to the sibling primitives.
- **Source enumeration for `unknown` detection.** When `sources=None`, enumerate via `schema_manager.get_log_sources(compartment_id)` to get the target set. A source only counts as "stopped" or "unknown" if we *expected* it to emit; otherwise we have nothing to compare against. If `sources=[...]` is supplied, skip enumeration and use the caller's list verbatim — plus compose `'Log Source' in (...)` into the probe query so OCI does the filtering server-side.
- **Pure classifier.** `_classify(last_log_ts, checked_at, threshold_s)` → `(status, severity, age_seconds, message)`. Three branches: `None` → `unknown/warn`; age ≥ threshold → `stopped/critical`; else → `healthy/info`. Pure function, easy to unit-test without a QueryEngine.
- **Timestamp handling.** OCI responses return `Time` values as ISO-8601 strings (e.g. `"2026-04-22T10:00:00Z"`). `_parse_ts()` accepts ISO-8601 with or without a trailing `Z`; returns `datetime` in UTC or `None` on parse failure. On the response, `last_log_ts` is emitted as an ISO-8601 string (JSON-safe) and `checked_at` is the tool's snapshot time. `age_seconds` is an integer.
- **Severity filter.** Post-classification, drop findings below the requested tier. `all` → keep everything (including `info`/healthy); `warn` → keep `warn`+`critical` (the spec default); `critical` → keep only `critical`. `summary` counts are always computed over the full set so the caller can see how many healthy sources exist even when filtered out.
- **Budget behavior.** The probe is a single `QueryEngine.execute(...)` call; `BudgetExceededError` propagates to the handler, which catches it and returns a structured `{"status": "budget_exceeded", "error": ..., "partial": null}` payload consistent with A2/A4.
- **What this is NOT.** No persistent baseline store, no DROP/LAG classification, no background sampler, no per-entity-within-source freshness. Those are P1 per the spec's "Deferred to P1" list. P0 is deliberately small and trustworthy.

**Tech Stack:** Python 3, pytest, `datetime` from stdlib, `zoneinfo`/`timezone.utc`. No new runtime dependencies.

**Spec:** [../specs/triage-toolkit.md#j1--ingestion-health-freshness--stoppage-detection](../specs/triage-toolkit.md) · feature J1.

---

## File Structure

**Create:**
- `src/oci_logan_mcp/ingestion_health.py` — `IngestionHealthTool` class + pure helpers (`_classify`, `_parse_ts`, `_extract_last_seen_map`, `_compose_probe_query`).
- `tests/test_ingestion_health.py` — unit tests for the five spec scenarios plus classifier/parser coverage.

**Modify:**
- `src/oci_logan_mcp/config.py` — add `IngestionHealthConfig` dataclass; wire into `Settings`, `to_dict()`, `_parse_config()`.
- `src/oci_logan_mcp/tools.py` — register `ingestion_health` schema (after `pivot_on_entity`, ~line 290).
- `src/oci_logan_mcp/handlers.py` — import `IngestionHealthTool`; construct in `__init__` (near `self.pivot_tool`); add `_ingestion_health` handler; register in the `handlers` dict inside `handle_tool_call`.
- `tests/test_handlers.py` — one smoke test that verifies routing + JSON payload shape.
- `tests/test_read_only_guard.py` — add `"ingestion_health"` to `KNOWN_READERS` (pure reader; only runs one probe query).
- `tests/test_config.py` — round-trip test for the new `IngestionHealthConfig` serialization/parse.
- `README.md` — add a short "Investigation toolkit → ingestion_health" section.

**Do NOT modify:**
- `query_engine.py` — J1 is a pure consumer.
- `schema_manager.py` — reused as-is (`get_log_sources(compartment_id)`).
- `read_only_guard.py` — `ingestion_health` is a reader; belongs in the test's `KNOWN_READERS` allowlist, not `MUTATING_TOOLS`.
- `budget_tracker.py`, `query_estimator.py` — reused via `QueryEngine`.

**Out of scope (deferred to P1, with rationale from spec):**
- **Persistent per-source baseline store** (bytes/hour, count/hour, stddev) with background refresh — requires a durable store and a scheduler; separate subsystem.
- **DROP classification** (current volume vs. baseline) — requires baseline first.
- **LAG classification** (ingestion-time vs. event-time skew) — requires a second timestamp field we're not reading today.
- **Per-entity-within-source freshness** (host-level staleness inside a source) — higher-cardinality probe; callers can reach the same answer today via `pivot_on_entity` on a suspected host.
- **Cross-compartment sweeps** — `compartment_id` argument follows the existing `run_query` convention (single compartment per call). Multi-compartment rollups are an A1 concern.

---

## Task 1: Config plumbing — `IngestionHealthConfig`

**Files:**
- Modify: `src/oci_logan_mcp/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_ingestion_health_defaults():
    """IngestionHealthConfig defaults match the spec."""
    from oci_logan_mcp.config import Settings

    s = Settings()
    assert s.ingestion_health.stoppage_threshold_seconds == 600
    assert s.ingestion_health.freshness_probe_window == "last_1_hour"


def test_ingestion_health_roundtrip(tmp_path):
    """to_dict()/_parse_config() preserve ingestion_health overrides."""
    from oci_logan_mcp.config import Settings, save_config, load_config

    s = Settings()
    s.ingestion_health.stoppage_threshold_seconds = 120
    s.ingestion_health.freshness_probe_window = "last_4_hours"

    cfg_path = tmp_path / "cfg.yaml"
    save_config(s, cfg_path)
    loaded = load_config(cfg_path)

    assert loaded.ingestion_health.stoppage_threshold_seconds == 120
    assert loaded.ingestion_health.freshness_probe_window == "last_4_hours"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py::test_ingestion_health_defaults tests/test_config.py::test_ingestion_health_roundtrip -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'ingestion_health'`.

- [ ] **Step 3: Add the dataclass and wire into Settings**

In `src/oci_logan_mcp/config.py`, add after `BudgetConfig` (~line 112):

```python
@dataclass
class IngestionHealthConfig:
    """J1 — ingestion health tool configuration."""

    # A source whose most recent log is older than this is classified `stopped/critical`.
    stoppage_threshold_seconds: int = 600
    # Time window for the freshness probe query; any source with no record in
    # this window is classified `unknown/warn`.
    freshness_probe_window: str = "last_1_hour"
```

Add the field to `Settings` (right after `budget`):

```python
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    ingestion_health: IngestionHealthConfig = field(default_factory=IngestionHealthConfig)
```

Add to `Settings.to_dict()` (right after the `"budget": {...}` block, before `"transcript_dir"`):

```python
            "ingestion_health": {
                "stoppage_threshold_seconds": self.ingestion_health.stoppage_threshold_seconds,
                "freshness_probe_window": self.ingestion_health.freshness_probe_window,
            },
```

Add to `_parse_config()` (right after the `budget_data` block):

```python
    if ih_data := data.get("ingestion_health"):
        settings.ingestion_health = IngestionHealthConfig(
            stoppage_threshold_seconds=ih_data.get(
                "stoppage_threshold_seconds",
                settings.ingestion_health.stoppage_threshold_seconds,
            ),
            freshness_probe_window=ih_data.get(
                "freshness_probe_window",
                settings.ingestion_health.freshness_probe_window,
            ),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS (new tests + existing tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/config.py tests/test_config.py
git commit -m "feat(j1): add IngestionHealthConfig with stoppage threshold + probe window"
```

---

## Task 2: Pure classifier + timestamp parser

**Files:**
- Create: `src/oci_logan_mcp/ingestion_health.py`
- Create: `tests/test_ingestion_health.py`

The classifier is pure; no mocking needed. Pinning it first means every later task can rely on its shape.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingestion_health.py`:

```python
"""Tests for ingestion_health tool."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from oci_logan_mcp.ingestion_health import (
    IngestionHealthTool,
    _classify,
    _parse_ts,
)


class TestParseTs:
    def test_parses_iso_z(self):
        dt = _parse_ts("2026-04-22T10:00:00Z")
        assert dt == datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)

    def test_parses_iso_offset(self):
        dt = _parse_ts("2026-04-22T10:00:00+00:00")
        assert dt == datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)

    def test_none_passthrough(self):
        assert _parse_ts(None) is None

    def test_empty_string(self):
        assert _parse_ts("") is None

    def test_garbage(self):
        assert _parse_ts("not-a-date") is None


class TestClassify:
    def test_unknown_when_last_log_ts_none(self):
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        status, severity, age, msg = _classify(None, now, threshold_s=600)
        assert status == "unknown"
        assert severity == "warn"
        assert age is None
        assert "no records" in msg.lower()

    def test_healthy_when_age_under_threshold(self):
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(seconds=60)
        status, severity, age, msg = _classify(last, now, threshold_s=600)
        assert status == "healthy"
        assert severity == "info"
        assert age == 60
        assert "60s" in msg or "60 s" in msg

    def test_stopped_when_age_at_or_above_threshold(self):
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(seconds=1800)  # 30 min
        status, severity, age, msg = _classify(last, now, threshold_s=600)
        assert status == "stopped"
        assert severity == "critical"
        assert age == 1800
        assert "stopped" in msg.lower() or "stale" in msg.lower()

    def test_boundary_exactly_at_threshold_is_stopped(self):
        now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(seconds=600)
        status, _, _, _ = _classify(last, now, threshold_s=600)
        assert status == "stopped"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingestion_health.py -v`
Expected: FAIL with `ModuleNotFoundError: oci_logan_mcp.ingestion_health`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/oci_logan_mcp/ingestion_health.py`:

```python
"""ingestion_health — freshness/stoppage detection for log sources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string to a UTC-aware datetime.

    Accepts trailing `Z` (RFC 3339). Returns None on any parse failure so the
    classifier can treat missing/malformed timestamps as `unknown` without
    raising into the handler.
    """
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _classify(
    last_log_ts: Optional[datetime],
    checked_at: datetime,
    threshold_s: int,
) -> Tuple[str, str, Optional[int], str]:
    """Classify a source as healthy/stopped/unknown based on last-seen age.

    Returns (status, severity, age_seconds, message).
    """
    if last_log_ts is None:
        return (
            "unknown",
            "warn",
            None,
            "No records in freshness probe window.",
        )
    age = int((checked_at - last_log_ts).total_seconds())
    if age >= threshold_s:
        return (
            "stopped",
            "critical",
            age,
            f"Ingestion stopped — last record {age}s ago (threshold {threshold_s}s).",
        )
    return (
        "healthy",
        "info",
        age,
        f"Healthy — last record {age}s ago.",
    )


class IngestionHealthTool:
    """Probe log-source freshness and classify stoppages."""

    def __init__(self, query_engine, schema_manager, settings):
        self._engine = query_engine
        self._schema = schema_manager
        self._settings = settings

    async def run(
        self,
        compartment_id: Optional[str] = None,
        sources: Optional[List[str]] = None,
        severity_filter: str = "warn",
    ) -> Dict[str, Any]:
        raise NotImplementedError  # wired in Task 3
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ingestion_health.py -v`
Expected: PASS (9 tests: 5 parse + 4 classify).

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/ingestion_health.py tests/test_ingestion_health.py
git commit -m "feat(j1): IngestionHealthTool skeleton with classifier + ts parser"
```

---

## Task 3: Wire probe query + orchestration (healthy/stopped/unknown paths)

Covers spec tests 1 (healthy), 2 (stopped), and 3 (unknown) end-to-end.

**Files:**
- Modify: `src/oci_logan_mcp/ingestion_health.py`
- Modify: `tests/test_ingestion_health.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingestion_health.py`:

```python
# ---------------------------------------------------------------------------
# Fixtures for orchestration tests
# ---------------------------------------------------------------------------

def _make_engine(response):
    """Mock QueryEngine whose execute() returns `response` on one call."""
    engine = MagicMock()
    engine.execute = AsyncMock(return_value=response)
    return engine


def _make_schema(source_names):
    """Mock SchemaManager whose get_log_sources() returns these source dicts."""
    schema = MagicMock()
    schema.get_log_sources = AsyncMock(
        return_value=[{"name": n} for n in source_names]
    )
    return schema


def _make_settings(threshold_s=600, probe_window="last_1_hour"):
    from oci_logan_mcp.config import IngestionHealthConfig, Settings
    s = Settings()
    s.ingestion_health = IngestionHealthConfig(
        stoppage_threshold_seconds=threshold_s,
        freshness_probe_window=probe_window,
    )
    return s


def _probe_result(rows):
    """Shape a QueryEngine response around `[(source, last_log_ts), ...]` rows."""
    return {
        "source": "live",
        "data": {
            "columns": [{"name": "Log Source"}, {"name": "last_log_ts"}],
            "rows": [[src, ts] for src, ts in rows],
        },
        "metadata": {},
    }


class TestOrchestration:
    @pytest.mark.asyncio
    async def test_healthy_source_recent_record(self, monkeypatch):
        # "now" is frozen inside the tool by monkeypatching _utcnow.
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([
            ("Linux Syslog", "2026-04-22T09:59:30Z"),  # 30s ago → healthy
        ]))
        schema = _make_schema(["Linux Syslog"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run()

        # Summary counts are global — healthy source is counted here.
        assert result["summary"] == {
            "sources_healthy": 1,
            "sources_stopped": 0,
            "sources_unknown": 0,
        }
        # But `severity_filter` defaults to "warn", which drops info-severity
        # (healthy) findings from the list. Pin that: summary shows 1 healthy
        # source; findings is empty. The positive-case shape assertions live
        # in `test_severity_filter_all_shows_healthy` below.
        assert result["findings"] == []

    @pytest.mark.asyncio
    async def test_severity_filter_all_shows_healthy(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([
            ("Linux Syslog", "2026-04-22T09:59:30Z"),
        ]))
        schema = _make_schema(["Linux Syslog"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run(severity_filter="all")

        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["source"] == "Linux Syslog"
        assert f["status"] == "healthy"
        assert f["severity"] == "info"
        assert f["age_seconds"] == 30
        assert f["last_log_ts"] == "2026-04-22T09:59:30+00:00"

    @pytest.mark.asyncio
    async def test_stopped_source_30min_stale(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([
            ("Apache Access", "2026-04-22T09:30:00Z"),  # 30 min ago → stopped
        ]))
        schema = _make_schema(["Apache Access"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run()  # default severity_filter="warn"

        assert result["summary"]["sources_stopped"] == 1
        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["source"] == "Apache Access"
        assert f["status"] == "stopped"
        assert f["severity"] == "critical"
        assert f["age_seconds"] == 1800

    @pytest.mark.asyncio
    async def test_unknown_source_no_records(self, monkeypatch):
        """Source enumerated via schema but absent from the probe result → unknown."""
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([]))  # nothing in window
        schema = _make_schema(["Silent Source"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run()

        assert result["summary"]["sources_unknown"] == 1
        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["source"] == "Silent Source"
        assert f["status"] == "unknown"
        assert f["severity"] == "warn"
        assert f["age_seconds"] is None
        assert f["last_log_ts"] is None

    @pytest.mark.asyncio
    async def test_checked_at_is_included(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([]))
        schema = _make_schema(["Anything"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run()

        assert result["checked_at"] == "2026-04-22T10:00:00+00:00"

    @pytest.mark.asyncio
    async def test_probe_query_uses_configured_window(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([]))
        schema = _make_schema([])
        settings = _make_settings(probe_window="last_4_hours")
        tool = IngestionHealthTool(engine, schema, settings)

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        await tool.run()

        kwargs = engine.execute.call_args.kwargs
        assert kwargs["time_range"] == "last_4_hours"
        assert "stats max('Time') as last_log_ts by 'Log Source'" in kwargs["query"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingestion_health.py::TestOrchestration -v`
Expected: FAIL — `IngestionHealthTool.run` raises `NotImplementedError`.

- [ ] **Step 3: Implement the orchestration**

Replace the placeholder `run()` in `src/oci_logan_mcp/ingestion_health.py` and add the new helpers:

```python
"""ingestion_health — freshness/stoppage detection for log sources."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _utcnow() -> datetime:
    """Seam for tests — monkeypatch this in unit tests to freeze time."""
    return datetime.now(timezone.utc)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string to a UTC-aware datetime.

    Accepts trailing `Z` (RFC 3339). Returns None on any parse failure so the
    classifier can treat missing/malformed timestamps as `unknown` without
    raising into the handler.
    """
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _classify(
    last_log_ts: Optional[datetime],
    checked_at: datetime,
    threshold_s: int,
) -> Tuple[str, str, Optional[int], str]:
    """Classify a source as healthy/stopped/unknown based on last-seen age."""
    if last_log_ts is None:
        return (
            "unknown",
            "warn",
            None,
            "No records in freshness probe window.",
        )
    age = int((checked_at - last_log_ts).total_seconds())
    if age >= threshold_s:
        return (
            "stopped",
            "critical",
            age,
            f"Ingestion stopped — last record {age}s ago (threshold {threshold_s}s).",
        )
    return (
        "healthy",
        "info",
        age,
        f"Healthy — last record {age}s ago.",
    )


_SEVERITY_RANK = {"info": 0, "warn": 1, "critical": 2}


def _passes_severity_filter(severity: str, filter_level: str) -> bool:
    if filter_level == "all":
        return True
    required = _SEVERITY_RANK.get(filter_level, 1)  # default: warn
    return _SEVERITY_RANK.get(severity, 0) >= required


def _compose_probe_query(sources: Optional[List[str]]) -> str:
    """Build the `max(Time) by 'Log Source'` probe query, optionally filtered."""
    base = "* | stats max('Time') as last_log_ts by 'Log Source'"
    if not sources:
        return base
    escaped = ", ".join(f"'{s}'" for s in sources)
    return f"'Log Source' in ({escaped}) | stats max('Time') as last_log_ts by 'Log Source'"


def _extract_last_seen_map(response: Dict[str, Any]) -> Dict[str, Optional[datetime]]:
    """Map source name → parsed last_log_ts from a probe response."""
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", [])]
    rows = data.get("rows", [])
    if "Log Source" not in columns or "last_log_ts" not in columns:
        return {}
    src_idx = columns.index("Log Source")
    ts_idx = columns.index("last_log_ts")
    out: Dict[str, Optional[datetime]] = {}
    for row in rows:
        if not row:
            continue
        name = str(row[src_idx])
        out[name] = _parse_ts(row[ts_idx] if ts_idx < len(row) else None)
    return out


class IngestionHealthTool:
    """Probe log-source freshness and classify stoppages."""

    def __init__(self, query_engine, schema_manager, settings):
        self._engine = query_engine
        self._schema = schema_manager
        self._settings = settings

    async def run(
        self,
        compartment_id: Optional[str] = None,
        sources: Optional[List[str]] = None,
        severity_filter: str = "warn",
    ) -> Dict[str, Any]:
        ih_cfg = self._settings.ingestion_health
        checked_at = _utcnow()

        # 1. Target set: caller-provided or enumerated via schema_manager.
        if sources is None:
            discovered = await self._schema.get_log_sources(
                compartment_id=compartment_id
            )
            target_sources = [s.get("name") for s in discovered if s.get("name")]
        else:
            target_sources = list(sources)

        # 2. Run the probe query.
        query = _compose_probe_query(sources)
        response = await self._engine.execute(
            query=query,
            time_range=ih_cfg.freshness_probe_window,
            compartment_id=compartment_id,
        )
        last_seen = _extract_last_seen_map(response)

        # 3. Classify every target source.
        findings_all: List[Dict[str, Any]] = []
        summary = {"sources_healthy": 0, "sources_stopped": 0, "sources_unknown": 0}
        for name in target_sources:
            last_dt = last_seen.get(name)
            status, severity, age, message = _classify(
                last_dt, checked_at, ih_cfg.stoppage_threshold_seconds
            )
            summary[f"sources_{status}"] += 1
            findings_all.append({
                "source": name,
                "status": status,
                "last_log_ts": last_dt.isoformat() if last_dt else None,
                "age_seconds": age,
                "severity": severity,
                "message": message,
            })

        # 4. Apply severity filter to findings (summary counts stay global).
        findings = [
            f for f in findings_all
            if _passes_severity_filter(f["severity"], severity_filter)
        ]

        return {
            "summary": summary,
            "checked_at": checked_at.isoformat(),
            "findings": findings,
            "metadata": {
                "probe_query": query,
                "freshness_probe_window": ih_cfg.freshness_probe_window,
                "stoppage_threshold_seconds": ih_cfg.stoppage_threshold_seconds,
                "severity_filter": severity_filter,
                "sources_queried": target_sources,
            },
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ingestion_health.py -v`
Expected: PASS (all tests from Task 2 + Task 3).

- [ ] **Step 5: Commit**

```bash
git add src/oci_logan_mcp/ingestion_health.py tests/test_ingestion_health.py
git commit -m "feat(j1): probe query + orchestration (healthy/stopped/unknown)"
```

---

## Task 4: `sources` filter limits the probe set

Spec test 4. Pins that a caller-supplied list:
(a) is used verbatim as the target set (no schema enumeration), and
(b) gets composed into the probe query as `'Log Source' in (...)`.

**Files:**
- Modify: `tests/test_ingestion_health.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingestion_health.py`:

```python
class TestSourcesFilter:
    @pytest.mark.asyncio
    async def test_sources_arg_limits_probe_and_skips_enumeration(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        # Probe returns only web; audit was in the request but had no records.
        engine = _make_engine(_probe_result([
            ("web", "2026-04-22T09:59:00Z"),
        ]))
        # Schema returns something wildly different — must NOT be used.
        schema = _make_schema(["UNEXPECTED"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run(
            sources=["web", "audit"],
            severity_filter="all",
        )

        # schema.get_log_sources was not called — caller's list wins.
        schema.get_log_sources.assert_not_called()

        # Probe query filters to the caller's two sources.
        query = engine.execute.call_args.kwargs["query"]
        assert "'Log Source' in ('web', 'audit')" in query

        # Findings cover exactly the caller's two sources, one healthy, one unknown.
        by_source = {f["source"]: f for f in result["findings"]}
        assert set(by_source.keys()) == {"web", "audit"}
        assert by_source["web"]["status"] == "healthy"
        assert by_source["audit"]["status"] == "unknown"
        assert result["summary"] == {
            "sources_healthy": 1,
            "sources_stopped": 0,
            "sources_unknown": 1,
        }

    def test_compose_probe_query_no_filter(self):
        from oci_logan_mcp.ingestion_health import _compose_probe_query
        q = _compose_probe_query(None)
        assert q == "* | stats max('Time') as last_log_ts by 'Log Source'"

    def test_compose_probe_query_with_sources(self):
        from oci_logan_mcp.ingestion_health import _compose_probe_query
        q = _compose_probe_query(["A", "B"])
        assert q == "'Log Source' in ('A', 'B') | stats max('Time') as last_log_ts by 'Log Source'"
```

- [ ] **Step 2: Run tests to verify they pass (already implemented in Task 3)**

Run: `pytest tests/test_ingestion_health.py::TestSourcesFilter -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ingestion_health.py
git commit -m "test(j1): pin sources-filter composition and schema-bypass"
```

---

## Task 5: `severity_filter="critical"` omits healthy + unknown

Spec test 5. Pins the filter behavior so a future refactor can't silently surface warn/info rows when the caller asked for critical only.

**Files:**
- Modify: `tests/test_ingestion_health.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingestion_health.py`:

```python
class TestSeverityFilter:
    @pytest.mark.asyncio
    async def test_critical_filter_keeps_only_stopped(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([
            ("fresh",  "2026-04-22T09:59:30Z"),  # healthy/info
            ("stale",  "2026-04-22T09:30:00Z"),  # stopped/critical
            # "silent" is enumerated but absent from the probe → unknown/warn
        ]))
        schema = _make_schema(["fresh", "stale", "silent"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run(severity_filter="critical")

        # Summary counts are global — unchanged by the filter.
        assert result["summary"] == {
            "sources_healthy": 1,
            "sources_stopped": 1,
            "sources_unknown": 1,
        }
        # Findings are filtered to critical only.
        sources_in_findings = {f["source"] for f in result["findings"]}
        assert sources_in_findings == {"stale"}

    @pytest.mark.asyncio
    async def test_warn_filter_keeps_warn_and_critical(self, monkeypatch):
        frozen_now = datetime(2026, 4, 22, 10, 0, 0, tzinfo=timezone.utc)
        engine = _make_engine(_probe_result([
            ("fresh",  "2026-04-22T09:59:30Z"),
            ("stale",  "2026-04-22T09:30:00Z"),
        ]))
        schema = _make_schema(["fresh", "stale", "silent"])
        tool = IngestionHealthTool(engine, schema, _make_settings())

        import oci_logan_mcp.ingestion_health as ih
        monkeypatch.setattr(ih, "_utcnow", lambda: frozen_now)

        result = await tool.run(severity_filter="warn")

        sources_in_findings = {f["source"] for f in result["findings"]}
        assert sources_in_findings == {"stale", "silent"}

    def test_passes_severity_filter_all(self):
        from oci_logan_mcp.ingestion_health import _passes_severity_filter
        assert _passes_severity_filter("info", "all")
        assert _passes_severity_filter("warn", "all")
        assert _passes_severity_filter("critical", "all")

    def test_passes_severity_filter_warn(self):
        from oci_logan_mcp.ingestion_health import _passes_severity_filter
        assert not _passes_severity_filter("info", "warn")
        assert _passes_severity_filter("warn", "warn")
        assert _passes_severity_filter("critical", "warn")

    def test_passes_severity_filter_critical(self):
        from oci_logan_mcp.ingestion_health import _passes_severity_filter
        assert not _passes_severity_filter("info", "critical")
        assert not _passes_severity_filter("warn", "critical")
        assert _passes_severity_filter("critical", "critical")
```

- [ ] **Step 2: Run tests to verify they pass (already implemented in Task 3)**

Run: `pytest tests/test_ingestion_health.py::TestSeverityFilter -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ingestion_health.py
git commit -m "test(j1): pin severity_filter drop-below-threshold behavior"
```

---

## Task 6: Register `ingestion_health` MCP tool

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Modify: `tests/test_handlers.py`
- Modify: `tests/test_read_only_guard.py`

- [ ] **Step 1: Write the failing handler test**

Append to `tests/test_handlers.py`. The file already exposes a `handlers` fixture (~line 111) and already imports `AsyncMock`.

```python
class TestIngestionHealth:
    @pytest.mark.asyncio
    async def test_ingestion_health_routes_through_handler(self, handlers):
        """ingestion_health tool routes to IngestionHealthTool and returns JSON."""
        handlers.ingestion_health_tool.run = AsyncMock(return_value={
            "summary": {"sources_healthy": 1, "sources_stopped": 0, "sources_unknown": 0},
            "checked_at": "2026-04-22T10:00:00+00:00",
            "findings": [],
            "metadata": {},
        })

        result = await handlers.handle_tool_call(
            "ingestion_health",
            {"severity_filter": "warn"},
        )

        assert result[0]["type"] == "text"
        payload = json.loads(result[0]["text"])
        assert payload["summary"]["sources_healthy"] == 1
        handlers.ingestion_health_tool.run.assert_awaited_once_with(
            compartment_id=None,
            sources=None,
            severity_filter="warn",
        )

    @pytest.mark.asyncio
    async def test_ingestion_health_budget_exceeded_structured(self, handlers):
        """BudgetExceededError surfaces as a structured payload, not plain text."""
        from oci_logan_mcp.budget_tracker import BudgetExceededError
        handlers.ingestion_health_tool.run = AsyncMock(
            side_effect=BudgetExceededError("bytes limit hit")
        )

        result = await handlers.handle_tool_call("ingestion_health", {})

        payload = json.loads(result[0]["text"])
        assert payload["status"] == "budget_exceeded"
        assert "bytes limit hit" in payload["error"]
```

- [ ] **Step 2: Run handler tests to verify they fail**

Run: `pytest tests/test_handlers.py::TestIngestionHealth -v`
Expected: FAIL — `handlers.ingestion_health_tool` attribute doesn't exist; route returns "Unknown tool".

- [ ] **Step 3: Register schema in `tools.py`**

Insert after the `pivot_on_entity` block (~line 289) in `src/oci_logan_mcp/tools.py`:

```python
        {
            "name": "ingestion_health",
            "description": (
                "Check whether log ingestion is currently working. Runs one aggregate "
                "probe query and classifies each source as healthy (emitted recently), "
                "stopped (last record older than threshold), or unknown (no records in "
                "probe window). Answers 'is ingestion even working right now?' and is "
                "a foundational input to investigate_incident."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "compartment_id": {
                        "type": "string",
                        "description": "Optional compartment OCID. Uses default if not specified.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional. Limit the probe to these source names. If omitted, all discovered sources are probed.",
                    },
                    "severity_filter": {
                        "type": "string",
                        "enum": ["all", "warn", "critical"],
                        "description": "Drop findings below this severity tier. Default: 'warn' (shows warn + critical).",
                    },
                },
            },
        },
```

- [ ] **Step 4: Wire the handler**

In `src/oci_logan_mcp/handlers.py`:

1. Add an import near the existing tool imports (~line 30):

```python
from .ingestion_health import IngestionHealthTool
```

2. In `__init__`, after `self.pivot_tool = PivotTool(self.query_engine)` (~line 97), add:

```python
        self.ingestion_health_tool = IngestionHealthTool(
            self.query_engine, self.schema_manager, settings,
        )
```

3. In `handle_tool_call`, register the route. In the `handlers` dict (the block around lines 118–135), add next to `pivot_on_entity`:

```python
            "ingestion_health": self._ingestion_health,
```

4. Add the handler method near `_pivot_on_entity`:

```python
    async def _ingestion_health(self, args: Dict) -> List[Dict]:
        """Run ingestion_health. Catch budget breaches and return them as a
        structured payload instead of letting the generic exception path
        stringify them — keeps the shape consistent with A2/A4."""
        try:
            result = await self.ingestion_health_tool.run(
                compartment_id=args.get("compartment_id"),
                sources=args.get("sources"),
                severity_filter=args.get("severity_filter", "warn"),
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

- [ ] **Step 5: Keep the read-only drift test happy**

`ingestion_health` is a pure reader — it only runs a stats query. Do **not** add it to `MUTATING_TOOLS` in `read_only_guard.py`.

In `tests/test_read_only_guard.py` (~line 118), add `"ingestion_health"` to the `KNOWN_READERS` set, alongside `"diff_time_windows"` and `"pivot_on_entity"`:

```python
        "diff_time_windows",
        "pivot_on_entity",
        "ingestion_health",
```

- [ ] **Step 6: Run all affected tests**

Run: `pytest tests/test_ingestion_health.py tests/test_handlers.py tests/test_read_only_guard.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite — nothing else should regress**

Run: `pytest tests/ -x -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/oci_logan_mcp/tools.py src/oci_logan_mcp/handlers.py \
        tests/test_handlers.py tests/test_read_only_guard.py
git commit -m "feat(j1): register ingestion_health MCP tool"
```

---

## Task 7: README + tool-capability docs entry

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a docs section**

Find the existing "Investigation toolkit" / A2 + A4 section in `README.md` (the one added in A2 task 7) and append an `ingestion_health` entry below it:

```markdown
### `ingestion_health` — is ingestion even working?

Probe log-source freshness in one call. Classifies every source as `healthy`, `stopped`, or `unknown` based on how recently it last emitted a record. Cheapest signal-quality primitive.

```json
{
  "tool": "ingestion_health",
  "sources": ["Linux Syslog", "Apache Access"],
  "severity_filter": "warn"
}
```

Returns `{summary, checked_at, findings: [...]}` where each finding carries `status`, `severity`, `last_log_ts`, `age_seconds`, and a human-readable `message`.

Configurable via `ingestion_health.stoppage_threshold_seconds` (default 600s) and `ingestion_health.freshness_probe_window` (default `last_1_hour`) in `config.yaml`.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(j1): document ingestion_health tool"
```

---

## Verification checklist

Before marking the plan done:

- [ ] `pytest tests/test_ingestion_health.py -v` — green, covers all 5 spec scenarios (healthy / stopped / unknown / sources-filter / severity-filter) plus classifier and parser unit coverage.
- [ ] `pytest tests/ -x -q` — full suite still green.
- [ ] `ingestion_health` appears in `get_tools()`: `python -c "from oci_logan_mcp.tools import get_tools; print([t['name'] for t in get_tools()])"`.
- [ ] Read-only guard classifies `ingestion_health` as a reader (not in `MUTATING_TOOLS`; present in test-file `KNOWN_READERS`).
- [ ] No changes to `query_engine.py`, `schema_manager.py`, `budget_tracker.py`, or `read_only_guard.py` — J1 is a pure consumer.
- [ ] README has the new section and config keys are documented.

## Post-landing follow-ups (do not do in this plan)

- J2 `parser_failure_triage` is the sibling signal-quality primitive; implement via a separate plan (parallel subagent track).
- Once J1 + J2 ship, A1 `investigate_incident` step 2 can call J1 to seed its "stopped sources" enumeration.
- P1 expansion (per spec): persistent per-source baseline store enabling DROP and LAG classifications; J1 can then add `severity=warn` findings for "volume dropped but not zero" cases. H1 can also consume the baseline to sharpen its cost/ETA estimates.
