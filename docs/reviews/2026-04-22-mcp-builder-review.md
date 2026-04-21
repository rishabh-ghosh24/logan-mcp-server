# MCP Builder Review — 2026-04-22

Review of the logan-mcp-server `main` branch against the `mcp-builder` skill
guidelines and a deeper correctness/safety audit. Two reviewers: Claude (full
checklist pass) and Codex (prioritization pass). This document records what
was shipped now and what is deferred, with reasoning so future-us
doesn't have to re-derive the tradeoffs.

## Approach

Codex's framing was applied: **do-now items must prevent wrong behavior**;
everything else ("protocol polish, interoperability, future maintainability")
is deferred. The goal was a short hardening pass, not a cleanup wave.

All three fixes were implemented TDD-style (RED → GREEN), run against the
full suite (832 tests, all passing).

---

## Shipped — 2026-04-22

### 1. `create_*` tools now enforce two-factor confirmation

**Problem.** `create_alert`, `create_saved_search`, and `create_dashboard`
tool descriptions said *"APPROVAL REQUIRED: This tool creates OCI resources.
Confirm with the user before invoking."* — but that was purely advisory text
to the LLM. `confirmation.py:GUARDED_TOOLS` contained only `delete_*`,
`update_*`, and `add_dashboard_tile`. A misaligned client, a
prompt-injected agent, or a buggy wrapper could skip the approval step
entirely and create OCI resources without the user's explicit secret.

This is worse than no promise at all: downstream systems trusted the
description's safety claim, but the server didn't back it up.

**Fix.**
- `src/oci_logan_mcp/confirmation.py` — added the three creates to
  `GUARDED_TOOLS` and extended `_SUMMARY_KEYS` with their summary fields.
- `src/oci_logan_mcp/tools.py` — added `destructive: True`,
  `confirmation_token` / `confirmation_secret` input params, and rewrote
  each description to use the standard "TWO-FACTOR CONFIRMATION REQUIRED"
  wording.
- `tests/test_confirmation.py::test_create_tools_are_guarded` — new
  regression guard asserting all three creates are guarded.
- `tests/test_handlers.py::test_create_alert_is_guarded` — replaces the
  old `test_create_alert_not_guarded` which pinned the buggy behavior.
- `tests/test_tools.py` — the existing drift-catchers
  (`test_guarded_tools_have_confirmation_params`, `..._have_destructive_flag`,
  `..._description_mentions_confirmation`) now enforce the three creates
  automatically through the shared `GUARDED_TOOLS` frozenset.

**Why this is safe to enable now.** The existing two-factor flow was
already battle-tested for `update_*` and `delete_*` — we are extending
coverage, not introducing a new mechanism. Users who already have a
confirmation secret configured need no action; users without one were
already locked out of destructive ops.

### 2. `next_steps` no longer points to a nonexistent tool

**Problem.** `next_steps.py::_h_request_id` emitted
`NextStep(tool_name="trace_request_id", ...)` whenever a query result
contained a column matching the `request_id` / `trace_id` / `correlation_id`
regex. No tool by that name exists in `handlers.py::handle_tool_call`.
The LLM would either fail to call anything, hallucinate arguments, or
invent the tool from the hint — all waste.

**Fix.**
- `src/oci_logan_mcp/next_steps.py` — rewrote the hint to emit
  `NextStep(tool_name="pivot_on_entity", ...)` with
  `entity_type="request_id"`, `entity_value=<sample>`,
  `field_name=<column_name>`, and a default 1-hour time window. The
  existing `pivot_on_entity` tool already supports `entity_type` values
  including `"request_id"` and takes a `field_name` for custom fields.
- `tests/test_next_steps.py` — renamed and expanded the three request-id
  tests to assert the new shape and explicitly check that
  `"trace_request_id"` never appears.

### 3. `run_saved_search` requires `name` or `id`

**Problem.** Both fields were optional and unconstrained — the LLM could
call the tool with no arguments and receive a plain-text `"Saved search
not found"` response, which isn't structured and gives no recovery path.

**Fix.**
- `src/oci_logan_mcp/tools.py` — added an `anyOf` constraint on the
  input schema requiring at least one of `name` or `id`, and clarified
  the description to direct the LLM to `list_saved_searches` first.
- `src/oci_logan_mcp/handlers.py::_run_saved_search` — replaced plain-text
  error returns with structured JSON (`status`, `error_code`, `message`,
  `next_step`) for both the missing-argument and not-found cases.
- `tests/test_tools.py::test_run_saved_search_requires_name_or_id` — new
  schema regression test.

---

## Deferred — Protocol polish / feature work

Grouped by source (Claude's first pass, Claude's deeper pass, Codex's
review). None of these cause wrong behavior today; they're interoperability
improvements, UX polish, or net-new features.

### From the mcp-builder checklist

- **Tool naming with service prefix** (e.g. `logan_run_query`,
  `logan_list_fields`). Prevents collisions when the server is used
  alongside other MCP servers. Breaking change — batch for a v2.
- **Full MCP tool annotations**: `readOnlyHint`, `idempotentHint`,
  `openWorldHint` across all tools. Currently only `destructiveHint` is
  set (via the `destructive: True` metadata flag). Hints-only, no behavior
  change.
- **`response_format: "json" | "markdown"` parameter.** Lets the LLM
  request human-readable vs machine-readable output per call.
- **`outputSchema` / `structuredContent`.** Modern MCP SDK feature for
  structured tool responses. Current server returns everything as
  `TextContent(type="text", text=json.dumps(...))`.
- **Pagination on `list_*` tools.** `list_log_sources`, `list_fields`,
  `list_entities`, `list_log_groups`, `list_saved_searches`, `list_parsers`,
  `list_labels` lack `limit` / `offset` and `has_more` / `total_count`
  metadata. Matters more as tenancies grow.
- **Weak descriptions on utility tools.** `list_parsers`, `list_labels`,
  `export_results` have terse descriptions that don't guide the LLM on
  when to reach for them. Compare to `diff_time_windows` which tells the
  LLM exactly why it exists.
- **Eval suite.** No XML eval file exists. Phase 4 of the mcp-builder
  skill recommends ~10 realistic Q/A pairs for regression testing. Still
  the single highest-ROI infrastructure investment — the thing that tells
  you whether the *next* feature you add breaks the last one. Not urgent.

### From the deeper correctness pass

- **`send_to_slack` / `send_to_telegram` are not in `GUARDED_TOOLS`.**
  They can exfiltrate query results to pre-configured external channels.
  Deferred per Codex's analysis: their descriptions make no safety promise
  (unlike `create_*`), they're already blocked by `--read-only`, and the
  blast radius is bounded by pre-configured webhooks. Re-evaluate if this
  server is ever exposed to untrusted input or multi-user contexts.
- **No OCID format validation.** Fields like `compartment_id`,
  `destination_topic_id`, `alert_id`, `saved_search_id`, `dashboard_id`
  are typed as bare strings. A `pattern` on each input schema enforcing
  the `ocid1.{type}.oc1.{region}.{hash}` shape would short-circuit bad
  LLM-generated OCIDs before the OCI SDK call.
- **No `dry_run` mode on creates.** A `dry_run: true` flag on `create_*`
  that returns the full resource spec without actually creating it would
  give a cleaner "render → confirm → execute" flow than the token/secret
  dance. Complementary to the confirmation gate, not a replacement.
- **Error responses aren't consistently structured.** The catch-all at
  `handlers.py::handle_tool_call` returns `f"Error executing {name}:
  {str(e)}"` as plain text, exposing raw exception strings. Known error
  classes (OCI `ServiceError`, auth failures, etc.) should map to
  structured `{status, error_code, message, next_step}` responses —
  similar to the run_saved_search fix but for all handlers.
- **MCP Prompts not exposed.** The protocol supports server-defined,
  templated prompt workflows (e.g. `/logan-triage-host <hostname>`,
  `/logan-recent-errors`). `docs/phase-2/specs/triage-toolkit.md` and
  `reports-and-playbooks.md` already contain the content. Probably the
  single highest-value *new feature* addition from this review.
- **MCP Sampling not used.** Servers can ask the LLM to generate
  content — cleaner factoring than loading all query syntax into tool
  descriptions. Future-facing.
- **Per-tool cost/latency not consistently reported.** `BudgetTracker`
  and `get_session_budget` exist, but individual tool results don't
  uniformly return `elapsed_ms` / `bytes_scanned`. Adding those to every
  query response lets the LLM adapt ("that was slow — narrow the window").

---

## Test suite status after fixes

```
832 passed, 53 warnings in ~12s
```

The 53 warnings are all the same preexisting `datetime.utcnow()`
deprecation in `context_manager.py:87` — unrelated to this change.

## Files touched

Production:
- `src/oci_logan_mcp/confirmation.py` — `GUARDED_TOOLS` + `_SUMMARY_KEYS`
- `src/oci_logan_mcp/tools.py` — three `create_*` schemas + `run_saved_search`
- `src/oci_logan_mcp/handlers.py` — `_run_saved_search` structured errors
- `src/oci_logan_mcp/next_steps.py` — `_h_request_id` hint target

Tests:
- `tests/test_confirmation.py` — new `test_create_tools_are_guarded`
- `tests/test_tools.py` — updated `test_guarded_tools_have_destructive_flag`,
  new `test_run_saved_search_requires_name_or_id`
- `tests/test_next_steps.py` — renamed and expanded request_id tests
- `tests/test_handlers.py` — flipped `test_create_alert_not_guarded` →
  `test_create_alert_is_guarded`
