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
- **Config propagation**: expose `Settings.read_only: bool` via `config.py`; env parser warns on unrecognized values.
- **Tool guard**: single denylist + check in `handlers.handle_tool_call`, applied **before** the existing confirmation gate. Structured response shape: `{"status": "read_only_blocked", "tool": <name>, "error": <message>}`.
- **Side-effect suppression in allowed reads**: `list_log_sources`, `list_fields`, `list_compartments` currently auto-write to the shared tenancy-context YAML file. Under read-only, the reads still return data but the persistence side-effect is suppressed with `if not self.settings.read_only:` guards at each call site.

### What read-only blocks (P0)
- **OCI resource mutations:** `create_alert`, `update_alert`, `delete_alert`; `create_saved_search`, `update_saved_search`, `delete_saved_search`; `create_dashboard`, `add_dashboard_tile`, `delete_dashboard`.
- **Outbound notifications:** `send_to_slack`, `send_to_telegram`.
- **Shared/tenancy/secret state:** `set_compartment`, `set_namespace`, `update_tenancy_context`, `setup_confirmation_secret`.
- **Explicit user-state tool calls:** `save_learned_query`, `remember_preference`.
- **Incidental shared writes:** tenancy-context auto-capture from the three `list_*` paths above.

### What read-only does NOT block (intentional)
- **Reads:** all `run_*`, `list_*`, `validate_*`, `visualize`, `test_connection`, `find_compartment`, `get_*`.
- **`export_results`:** returns CSV/JSON text; no file is written in current code. Stays allowed.
- **Per-user incidental writes:** query log, result cache, per-user learned-query auto-save, preference usage tracking. These help the user's own tooling and are not reachable as tools an agent can weaponize. Future: if the set of incidental writes grows, revisit.

### Files
- Modify: `src/oci_logan_mcp/config.py` — add `Settings.read_only` + `OCI_LOGAN_MCP_READ_ONLY` env parse.
- Modify: `src/oci_logan_mcp/__main__.py` — `--read-only` CLI flag sets env var before `server_main()`.
- Create: `src/oci_logan_mcp/read_only_guard.py` — `MUTATING_TOOLS` frozenset, `ReadOnlyError`, `raise_if_read_only()`.
- Modify: `src/oci_logan_mcp/handlers.py` — guard check in `handle_tool_call` before confirmation gate; three `context_manager.update_*` call sites wrapped.
- Create: `tests/test_read_only_guard.py` — unit tests + AST drift test.
- Modify: `tests/test_handlers.py` — integration tests including tenancy-context suppression.
- Modify: `tests/test_config.py` — `read_only` default + env-override + unrecognized-value warn.

### Test outline
1. Test: `Settings.read_only=True` → every mutating tool returns `read_only_blocked` without reaching OCI.
2. Test: `Settings.read_only=False` → existing behavior unchanged.
3. Test: `--read-only` CLI flag and `OCI_LOGAN_MCP_READ_ONLY=1` env var both set `Settings.read_only=True`.
4. Test: unrecognized env value (e.g. `yez`) logs a warning and leaves `read_only` at default.
5. Test: read tool (`list_saved_searches`) is unaffected under read-only.
6. Test: `list_log_sources` / `list_fields` / `list_compartments` still return data under read-only, but `context_manager.update_*` is NOT called.
7. AST drift test: every registered handler is either in `MUTATING_TOOLS` or in a known-readers allowlist.

### Dependencies
None — ships first.

### Implementation plan
See [../plans/2026-04-20-l1-read-only-flag.md](../plans/2026-04-20-l1-read-only-flag.md).

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

### Estimation strategy (P0 — self-contained, probe-based)
OCI Log Analytics doesn't expose a clean "explain" API. H1 is self-contained in P0; it does not depend on J1.

1. **Probe-based volume estimate** — for each source matched by the query's `Log Source = ...` filters, issue a cheap count-last-hour probe to approximate bytes/hour, then scale linearly to the query's time range: `bytes ≈ sum(probe_bytes_per_hour[source] * hours_in_range)`. Cache probe results for `probe_ttl_seconds` (default 900) to avoid re-probing on every explain call.
2. **Filter selectivity discount** — apply a simple discount factor if query has `WHERE`/field filters (start with 0.2× as a conservative default; refine later from observed queries).
3. **Cost** — bytes × published OCI Log Analytics per-GB-scanned rate (from config).
4. **ETA** — linear model: `eta_seconds = bytes / throughput_bytes_per_second` where throughput starts as a conservative constant (e.g., 50 MB/s) and is refined per-tenancy over time.
5. **Confidence** — `medium` when probe succeeds; `low` when the probe returns no data or errors out. No `high` in P0 (reserved for the future J1-backed baseline).

> **P1 upgrade path:** when J1 ships with a persistent baseline store (see [../specs/triage-toolkit.md](triage-toolkit.md)), H1 can read from it instead of probing for each explain call, gaining `confidence=high` and avoiding the probe cost. That's a P1 refinement — it does not block H1 P0.

### Files
- Create: `src/oci_logan_mcp/query_estimator.py` — estimator class + heuristics.
- Modify: `src/oci_logan_mcp/tools.py` — register `explain_query` tool.
- Modify: `src/oci_logan_mcp/query_engine.py` — call estimator before query; embed result in response.
- Modify: `src/oci_logan_mcp/config.py` — add `cost_per_gb_usd`, `eta_throughput_mbps`, `eta_high_threshold_seconds` (default 60), `probe_ttl_seconds` (default 900).
- Create: `tests/test_query_estimator.py`.

### Test outline
1. Test: single-source short range with a working probe → plausible estimate with `confidence=medium`.
2. Test: unknown source or probe error → `confidence=low` and still returns non-negative estimates.
3. Test: large range scales bytes linearly from the probe result.
4. Test: `run_query` response includes estimate fields.
5. Test: estimate never throws — estimator failure becomes `confidence=low`, not an error.
6. Test: cached probe within TTL is reused on a second `explain_query` for the same source.

### Dependencies
- None. H1 is self-contained in P0.
- **Later upgrade (P1):** J1's baseline store replaces the probe path when available.

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
Export the tool-call chain recorded in the audit log as JSONL for audit, debugging, and future NL-to-query training (K3 in P1).

### Current state (baseline)
Today's `audit.py` records tool calls **only** for the confirmation-gated operations and for secret-management. Ordinary `run_query` / `list_*` / `visualize` calls are not captured, and there is no `session_id` on any entry. N6 P0 must fix both of those gaps first; the "export" part is easy once they exist.

### Scope (P0)
- **Session identity (process-scoped).** `AuditLogger` accepts an optional `session_id` at construction. `server.py` passes a `uuid.uuid4().hex` at boot. `__main__.py`'s promotion path passes a literal `"promote-run"` so promotion entries stay distinguishable. Every audit entry carries the id. One id per server process — long-lived servers aggregate many logical investigations under one id; that is an accepted P0 limitation.
- **Per-call `invoked` event.** `handlers.handle_tool_call` writes a single `invoked` audit event for every tool call, right after `user_id` is resolved and before the read-only / confirmation gates. Captures: `session_id, timestamp, user, pid, tool, args (sanitized), outcome="invoked"`. Existing confirmation-gate events (`executed`, `execution_failed`, etc.) stay in place and provide completion data for guarded tools.
- **Export tool.** `export_transcript(session_id: str = "current", include_results: bool = True, redact: bool = False) -> {path: str, event_count: int}`. Writes to `config.transcript_dir` (default `~/.oci-logan-mcp/transcripts/`). `"current"` resolves to the process-scoped id — **documented as a debugging grouping, not an investigation boundary**. Callers that need a sharper boundary must pass the explicit id they recorded at record-time (see N1 P0 contract).
- **Schema (per JSONL line):** required — `timestamp, session_id, user, pid, tool, args, outcome`. Optional — `result_preview, duration_ms, error`. Optional fields are populated only when the confirmation gate emits `executed`/`execution_failed`; P0 does not add completion capture for non-guarded tools.
- **Redaction.** `redact=True` applies existing `sanitize.py` redaction (stretch G1 will extend this).

### Deferred to P1
- `completed` / result-summary capture for non-guarded tools.
- Client-supplied session ids (accept from tool args or MCP request metadata, fall back to process id).
- Per-investigation session semantics — requires explicit `start_investigation()` / `end_investigation()` or MCP-session plumbing.

### Files
- Modify: `src/oci_logan_mcp/audit.py` — add `session_id` constructor arg + field on every entry; add JSONL-export helper.
- Modify: `src/oci_logan_mcp/server.py:219` — pass `session_id=uuid.uuid4().hex` at construction.
- Modify: `src/oci_logan_mcp/__main__.py:114` — pass `session_id="promote-run"` at construction.
- Modify: `src/oci_logan_mcp/handlers.py` — `invoked` event at top of `handle_tool_call`.
- Modify: `src/oci_logan_mcp/tools.py` — register `export_transcript`.
- Modify: `src/oci_logan_mcp/config.py` — add `transcript_dir`.
- Create: `tests/test_transcript_export.py`.
- Modify: `tests/test_audit.py` — cover `session_id` presence on every entry.

### Test outline
1. Test: every call to `handlers.handle_tool_call` produces exactly one `invoked` audit entry with the configured `session_id`.
2. Test: confirmation-gated tools still produce their existing `executed` / `execution_failed` entries in addition to the `invoked` entry.
3. Test: after N tool calls, `export_transcript` writes N+M JSONL lines (N invoked + M completion events for guarded calls) for the current session.
4. Test: `session_id="other-id"` filter returns only that session's events, none of the current session's.
5. Test: `redact=True` masks known PII patterns in args/results.
6. Test: `include_results=False` omits `result_preview` but retains metadata.
7. Test: promotion path uses `session_id="promote-run"` and appears under that id.

### Dependencies
- Extends existing audit log; no new capture subsystem.
- N1 (in reports-and-playbooks) references the same session id for transcript linkage.

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
