# H1 + N5 — `explain_query` and Session Query Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Why bundled:** N5 (budget) has a hard dependency on H1 (cost estimation) — N5's pre-flight check calls the estimator. The two features ship in a single PR to keep the contract coherent.

**Goal:** Give every query an *a-priori* cost/ETA estimate (H1) and enforce a session-level budget on top of it (N5). After this lands, an LLM caller cannot blindly run a 30-day tenancy-wide scan, and cannot runaway-loop past a configured ceiling.

**Architecture:**
- **H1 — `QueryEstimator`** is a stateless service that, given `(query, time_range)`, returns a `QueryEstimate` with fields `estimated_bytes`, `estimated_rows`, `estimated_cost_usd`, `estimated_eta_seconds`, `confidence`, `rationale`. Estimation is **probe-based**: for each source matched by the query's `Log Source = ...` filters, issue a cheap "last hour count" probe against OCI, scale linearly to the query window, apply a conservative filter-selectivity discount, convert to cost + ETA. Probe results are cached with TTL.
- **Response shape — flat, per the branch spec.** `run_query` responses carry `estimated_bytes`, `estimated_rows`, `estimated_cost_usd`, `estimated_eta_seconds`, `estimate_confidence`, and `estimate_rationale` as **top-level** fields on the response dict. This matches [../specs/agent-guardrails.md](../specs/agent-guardrails.md) line 19 exactly. The `explain_query` tool returns the full `QueryEstimate.to_dict()` as its standalone payload.
- **Cache-first ordering.** On cache hit, return immediately with the cached result **and** the cached estimate — no probe, no budget charge. On cache miss, compute the estimate, run the N5 pre-flight, execute the query, and persist `{result, estimate}` together in the cache so subsequent hits replay the estimate for free.
- **N5 — `BudgetTracker`** is an in-memory per-session counter store: queries, bytes, cost. Hooks into `QueryEngine.execute` at two points: (1) **pre-flight on cache miss only** — if `(usage + estimate)` would breach any of `max_queries_per_session` / `max_bytes_per_session` / `max_cost_usd_per_session`, raise `BudgetExceededError` **without** touching OCI; (2) **post-flight on successful live execution** — increment counters with the estimate (Logan does not currently return actual bytes-scanned in P0). A `get_session_budget` tool returns `{used, remaining, limits}`. **Cache hits do not consume budget** — re-running the same query within TTL is free.
- **Budget scope in P0: `run_query` only.** `run_batch_queries` uses `asyncio.gather(...)` inside `QueryEngine.execute_batch`, which makes per-item `check()`/`record()` race-prone — two siblings can both pass the same snapshot and then both execute, overshooting. Rather than serialize batches or add a reservation model, **P0 leaves `run_batch_queries` unbudgeted**. Called out explicitly in the README. P1 decides between serialization vs. atomic reservation.
- **Budget override** — `run_query(..., budget_override=True, confirmation_token=..., confirmation_secret=...)` is routed through a **per-arg** variant of the existing confirmation gate: `run_query` is not in `GUARDED_TOOLS`, but `ConfirmationManager.is_guarded_call(name, arguments)` returns `True` when `name == "run_query"` and `budget_override=True` is set. `handlers.handle_tool_call` computes this predicate once and reuses the same value for the gate entry check **and** for the post-execution audit branch — so override runs still produce the normal `executed` / `execution_failed` audit entries.
- **Session identity** — N5 key is a process-scoped session id generated in `MCPHandlers.__init__`. When N6 lands it will hoist session_id ownership up to `server.py` so both subsystems share the same id; until then the tracker has its own. Long-lived servers aggregate all logical investigations under one id — accepted P0 limitation, called out in the README.
- **Read-only posture** — N5 still enforces; cost protection matters even when writes are blocked. H1 runs unconditionally.

**Tech Stack:** Python 3, pytest, dataclasses, asyncio. No new runtime dependencies. Probes reuse the existing `OCILogAnalyticsClient.query` method.

**Spec:** [../specs/agent-guardrails.md](../specs/agent-guardrails.md) · features H1 and N5.

---

## File Structure

**Create:**
- `src/oci_logan_mcp/query_estimator.py` — `QueryEstimate` dataclass, `QueryEstimator` class with probe cache.
- `src/oci_logan_mcp/budget_tracker.py` — `BudgetLimits` dataclass, `BudgetExceededError`, `BudgetTracker` class.
- `tests/test_query_estimator.py` — estimator heuristics, probe caching, confidence levels.
- `tests/test_budget_tracker.py` — pre-flight check, post-flight increment, override flow.

**Modify:**
- `src/oci_logan_mcp/config.py` — add `CostConfig` dataclass (cost rates + thresholds); add `BudgetConfig` dataclass (per-session limits); add both to `Settings`.
- `src/oci_logan_mcp/query_engine.py` — inject estimator + tracker in `__init__`; rewrite `execute` to cache-first ordering; surface **flat** `estimated_*` / `estimate_confidence` / `estimate_rationale` fields at the top level of the response on both live and cache-hit paths; preserve N2's `next_steps` on both paths; pre-flight budget check on cache-miss only; post-flight `record()` on successful live execution; batch path is unbudgeted via a `skip_budget=True` flag on the internal `_execute_inner`.
- `src/oci_logan_mcp/handlers.py` — new `_explain_query` and `_get_session_budget` handlers; register in `handle_tool_call` dispatch; construct a single shared `BudgetTracker` on `MCPHandlers.__init__`.
- `src/oci_logan_mcp/tools.py` — register `explain_query` and `get_session_budget` tool schemas.
- `src/oci_logan_mcp/confirmation.py` — add a new `is_guarded_call(tool_name, arguments)` method that classifies `run_query` as guarded **only when** `budget_override=True` is in `arguments`; all other tools fall back to the existing `GUARDED_TOOLS` membership check. `run_query` is **not** added to `GUARDED_TOOLS` — per-arg gating is the pattern. `is_guarded(...)` stays for backward compatibility; internal handler callers switch to `is_guarded_call(...)`.
- `src/oci_logan_mcp/read_only_guard.py` — **no change.** `explain_query` and `get_session_budget` are reads. `run_query` stays non-mutating even with `budget_override=True` (the budget is local state, not a mutation of anything an auditor cares about).
- `tests/test_read_only_guard.py` — update `KNOWN_READERS` to include the two new tools.
- `tests/test_handlers.py` — integration tests for the two new tool handlers.
- `tests/test_config.py` — cover new `cost` and `budget` config sections.
- `README.md` — document `explain_query`, cost/ETA on every `run_query`, budget limits, override flow.

**Out of scope** (deferred):
- **J1-baseline upgrade path (P1).** When J1's ingestion-health baseline store exists, H1 will read from it instead of probing. Don't design for it yet — ship probe-only.
- **Cross-process budget sharing.** Each server process has its own tracker. Users running multiple clients against the same tenancy get independent budgets. Fine for P0.
- **Per-user budgets.** One tracker per session-id; multi-user servers aggregate. Acceptable given current single-user model.
- **Actual bytes from OCI response.** If Logan doesn't return bytes-scanned in P0, we increment with the estimate. Called out in the rationale.
- **Budgeting for `run_batch_queries` (P1).** P0 enforces budget on `run_query` only. Batch queries run concurrently via `asyncio.gather` and would race under a non-atomic `check`/`record`. Deferred — P1 picks between serializing batches under the tracker or an atomic reservation model.

---

## Config additions

New sections for `config.py`:

```python
@dataclass
class CostConfig:
    """Cost + ETA estimation tunables."""

    cost_per_gb_usd: float = 0.05          # Published OCI Log Analytics per-GB-scanned rate.
    eta_throughput_mbps: float = 50.0      # Conservative throughput constant for ETA.
    eta_high_threshold_seconds: float = 60.0  # Agents should prompt user before exceeding this.
    probe_ttl_seconds: int = 900           # 15 minutes.
    filter_selectivity_discount: float = 0.2  # When query has WHERE/field filters, multiply bytes by this.


@dataclass
class BudgetConfig:
    """Per-session query budget."""

    enabled: bool = True
    max_queries_per_session: int = 100
    max_bytes_per_session: int = 10 * 1024**3   # 10 GiB
    max_cost_usd_per_session: float = 5.00
```

Wired into `Settings`:

```python
cost: CostConfig = field(default_factory=CostConfig)
budget: BudgetConfig = field(default_factory=BudgetConfig)
```

---

## Task 1: `CostConfig` + `BudgetConfig` in `Settings`

**Files:**
- Modify: `src/oci_logan_mcp/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_settings_has_cost_config_defaults():
    from oci_logan_mcp.config import Settings
    s = Settings()
    assert s.cost.cost_per_gb_usd == 0.05
    assert s.cost.eta_throughput_mbps == 50.0
    assert s.cost.eta_high_threshold_seconds == 60.0
    assert s.cost.probe_ttl_seconds == 900
    assert 0 < s.cost.filter_selectivity_discount <= 1


def test_settings_has_budget_config_defaults():
    from oci_logan_mcp.config import Settings
    s = Settings()
    assert s.budget.enabled is True
    assert s.budget.max_queries_per_session == 100
    assert s.budget.max_bytes_per_session == 10 * 1024**3
    assert s.budget.max_cost_usd_per_session == 5.00


def test_cost_and_budget_loaded_from_yaml(tmp_path):
    import yaml
    from oci_logan_mcp.config import load_config
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "cost": {"cost_per_gb_usd": 0.10, "probe_ttl_seconds": 120},
        "budget": {"enabled": False, "max_queries_per_session": 5},
    }))
    s = load_config(config_path=cfg_path)
    assert s.cost.cost_per_gb_usd == 0.10
    assert s.cost.probe_ttl_seconds == 120
    assert s.budget.enabled is False
    assert s.budget.max_queries_per_session == 5
```

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_config.py -v -k "cost_config or budget_config or cost_and_budget"
```

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'cost'`.

- [ ] **Step 3: Implement config dataclasses**

In `src/oci_logan_mcp/config.py`, add before `class Settings`:

```python
@dataclass
class CostConfig:
    cost_per_gb_usd: float = 0.05
    eta_throughput_mbps: float = 50.0
    eta_high_threshold_seconds: float = 60.0
    probe_ttl_seconds: int = 900
    filter_selectivity_discount: float = 0.2


@dataclass
class BudgetConfig:
    enabled: bool = True
    max_queries_per_session: int = 100
    max_bytes_per_session: int = 10 * 1024**3
    max_cost_usd_per_session: float = 5.00
```

Inside `Settings`, add fields:

```python
    cost: CostConfig = field(default_factory=CostConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
```

In `_parse_config`, after the `notif_data` block:

```python
    if cost_data := data.get("cost"):
        settings.cost = CostConfig(
            cost_per_gb_usd=cost_data.get("cost_per_gb_usd", settings.cost.cost_per_gb_usd),
            eta_throughput_mbps=cost_data.get("eta_throughput_mbps", settings.cost.eta_throughput_mbps),
            eta_high_threshold_seconds=cost_data.get(
                "eta_high_threshold_seconds", settings.cost.eta_high_threshold_seconds
            ),
            probe_ttl_seconds=cost_data.get("probe_ttl_seconds", settings.cost.probe_ttl_seconds),
            filter_selectivity_discount=cost_data.get(
                "filter_selectivity_discount", settings.cost.filter_selectivity_discount
            ),
        )

    if budget_data := data.get("budget"):
        settings.budget = BudgetConfig(
            enabled=budget_data.get("enabled", settings.budget.enabled),
            max_queries_per_session=budget_data.get(
                "max_queries_per_session", settings.budget.max_queries_per_session
            ),
            max_bytes_per_session=budget_data.get(
                "max_bytes_per_session", settings.budget.max_bytes_per_session
            ),
            max_cost_usd_per_session=budget_data.get(
                "max_cost_usd_per_session", settings.budget.max_cost_usd_per_session
            ),
        )
```

Also extend `to_dict()` with new sections so save_config round-trips correctly:

```python
            "cost": {
                "cost_per_gb_usd": self.cost.cost_per_gb_usd,
                "eta_throughput_mbps": self.cost.eta_throughput_mbps,
                "eta_high_threshold_seconds": self.cost.eta_high_threshold_seconds,
                "probe_ttl_seconds": self.cost.probe_ttl_seconds,
                "filter_selectivity_discount": self.cost.filter_selectivity_discount,
            },
            "budget": {
                "enabled": self.budget.enabled,
                "max_queries_per_session": self.budget.max_queries_per_session,
                "max_bytes_per_session": self.budget.max_bytes_per_session,
                "max_cost_usd_per_session": self.budget.max_cost_usd_per_session,
            },
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_config.py -v
```

Expected: PASS — new tests + all existing tests.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(h1,n5): add CostConfig + BudgetConfig sections"
```

---

## Task 2: `QueryEstimator` — source extraction and heuristic fallback

Build the estimator in two layers: first a **no-probe fallback** so estimates are always safe, then probe-based augmentation.

**Files:**
- Create: `src/oci_logan_mcp/query_estimator.py`
- Create: `tests/test_query_estimator.py`

- [ ] **Step 1: Write failing tests (no-probe fallback path)**

Create `tests/test_query_estimator.py`:

```python
"""Tests for QueryEstimator."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.config import Settings
from oci_logan_mcp.query_estimator import QueryEstimator, QueryEstimate


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def oci_client():
    client = MagicMock()
    client.query = AsyncMock()
    return client


@pytest.fixture
def estimator(oci_client, settings):
    return QueryEstimator(oci_client=oci_client, settings=settings)


def test_extract_log_sources_single():
    sources = QueryEstimator._extract_sources("'Log Source' = 'Linux Syslog'")
    assert sources == ["Linux Syslog"]


def test_extract_log_sources_multiple_or_clause():
    q = "'Log Source' in ('Linux Syslog', 'Apache HTTP Server') | head 10"
    sources = QueryEstimator._extract_sources(q)
    assert sorted(sources) == ["Apache HTTP Server", "Linux Syslog"]


def test_extract_log_sources_none_when_query_wildcards():
    assert QueryEstimator._extract_sources("* | head 10") == []


def test_has_filters_detected():
    assert QueryEstimator._has_filters("'Log Source' = 'x' and severity = 'ERROR'")
    assert QueryEstimator._has_filters("where user = 'bob'")
    assert not QueryEstimator._has_filters("* | head 1")


@pytest.mark.asyncio
async def test_estimate_unknown_source_returns_low_confidence(estimator, oci_client):
    oci_client.query.side_effect = RuntimeError("probe failed")
    est = await estimator.estimate("'Log Source' = 'Unknown'", "last_1_hour")
    assert isinstance(est, QueryEstimate)
    assert est.confidence == "low"
    assert est.estimated_bytes >= 0
    assert est.estimated_cost_usd is None or est.estimated_cost_usd >= 0


@pytest.mark.asyncio
async def test_estimate_never_raises_on_garbage_query(estimator):
    # Should not raise; should return low-confidence safe default.
    est = await estimator.estimate("", "last_1_hour")
    assert est.confidence == "low"
```

- [ ] **Step 2: Run tests, verify fail (module missing)**

```
pytest tests/test_query_estimator.py -v
```

Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Create `query_estimator.py` skeleton**

Create `src/oci_logan_mcp/query_estimator.py`:

```python
"""Query cost + ETA estimation service (H1).

P0 strategy: probe-based volume estimate. For each Log Source referenced in
the query, issue a cheap count-over-last-hour probe against OCI, scale to
the query's window, apply a conservative filter-selectivity discount, and
convert bytes → cost and bytes → ETA via config constants. Probes are cached
per-source with a configurable TTL.

Estimator NEVER raises. On any failure it returns a safe `confidence="low"`
estimate so callers can decide without exception-handling boilerplate.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .client import OCILogAnalyticsClient
from .config import Settings
from .time_parser import parse_time_range

logger = logging.getLogger(__name__)


@dataclass
class QueryEstimate:
    estimated_bytes: int
    estimated_rows: Optional[int]
    estimated_cost_usd: Optional[float]
    estimated_eta_seconds: float
    confidence: str                  # "high" | "medium" | "low"
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "estimated_bytes": self.estimated_bytes,
            "estimated_rows": self.estimated_rows,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_eta_seconds": self.estimated_eta_seconds,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


# Regex for "'Log Source' = 'name'" and "'Log Source' in ('a', 'b')".
_SOURCE_EQ_RE = re.compile(r"'Log Source'\s*=\s*'([^']+)'", re.IGNORECASE)
_SOURCE_IN_RE = re.compile(r"'Log Source'\s+in\s*\(([^)]+)\)", re.IGNORECASE)
_FILTER_SIGNALS = re.compile(r"\b(where|and|or)\b|=|!=|>|<|\bin\b", re.IGNORECASE)


class QueryEstimator:
    def __init__(self, oci_client: OCILogAnalyticsClient, settings: Settings) -> None:
        self.oci_client = oci_client
        self.settings = settings
        # {source_name: (bytes_per_hour, cached_at_epoch)}
        self._probe_cache: Dict[str, tuple[float, float]] = {}

    @staticmethod
    def _extract_sources(query: str) -> List[str]:
        sources: List[str] = []
        for m in _SOURCE_EQ_RE.finditer(query or ""):
            sources.append(m.group(1))
        for m in _SOURCE_IN_RE.finditer(query or ""):
            for raw in m.group(1).split(","):
                name = raw.strip().strip("'").strip('"')
                if name:
                    sources.append(name)
        # Deduplicate preserving order.
        seen = set()
        out: List[str] = []
        for s in sources:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    @staticmethod
    def _has_filters(query: str) -> bool:
        if not query:
            return False
        # Query has filters if it references 'Log Source' with = / in, or has a where clause,
        # or uses comparators beyond a trivial wildcard.
        if "'Log Source'" in query:
            return True
        if re.search(r"\bwhere\b", query, re.IGNORECASE):
            return True
        if re.search(r"\s[=<>!]=?\s|\bin\s*\(", query, re.IGNORECASE):
            return True
        return False

    def _window_hours(self, time_range: Optional[str]) -> float:
        try:
            start, end = parse_time_range(None, None, time_range or "last_1_hour")
            return max(0.1, (end - start).total_seconds() / 3600.0)
        except Exception:
            return 1.0

    async def estimate(
        self,
        query: str,
        time_range: Optional[str] = None,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
    ) -> QueryEstimate:
        """Return an estimate. Never raises."""
        try:
            return await self._estimate_inner(query, time_range, time_start, time_end)
        except Exception as e:
            logger.warning("QueryEstimator failed unexpectedly: %s", e)
            return self._safe_default("internal_error")

    def _safe_default(self, reason: str) -> QueryEstimate:
        return QueryEstimate(
            estimated_bytes=0,
            estimated_rows=None,
            estimated_cost_usd=None,
            estimated_eta_seconds=0.0,
            confidence="low",
            rationale=f"No estimate available ({reason}).",
        )

    async def _estimate_inner(
        self,
        query: str,
        time_range: Optional[str],
        time_start: Optional[str],
        time_end: Optional[str],
    ) -> QueryEstimate:
        sources = self._extract_sources(query)
        if not sources:
            return self._safe_default("no source filter")

        hours = self._window_hours(time_range) if not (time_start and time_end) else \
                max(0.1, (parse_time_range(time_start, time_end, None)[1]
                          - parse_time_range(time_start, time_end, None)[0]).total_seconds() / 3600.0)

        total_bytes = 0.0
        confidences: List[str] = []
        rationales: List[str] = []
        for source in sources:
            bph = await self._probe_bytes_per_hour(source)
            if bph is None:
                rationales.append(f"{source}: probe failed")
                confidences.append("low")
                continue
            total_bytes += bph * hours
            confidences.append("medium")
            rationales.append(f"{source}: ~{int(bph)} bytes/hr × {hours:.1f}h")

        if self._has_filters(query):
            total_bytes *= self.settings.cost.filter_selectivity_discount
            rationales.append(f"× {self.settings.cost.filter_selectivity_discount} filter discount")

        cost = (total_bytes / (1024**3)) * self.settings.cost.cost_per_gb_usd
        throughput_bps = self.settings.cost.eta_throughput_mbps * 1024 * 1024
        eta = total_bytes / throughput_bps if throughput_bps > 0 else 0.0

        confidence = "medium" if confidences and all(c == "medium" for c in confidences) else "low"
        if total_bytes == 0:
            confidence = "low"

        return QueryEstimate(
            estimated_bytes=int(total_bytes),
            estimated_rows=None,
            estimated_cost_usd=round(cost, 4),
            estimated_eta_seconds=round(eta, 2),
            confidence=confidence,
            rationale="; ".join(rationales),
        )

    async def _probe_bytes_per_hour(self, source: str) -> Optional[float]:
        ttl = self.settings.cost.probe_ttl_seconds
        now = time.time()
        cached = self._probe_cache.get(source)
        if cached and (now - cached[1]) < ttl:
            return cached[0]
        try:
            probe_result = await self.oci_client.query(
                query_string=f"'Log Source' = '{source}' | stats count",
                time_start=None, time_end=None,
                max_results=1,
                include_subcompartments=True,
            )
            rows = (probe_result or {}).get("rows", [])
            count = 0
            if rows and isinstance(rows[0], list) and rows[0]:
                try:
                    count = int(rows[0][0] or 0)
                except (TypeError, ValueError):
                    count = 0
            # Assume 500 bytes/row average. Conservative — tune later.
            bytes_per_hour = float(count) * 500.0
            self._probe_cache[source] = (bytes_per_hour, now)
            return bytes_per_hour
        except Exception as e:
            logger.info("probe failed for source=%s: %s", source, e)
            return None
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_query_estimator.py -v
```

Expected: PASS — 6/6.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(h1): QueryEstimator with probe-based bytes estimation"
```

---

## Task 3: Probe caching + medium-confidence integration tests

**Files:**
- Modify: `tests/test_query_estimator.py`

- [ ] **Step 1: Write tests for probe caching + medium confidence**

Append to `tests/test_query_estimator.py`:

```python
@pytest.mark.asyncio
async def test_estimate_with_working_probe_returns_medium_confidence(estimator, oci_client):
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est = await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    assert est.confidence == "medium"
    assert est.estimated_bytes > 0
    assert est.estimated_cost_usd is not None and est.estimated_cost_usd >= 0


@pytest.mark.asyncio
async def test_estimate_scales_linearly_with_time_range(estimator, oci_client):
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est_1h = await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    # Reset cache to force re-probe (we want to inspect scaling, not caching)
    estimator._probe_cache.clear()
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est_24h = await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_24_hours")
    # 24h window should be ~24× bytes of 1h window (allow ±10% for parser rounding).
    assert 20 * est_1h.estimated_bytes <= est_24h.estimated_bytes <= 28 * est_1h.estimated_bytes


@pytest.mark.asyncio
async def test_probe_cache_reused_within_ttl(estimator, oci_client):
    oci_client.query.return_value = {"rows": [[100]], "columns": []}
    await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    # Probe called exactly once even across two estimate() calls.
    assert oci_client.query.await_count == 1


@pytest.mark.asyncio
async def test_probe_cache_expires(estimator, oci_client, monkeypatch):
    import oci_logan_mcp.query_estimator as qe_mod
    oci_client.query.return_value = {"rows": [[100]], "columns": []}
    await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    # Advance time past TTL.
    fake_now = [time_module_now(qe_mod) + estimator.settings.cost.probe_ttl_seconds + 1]
    monkeypatch.setattr(qe_mod.time, "time", lambda: fake_now[0])
    await estimator.estimate("'Log Source' = 'Linux Syslog'", "last_1_hour")
    assert oci_client.query.await_count == 2


def time_module_now(qe_mod):
    return qe_mod.time.time()


@pytest.mark.asyncio
async def test_filter_discount_reduces_bytes(estimator, oci_client):
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est_no_filter = await estimator.estimate("'Log Source' = 'x'", "last_1_hour")
    estimator._probe_cache.clear()
    oci_client.query.return_value = {"rows": [[1000]], "columns": []}
    est_filter = await estimator.estimate("'Log Source' = 'x' and severity = 'ERROR'", "last_1_hour")
    # Filter discount should shrink bytes.
    assert est_filter.estimated_bytes < est_no_filter.estimated_bytes
```

- [ ] **Step 2: Run tests, verify pass**

```
pytest tests/test_query_estimator.py -v
```

Expected: PASS — all tests (the estimator already implements the needed logic; these lock behavior).

- [ ] **Step 3: Commit**

```bash
git commit -am "test(h1): probe caching, scaling, filter discount"
```

---

## Task 4: `BudgetTracker` — data model + `check` + `record`

**Files:**
- Create: `src/oci_logan_mcp/budget_tracker.py`
- Create: `tests/test_budget_tracker.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_budget_tracker.py`:

```python
"""Tests for BudgetTracker."""

import pytest
from oci_logan_mcp.budget_tracker import (
    BudgetTracker, BudgetLimits, BudgetExceededError, BudgetUsage,
)


@pytest.fixture
def limits():
    return BudgetLimits(
        enabled=True,
        max_queries_per_session=3,
        max_bytes_per_session=10_000,
        max_cost_usd_per_session=1.00,
    )


@pytest.fixture
def tracker(limits):
    return BudgetTracker(session_id="s1", limits=limits)


def test_tracker_starts_at_zero(tracker):
    usage = tracker.snapshot()
    assert usage.queries == 0
    assert usage.bytes == 0
    assert usage.cost_usd == 0.0


def test_check_under_budget_passes(tracker):
    tracker.check(estimated_bytes=1000, estimated_cost_usd=0.10)  # no raise


def test_check_over_query_count_blocks(tracker):
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    # Next one should fail pre-flight.
    with pytest.raises(BudgetExceededError) as exc:
        tracker.check(estimated_bytes=100, estimated_cost_usd=0.01)
    assert "query count" in str(exc.value).lower() or "queries" in str(exc.value).lower()


def test_check_over_bytes_blocks(tracker):
    tracker.record(actual_bytes=9_000, actual_cost_usd=0.01)
    with pytest.raises(BudgetExceededError) as exc:
        tracker.check(estimated_bytes=5_000, estimated_cost_usd=0.01)
    assert "bytes" in str(exc.value).lower()


def test_check_over_cost_blocks(tracker):
    tracker.record(actual_bytes=100, actual_cost_usd=0.90)
    with pytest.raises(BudgetExceededError) as exc:
        tracker.check(estimated_bytes=100, estimated_cost_usd=0.20)
    assert "cost" in str(exc.value).lower()


def test_record_accumulates(tracker):
    tracker.record(actual_bytes=1000, actual_cost_usd=0.10)
    tracker.record(actual_bytes=2000, actual_cost_usd=0.20)
    u = tracker.snapshot()
    assert u.queries == 2
    assert u.bytes == 3000
    assert abs(u.cost_usd - 0.30) < 1e-6


def test_disabled_tracker_never_raises(limits):
    limits.enabled = False
    t = BudgetTracker("s", limits)
    # Record past the limit, check should still pass.
    for _ in range(10):
        t.record(actual_bytes=100_000, actual_cost_usd=1.00)
    t.check(estimated_bytes=100_000, estimated_cost_usd=10.00)


def test_remaining_reports_correctly(tracker):
    tracker.record(actual_bytes=2_500, actual_cost_usd=0.25)
    remaining = tracker.remaining()
    assert remaining["queries"] == 2
    assert remaining["bytes"] == 7_500
    assert abs(remaining["cost_usd"] - 0.75) < 1e-6


def test_override_skips_check(tracker):
    # Burn budget.
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    tracker.record(actual_bytes=100, actual_cost_usd=0.01)
    # Normally would raise; override must skip.
    tracker.check(estimated_bytes=100, estimated_cost_usd=0.01, override=True)
```

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_budget_tracker.py -v
```

Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `budget_tracker.py`**

Create `src/oci_logan_mcp/budget_tracker.py`:

```python
"""Per-session query budget enforcement (N5).

In-memory only. One tracker per server process; session_id keys come from
the audit-logger session_id (N6) so budget events and audit events line up.

`check()` does a pre-flight estimate-vs-limits comparison. `record()` is
the post-flight increment. `remaining()` is a read-only snapshot.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict


class BudgetExceededError(Exception):
    """Raised when a query would push per-session usage over a limit."""


@dataclass
class BudgetLimits:
    enabled: bool = True
    max_queries_per_session: int = 100
    max_bytes_per_session: int = 10 * 1024**3
    max_cost_usd_per_session: float = 5.00


@dataclass
class BudgetUsage:
    queries: int = 0
    bytes: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"queries": self.queries, "bytes": self.bytes, "cost_usd": round(self.cost_usd, 4)}


class BudgetTracker:
    def __init__(self, session_id: str, limits: BudgetLimits) -> None:
        self.session_id = session_id
        self.limits = limits
        self._usage = BudgetUsage()
        self._lock = threading.Lock()

    def snapshot(self) -> BudgetUsage:
        with self._lock:
            return BudgetUsage(
                queries=self._usage.queries,
                bytes=self._usage.bytes,
                cost_usd=self._usage.cost_usd,
            )

    def remaining(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "queries": max(0, self.limits.max_queries_per_session - self._usage.queries),
                "bytes": max(0, self.limits.max_bytes_per_session - self._usage.bytes),
                "cost_usd": round(max(0.0, self.limits.max_cost_usd_per_session - self._usage.cost_usd), 4),
            }

    def check(
        self,
        *,
        estimated_bytes: int = 0,
        estimated_cost_usd: float = 0.0,
        override: bool = False,
    ) -> None:
        if not self.limits.enabled or override:
            return
        with self._lock:
            if self._usage.queries + 1 > self.limits.max_queries_per_session:
                raise BudgetExceededError(
                    f"Session query count limit reached: "
                    f"{self._usage.queries}/{self.limits.max_queries_per_session}. "
                    f"Use budget_override=True with confirmation, or start a new session."
                )
            if self._usage.bytes + estimated_bytes > self.limits.max_bytes_per_session:
                raise BudgetExceededError(
                    f"Session bytes budget would be exceeded: "
                    f"{self._usage.bytes + estimated_bytes} > {self.limits.max_bytes_per_session}."
                )
            if self._usage.cost_usd + estimated_cost_usd > self.limits.max_cost_usd_per_session:
                raise BudgetExceededError(
                    f"Session cost budget would be exceeded: "
                    f"${self._usage.cost_usd + estimated_cost_usd:.2f} > "
                    f"${self.limits.max_cost_usd_per_session:.2f}."
                )

    def record(self, *, actual_bytes: int, actual_cost_usd: float) -> None:
        with self._lock:
            self._usage.queries += 1
            self._usage.bytes += max(0, int(actual_bytes))
            self._usage.cost_usd += max(0.0, float(actual_cost_usd))
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_budget_tracker.py -v
```

Expected: PASS — all 9 tests.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(n5): BudgetTracker with check/record/remaining"
```

---

## Task 5: Wire estimator into `QueryEngine` — flat fields + cache-first

The branch spec requires estimate fields **at top level** on the response. Cache hits must replay the cached estimate without probing. This task wires both.

**Files:**
- Modify: `src/oci_logan_mcp/query_engine.py`
- Test: new integration tests

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_query_estimator.py`:

```python
@pytest.mark.asyncio
async def test_run_query_carries_flat_estimate_fields_on_live():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock()
    oci_client.query.side_effect = [
        {"rows": [[500]], "columns": []},           # probe
        {"rows": [], "columns": [{"name": "Time"}]},  # actual
    ]
    estimator = QueryEstimator(oci_client, settings)
    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(), estimator=estimator)
    resp = await engine.execute(
        query="'Log Source' = 'Linux Syslog'",
        time_range="last_1_hour",
    )
    # Flat, per spec line 19.
    for key in ("estimated_bytes", "estimated_rows", "estimated_cost_usd",
                "estimated_eta_seconds", "estimate_confidence", "estimate_rationale"):
        assert key in resp, f"missing flat field: {key}"
    assert resp["estimate_confidence"] in {"low", "medium", "high"}


@pytest.mark.asyncio
async def test_cache_hit_replays_estimate_without_probing():
    """On cache hit: no probe call, no new query call, flat estimate still present."""
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock()  # should NEVER be called

    estimator = QueryEstimator(oci_client, settings)
    # Cache returns a CachedBundle dict with result + saved estimate.
    cached_payload = {
        "result": {"rows": [["x"]], "columns": [{"name": "Time"}]},
        "estimate": {
            "estimated_bytes": 123, "estimated_rows": None,
            "estimated_cost_usd": 0.01, "estimated_eta_seconds": 0.5,
            "confidence": "medium", "rationale": "replayed from cache",
        },
    }
    cache = MagicMock(get=MagicMock(return_value=cached_payload), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(), estimator=estimator)
    resp = await engine.execute(
        query="'Log Source' = 'Linux Syslog'",
        time_range="last_1_hour",
    )
    assert resp["source"] == "cache"
    assert resp["estimated_bytes"] == 123
    assert resp["estimate_confidence"] == "medium"
    # No OCI calls — neither probe nor query.
    assert oci_client.query.await_count == 0


@pytest.mark.asyncio
async def test_next_steps_preserved_on_live_and_cache_paths():
    """H1 rewrite must not drop N2's next_steps field — check both paths carry it."""
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(side_effect=[
        {"rows": [[500]], "columns": []},                                # probe
        {"rows": [], "columns": [{"name": "Time"}]},                     # live (zero rows → validate hint)
    ])
    estimator = QueryEstimator(oci_client, settings)
    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(), estimator=estimator)
    live = await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")
    assert "next_steps" in live
    assert isinstance(live["next_steps"], list)
    # Zero-row result should trigger N2's validate_query hint.
    assert any(s["tool_name"] == "validate_query" for s in live["next_steps"])

    # Now simulate a cache hit with the bundle that would've been stored.
    cached_bundle = {
        "result": {"rows": [], "columns": [{"name": "Time"}]},
        "estimate": {"estimated_bytes": 1, "estimated_rows": None,
                     "estimated_cost_usd": 0.0, "estimated_eta_seconds": 0.0,
                     "confidence": "medium", "rationale": ""},
    }
    cache.get = MagicMock(return_value=cached_bundle)
    cached = await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")
    assert cached["source"] == "cache"
    assert "next_steps" in cached
    assert any(s["tool_name"] == "validate_query" for s in cached["next_steps"])


@pytest.mark.asyncio
async def test_live_path_caches_result_with_estimate_bundle():
    """After a live execution, cache.set is called with a {result, estimate} bundle."""
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(side_effect=[
        {"rows": [[500]], "columns": []},  # probe
        {"rows": [["x"]], "columns": [{"name": "Time"}]},  # actual
    ])
    estimator = QueryEstimator(oci_client, settings)
    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(), estimator=estimator)
    await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")

    # cache.set called exactly once with a bundle dict.
    assert cache.set.call_count == 1
    _key, payload = cache.set.call_args.args
    assert isinstance(payload, dict)
    assert "result" in payload and "estimate" in payload
```

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_query_estimator.py -v -k "flat or cache_hit or caches_result"
```

Expected: FAIL — `QueryEngine.__init__() got an unexpected keyword argument 'estimator'` and/or missing keys.

- [ ] **Step 3: Modify `QueryEngine`**

In `src/oci_logan_mcp/query_engine.py`:

**Imports:** (N2 has already added `from .next_steps import suggest as _suggest_next_steps` to this file in its Task 7 — reuse it rather than re-importing.)

```python
from typing import Optional
from .query_estimator import QueryEstimator, QueryEstimate
from .budget_tracker import BudgetTracker, BudgetExceededError  # Used in Task 6.
# (already present from N2):
# from .next_steps import suggest as _suggest_next_steps
```

**Constructor:**

```python
    def __init__(
        self,
        oci_client: OCILogAnalyticsClient,
        cache: CacheManager,
        logger: QueryLogger,
        estimator: Optional[QueryEstimator] = None,
        budget_tracker: Optional[BudgetTracker] = None,
    ):
        self.oci_client = oci_client
        self.cache = cache
        self.logger = logger
        self.estimator = estimator
        self.budget_tracker = budget_tracker
```

**Estimate-flattening helper** (private method):

```python
    @staticmethod
    def _flatten_estimate(response: Dict[str, Any], estimate_dict: Optional[Dict[str, Any]]) -> None:
        """Attach flat estimate fields to `response`, per spec line 19.

        Mutates response in place. Safe when estimate_dict is None — does nothing.
        """
        if not estimate_dict:
            return
        response["estimated_bytes"] = estimate_dict.get("estimated_bytes")
        response["estimated_rows"] = estimate_dict.get("estimated_rows")
        response["estimated_cost_usd"] = estimate_dict.get("estimated_cost_usd")
        response["estimated_eta_seconds"] = estimate_dict.get("estimated_eta_seconds")
        response["estimate_confidence"] = estimate_dict.get("confidence")
        response["estimate_rationale"] = estimate_dict.get("rationale")
```

**Rewrite `execute` with cache-first ordering. `next_steps` (N2) must be preserved on both paths.**

Replace the existing body with the sequence below. Preserve existing parameter names and `parse_time_range` usage — only the orchestration changes.

```python
    async def execute(
        self,
        query: str,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
        time_range: Optional[str] = None,
        max_results: Optional[int] = None,
        include_subcompartments: bool = True,
        use_cache: bool = True,
        compartment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        start, end = parse_time_range(time_start, time_end, time_range)
        effective_compartment = compartment_id or self.oci_client.compartment_id
        cache_key = self._make_cache_key(query, start, end, include_subcompartments, effective_compartment)

        # --- 1) Cache-first: a hit replays result + estimate with zero side effects. ---
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached:
                # New bundle shape: {"result": ..., "estimate": {...}}. Be tolerant of
                # legacy entries that stored only the raw result.
                if isinstance(cached, dict) and "result" in cached and "estimate" in cached:
                    cached_result = cached["result"]
                    cached_estimate = cached["estimate"]
                else:
                    cached_result = cached
                    cached_estimate = None

                response: Dict[str, Any] = {
                    "source": "cache",
                    "data": cached_result,
                    "metadata": {
                        "query": query,
                        "compartment_id": effective_compartment,
                        "time_start": start.isoformat(),
                        "time_end": end.isoformat(),
                        "include_subcompartments": include_subcompartments,
                    },
                }
                self._flatten_estimate(response, cached_estimate)
                # N2: next_steps is a pure, shape-based function of (query, response).
                # Recompute on cache hit — cheaper than caching and always in sync with
                # whatever heuristics the current code defines.
                response["next_steps"] = [s.to_dict() for s in _suggest_next_steps(query, response)]
                return response

        # --- 2) Cache miss: compute estimate once, use it for budget + response. ---
        estimate: Optional[QueryEstimate] = None
        if self.estimator is not None:
            estimate = await self.estimator.estimate(query, time_range, time_start, time_end)

        # Task 6 will insert the budget pre-flight check here.

        # --- 3) Live execution. ---
        start_time = datetime.now()
        try:
            result = await self.oci_client.query(
                query_string=query,
                time_start=start.isoformat(),
                time_end=end.isoformat(),
                max_results=max_results,
                include_subcompartments=include_subcompartments,
                compartment_id=compartment_id,
            )
            execution_time = (datetime.now() - start_time).total_seconds()

            if use_cache:
                bundle = {
                    "result": result,
                    "estimate": estimate.to_dict() if estimate is not None else None,
                }
                self.cache.set(cache_key, bundle)

            self.logger.log_query(
                query=query, time_start=start, time_end=end,
                execution_time=execution_time,
                result_count=len(result.get("rows", [])), success=True,
            )

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
            self._flatten_estimate(response, estimate.to_dict() if estimate is not None else None)
            # N2: attach next_steps on live path (N2's own wiring did this; preserved here
            # explicitly so the H1 rewrite doesn't silently drop it).
            response["next_steps"] = [s.to_dict() for s in _suggest_next_steps(query, response)]

            # Task 6 will insert budget_tracker.record() here on success.

            return response

        except Exception as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            self.logger.log_query(
                query=query, time_start=start, time_end=end,
                execution_time=execution_time,
                result_count=0, success=False, error=str(e),
            )
            raise
```

> **Why this ordering matters:**
> - Cache hits short-circuit *before* any probe call → no wasted OCI calls for repeated queries.
> - Cache hits also bypass the budget (no `record()` on cache path) → re-running the same query is free, which matches what users expect.
> - Live cache writes store a `{result, estimate}` bundle so future hits can replay the estimate fields without recomputing.
> - Legacy cache entries (bare result dicts) are handled tolerantly — they just miss the flat estimate fields until re-cached.
> - `next_steps` (N2) is **recomputed** on both paths rather than cached: it is pure, shape-based, and cheap, so storing it would add a cache-invalidation concern (heuristics evolve) without saving real work.

**Decision vs. N2 Task 7:** N2 already attaches `next_steps` to responses from its own wiring. This H1 rewrite replaces that wiring; the `response["next_steps"] = ...` lines above are the functional equivalent and supersede N2's Task 7 patches. When executing this plan, drop N2's live/cache-hit attachment blocks (the `response["next_steps"] = [s.to_dict() for s in _suggest_next_steps(query, response)]` lines that N2 added) before applying this rewrite, so the final code has exactly one pair of attachment points.

- [ ] **Step 4: Run tests**

```
pytest tests/test_query_estimator.py -v
```

Expected: PASS — including the three new tests.

- [ ] **Step 5: Run the full suite**

```
pytest tests/ -q
```

Expected: All existing tests pass. The cache shape changed, but the new code tolerates legacy entries; if a test pre-seeded a bare-result cache, it still works.

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(h1): surface flat estimate fields, cache-first ordering"
```

---

## Task 6: Wire `BudgetTracker` into `QueryEngine.execute` (cache-miss path only)

Budget pre-flight runs **after** the cache check — so cache hits never consume budget — and **only** for `run_query`. `run_batch_queries` stays unbudgeted in P0 per the architecture section.

**Files:**
- Modify: `src/oci_logan_mcp/query_engine.py`
- Test: new integration tests in `tests/test_budget_tracker.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_budget_tracker.py`:

```python
@pytest.mark.asyncio
async def test_budget_preflight_blocks_on_cache_miss():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits, BudgetExceededError
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(return_value={"rows": [[1_000_000]], "columns": []})  # probe only
    estimator = QueryEstimator(oci_client, settings)

    limits = BudgetLimits(
        enabled=True,
        max_queries_per_session=5,
        max_bytes_per_session=100,  # tiny → any real estimate blows it
        max_cost_usd_per_session=100.0,
    )
    tracker = BudgetTracker("s", limits)

    engine = QueryEngine(
        oci_client,
        MagicMock(get=MagicMock(return_value=None), set=MagicMock()),
        MagicMock(),
        estimator=estimator, budget_tracker=tracker,
    )

    with pytest.raises(BudgetExceededError):
        await engine.execute(query="'Log Source' = 'Linux Syslog'", time_range="last_1_hour")
    # Probe ran (for the estimate) but the actual live query did NOT.
    assert oci_client.query.await_count == 1


@pytest.mark.asyncio
async def test_budget_does_not_charge_on_cache_hit():
    """Cache hits bypass budget entirely — no probe, no check, no record."""
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock()  # must not be called
    estimator = QueryEstimator(oci_client, settings)

    limits = BudgetLimits(enabled=True, max_queries_per_session=1,
                          max_bytes_per_session=1, max_cost_usd_per_session=0.01)
    tracker = BudgetTracker("s", limits)
    # Burn the query count so any check() would raise.
    tracker.record(actual_bytes=0, actual_cost_usd=0.0)

    cached = {"result": {"rows": [], "columns": []},
              "estimate": {"estimated_bytes": 999, "estimated_rows": None,
                           "estimated_cost_usd": 1.0, "estimated_eta_seconds": 0.0,
                           "confidence": "medium", "rationale": ""}}
    cache = MagicMock(get=MagicMock(return_value=cached), set=MagicMock())

    engine = QueryEngine(oci_client, cache, MagicMock(),
                         estimator=estimator, budget_tracker=tracker)
    resp = await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")
    assert resp["source"] == "cache"
    assert oci_client.query.await_count == 0
    # Tracker state unchanged: still just the single pre-seeded record.
    assert tracker.snapshot().queries == 1


@pytest.mark.asyncio
async def test_budget_records_on_successful_live():
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(side_effect=[
        {"rows": [[1000]], "columns": []},  # probe
        {"rows": [["a"]], "columns": [{"name": "X"}]},  # real query
    ])
    estimator = QueryEstimator(oci_client, settings)
    tracker = BudgetTracker("s", BudgetLimits())

    engine = QueryEngine(
        oci_client,
        MagicMock(get=MagicMock(return_value=None), set=MagicMock()),
        MagicMock(),
        estimator=estimator, budget_tracker=tracker,
    )
    await engine.execute(query="'Log Source' = 'x'", time_range="last_1_hour")

    snap = tracker.snapshot()
    assert snap.queries == 1
    assert snap.bytes >= 0


@pytest.mark.asyncio
async def test_batch_queries_do_not_consume_budget_in_p0():
    """P0 contract: run_batch_queries is unbudgeted. Tracker stays at zero."""
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(return_value={"rows": [], "columns": []})
    estimator = QueryEstimator(oci_client, settings)
    tracker = BudgetTracker("s", BudgetLimits())

    cache = MagicMock(get=MagicMock(return_value=None), set=MagicMock())
    engine = QueryEngine(oci_client, cache, MagicMock(),
                         estimator=estimator, budget_tracker=tracker)

    await engine.execute_batch([
        {"query": "a", "time_range": "last_1_hour"},
        {"query": "b", "time_range": "last_1_hour"},
    ])
    # Batch doesn't go through per-query budget recording in P0.
    # NOTE: execute_batch calls execute() internally, which DOES record. If this
    # test fails, we need to make execute_batch bypass budget OR update the spec.
    # Codex flagged this — the "correct" P0 answer is to bypass.
    snap = tracker.snapshot()
    assert snap.queries == 0, (
        "P0 spec: run_batch_queries is unbudgeted. Test must be updated with "
        "the branch spec if the decision changes."
    )
```

> **About the last test:** it codifies the P0 decision that batch runs are budget-exempt. The implementation path is: `execute_batch` calls a new private `_execute_unbudgeted(...)` that skips the pre-flight and the post-flight `record()`. See Step 3 below.

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_budget_tracker.py -v
```

Expected: FAIL on all four new tests.

- [ ] **Step 3: Hook tracker into `execute` — cache-miss path only**

In `src/oci_logan_mcp/query_engine.py`, inside `execute`, at the "Task 6 will insert..." placeholder from Task 5:

```python
        # --- Budget pre-flight (cache miss, run_query scope). ---
        if self.budget_tracker is not None and estimate is not None:
            self.budget_tracker.check(
                estimated_bytes=estimate.estimated_bytes,
                estimated_cost_usd=float(estimate.estimated_cost_usd or 0.0),
                override=False,  # Task 7 plumbs override through.
            )
```

Before the `return response` on the live path:

```python
            if self.budget_tracker is not None and estimate is not None:
                self.budget_tracker.record(
                    actual_bytes=int(estimate.estimated_bytes),
                    actual_cost_usd=float(estimate.estimated_cost_usd or 0.0),
                )
```

> **Why record with the estimate, not actual bytes:** Logan's `query` response doesn't expose bytes-scanned in P0. Using the estimate is conservative — an overshoot makes the budget stricter than reality (acceptable); an undershoot shows up in prod and we tighten probe math.

**Split `execute` into a budgeted path and an internal unbudgeted path for batch use:**

Refactor so `execute_batch` does not pay the budget cost. The cleanest split:

```python
    async def execute_batch(
        self,
        queries: List[Dict[str, Any]],
        include_subcompartments: bool = True,
        compartment_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Execute multiple queries concurrently.

        P0: batch queries are UNBUDGETED. Each sibling still gets its estimate
        attached to the response, but no budget check or record runs.
        """
        tasks = [
            self._execute_inner(
                query=q["query"],
                time_start=q.get("time_start"),
                time_end=q.get("time_end"),
                time_range=q.get("time_range"),
                max_results=q.get("max_results"),
                include_subcompartments=q.get("include_subcompartments", include_subcompartments),
                compartment_id=q.get("compartment_id", compartment_id),
                use_cache=True,
                skip_budget=True,
            )
            for q in queries
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            {"success": True, "result": r}
            if not isinstance(r, Exception)
            else {"success": False, "error": str(r)}
            for r in results
        ]
```

And rename the big `execute` body to `_execute_inner` with a new `skip_budget: bool = False` parameter. Public `execute` becomes a thin wrapper:

```python
    async def execute(self, *args, **kwargs) -> Dict[str, Any]:
        kwargs.setdefault("skip_budget", False)
        return await self._execute_inner(*args, **kwargs)
```

Inside `_execute_inner`, guard both budget points on `not skip_budget`:

```python
        if not skip_budget and self.budget_tracker is not None and estimate is not None:
            self.budget_tracker.check(...)
```

```python
            if not skip_budget and self.budget_tracker is not None and estimate is not None:
                self.budget_tracker.record(...)
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_budget_tracker.py -v
```

Expected: PASS — all four.

- [ ] **Step 5: Full suite**

```
pytest tests/ -q
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(n5): enforce per-session budget on run_query, batch exempt"
```

---

## Task 7: `budget_override` — full confirmation-gate wiring

The override path defeats the safety net, so it must route through the existing confirmation machinery **completely** — including the follow-up call fields (`confirmation_token`, `confirmation_secret`) and the post-execution audit logging for guarded calls. This task wires a single context-aware predicate (`is_guarded_call`) and uses it at every handler site.

**Files:**
- Modify: `src/oci_logan_mcp/confirmation.py` — add `is_guarded_call(name, arguments)`.
- Modify: `src/oci_logan_mcp/handlers.py` — compute one `guarded_call` bool at the top of `handle_tool_call`, reuse it in the gate, in `executed` audit, and in `execution_failed` audit.
- Modify: `src/oci_logan_mcp/query_engine.py` — accept `budget_override: bool` on the public `execute` wrapper (forwarded into `_execute_inner`).
- Modify: `src/oci_logan_mcp/tools.py` — add `budget_override`, `confirmation_token`, and `confirmation_secret` to `run_query` schema.
- Modify: `tests/test_handlers.py` — three tests covering first-call confirmation request, follow-up confirmed run, and audit trail.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_budget_tracker.py`:

```python
@pytest.mark.asyncio
async def test_query_engine_override_bypasses_budget():
    """When budget_override=True, over-budget queries still execute AND record."""
    from unittest.mock import AsyncMock, MagicMock
    from oci_logan_mcp.query_engine import QueryEngine
    from oci_logan_mcp.query_estimator import QueryEstimator
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.config import Settings

    settings = Settings()
    oci_client = MagicMock()
    oci_client.compartment_id = "c"
    oci_client.query = AsyncMock(side_effect=[
        {"rows": [[1_000_000]], "columns": []},                 # probe (huge)
        {"rows": [], "columns": [{"name": "Time"}]},             # live
    ])
    estimator = QueryEstimator(oci_client, settings)
    tracker = BudgetTracker("s", BudgetLimits(
        enabled=True, max_queries_per_session=1,
        max_bytes_per_session=1, max_cost_usd_per_session=0.01,
    ))
    engine = QueryEngine(
        oci_client, MagicMock(get=MagicMock(return_value=None), set=MagicMock()),
        MagicMock(), estimator=estimator, budget_tracker=tracker,
    )

    resp = await engine.execute(
        query="'Log Source' = 'x'", time_range="last_1_hour",
        use_cache=False, budget_override=True,
    )
    assert resp["source"] == "live"
    # Override still records — we charge what we ran.
    assert tracker.snapshot().queries == 1
```

Append to `tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_run_query_override_first_call_returns_confirmation_request(fixtures):
    """Without a token, run_query(budget_override=True) must return a confirmation request."""
    handlers = fixtures.handlers
    result = await handlers.handle_tool_call(
        "run_query",
        {"query": "'Log Source' = 'x'", "time_range": "last_1_hour", "budget_override": True},
    )
    payload = json.loads(result[0]["text"])
    assert payload.get("status") in {"confirmation_required", "confirmation_unavailable"}


@pytest.mark.asyncio
async def test_run_query_override_without_override_bypasses_gate(fixtures):
    """Without budget_override, run_query is a plain (non-guarded) call — no confirmation."""
    handlers = fixtures.handlers
    # Expected: executes through normally (no confirmation_required status).
    result = await handlers.handle_tool_call(
        "run_query",
        {"query": "'Log Source' = 'x'", "time_range": "last_1_hour"},
    )
    text = result[0]["text"]
    # Payload may be a query result; just assert we did NOT get a gate response.
    try:
        payload = json.loads(text)
        assert payload.get("status") != "confirmation_required"
    except json.JSONDecodeError:
        pass  # not JSON, definitely not a gate payload


@pytest.mark.asyncio
async def test_run_query_override_produces_executed_audit_entry(fixtures_with_valid_confirmation):
    """After a valid confirmation, the override call produces an 'executed' audit entry.

    This asserts Codex's concern: the handler must reuse is_guarded_call(...) for both
    the gate AND the post-execution audit branch, not is_guarded(...).
    """
    handlers = fixtures_with_valid_confirmation.handlers
    audit_log = fixtures_with_valid_confirmation.audit_log_path

    # First call — get the token.
    first = await handlers.handle_tool_call(
        "run_query",
        {"query": "'Log Source' = 'x'", "time_range": "last_1_hour", "budget_override": True},
    )
    first_payload = json.loads(first[0]["text"])
    if first_payload.get("status") == "confirmation_unavailable":
        pytest.skip("Confirmation not configured in this test fixture.")
    token = first_payload["confirmation_token"]
    secret = fixtures_with_valid_confirmation.secret

    await handlers.handle_tool_call(
        "run_query",
        {
            "query": "'Log Source' = 'x'", "time_range": "last_1_hour",
            "budget_override": True,
            "confirmation_token": token,
            "confirmation_secret": secret,
        },
    )

    entries = [json.loads(l) for l in audit_log.read_text().splitlines() if l.strip()]
    outcomes = [e["outcome"] for e in entries if e["tool"] == "run_query"]
    # At minimum: 'executed' appears — meaning is_guarded_call returned True in the
    # post-execution branch, not just the entry branch.
    assert "executed" in outcomes, f"Expected 'executed' audit entry, got {outcomes}"
```

> **Fixture naming:** `fixtures_with_valid_confirmation` is a test-side helper you'll add to `tests/test_handlers.py`'s conftest or fixture scaffolding. It should expose:
> - `.handlers` — `MCPHandlers` instance with a live `ConfirmationManager` backed by a known secret
> - `.audit_log_path` — path to the audit log file that `AuditLogger` writes
> - `.secret` — the plaintext secret so the test can submit the follow-up call
> If the current `tests/test_handlers.py` conftest doesn't have this, follow the pattern of the existing `_invoke_confirmation_tool` tests when adding it.

- [ ] **Step 2: Run tests, verify fail**

```
pytest tests/test_budget_tracker.py tests/test_handlers.py -v -k "override"
```

Expected: FAIL — `execute() got an unexpected keyword argument 'budget_override'` and gate not wired.

- [ ] **Step 3: Plumb `budget_override` through `execute`**

In `src/oci_logan_mcp/query_engine.py`, update the public wrapper and the inner signature:

```python
    async def execute(
        self,
        query: str,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
        time_range: Optional[str] = None,
        max_results: Optional[int] = None,
        include_subcompartments: bool = True,
        use_cache: bool = True,
        compartment_id: Optional[str] = None,
        budget_override: bool = False,
    ) -> Dict[str, Any]:
        return await self._execute_inner(
            query=query, time_start=time_start, time_end=time_end, time_range=time_range,
            max_results=max_results, include_subcompartments=include_subcompartments,
            use_cache=use_cache, compartment_id=compartment_id,
            skip_budget=False, budget_override=budget_override,
        )
```

Update `_execute_inner` to accept `budget_override: bool = False` and pass `override=budget_override` to `self.budget_tracker.check(...)`. **Always record on success** — including when override is True — since we charge what actually ran.

- [ ] **Step 4: Add `is_guarded_call` on `ConfirmationManager`**

In `src/oci_logan_mcp/confirmation.py`, add:

```python
    def is_guarded_call(self, tool_name: str, arguments: dict) -> bool:
        """Context-aware variant of is_guarded.

        Classifies `run_query` as guarded only when `budget_override=True` is
        present. This is the only per-arg gate in P0.
        """
        if tool_name == "run_query":
            return bool(arguments.get("budget_override"))
        return tool_name in GUARDED_TOOLS
```

Leave `is_guarded(name)` in place for backward compatibility — internal callers switch to `is_guarded_call`.

> **Do NOT add `run_query` to `GUARDED_TOOLS`.** That would gate every `run_query` call. The whole point of per-arg gating is to opt in only when `budget_override=True` is set.

- [ ] **Step 5: Rewire `handlers.handle_tool_call` — single predicate everywhere**

In `src/oci_logan_mcp/handlers.py`, inside `handle_tool_call`, compute the predicate **once** right after `user_id = self.user_store.user_id`:

```python
        guarded_call = self.confirmation_manager.is_guarded_call(name, arguments)
```

Then replace **all three** `is_guarded(name)` call sites with `guarded_call`:

1. Gate entry (currently `if self.confirmation_manager.is_guarded(name):`) → `if guarded_call:`
2. Post-success audit (currently `if self.confirmation_manager.is_guarded(name) and self.audit_logger:`) → `if guarded_call and self.audit_logger:`
3. Post-failure audit (currently `if self.confirmation_manager.is_guarded(name) and self.audit_logger:`) → `if guarded_call and self.audit_logger:`

This guarantees that when `budget_override=True`, `run_query` both enters the gate **and** leaves the normal `executed` / `execution_failed` trail, matching the behavior of every other guarded tool.

- [ ] **Step 6: Update `_run_query` handler**

In `src/oci_logan_mcp/handlers.py`, inside `_run_query`, strip `budget_override` from args before forwarding (so it doesn't leak into the cache key), but pass it to the engine:

```python
    async def _run_query(self, args: Dict) -> List[Dict]:
        budget_override = bool(args.pop("budget_override", False))
        compartment_id, include_subs = self._resolve_scope(args)
        ...
        result = await self.query_engine.execute(
            query=args["query"],
            time_range=args.get("time_range"),
            time_start=args.get("time_start"),
            time_end=args.get("time_end"),
            max_results=args.get("max_results"),
            include_subcompartments=include_subs,
            compartment_id=compartment_id,
            budget_override=budget_override,
        )
```

> **Confirmation-param stripping:** the confirmation gate in `handle_tool_call` already strips `confirmation_token` / `confirmation_secret` / `confirmation_secret_confirm` from args before calling the handler (see the `arguments = clean_args` line in `handlers.py`). So `_run_query` doesn't need to pop those separately.

- [ ] **Step 7: Update `tools.py` schema**

In `src/oci_logan_mcp/tools.py`, in the `run_query` properties block, add all three fields:

```python
                    "budget_override": {
                        "type": "boolean",
                        "description": "If true, bypass the per-session query budget. This is a guarded follow-up pattern: the first call returns a confirmation request; call again with confirmation_token and confirmation_secret to execute.",
                        "default": False,
                    },
                    "confirmation_token": {
                        "type": "string",
                        "description": "Only used when budget_override=true. The confirmation token returned from the first (un-tokened) call.",
                    },
                    "confirmation_secret": {
                        "type": "string",
                        "description": "Only used when budget_override=true. The user's confirmation secret, required to validate the token.",
                    },
```

Add an explicit schema test in `tests/test_tools.py` (or wherever tool schemas are exercised) asserting `budget_override`, `confirmation_token`, and `confirmation_secret` all exist on the `run_query` schema — this is a trivial test that prevents silent schema regressions.

Example:

```python
def test_run_query_schema_carries_budget_override_fields():
    from oci_logan_mcp.tools import get_tools
    spec = next(t for t in get_tools() if t["name"] == "run_query")
    props = spec["inputSchema"]["properties"]
    assert "budget_override" in props
    assert "confirmation_token" in props
    assert "confirmation_secret" in props
```

- [ ] **Step 8: Run the full suite**

```
pytest tests/ -q
```

Expected: all green.

- [ ] **Step 9: Commit**

```bash
git commit -am "feat(n5): budget_override — full confirmation-gate wiring"
```

---

## Task 8: Register `explain_query` tool

**Files:**
- Modify: `src/oci_logan_mcp/tools.py` — add schema.
- Modify: `src/oci_logan_mcp/handlers.py` — add `_explain_query` handler + dispatch entry.
- Modify: `tests/test_read_only_guard.py` — add `explain_query` to `KNOWN_READERS`.
- Modify: `tests/test_handlers.py` — integration test.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_explain_query_returns_estimate(fixtures):
    handlers = fixtures.handlers
    result = await handlers.handle_tool_call(
        "explain_query",
        {"query": "'Log Source' = 'x'", "time_range": "last_1_hour"},
    )
    payload = json.loads(result[0]["text"])
    assert "estimated_bytes" in payload
    assert "estimated_cost_usd" in payload
    assert "estimated_eta_seconds" in payload
    assert payload["confidence"] in {"low", "medium", "high"}
```

- [ ] **Step 2: Run test, verify fail**

```
pytest tests/test_handlers.py::test_explain_query_returns_estimate -v
```

Expected: FAIL — `Unknown tool: explain_query`.

- [ ] **Step 3: Add schema to `tools.py`**

Append to the `_TOOLS` list (in the "Helper tools" section):

```python
        {
            "name": "explain_query",
            "description": "Estimate cost, bytes scanned, and runtime for a query before running it. Returns estimated_bytes, estimated_cost_usd, estimated_eta_seconds, confidence, and a human-readable rationale.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The Log Analytics query."},
                    "time_range": {
                        "type": "string",
                        "enum": ["last_15_min", "last_1_hour", "last_24_hours", "last_7_days", "last_30_days"],
                    },
                    "time_start": {"type": "string"},
                    "time_end": {"type": "string"},
                },
                "required": ["query"],
            },
        },
```

- [ ] **Step 4: Add handler in `handlers.py`**

In the handler class, add a new method:

```python
    async def _explain_query(self, args: Dict) -> List[Dict]:
        if self.query_engine.estimator is None:
            return [{"type": "text", "text": json.dumps({
                "error": "Estimator is not configured for this server instance.",
            })}]
        est = await self.query_engine.estimator.estimate(
            query=args["query"],
            time_range=args.get("time_range"),
            time_start=args.get("time_start"),
            time_end=args.get("time_end"),
        )
        return [{"type": "text", "text": json.dumps(est.to_dict(), indent=2)}]
```

Register in the dispatch dict inside `handle_tool_call`:

```python
            "explain_query": self._explain_query,
```

- [ ] **Step 5: Update read-only drift test**

In `tests/test_read_only_guard.py`, extend `KNOWN_READERS`:

```python
    KNOWN_READERS = {
        ...,
        "explain_query",
        "get_session_budget",  # added in Task 9
    }
```

(Task 9 will register `get_session_budget`; adding both now avoids a second touch.)

- [ ] **Step 6: Run tests**

```
pytest tests/ -q
```

Expected: all pass including the drift test.

- [ ] **Step 7: Commit**

```bash
git commit -am "feat(h1): register explain_query tool"
```

---

## Task 9: Register `get_session_budget` tool

**Files:**
- Modify: `src/oci_logan_mcp/tools.py`
- Modify: `src/oci_logan_mcp/handlers.py`
- Test: integration test

- [ ] **Step 1: Write the failing test**

Append to `tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_get_session_budget_returns_usage(fixtures):
    handlers = fixtures.handlers
    result = await handlers.handle_tool_call("get_session_budget", {})
    payload = json.loads(result[0]["text"])
    assert "used" in payload
    assert "remaining" in payload
    assert "limits" in payload
    for key in ("queries", "bytes", "cost_usd"):
        assert key in payload["used"]
        assert key in payload["remaining"]
```

- [ ] **Step 2: Run test, verify fail**

Expected: FAIL — `Unknown tool: get_session_budget`.

- [ ] **Step 3: Add schema**

```python
        {
            "name": "get_session_budget",
            "description": "Return the current session's query budget usage and remaining allowance.",
            "inputSchema": {"type": "object", "properties": {}},
        },
```

- [ ] **Step 4: Add handler**

```python
    async def _get_session_budget(self, args: Dict) -> List[Dict]:
        tracker = self.query_engine.budget_tracker
        if tracker is None:
            return [{"type": "text", "text": json.dumps({
                "enabled": False,
                "message": "Budget tracking is disabled on this server.",
            })}]
        used = tracker.snapshot().to_dict()
        remaining = tracker.remaining()
        limits = {
            "enabled": tracker.limits.enabled,
            "max_queries_per_session": tracker.limits.max_queries_per_session,
            "max_bytes_per_session": tracker.limits.max_bytes_per_session,
            "max_cost_usd_per_session": tracker.limits.max_cost_usd_per_session,
        }
        return [{"type": "text", "text": json.dumps(
            {"used": used, "remaining": remaining, "limits": limits}, indent=2
        )}]
```

Register:

```python
            "get_session_budget": self._get_session_budget,
```

- [ ] **Step 5: Run tests**

```
pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(n5): register get_session_budget tool"
```

---

## Task 10: Construct `QueryEstimator` + `BudgetTracker` in `MCPHandlers.__init__`

Now that handlers reference `self.query_engine.estimator` and `...budget_tracker`, wire them at construction time.

**Files:**
- Modify: `src/oci_logan_mcp/handlers.py` — `__init__` constructs both services.
- Modify: `src/oci_logan_mcp/server.py` — no changes (MCPHandlers already receives `settings`).

- [ ] **Step 1: Update `MCPHandlers.__init__`**

In the constructor, after `self.cache = cache`:

```python
        from .query_estimator import QueryEstimator
        from .budget_tracker import BudgetTracker, BudgetLimits
        import uuid

        self._query_estimator = QueryEstimator(oci_client, settings)
        self._budget_tracker = BudgetTracker(
            session_id=uuid.uuid4().hex,
            limits=BudgetLimits(
                enabled=settings.budget.enabled,
                max_queries_per_session=settings.budget.max_queries_per_session,
                max_bytes_per_session=settings.budget.max_bytes_per_session,
                max_cost_usd_per_session=settings.budget.max_cost_usd_per_session,
            ),
        )
```

And update `self.query_engine = QueryEngine(...)`:

```python
        self.query_engine = QueryEngine(
            oci_client, cache, query_logger,
            estimator=self._query_estimator,
            budget_tracker=self._budget_tracker,
        )
```

> **Note on session_id:** N6 will replace this with a server-scoped id that's shared with the audit logger. For now the budget tracker has its own id — fine because we don't yet expose it. When N6 lands it'll hoist session_id up to `server.py` and both subsystems will use the same one.

- [ ] **Step 2: Run the full suite**

```
pytest tests/ -q
```

Expected: all green. If any existing test constructs `MCPHandlers` with a mock `settings` that lacks `budget`/`cost` sections, fix by using `Settings()`.

- [ ] **Step 3: Commit**

```bash
git commit -am "feat(h1,n5): wire estimator + budget tracker into MCPHandlers"
```

---

## Task 11: README updates

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add sections**

Under "Features" (or the most analogous heading), add:

```markdown
### Cost + ETA estimation

Every `run_query` response now carries flat estimate fields at the top level:

```json
{
  "source": "live",
  "data": { ... },
  "metadata": { ... },
  "estimated_bytes": 12345678,
  "estimated_rows": null,
  "estimated_cost_usd": 0.05,
  "estimated_eta_seconds": 2.3,
  "estimate_confidence": "medium",
  "estimate_rationale": "Linux Syslog: ~500 bytes/hr × 1.0h"
}
```

Use `explain_query` to get the full estimate **without** running the query. Cache hits replay the last known estimate for the same query/time range — no additional OCI calls.

### Session query budget

Per-session caps prevent runaway agent loops:

| Limit | Default |
|---|---|
| `max_queries_per_session` | 100 |
| `max_bytes_per_session` | 10 GiB |
| `max_cost_usd_per_session` | $5.00 |

Call `get_session_budget` any time to see usage and remaining allowance.

**Scope of enforcement (P0):** budget is enforced on `run_query` only. Cache hits are **free** — re-running a query served from the cache does not consume budget. `run_batch_queries` is **unbudgeted** in P0 because its concurrent execution model would race under per-call checks; budgeting for batch is tracked for P1.

To exceed a budget in a specific call, pass `budget_override=true` to `run_query`. This is a guarded follow-up pattern: the first call returns a confirmation request, and a second call with the returned `confirmation_token` plus your `confirmation_secret` actually executes. The override does not exempt you from the usage recording — overriding means *the run is allowed*, not *the run is free*.

Configure in `~/.oci-logan-mcp/config.yaml`:

```yaml
budget:
  enabled: true
  max_queries_per_session: 100
  max_bytes_per_session: 10737418240
  max_cost_usd_per_session: 5.00
```

Disable entirely with `budget.enabled: false`.
```

- [ ] **Step 2: Commit**

```bash
git commit -am "docs(h1,n5): document explain_query, estimates, and session budget"
```

---

## Task 12: Manual verification

- [ ] **Step 1: `explain_query` end-to-end**

```
explain_query(query="'Log Source' = 'Linux Syslog'", time_range="last_1_hour")
```

Expected: returns an estimate with `confidence="medium"` (if the source exists and has recent data) or `"low"` (otherwise). No exceptions.

- [ ] **Step 2: Estimate embedded in `run_query`**

```
run_query(query="'Log Source' = 'Linux Syslog' | head 5", time_range="last_1_hour")
```

Expected: response JSON contains top-level `"estimated_bytes"`, `"estimated_cost_usd"`, `"estimated_eta_seconds"`, `"estimate_confidence"`, `"estimate_rationale"` fields **and** `"next_steps": [...]` (from N2 already merged). Run the query a second time — it should return `"source": "cache"` with the same flat estimate fields and no new OCI calls.

- [ ] **Step 3: Budget pre-flight (run_query only)**

Set very small limits in config and run a big query:

```yaml
budget:
  max_queries_per_session: 2
```

Make 3 `run_query` calls — the third should return `BudgetExceededError` without hitting OCI.

Then verify batch is **not** budgeted in P0:

```
run_batch_queries(queries=[{"query":"*"},{"query":"*"},{"query":"*"}])
```

Expected: all three siblings execute regardless of the budget. `get_session_budget` shows no increase.

- [ ] **Step 4: Override flow**

Call `run_query(..., budget_override=True)` without a token → confirmation request.
Call again with `confirmation_token` + `confirmation_secret` → executes, and `get_session_budget` shows `queries` increment (override doesn't exempt recording).

- [ ] **Step 5: `get_session_budget`**

```
get_session_budget()
```

Expected: `{used: {...}, remaining: {...}, limits: {...}}`.

---

## Branch acceptance checklist for H1+N5

- [ ] `query_estimator.py` created with `QueryEstimate` + probe-based estimator + TTL cache.
- [ ] `budget_tracker.py` created with per-session counter, check/record/remaining, override.
- [ ] `explain_query` tool registered and returns the full estimate payload.
- [ ] `get_session_budget` tool registered and returns usage/remaining/limits.
- [ ] `run_query` responses carry **flat** `estimated_bytes`, `estimated_rows`, `estimated_cost_usd`, `estimated_eta_seconds`, `estimate_confidence`, `estimate_rationale` fields — matches `docs/phase-2/specs/agent-guardrails.md` line 19.
- [ ] `run_query` responses also carry `next_steps: [...]` from N2 on both live and cache paths — the H1 rewrite does not drop it.
- [ ] `QueryEngine.execute` is cache-first: cache hits return immediately with no probe and no budget charge, and replay the cached estimate fields + recomputed `next_steps`.
- [ ] Cache entries store a `{result, estimate}` bundle; legacy bare-result entries remain tolerated.
- [ ] `run_batch_queries` is unbudgeted in P0 — verified by test. Clearly documented.
- [ ] `run_query` with `budget_override=true` goes through the confirmation gate and produces `executed` / `execution_failed` audit entries like any other guarded call.
- [ ] `handle_tool_call` uses one shared `guarded_call = is_guarded_call(...)` predicate at all three sites (gate, executed audit, execution_failed audit).
- [ ] `run_query` schema includes `budget_override`, `confirmation_token`, `confirmation_secret`.
- [ ] Drift test (`test_read_only_guard.py::test_all_registered_tools_are_classified`) passes with both new tools in `KNOWN_READERS`.
- [ ] Full test suite green.
- [ ] README updated to document the flat estimate fields, the budget, the P0 batch exemption, and the override flow.
- [ ] Manual smoke against a real tenancy produces sane estimates.
