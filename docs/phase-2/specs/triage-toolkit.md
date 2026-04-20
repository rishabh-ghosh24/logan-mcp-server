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
  dimensions: list[str] | None = None,  # fields to break out. If None and the query has a `by <fields>` clause, reuse it; else return a scalar total delta. A1 passes explicit dimensions when it wants a breakout.
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
3. Test: when `dimensions` is omitted and the query already contains a `by <fields>` clause, those fields are reused (no source-side field discovery in P0).
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

## J1 — Ingestion health (freshness / stoppage detection)

### Purpose
Answer the 2am question: "Is log ingestion even working right now?" P0 focuses on **freshness and stoppage detection** against the last record seen per source — no persistent baseline store, no background sampler. This keeps J1 small and trustworthy for a solo-dev P0; a full baseline-backed DROP/LAG subsystem is a P1 expansion.

### Tool interface (P0)
```
ingestion_health(
  compartment_id: str | None = None,      # default: current context
  sources: list[str] | None = None,       # default: all discovered sources
  severity_filter: "all" | "warn" | "critical" = "warn",
) -> {
  summary: {sources_healthy, sources_stopped, sources_unknown},
  checked_at: datetime,
  findings: list[{
    source: str,
    status: "healthy" | "stopped" | "unknown",
    last_log_ts: datetime | None,
    age_seconds: int | None,
    severity: "info" | "warn" | "critical",
    message: str,                         # human-readable diagnosis
  }],
}
```

### Detection rule (P0)
- **STOPPED** — `last_log_ts` older than `stoppage_threshold_seconds` (config, default 600). Severity `critical`.
- **HEALTHY** — `last_log_ts` within the threshold. Severity `info`.
- **UNKNOWN** — no records found within the freshness probe window. Severity `warn`.

No DROP/LAG classifications in P0 — those require a baseline reference.

### Files
- Create: `src/oci_logan_mcp/ingestion_health.py`.
- Modify: `src/oci_logan_mcp/tools.py`.
- Modify: `src/oci_logan_mcp/config.py` — add `stoppage_threshold_seconds` (default 600), `freshness_probe_window` (default `last_1_hour`).
- Create: `tests/test_ingestion_health.py`.

### Test outline (P0)
1. Test: source with a record in the last minute → `status=healthy`.
2. Test: source last seen 30 minutes ago → `status=stopped, severity=critical`.
3. Test: source with no records in probe window → `status=unknown, severity=warn`.
4. Test: `sources` filter limits the probe set.
5. Test: `severity_filter="critical"` omits healthy and unknown findings.

### Dependencies
- None. Pure query against `query_engine`.

### Deferred to P1
- Persistent per-source baseline store (bytes/hour, count/hour, stddev) with background refresh.
- DROP classification (current volume vs. baseline).
- LAG classification (ingestion-time vs. event-time skew).
- Once P1 baselines exist, H1 can read them instead of probing for each `explain_query` call.

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
2. **Enumerate stopped sources** — call J1 `ingestion_health` (freshness) to find sources not emitting during the investigation window. These are candidate culprits regardless of volume.
3. **Enumerate anomalous sources** — call A2 `diff_time_windows` with the investigation window vs. the prior equal-length window, per source. Top-K by delta magnitude. This replaces the old "baseline diff" step — A2 gives us spike/drop detection without a persistent baseline. A1 passes explicit `dimensions` (from field knowledge it already has via `list_fields`/learned queries) when it wants a per-field breakout; A2 does not perform field discovery in P0.
4. **For each top-K source, in parallel:**
   - Extract top error patterns (reuse Logan `cluster` command).
   - Collect top entities (hosts/users/request-ids) via A4 `pivot_on_entity`.
5. **Identify changed entities** — aggregate top entities with status changes across the top-K sources.
6. **Assemble timeline** — merge time-ordered top events across sources.
7. **Enforce budget** — use N5; abort gracefully if exceeded and return partial.

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
- **Hard:** A2 (diff_time_windows), A4 (pivot_on_entity), J1 (freshness), H1+N5 (budget).
- Ship only after A2+A4+J1 are merged locally on this branch.
- **Note:** A1 no longer depends on a persistent baseline store. Spike/drop detection is performed live via A2 using the prior equal-length window as a reference. When J1 gains baselines in P1, step 3 can optionally pivot to baseline-backed anomaly scoring.

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
