# A1 `investigate_incident` — Design Document

**Status:** Design approved, pending writing-plans.
**Branch:** `feat/investigate-incident` (off `main` after triage-toolkit merge).
**Supersedes:** `docs/phase-2/specs/triage-toolkit.md` §A1 wherever they disagree (§A1 will be updated to match this doc during implementation).

> **For agentic workers:** This is a design spec. Produce a TDD implementation plan via `superpowers:writing-plans`, then execute via `superpowers:subagent-driven-development`. Do NOT start coding from this doc directly.

---

## 1. Goal

One MCP tool call that takes a seed query and a time range and returns a structured first-cut investigation — who's broken, what's the error pattern, who's involved, what happened when, and what to look at next. The agent should be able to ask *"investigate the seed at this time window"* and get a coherent report back in ≤20s p95 on dogfood data, scoped to the incident the seed describes, not to background source noise.

This is the flagship of the triage toolkit: it composes J1 (`ingestion_health`), A2 (`diff_time_windows`), A4-adjacent primitives, J2 (`parser_failure_triage`), Logan's native `cluster` command, and the existing `next_steps.suggest()` heuristics.

## 2. Public interface

```python
investigate_incident(
    query: str,                        # seed query, required
    time_range: str = "last_1_hour",   # enum from TIME_RANGES (time_parser.py)
    top_k: int = 3,                    # sources to drill into; clamped [1, 10]
    compartment_id: Optional[str] = None,
) -> InvestigationReport
```

MCP schema exposes exactly these four parameters.

### 2.1 Deferrals (explicit, not silent)

| Spec field | Status | Rationale |
|---|---|---|
| `alarm_ocid` seed | Deferred to A6 `why_did_this_fire` | OCID → fire_time + trigger query translation is A6's primary concern; owning it in A1 duplicates work. |
| `description` seed | Deferred | No in-repo NL-to-query resolver exists today. A1 can't honor a description seed without building one, and that's out of scope for P0. |

Both are documented in the MCP schema's `description` field so callers see them, and the top-level spec doc will be updated to note the deferral.

### 2.2 P0 seed-query scoping limitation

**Drill-down scoping uses only the pre-pipe search clause of the seed query.** Later pipeline stages — `where`, `eval`, `stats`, `lookup`, etc. — are ignored for composing per-source queries.

Example: a seed of `'Event' = 'error' | where Severity = 'critical'` extracts a `seed_filter` of `'Event' = 'error'`. Drill-down queries will investigate **all** `'Event' = 'error'` rows, not just critical ones. The `where Severity = 'critical'` narrowing is dropped.

If the extracted filter is empty or `*`, A1 degrades to unscoped source-noise investigation (essentially: "what are the loudest sources right now"). The report's `seed.seed_filter_degraded` flag and the human-readable `summary` surface this degradation so the caller sees it.

Full pipeline-aware scoping is a P1 expansion and would require wiring a real Logan query parser (similar to what `diff_tool._BY_CLAUSE_RE` does for the `by` clause, but generalized).

## 3. InvestigationReport shape

```python
{
  "summary": str,                                  # 1-2 sentence human-readable synthesis

  "seed": {
    "query": str,                                  # original seed query as provided
    "seed_filter": str,                            # extracted pre-pipe clause (or "*" if degraded)
    "seed_filter_degraded": bool,                  # true if filter collapsed to unscoped "*"
    "time_range": str,                             # investigation window
    "compartment_id": str | None,
  },

  "ingestion_health": {
    "snapshot": {...},                             # raw J1 output (summary + findings)
    "probe_window": str,                           # J1's configured freshness_probe_window
    "note": str,                                   # explains the window-mismatch semantics (see §6)
  },

  "parser_failures": {...},                        # raw J2 output (failures + total_failure_count)

  "anomalous_sources": [                           # top_k by |delta|, from seed-scoped A2 breakout
    {
      "source": str,
      "current_count": int,
      "comparison_count": int,
      "pct_change": float,
      "top_error_clusters": [                      # up to 3 per source
        {"pattern": str, "count": int, "problem_priority": int | None},
      ],
      "top_entities": [                            # "loudest," not "changed"
        {"entity_type": "host"|"user"|"request_id",  # "ip" deferred to P1
         "entity_value": str, "count": int},
      ],
      "timeline": [                                # per-source, up to 20, time-sorted
        {"time": str, "severity": str | None, "message": str},
      ] | None,                                    # null if omitted for this source
      "errors": [str, ...],                        # per-source non-fatal errors
    },
  ],

  "cross_source_timeline": [                       # merged, time-sorted, capped at 50
    {"time": str, "source": str, "severity": str | None, "message": str},
  ] | None,                                        # null if nothing was gathered

  "next_steps": [{...NextStep.to_dict()...}, ...], # from next_steps.suggest()

  "budget": {"queries": int, "bytes": int, "cost_usd": float},

  "partial": bool,
  "partial_reasons": [str, ...],                   # [] if not partial; valid values: "budget_exceeded",
                                                    # "timeline_omitted", "entity_discovery_partial",
                                                    # "source_errors" (see §6 for when each is set)

  "elapsed_seconds": float,
}
```

### 3.1 Field-level notes

- `seed.seed_filter` truthfully reports what was investigated — essential for callers sanity-checking A1's behavior. `seed_filter_degraded: true` means "we're showing source-level noise, not incident-scoped signal."
- `ingestion_health.note` literally says: *"Freshness is evaluated over J1's configured probe window (`<probe_window>`), which may differ from the investigation `time_range`. A source marked healthy here could have been stopped during the investigation window."*
- `anomalous_sources[].top_entities` is **loudest** (top by count in the investigation window), not **changed** (requires a comparison window per entity per source, out of scope for P0).
- `anomalous_sources[].errors` is a list of non-fatal strings — e.g., `"top_entities[host]: InvalidParameter — 'Host Name (Server)' not recognized"`. Empty list for clean runs.
- `cross_source_timeline` is `null`, not `[]`, when no per-source timeline could be collected — distinguishes "empty investigation" from "timeline intentionally dropped."
- Empty vs null semantics for `anomalous_sources[*].timeline`:
  - `[]` = timeline query ran successfully and returned zero rows (source is quiet)
  - `null` = timeline query errored, was budget-cancelled, or the whole timeline phase was skipped for this source
  - Same distinction at the top-level `cross_source_timeline`: `[]` means "all per-source timelines ran clean but no merged events crossed the threshold"; `null` means "no per-source timeline produced anything mergeable"

## 4. Architecture — pure functions + thin class

Matches the codebase's established per-tool pattern (`ingestion_health.py`, `parser_triage.py`):

- Module-level pure functions for each phase, independently testable
- `InvestigateIncidentTool` class wraps them with a thin `async run()` orchestrator
- An accumulator `dict` threads through the phases; on `BudgetExceededError` anywhere, the orchestrator catches and returns `_finalize(accumulator, partial_reasons=["budget_exceeded", ...])`

Alternatives considered and rejected:

- **Subpackage `investigate/`** with `phases/*.py` — premature decomposition for a 500-ish LOC first version, breaks the one-module-per-tool convention.
- **Monolithic class with method-per-phase** — loses unit testability of phase logic without mocking out `self._engine` per test.

## 5. Seed-filter extraction (quote-aware)

Naive `query.split("|", 1)[0]` is brittle: a seed like `'Log Source' = 'http://x.com/a|b'` would mis-scope to `'Log Source' = 'http://x.com/a`. The codebase already avoids this class of bug in `diff_tool._BY_CLAUSE_RE` with explicit string-literal anchoring.

**Algorithm** (implement in `_extract_seed_filter`):

```python
def _extract_seed_filter(query: str) -> str:
    """Return the pre-pipe search clause, quote-aware. Returns '*' for empty/unscoped.

    Scans char-by-char tracking single-quote and double-quote string context,
    handling OCI LA's doubled-quote escape (e.g. 'O''Brien'). Pipes inside
    quoted literals do not terminate the filter clause.
    """
    if not query:
        return "*"

    in_single = in_double = False
    i, end = 0, len(query)

    while i < end:
        c = query[i]
        if in_single:
            if c == "'":
                # '' inside a single-quoted string is an escaped quote, not a terminator
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

**Mandatory test coverage** for this function alone (pin regressions):

| Input | Expected |
|---|---|
| `""` | `"*"` |
| `"*"` | `"*"` |
| `"   *   "` | `"*"` |
| `"'Event' = 'error'"` | `"'Event' = 'error'"` |
| `"'Event' = 'error' \| stats count"` | `"'Event' = 'error'"` |
| `"'Event' = 'error' \| where X = 'y'"` | `"'Event' = 'error'"` |
| `"'Log Source' = 'http://x.com/a\|b' \| stats count"` | `"'Log Source' = 'http://x.com/a\|b'"` |
| `"'User' = 'O''Brien' \| stats count"` | `"'User' = 'O''Brien'"` |
| `'"Field" = "val\|with\|pipes" \| stats count'` | `'"Field" = "val\|with\|pipes"'` |
| `"'Log Source' = 'X'"` | `"'Log Source' = 'X'"` (already single-source pinned) |

## 6. Orchestration flow

### 6.1 Phase 1 — Seed execution
Run the unmodified seed query with `time_range` and `compartment_id`. Captures row count + `Log Source` breakdown if the seed naturally produces one. This is the only phase where the seed is run verbatim; later phases use `seed_filter`.

### 6.2 Phase 2 — J1 freshness snapshot
Call `IngestionHealthTool.run(compartment_id=compartment_id, severity_filter="all")`. **No `time_range` argument** — J1 doesn't accept one; it uses its configured `freshness_probe_window`.

The report wraps J1's output with:
- `probe_window`: the config value used (pulled from `Settings.ingestion_health.freshness_probe_window`)
- `note`: the freshness-vs-investigation-window caveat text (see §3.1)

Stopped sources are excluded from Phase 4's ranking (their counts would be artifacts of having stopped, not incident signal) but stay visible in the report.

### 6.3 Phase 3 — J2 parser failures
Call `ParserTriageTool.run(time_range=time_range, top_n=10)`. Unconditional — the signal "source's parser is broken" is load-bearing for interpreting any anomaly ranking, and the cost is ~2 queries at this scale.

### 6.4 Phase 4 — Seed-scoped anomaly ranking (A2)

Compose a breakout query:
```
{seed_filter} | stats count as n by 'Log Source'
```
(If `seed_filter == "*"`, the query is `* | stats count as n by 'Log Source'` — the degraded-unscoped path.)

**Comparison window computation:** `DiffTool` accepts each window dict as either `{"time_range": "..."}` (TIME_RANGES token) or `{"time_start": "...ISO...", "time_end": "...ISO..."}`. "Prior equal-length" is not expressible as a single TIME_RANGES token, so A1 computes absolute timestamps:

```python
delta = TIME_RANGES[time_range]  # e.g. timedelta(hours=1) for "last_1_hour"
now = _utcnow()                  # UTC-aware
current = {"time_range": time_range}
comparison = {
    "time_start": (now - 2 * delta).isoformat(),
    "time_end":   (now - delta).isoformat(),
}
```

Call: `DiffTool.run(query=<composed>, current_window=current, comparison_window=comparison)`.

Resulting delta is sorted by `abs(pct_change)` (or by `abs(current - comparison)` when the comparison is zero), and the top_k sources — excluding anything J1 flagged as `stopped` — are selected for drill-down.

### 6.5 Phases 5+6 — Per-source drill-down (bounded concurrency)

Phases 5 (cluster + entity discovery) and 6 (timeline) run as **one per-source coroutine**, not two separate `asyncio.gather` passes. Each branch's queries are sequential inside so budget tracking stays deterministic.

Per-source branch, executed under `asyncio.Semaphore(2)`:

1. **Cluster** — `{seed_filter} and 'Log Source' = '{source}' | cluster | sort -Count | head 3`
2. **Entity discovery** for each entity field — **P0 uses 3 fields: `host`, `user`, `request_id`** (sequential inside the branch). `ip` is deferred to P1 to keep the per-source query count bounded:
   ```
   {seed_filter} and 'Log Source' = '{source}' | stats count as n by '{entity_field}' | sort -n | head 5
   ```
   Entity field names come from `ENTITY_FIELD_MAP` in `pivot_tool.py` (already live-probed against the dogfood tenancy). If a query errors with `InvalidParameter` (some sources don't have certain fields), that entity type's list stays empty for that source, an entry goes on `per_source.errors`, and `partial_reasons` gains `"entity_discovery_partial"` (deduplicated).
3. **Timeline** (best-effort):
   ```
   {seed_filter} and 'Log Source' = '{source}' | fields Time, Severity, 'Original Log Content' | sort -Time | head 20
   ```
   Timeline errors are non-fatal: that source's `timeline` is set to `None`, the error string is appended to `per_source.errors`, and `partial_reasons` gains `"timeline_omitted"` (deduplicated — recorded once even if multiple sources drop their timeline).

Semaphore semantics: at most 2 source branches run concurrently. `asyncio.gather(..., return_exceptions=True)` wraps the set so one branch blowing up doesn't take down the investigation.

When a branch returns an `Exception` object (from `return_exceptions=True`), it's recorded on the accumulator's `source_errors` list, that source's entry is built with empty drill-down data + the error string in `errors`, and `partial_reasons` gains `"source_errors"`.

**Per-source query cost:** 1 cluster + 3 entity discovery (we skip `ip` unless it's cheap to add later) + 1 timeline = up to 5 queries per source. At `top_k=3` with Semaphore(2), this is the bulk of A1's budget: ~15 queries, ~6-10s with parallelism.

**Timestamp normalization:** every phase that consumes `Time`, `first_seen`, `last_seen`, etc. routes through the existing `_parse_ts` / `_ts_to_iso` helpers (J1/J2 already handle OCI's epoch-millisecond TIMESTAMP wire shape). Merged `cross_source_timeline` is sorted by normalized time and capped at 50 entries.

### 6.7 Phase 7 — next_steps + summary assembly

Call `next_steps.suggest(seed_query, seed_result)` on the Phase 1 output. Attach to the report.

`summary` is a templated 1-2 sentence synthesis, e.g.:
> *"Investigated {seed_filter_or_unscoped} over {time_range}. {N} sources anomalous (top: {source_1} +{pct}%, {source_2} ...). J1 flags {M} stopped. J2 reports {P} parser failures. See `anomalous_sources` for drill-down."*

If `partial: true`, the reasons are appended.

### 6.8 Budget handling

- `BudgetExceededError` anywhere in phases 1-6 → caught at the orchestrator; `_finalize(accumulator, partial_reasons=["budget_exceeded"] + existing_reasons)` returns whatever has been gathered.
- `Semaphore(2)` + `return_exceptions=True` in phases 5 and 6 mean at most 2 queries land after the first budget error before remaining branches fail fast (each branch's next `engine.execute` hits the same tracker and raises).
- No "budget tight" heuristic — the budget tracker doesn't expose that granularity and over-engineering it now adds surface without behavioral gain.

## 7. File layout

```
src/oci_logan_mcp/investigate.py      # new module: phase functions + InvestigateIncidentTool
src/oci_logan_mcp/handlers.py         # add import, instantiation, route, _investigate_incident handler
src/oci_logan_mcp/tools.py            # add MCP schema with enum'd time_range and bounded top_k
tests/test_investigate.py             # unit (per phase function) + orchestration
tests/test_handlers.py                # TestInvestigateIncident (routing, budget, validation)
tests/test_read_only_guard.py         # "investigate_incident" added to KNOWN_READERS
README.md                             # Investigation Toolkit entry
docs/phase-2/specs/triage-toolkit.md  # §A1 updated to match this doc
```

## 8. Testing

### 8.1 Unit tests (phase functions, mocked engine)

- `_extract_seed_filter` — the 10 cases in §5
- `_compose_source_scoped_query` — handles `*` degradation (no `and` prefix), handles quote-escaping of source names, handles seed that's already `'Log Source' = 'X'`
- `_rank_anomalous_sources` — sorts by `|pct_change|`, excludes J1-stopped sources, handles zero-comparison-count edge case
- `_select_top_entities` — groups stats response by entity-type, handles `InvalidParameter` as "no entities for this field"
- `_merge_cross_source_timeline` — deduplicates, sorts by normalized time, caps at 50, handles null and empty per-source inputs
- `_templated_summary` — renders for clean / degraded-seed / partial variants

### 8.2 Orchestration tests (mocked engine, one scenario per test)

- `test_run_happy_path` — all phases return, no partial, all top-level fields present
- `test_run_source_pinned_seed` — seed `'Log Source' = 'X' | ...` → drill-down for other sources produces empty-but-valid responses; top source is X; no false cross-source expansion
- `test_run_unscoped_seed_sets_degraded_flag` — seed `*` → `seed.seed_filter_degraded: true`; drill-down queries omit the `and` prefix
- `test_run_invalid_entity_field_partial_not_fatal` — one entity-field query errors → source's `top_entities` for that field is `[]`; `source.errors` populated; `partial_reasons` includes `"entity_discovery_partial"`; investigation completes
- `test_run_budget_exceeded_mid_drill_down` — `BudgetExceededError` from the 3rd source's cluster → `partial_reasons == ["budget_exceeded"]`, accumulator's completed sources intact
- `test_run_per_source_exception_isolated` — one source's gather returns an `Exception` → that source's entry is a shell with `errors` populated, others complete, `partial_reasons` includes `"source_errors"`
- `test_run_all_timelines_drop_cross_source_null` — every per-source timeline query errors → `cross_source_timeline is None`; `partial_reasons` includes `"timeline_omitted"`; `anomalous_sources[*].timeline is None`
- `test_run_multi_reason_partial` — scenario triggering two partials at once; `partial_reasons` is the union
- `test_run_j1_note_and_probe_window_populated` — `ingestion_health.probe_window` matches the config; `note` text present and mentions the window-mismatch
- `test_run_stopped_sources_excluded_from_ranking` — J1 flags `stopped`; that source is absent from `anomalous_sources` even if it has a large delta; still visible in `ingestion_health.snapshot`
- `test_run_epoch_ms_timestamps_normalize` — engine returns epoch-ms ints for `Time`; report's `cross_source_timeline[*].time` is an ISO string

### 8.3 Handler tests (`tests/test_handlers.py::TestInvestigateIncident`)

- `test_routes_to_investigate_tool` — `handle_tool_call("investigate_incident", {...})` dispatches and returns the JSON-serialized report
- `test_budget_exceeded_returns_structured_payload` — consistent with other triage tools
- `test_default_time_range_is_valid_token` — handler default ∈ `TIME_RANGES`
- `test_missing_query_returns_structured_error`
- `test_bad_top_k_returns_structured_error` — negative, zero, >10, non-integer

### 8.4 Read-only guard

`KNOWN_READERS` in `tests/test_read_only_guard.py` gets `"investigate_incident"`. The existing AST walker catches if someone registers the handler without updating the set.

### 8.5 Live-probe discipline before test-writing

Per the J2 / A4 post-mortems, **every new query shape must be live-probed against the dogfood tenancy before its test fixture is written**. For A1, that means running each of the following at least once on `emdemo-logan` and capturing the real response shape:

- A2 breakout: `'Parse Failed' = 1 | stats count as n by 'Log Source'` current vs prior-window
- Per-source cluster: `'Parse Failed' = 1 and 'Log Source' = 'Kubernetes Kubelet Logs' | cluster | sort -Count | head 3`
- Per-source entity discovery (all 3 types)
- Per-source timeline: `... | fields Time, Severity, 'Original Log Content' | sort -Time | head 20`

Test fixtures pin the actual column shapes, types, and sample values captured from these probes.

## 9. Documentation changes

- **README** — "Investigation Toolkit" section gets an `### investigate_incident` entry with: example seed, the seed-filter limitation, the J1 freshness-snapshot caveat, the partial-response behavior
- **`docs/phase-2/specs/triage-toolkit.md` §A1** — updated to: (a) list `top_entities` instead of `changed_entities`; (b) reference this design doc for the full shape; (c) note the P0 seed-query limitation and deferrals; (d) correct the `partial_reasons` shape
- **Spec breadcrumb** — this design doc will link forward to the implementation plan (`docs/phase-2/plans/2026-04-22-a1-investigate-incident.md`) once `writing-plans` produces it

## 10. Out of scope for P0

- `alarm_ocid` seed (→ A6)
- `description` seed (no NL-to-query resolver)
- Full pipeline-aware seed-query parsing (quote-aware split only for P0; later stages ignored)
- Per-entity change-detection (`top_entities` is by count, not by delta-vs-prior-window)
- Deep A4 fan-out per discovered entity (spec's "collect top entities via A4 `pivot_on_entity`" is honored at discovery granularity only; callers can run A4 directly on any entity the report surfaces)
- Budget-introspection-based scheduling ("stop scheduling when tight") — relies on `BudgetExceededError` + bounded concurrency + partial semantics

## 11. Success criteria

- **Functional**: all tests in §8 pass; live-probe integration run completes without schema-fiction errors
- **Latency**: ≤20s p95 on the dogfood tenancy with a realistic seed (e.g., `'Parse Failed' = 1`). Expected typical: 10-15s
- **Query budget**: P0 cap is roughly 21 queries per invocation (1 seed + 1 J1 + 2 A2 + 2 J2 + 3 sources × 5 per-source queries [1 cluster + 3 entity + 1 timeline]). Upper bound; partial paths finish with fewer.
- **Partial behavior**: every test scenario in §8.2 that sets `partial: true` also produces a non-empty, consistent `partial_reasons` list; the report is still well-typed

## 12. After this doc

1. Commit this design doc on `feat/investigate-incident`
2. Invoke `superpowers:writing-plans` to produce `docs/phase-2/plans/2026-04-22-a1-investigate-incident.md`
3. Execute via `superpowers:subagent-driven-development` with TDD per task and the live-probe discipline mandate from §8.5
