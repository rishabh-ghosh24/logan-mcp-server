# Phase 2 — Follow-up Backlog

Centralized backlog for **explicit post-landing follow-ups and P0 deferrals** from Phase 2 work.

This is **not** the same thing as the feature backlog in:
- [roadmap.md](roadmap.md)
- [feature-catalog.md](feature-catalog.md)

Those files track larger feature lanes by phase (`P0`, `P1`, `P2`). This file tracks the smaller follow-up items that were intentionally deferred while shipping Phase 2 features.

## Scope

Include an item here only if at least one of these is true:
- it was explicitly deferred in a landed Phase 2 plan/spec/README section
- it came out of review on an active Phase 2 feature branch and we intentionally chose not to fix it in the shipping patch

Do not use this file for:
- already-tracked roadmap features (`A3`, `A5`, `G1`, etc.)
- speculative cleanup ideas that were never explicitly deferred
- resolved follow-ups

## Open Follow-ups

### Agent Guardrails

#### L1 — Read-only mode
Source: [2026-04-20-l1-read-only-flag.md](plans/2026-04-20-l1-read-only-flag.md)

- `L1-F1` — Per-tool read-only semantics instead of the current all-or-nothing denylist.
- `L1-F2` — Richer audit coverage for read-only rejections beyond the current handler-level logging.
- `L1-F3` — UI / agent-side affordance that advertises when the server is running in read-only mode.

#### H1 / N5 — Explain query + per-session budgets
Source: [2026-04-20-h1-n5-explain-and-budget.md](plans/2026-04-20-h1-n5-explain-and-budget.md)

- `H1N5-F1` — Feed H1 from J1’s future baseline store instead of probe-only logic.
- `H1N5-F2` — Cross-process budget sharing instead of one tracker per server process.
- `H1N5-F3` — Per-user budgets instead of session-scoped aggregation.
- `H1N5-F4` — Replace estimated bytes with actual OCI response bytes when Logan exposes them reliably.
- `H1N5-F5` — Add correct budget enforcement for `run_batch_queries` instead of the current `run_query`-only enforcement.

#### N2 — Suggested next query
Source: [2026-04-20-n2-suggested-next-query.md](plans/2026-04-20-n2-suggested-next-query.md)

- `N2-F1` — Semantic / ML-aware suggestion logic beyond the current shape-based heuristics.
- `N2-F2` — Suggestion support for `visualize` responses.
- `N2-F3` — Configurable heuristic thresholds instead of P0 hardcoded defaults.

#### N6 — Transcript export
Source: [2026-04-20-n6-transcript-export.md](plans/2026-04-20-n6-transcript-export.md)

- `N6-F1` — Completion / result-summary capture for non-guarded tools.
- `N6-F2` — Client-supplied session IDs instead of process-generated IDs only.
- `N6-F3` — Per-investigation session semantics instead of process-scoped debugging groups.
- `N6-F4` — Promotion-run audit coverage for `promote.py --promote-and-exit`.

#### N1 — Investigation recorder
Source: [reports-and-playbooks.md](specs/reports-and-playbooks.md), [feature-catalog.md](feature-catalog.md), and [2026-04-24-n1-investigation-recorder.md](plans/2026-04-24-n1-investigation-recorder.md)

- `N1-F1` — Replay recorded playbooks via `replay_investigation(playbook_id, params, dry_run)`.
- `N1-F2` — Auto-parameterization for time ranges, entities, sources, and other replay-safe fields.
- `N1-F3` — `capture_as` chaining so later replay steps can consume earlier step outputs.
- `N1-F4` — Optional `session_id` parameter on `record_investigation` once N6 supports client-supplied session ids.

### Triage Toolkit

#### A2 — `diff_time_windows`
Source: [2026-04-20-a2-diff-time-windows.md](plans/2026-04-20-a2-diff-time-windows.md)

- `A2-F1` — Shared Logan query parser / AST utility for reliable `by` extraction and future pipeline-aware parsing.
- `A2-F2` — Source-side field discovery when callers omit `dimensions` and the query has no `by` clause.
- `A2-F3` — Ratio / rate-normalized deltas when callers compare mismatched window lengths.
- `A2-F4` — Cross-dimension interaction analysis beyond the current flat dimension-tuple delta output.

#### A3 — `find_rare_events`
Source: live A3 probe on `feat/find-rare-events`

- `A3-F1` — Investigate OCI SDK/API single-group `rare` payloads that surface a null-only row (`[None, None, None]`) for source-scoped queries with `total_group_count=1`. The current parser aligns the response safely and A3 returns `rare_values: []`, but single-value / high-threshold cases cannot surface the actual value until the upstream payload exposes it.

#### J1 — `ingestion_health`
Source: [2026-04-22-j1-ingestion-health.md](plans/2026-04-22-j1-ingestion-health.md)

- `J1-F1` — Persistent per-source baseline store with background refresh.
- `J1-F2` — DROP classification (degraded volume without full stoppage).
- `J1-F3` — LAG classification (ingestion-time vs event-time skew).
- `J1-F4` — Per-entity freshness checks within a source.
- `J1-F5` — Cross-compartment sweeps instead of single-compartment-per-call behavior.

#### A1 — `investigate_incident`
Source: [2026-04-22-a1-investigate-incident.md](plans/2026-04-22-a1-investigate-incident.md)

- `A1-F1` — NL `description` seed support.
- `A1-F2` — Direct `alarm_ocid` entrypoint instead of query-only seeding.
- `A1-F3` — Full pipeline-aware seed parsing instead of the current quote-aware pre-pipe extraction.
- `A1-F4` — `ip` entity discovery in addition to `host`, `user`, and `request_id`.
- `A1-F5` — Per-entity change detection instead of count-only `top_entities`.
- `A1-F6` — Deeper A4 fan-out per discovered entity where it improves investigations.
- `A1-F7` — Budget-introspection scheduling / query-shaping instead of the current fixed orchestration shape.

#### A6 — `why_did_this_fire`
Source: [README.md](../README.md) and [2026-04-23-a6-why-did-this-fire.md](plans/2026-04-23-a6-why-did-this-fire.md)

- `A6-F1` — Support generic OCI Monitoring alarms beyond Logan-managed alarms.
- `A6-F2` — Explicit dashboard linkage so `dashboard_id` is not always `null`.
- `A6-F3` — Historical handoff into A1 once A1 supports absolute-window investigations.

#### A5 — `trace_request_id`
Source: current review on `feat/trace-request-id`

- `A5-F1` — Propagate incompleteness metadata (`partial`, `truncated_sources`, or equivalent) so callers can tell when the merged event stream is incomplete due to underlying pivot truncation or mid-probe budget limits.
- `A5-F2` — Further harden soft-miss detection by distinguishing generic invalid field-syntax parse errors from true unknown-field misses, preferring `exc.message` cleanly when present, and adding direct coverage for exception types that expose `.message`.
- `A5-F3` — Use timezone-aware timestamp parsing if Logan surfaces offset-based timestamps instead of the current lexicographic ISO-string sort.

#### A7 — `related_dashboards_and_searches`
Source: current review on `feat/related-dashboards-and-searches`

- `A7-F1` — Skip low-value saved-search detail fetches when the shortlist has enough positive-score candidates and the remaining zero-score entries are unlikely to improve ranking. Current behavior is safe and capped at 10 detail fetches; this is a latency / efficiency follow-up, not a correctness blocker.

## Notes From the Audit

Some earlier plan-doc follow-ups were already resolved later in Phase 2 and are therefore **not** tracked as open backlog here. Examples:
- A2’s “A4 sibling primitive” and “A1 consumes A2 + A4” follow-ups were satisfied once A4 and A1 shipped.
- J1’s “J2 sibling primitive” and “A1 integration” follow-ups were satisfied once J2 and A1 shipped.

Keep this file action-oriented: open items only.
