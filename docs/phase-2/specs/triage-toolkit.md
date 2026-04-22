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

### OCI LA schema notes (verified live)
Parse-failure records carry four fields: `'Parse Failed'` (LONG, set to 1 when a line fails to parse), `'Log Source'`, `'Original Log Content'`, and `'_Truncated'`. **There is no `'Parser Name'` field.** Each log source in OCI LA has exactly one parser configured, so reporting by `'Log Source'` is operationally equivalent to "which parser is broken."

### Tool interface
```
parser_failure_triage(
  time_range: TimeRange = "last_24_hours",
  top_n: int = 20,
) -> {
  failures: list[{
    source: str,                   # the Log Source whose parser failed
    failure_count: int,
    sample_raw_lines: list[str],   # up to 3 samples (best-effort)
    first_seen: datetime,
    last_seen: datetime,
  }],
  total_failure_count: int,
  partial?: bool,                  # present only if samples query was truncated
  partial_reason?: str,            # e.g. "samples_budget_exceeded"
}
```

### Approach
- Stats query: `'Parse Failed' = 1 | stats count as failure_count, earliest('Time') as first_seen, latest('Time') as last_seen by 'Log Source' | sort -failure_count | head {top_n}`.
- Samples query (only if stats non-empty): `'Parse Failed' = 1 AND 'Log Source' in (<top_sources>) | fields 'Log Source', 'Original Log Content' | head {len(sources) * 3}`.
- Python merge caps samples at 3 per source.
- Budget handling: budget exhaustion on the stats query propagates as a hard error via the handler. Budget exhaustion on the samples query returns the ranked stats with empty `sample_raw_lines` and sets `partial=true, partial_reason="samples_budget_exceeded"`.

### Files
- Create: `src/oci_logan_mcp/parser_triage.py`.
- Modify: `src/oci_logan_mcp/tools.py`.
- Create: `tests/test_parser_triage.py`.

### Test outline
1. Test: mock Logan returning N failure events → aggregated correctly.
2. Test: samples limited to 3 per source.
3. Test: empty result → returns empty list, `total_failure_count=0`.
4. Test: samples-query budget exhaustion → `partial=true` with empty samples.

### Dependencies
- None beyond existing query engine.

---

## A1 — `investigate_incident` (orchestrator)

> **Implementation status:** Shipped in P0 per the detailed design at
> [`2026-04-22-a1-investigate-incident-design.md`](2026-04-22-a1-investigate-incident-design.md).
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
