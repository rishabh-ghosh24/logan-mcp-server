# Spec — `feat/agent-guardrails`

**Branch:** `feat/agent-guardrails` (off `main`)
**Theme:** T2 — Trustworthy Autonomous Agent
**Features:** L1, H1, N5, N2, N6
**Ship order:** L1 → N2 → H1 + N5 (bundled) → N6
**Companion docs:** [../feature-catalog.md](../feature-catalog.md) · [../roadmap.md](../roadmap.md)

> **For agentic workers:** This spec is design-level, not step-by-step TDD. When execution starts, use `superpowers:writing-plans` inside this branch to produce a true TDD plan per feature before writing code.

---

## Goal

Make every tool call on the server **predictable, safe, and observable** for an LLM caller. After this branch merges, an agent cannot: run unknown-cost queries without warning, run away in an infinite loop, mutate anything in read-only deployments, or leave an audit-less session.

## Acceptance criteria for the branch

- Every `run_query` response carries `estimated_bytes`, `estimated_cost_usd`, `estimated_eta_seconds`, and `next_steps[]`.
- `--read-only` flag at server startup disables every mutating tool; tests assert that each mutating tool returns a clean error when flag is set.
- Per-session query budget enforced; exceeding the budget returns a structured error (no OCI call made).
- Session transcripts can be exported as JSONL via a tool call.
- All existing tests still pass.

---

## L1 — `--read-only` mode flag

### Purpose
Single binary-level startup flag that disables every mutating tool. Unlocks enterprise/audit deployments and safe agent experimentation.

### Scope
- **CLI flag** on server startup: `--read-only` (also accept `OCI_LOGAN_MCP_READ_ONLY=1` env var).
- **Config propagation**: expose `config.read_only: bool` read across the codebase via the existing `config.py` surface.
- **Tool guard**: decorator or middleware that rejects mutating tools with a structured error `ReadOnlyError: this deployment is read-only; tool '<name>' is disabled`.

### Mutating tools to guard (from current codebase)
- `create_alert`, `delete_alert`, `update_alert`
- `create_dashboard`, `delete_dashboard`, `add_dashboard_tile`
- `create_saved_search`, `update_saved_search`, `delete_saved_search`
- `save_learned_query`, `delete_learned_query`
- `remember_preference`
- `send_to_slack`, `send_to_telegram`
- `export_results` (writes to disk — treat as mutating)
- `set_compartment`, `set_namespace`, `update_tenancy_context` (modify server state)
- `setup_confirmation_secret`
- `run_saved_search` — **non-mutating** (still allowed)

### Files
- Modify: `src/oci_logan_mcp/config.py` — add `read_only` field.
- Modify: `src/oci_logan_mcp/__main__.py` or `server.py` — parse flag/env into config.
- Create: `src/oci_logan_mcp/read_only_guard.py` — decorator `@requires_write` and helper `is_read_only()`.
- Modify: `src/oci_logan_mcp/tools.py` and each `*_service.py` — apply guard to mutating tools.
- Create: `tests/test_read_only_guard.py` — per-tool assertion.

### Test outline
1. Test: `config.read_only=True` → every mutating tool returns `ReadOnlyError` without calling OCI.
2. Test: `config.read_only=False` → existing behavior unchanged (existing tests still pass).
3. Test: CLI `--read-only` and env var both set `config.read_only=True`.
4. Test: read-only tool (e.g., `run_query`) is unaffected.

### Dependencies
None — ships first.

---

## N2 — Suggested next query

### Purpose
After every `run_query` (and other result-producing tools), attach a `next_steps[]` list of pivot suggestions so agents learn what to try next instead of blind-looping.

### Scope
- Returns contextual next-step hints based on the result shape. Heuristics, not ML.
- **Non-invasive:** existing `run_query` return shape gains one extra field `next_steps: list[NextStep]`. Existing callers ignore extra fields gracefully.

### Suggested heuristics (minimum viable set)
1. **If query returned error rows** → suggest `group by <status_field>` and `pivot_on_entity(<first_entity_field>)`.
2. **If query aggregates over time and shows a spike** → suggest `diff_time_windows` for the spike window vs. baseline (depends on A2 — ship a stub that points at it).
3. **If query returned rows with a request-id/trace-id field** → suggest `trace_request_id` (stub for stretch A5).
4. **If query returned >N rows** → suggest a narrower time window.
5. **If query returned 0 rows** → suggest `validate_query` or loosening filters.

### Files
- Create: `src/oci_logan_mcp/next_steps.py` — heuristic engine with typed `NextStep` dataclass (`tool_name`, `suggested_args`, `reason`).
- Modify: `src/oci_logan_mcp/query_engine.py` — after query execution, call `next_steps.suggest(query, result)`.
- Modify: `src/oci_logan_mcp/tools.py` — ensure `run_query` and `run_batch_queries` propagate the field.
- Create: `tests/test_next_steps.py` — one test per heuristic.

### Test outline
1. Test: error rows → includes a `pivot_on_entity` hint.
2. Test: time-bucket spike → includes a diff-windows hint.
3. Test: large result → includes narrower-window hint.
4. Test: empty result → includes validate-query hint.
5. Test: `next_steps` field always present (empty list allowed).

### Dependencies
- Pure heuristics — ships before H1/N5.
- Soft dependencies on A2/A4/A5 tool names (we reference them before they exist — ship as "stubbed hints"; when those tools land they become live).

---

## H1 — `explain_query` (cost + ETA)

### Purpose
Return **estimated bytes scanned, estimated cost, and estimated runtime** for a query *before* it runs. Agents and users gate expensive queries on this.

### Scope
- New tool: `explain_query(query: str, time_range: ...) -> QueryEstimate`.
- Return shape: `{estimated_bytes: int, estimated_rows: int | None, estimated_cost_usd: float | None, estimated_eta_seconds: float, confidence: "high" | "medium" | "low", rationale: str}`.
- Also **embed** the estimate in `run_query` responses by default, so agents see it without a separate call.

### Estimation strategy
OCI Log Analytics doesn't expose a clean "explain" API. We use a layered heuristic:

1. **Time-range × source volume baseline** — reuse J1's baseline volumes (per source, bytes/hour) to estimate bytes scanned: `bytes = sum(baseline_bytes_per_hour[source] * hours_in_range)` for sources matched by the query's `Log Source = ...` filters.
2. **Filter selectivity discount** — apply a simple discount factor if query has `WHERE`/field filters (start with 0.2× as a conservative default; refine later from observed queries).
3. **Cost** — bytes × published OCI Log Analytics per-GB-scanned rate (from config).
4. **ETA** — linear model: `eta_seconds = bytes / throughput_bytes_per_second` where throughput starts as a conservative constant (e.g., 50 MB/s) and is refined per-tenancy over time.
5. **Confidence** — `high` when baseline data is fresh (<24h), `medium` when stale, `low` when source has no baseline.

> Note: J1 creates the baseline store. H1 reads it. If H1 ships before J1, seed with a simple count-last-hour probe as baseline. Order in this branch: ship H1 with the probe-based fallback; J1 (in `feat/triage-toolkit`) upgrades it.

### Files
- Create: `src/oci_logan_mcp/query_estimator.py` — estimator class + heuristics.
- Modify: `src/oci_logan_mcp/tools.py` — register `explain_query` tool.
- Modify: `src/oci_logan_mcp/query_engine.py` — call estimator before query; embed result in response.
- Modify: `src/oci_logan_mcp/config.py` — add `cost_per_gb_usd`, `eta_throughput_mbps`, `eta_high_threshold_seconds` (default 60).
- Create: `tests/test_query_estimator.py`.

### Test outline
1. Test: single-source short range → plausible estimate with `confidence=high` when baseline exists.
2. Test: unknown source → `confidence=low` and still returns non-negative estimates.
3. Test: large range scales bytes linearly.
4. Test: `run_query` response includes estimate fields.
5. Test: estimate never throws — estimator failure becomes `confidence=low`, not an error.

### Dependencies
- Soft: J1 (baseline store). Ship H1 with probe-fallback; J1 upgrades it later.

---

## N5 — Query budget per session

### Purpose
Hard cap on per-session query volume and cost, preventing runaway agent loops.

### Scope
- New session-scoped budget tracker: `BudgetTracker` keyed by session id (or user id if no session concept).
- Configurable limits: `max_queries_per_session` (default 100), `max_bytes_per_session` (default 10 GiB), `max_cost_usd_per_session` (default 5.00).
- On each `run_query`: before executing, read H1 estimate; if *current usage + estimate* exceeds any limit → return `BudgetExceededError` without calling OCI.
- On success: increment counters with actual bytes (if Logan returns them) or estimate.
- New tool: `get_session_budget()` returns current usage + remaining.

### Bypass
- If `config.read_only=True` **and** query doesn't mutate anything: still enforce budget (cost protection applies in read-only too).
- Explicit override: `run_query(..., budget_override=True)` requires confirmation secret (reuse existing two-factor pattern).

### Files
- Create: `src/oci_logan_mcp/budget_tracker.py` — per-session counter store (in-memory; no persistence needed in P0).
- Modify: `src/oci_logan_mcp/query_engine.py` — pre-flight check + post-flight increment.
- Modify: `src/oci_logan_mcp/config.py` — add the three limit fields.
- Modify: `src/oci_logan_mcp/tools.py` — register `get_session_budget`.
- Create: `tests/test_budget_tracker.py`.

### Test outline
1. Test: first query → usage recorded, remaining decreases.
2. Test: query that would exceed bytes budget → `BudgetExceededError`, no OCI call.
3. Test: hitting query count limit blocks further runs.
4. Test: `budget_override` with valid confirmation secret permits the run.
5. Test: `get_session_budget` returns accurate snapshot.

### Dependencies
- **Hard dependency on H1** — needs the estimate to do pre-flight checks.
- Ship H1 and N5 in the same PR.

---

## N6 — Transcript export

### Purpose
Export a session's full tool-call chain as JSONL for audit, debugging, and future NL-to-query training (K3 in P1).

### Scope
- New tool: `export_transcript(session_id: str | None = "current", include_results: bool = True, redact: bool = False) -> {path: str, event_count: int}`.
- Writes to a configurable directory (`config.transcript_dir`, default `~/.oci-logan-mcp/transcripts/`).
- Each line is a JSON object: `{ts, session_id, tool, args, result_preview, duration_ms, error}`.
- `redact=True` applies existing `sanitize.py` redaction (stretch G1 will extend this).

### Source of truth
- Existing `audit.py` already records tool calls. Extend its schema if needed and export from there. **Do not duplicate capture.**

### Files
- Modify: `src/oci_logan_mcp/audit.py` — ensure it captures enough fields; add JSONL-export helper.
- Modify: `src/oci_logan_mcp/tools.py` — register `export_transcript`.
- Modify: `src/oci_logan_mcp/config.py` — add `transcript_dir`.
- Create: `tests/test_transcript_export.py`.

### Test outline
1. Test: after N tool calls, `export_transcript` writes N JSONL lines.
2. Test: `redact=True` masks known PII patterns in args/results.
3. Test: `session_id` filter returns only that session's events.
4. Test: `include_results=False` omits result bodies but retains metadata.

### Dependencies
- Piggybacks on existing audit log.
- N1 (in reports-and-playbooks) will reuse the same plumbing.

---

## Branch merge criteria

- All tests green: `pytest tests/`.
- New tests for each feature exist and pass.
- `--read-only` mode verified manually against all mutating tools.
- `run_query` response on a known small query shows `estimated_*` and `next_steps[]` fields populated.
- `get_session_budget` returns accurate counters after a test session.
- `export_transcript` produces valid JSONL that round-trips through `jq .`.
- README updated to document `--read-only` flag, budget limits, estimate fields.
- PR description includes the "acceptance criteria for the branch" checklist marked done.
