# Spec — `feat/triage-toolkit`

**Branch:** `feat/triage-toolkit` (off `main`, started after `feat/agent-guardrails` merges)
**Theme:** T1 Triage Velocity + T3 Signal Quality at Source
**Core features:** A1, A2, A4, A6, J1, J2
**Stretch features:** A3, A5, A7
**Ship order within branch:**
1. A2 + A4 (parallel; primitives for A1)
2. J1 + J2 (parallel independent track)
3. A1 (depends on A2, A4)
4. A6 (depends on A1's primitives)
5. Stretch: A3, A5, A7

**Companion docs:** [../feature-catalog.md](../feature-catalog.md) · [../roadmap.md](../roadmap.md)

> **For agentic workers:** Design-level spec. Produce a TDD plan per feature via `superpowers:writing-plans` before execution.

---

## Goal

Give the agent first-class investigation primitives (A2, A4) and the orchestrator (A1) that stitches them together, plus platform health surfaces (J1, J2) that tell the agent whether the data it's reasoning about is even trustworthy.

## Acceptance criteria for the branch

- `investigate_incident(alert_id | query, time_range)` returns a structured investigation: ranked sources, top clusters, changed entities, draft timeline — in ≤20s p95 on dogfood data.
- `diff_time_windows` returns a before/after delta view on a single call.
- `pivot_on_entity` returns cross-source results for a given entity in one call.
- `why_did_this_fire(alarm_ocid, fire_time)` returns structured post-mortem context.
- `ingestion_health()` returns per-source last-seen/volume/stoppage alerts using a persisted baseline.
- `parser_failure_triage()` returns top parser failures with sample lines.
- All existing tests still pass.

---

## A2 — `diff_time_windows`

### Purpose
Compare "this hour" vs. "same hour yesterday/last week" on a source or field distribution. The cheapest high-signal triage primitive.

### Tool interface
```
diff_time_windows(
  query: str,                  # base query to execute in both windows
  current_window: TimeRange,
  comparison_window: TimeRange,
  dimensions: list[str] | None = None,  # fields to break out (default: auto-detect top-k)
) -> {
  current: AggregateResult,
  comparison: AggregateResult,
  delta: list[{dimension: str, current: int, comparison: int, pct_change: float}],
  summary: str,
}
```

### Files
- Create: `src/oci_logan_mcp/diff_tool.py`.
- Modify: `src/oci_logan_mcp/tools.py` — register tool.
- Create: `tests/test_diff_tool.py`.

### Test outline
1. Test: two identical windows → empty delta, summary "no significant change."
2. Test: window with 2× volume → delta shows +100%, summary names the spike.
3. Test: dimension auto-detect picks top categorical fields when not provided.
4. Test: missing dimension in one window → handled gracefully.

### Dependencies
- Reuses existing `query_engine.py` + `time_parser.py`.

---

## A4 — `pivot_on_entity`

### Purpose
Given a host/user/request-id, pull everything about that entity across log sources in one call.

### Tool interface
```
pivot_on_entity(
  entity_type: "host" | "user" | "request_id" | "ip" | "custom",
  entity_value: str,
  time_range: TimeRange,
  sources: list[str] | None = None,  # default: auto-discover sources with matching field
  max_rows_per_source: int = 100,
) -> {
  entity: {type, value},
  by_source: list[{source: str, rows: list[dict], truncated: bool}],
  cross_source_timeline: list[Event],  # merged, time-ordered
  stats: {total_events, sources_matched},
}
```

### Approach
1. Discover which log sources contain a field matching the entity type (reuse `list_fields` + `list_log_sources`).
2. Run a batched query per source filtered by entity_value.
3. Merge timestamps into a cross-source timeline (respect H1 budget — abort early if estimate exceeds cap).

### Files
- Create: `src/oci_logan_mcp/pivot_tool.py`.
- Modify: `src/oci_logan_mcp/tools.py`.
- Create: `tests/test_pivot_tool.py`.

### Test outline
1. Test: known host with data in 2 sources → both appear in `by_source`.
2. Test: nonexistent entity → empty timeline, `total_events=0`.
3. Test: budget exceeded mid-pivot → returns partial with `truncated=true`.
4. Test: sources filter respects provided list.

### Dependencies
- Soft dependency on H1 budget (hard-fails gracefully if not yet merged).

---

## J1 — Ingestion health (with stoppage detection)

### Purpose
Answer the 2am question: "Is log ingestion even working right now?" Emphasis on **stoppage and drop detection**, not just descriptive stats.

### Tool interface
```
ingestion_health(
  compartment_id: str | None = None,  # default: current context
  severity_filter: "all" | "warn" | "critical" = "warn",
) -> {
  summary: {sources_healthy, sources_warn, sources_critical},
  baseline_age_hours: float,
  findings: list[{
    source: str,
    status: "healthy" | "stopped" | "dropped" | "lag",
    last_log_ts: datetime,
    baseline_hourly_volume: int,
    current_hourly_volume: int,
    severity: "info" | "warn" | "critical",
    message: str,  # human-readable diagnosis
  }],
}
```

### Baseline store
- Persist per-source baselines: `(source, compartment) -> {avg_hourly_bytes, avg_hourly_count, stddev, updated_at}`.
- Storage: SQLite or simple JSON store under `config.state_dir`.
- Refreshed via a background sampler (or on-demand at first call with warning `baseline_age_hours=fresh`).

### Detection rules
- **STOPPED** — last_log_ts > max(10 min, 5× median_gap) old.
- **DROP** — current hourly count < 10% of baseline_hourly_count.
- **LAG** — ingestion timestamp lag vs. event timestamp > threshold.

### Files
- Create: `src/oci_logan_mcp/ingestion_health.py`.
- Create: `src/oci_logan_mcp/baseline_store.py` — persistent baseline.
- Modify: `src/oci_logan_mcp/tools.py`.
- Modify: `src/oci_logan_mcp/config.py` — add `baseline_store_path`, thresholds.
- Create: `tests/test_ingestion_health.py`.
- Create: `tests/test_baseline_store.py`.

### Test outline
1. Test: source with recent data and healthy volume → `status=healthy`.
2. Test: source last seen 3h ago with baseline 2min gap → `status=stopped, severity=critical`.
3. Test: source at 5% of baseline volume → `status=dropped, severity=warn`.
4. Test: no baseline → status=info with `"baseline not yet established"`.
5. Test: baseline store refreshes when stale.

### Dependencies
- Upgrades H1's estimator (H1 can read `baseline_store` for better accuracy once J1 lands).

---

## J2 — Parser failure triage

### Purpose
Surface recent parser failures with sample raw lines, ranked by volume. Tells admins which parsers need fixing.

### Tool interface
```
parser_failure_triage(
  time_range: TimeRange = "last_24h",
  top_n: int = 20,
) -> {
  failures: list[{
    parser_name: str,
    source: str,
    failure_count: int,
    sample_raw_lines: list[str],  # up to 3 samples
    first_seen: datetime,
    last_seen: datetime,
  }],
  total_failure_count: int,
}
```

### Approach
- Query Logan for parser-failure events (known query pattern in OCI Log Analytics: `Log Source = 'Parser Failure' | ...`).
- Aggregate, rank, sample — pure query + post-processing.

### Files
- Create: `src/oci_logan_mcp/parser_triage.py`.
- Modify: `src/oci_logan_mcp/tools.py`.
- Create: `tests/test_parser_triage.py`.

### Test outline
1. Test: mock Logan returning N failure events → aggregated correctly.
2. Test: samples limited to 3 per parser.
3. Test: empty result → returns empty list, `total_failure_count=0`.

### Dependencies
- None beyond existing query engine.

---

## A1 — `investigate_incident` (orchestrator)

### Purpose
Flagship. One tool call that takes an alarm/query and time-range and returns a first-cut investigation.

### Tool interface
```
investigate_incident(
  seed: {
    alarm_ocid: str | None,
    query: str | None,
    description: str | None,
  },
  time_range: TimeRange,
  compartment_id: str | None = None,
  max_budget_usd: float | None = None,
) -> InvestigationReport {
  summary: str,
  anomalous_sources: list[{source, anomaly_score, why}],
  top_error_clusters: list[{pattern, count, sample_rows}],
  changed_entities: list[{entity_type, entity_value, change_description}],
  timeline: list[TimelineEvent],
  next_steps: list[NextStep],  # populated via N2
  budget_consumed: BudgetSnapshot,
}
```

### Orchestration flow
1. **Resolve seed** — if `alarm_ocid`, pull alarm context + fire-time. If `query`, run it to get seed rows. If `description`, use it as an NL prompt against current NL-to-query path.
2. **Enumerate suspect sources** — use J1's ingestion_health + diff against baseline to find sources with spikes/drops.
3. **For each top-K source, in parallel:**
   - Run `diff_time_windows` (A2) to quantify change.
   - Extract top error patterns (reuse Logan `cluster` command).
4. **Identify changed entities** — aggregate top entities (hosts/users) with status changes.
5. **Assemble timeline** — merge time-ordered top events across sources.
6. **Enforce budget** — use N5; abort gracefully if exceeded and return partial.

### Files
- Create: `src/oci_logan_mcp/investigate.py` — orchestrator.
- Modify: `src/oci_logan_mcp/tools.py`.
- Create: `tests/test_investigate.py`.

### Test outline
1. Test: alarm-seeded investigation on canned data returns expected summary fields.
2. Test: budget exceeded mid-run → returns `partial=true` with whatever was gathered.
3. Test: all sources healthy, no anomalies → summary "no anomalies detected in range."
4. Test: query-seeded path produces equivalent output to alarm-seeded.
5. Test: p95 runtime under 20s on canned-data harness.

### Dependencies
- **Hard:** A2 (diff_time_windows), A4 (pivot_on_entity), J1 (baseline), H1+N5 (budget).
- Ship only after A2+A4+J1 are merged locally on this branch.

---

## A6 — `why_did_this_fire`

### Purpose
Lightweight variant of A1 scoped to alarm post-mortem. Given an alarm OCID and fire-time, reconstruct "why did this page me."

### Tool interface
```
why_did_this_fire(
  alarm_ocid: str,
  fire_time: datetime,
  window_before_seconds: int = 300,
  window_after_seconds: int = 60,
) -> {
  alarm: AlarmSnapshot,
  trigger_query_result: QueryResult,  # re-run of the alarm's query at fire_time
  top_contributing_rows: list[dict],
  dashboard_link: str | None,
  related_saved_search_link: str | None,
}
```

### Files
- Create: `src/oci_logan_mcp/alarm_postmortem.py`.
- Modify: `src/oci_logan_mcp/alarm_service.py` — helper to pull alarm definition by OCID.
- Modify: `src/oci_logan_mcp/tools.py`.
- Create: `tests/test_alarm_postmortem.py`.

### Test outline
1. Test: given alarm + fire-time → re-runs trigger query with correct window.
2. Test: top contributing rows ranked by contribution to threshold breach.
3. Test: unknown alarm OCID → structured error.
4. Test: dashboard link included when alarm has associated dashboard.

### Dependencies
- Reuses A1's timeline and entity-change helpers.

---

## Stretch: A3 — `find_rare_events`

### Purpose
Return rows with low historical frequency for a given source+field (first-seen detection).

### Tool interface
```
find_rare_events(
  source: str,
  field: str,
  time_range: TimeRange,
  rarity_threshold_percentile: float = 5.0,
  history_days: int = 30,
) -> {
  rare_values: list[{value, count_in_range, count_in_history, first_seen, last_seen}],
}
```

### Approach
- Wrapper around Logan's `rare` command (OCI Log Analytics supports least-frequent value queries).
- Post-process to annotate first-seen/last-seen.

### Files
- Create: `src/oci_logan_mcp/rare_events.py`.
- Tests: `tests/test_rare_events.py`.

### Dependencies
- None beyond query engine.

---

## Stretch: A5 — `trace_request_id`

### Purpose
Search all sources for a request/trace id and produce ordered events.

### Tool interface
```
trace_request_id(
  request_id: str,
  time_range: TimeRange,
  id_fields: list[str] | None = None,  # default: auto-probe common ones
) -> {
  events: list[TimelineEvent],  # time-ordered, cross-source
  sources_matched: list[str],
}
```

### Approach
- Reuses A4's discovery logic with a default field set (`request_id`, `traceId`, `x-request-id`, etc.).

### Files
- Create: `src/oci_logan_mcp/trace_lookup.py`.
- Tests: `tests/test_trace_lookup.py`.

### Dependencies
- A4 (pivot_on_entity) for source discovery.

---

## Stretch: A7 — `related_dashboards_and_searches`

### Purpose
Given a source/entity/field, suggest existing dashboards, saved searches, learned queries that reference it.

### Tool interface
```
related_dashboards_and_searches(
  source: str | None = None,
  entity: {type, value} | None = None,
  field: str | None = None,
) -> {
  dashboards: list[{id, name, score, reason}],
  saved_searches: list[{id, name, score, reason}],
  learned_queries: list[{id, name, score, reason}],
}
```

### Approach
- Text match over dashboard/saved-search definitions; score by field/source overlap.
- Reuse existing `fuzzy_match.py`.

### Files
- Create: `src/oci_logan_mcp/related_resources.py`.
- Tests: `tests/test_related_resources.py`.

### Dependencies
- Existing dashboard/saved-search/learned-query stores.

---

## Branch merge criteria

- All new and existing tests pass: `pytest tests/`.
- `investigate_incident` produces a structured result in ≤20s p95 on a canned-data harness.
- `ingestion_health` detects a simulated stoppage on a test source.
- `parser_failure_triage` returns expected aggregation against mock Logan responses.
- Stretch items either shipped or deferred with a one-line rationale in the PR description.
- README section added: "Investigation toolkit."
- Acceptance-criteria checklist marked done in PR description.
