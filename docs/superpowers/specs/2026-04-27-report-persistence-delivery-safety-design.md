# Report Persistence, Delivery By ID, And Destructive-Action Safety Design

**Date:** 2026-04-27
**Status:** Draft for review
**Scope:** Logan MCP report artifacts, report delivery by `report_id`, and 2FA hardening for destructive actions

---

## Goal

Make generated incident reports durable and retrievable, allow delivery tools to use a saved `report_id`, and tighten destructive-operation safeguards so accidental deletes or in-place destructive updates require the user's confirmation secret.

This closes the gap seen in the live demo:

1. `generate_incident_report` returned a `report_id`, but there was no stored report behind that ID.
2. `deliver_report` could only accept inline Markdown, so the assistant had to keep the generated report body in context.
3. Delivery did not happen unless the assistant explicitly called the next tool, which is correct, but the product surface did not make manual read/download easy.
4. Some destructive local-state tools were read-only blocked but not protected by the two-factor confirmation gate.

## Non-Goals

- Do not add a one-shot `investigate_and_deliver_report` orchestration tool. Delivery must remain explicit and opt-in.
- Do not make `deliver_report` 2FA-gated in this change. It sends outbound notifications and remains read-only blocked, but it should not require the confirmation secret unless the user later chooses a stricter policy.
- Do not reintroduce `delete_learned_query`. It is not a registered MCP tool today. If it returns later, the drift tests in this design should require it to be guarded.
- Do not implement automatic report retention or pruning in this first change. Retention is named below and enabled by metadata, but deletion policy is deferred.

## Current State

- `ReportGenerator.generate()` creates a random `report_id`, returns Markdown/optional HTML, and returns `artifacts: []`.
- `ReportDeliveryService._validate_report()` requires `report.markdown`; `report.report_id` is rejected.
- `ReportDeliveryConfig.artifact_dir` already exists and defaults to `~/.oci-logan-mcp/reports`.
- `deliver_report` supports channels `telegram`, `email`, and `slack`; ONS delivery is the `email` channel.
- `read_only_guard.MUTATING_TOOLS` blocks mutating tools when read-only mode is enabled.
- `confirmation.GUARDED_TOOLS` protects the main OCI-impacting create/update/delete tools, but not every destructive local-state tool.

## Design Summary

Implement three small tracks.

### 1. Report Persistence

Add `ReportStore` as the persistence boundary for generated reports.

Storage uses the existing settings-driven base path, but does not mix saved report
records with delivery PDFs. Delivery PDFs stay as flat files in
`settings.report_delivery.artifact_dir`; saved report records live under a
dedicated `store/` namespace:

```text
settings.report_delivery.artifact_dir/
  report-20260427T145325Z-a1b2c3d4.pdf
  store/
    rpt_<32 hex>/
      report.md
      report.html          # only when generated
      metadata.json
```

`generate_incident_report` persists the report before returning. Its response keeps the existing fields and populates `artifacts` with file paths:

```json
{
  "report_id": "rpt_c37cade747674f64ba06eb94df2f1dab",
  "markdown": "# Incident Report\n...",
  "html": null,
  "metadata": {
    "generated_at": "2026-04-27T14:53:25.363412+00:00",
    "title": "Incident Report",
    "source_type": "investigation",
    "summary_length": "standard",
    "included_sections": ["executive_summary", "timeline"],
    "word_count": 298
  },
  "artifacts": [
    {
      "name": "markdown",
      "type": "markdown",
      "path": "/home/opc/.oci-logan-mcp/reports/store/rpt_c37cade747674f64ba06eb94df2f1dab/report.md"
    },
    {
      "name": "metadata",
      "type": "json",
      "path": "/home/opc/.oci-logan-mcp/reports/store/rpt_c37cade747674f64ba06eb94df2f1dab/metadata.json"
    }
  ]
}
```

`ReportStore` validates report IDs with:

```text
^rpt_[0-9a-f]{32}$
```

before any path construction. Invalid IDs return a structured validation error. This prevents path traversal and accidental lookup of unrelated files.

Writes must be atomic. `ReportStore` writes `report.md`, optional `report.html`,
and `metadata.json` through temporary files in the destination directory and
renames them into place. `metadata.json` is written last; list operations only
include entries with readable metadata.

### 2. Manual Read/Download And Delivery By ID

Add:

```python
get_incident_report(report_id: str) -> {
  report_id: str,
  markdown: str,
  markdown_path: str,
  html: str | None,
  html_path: str | None,
  metadata: dict,
  metadata_path: str,
}
```

The tool supports both needs from the demo:

- in-conversation reading through returned `markdown`
- manual filesystem download through returned paths

Add:

```python
list_incident_reports(limit: int = 20) -> {
  reports: list[{
    report_id: str,
    generated_at: str,
    title: str,
    summary_length: str,
    word_count: int,
    markdown_path: str,
    html_path: str | None
  }],
  warnings: {
    corrupt_count: int
  }
}
```

Update `deliver_report` so the report input accepts either inline Markdown or a saved ID:

```json
{
  "report": {
    "report_id": "rpt_c37cade747674f64ba06eb94df2f1dab"
  },
  "channels": ["email"],
  "format": "markdown",
  "title": "24-hour failures and issues report"
}
```

Delivery remains explicit. The assistant/client must ask the user before calling `deliver_report`.

If both `report.markdown` and `report.report_id` are present, the call fails
with `conflicting_report_inputs`. Silent precedence is unsafe because it can
send stale inline content when the caller intended to deliver the stored report.

### 3. Destructive-Action 2FA Hardening

Clarify the invariant:

> Any registered MCP tool whose direct effect deletes persisted state or updates existing persisted/OCI resources in place must require two-factor confirmation, unless it is explicitly exempted with a named reason.

Add `delete_playbook` to `GUARDED_TOOLS`.

Add a named exemption map next to `GUARDED_TOOLS`, for example:

```python
NON_DESTRUCTIVE_MUTATION_EXEMPTIONS = {
    "save_learned_query": "Additive user learning state; overwrite requires force/rename collision flow.",
    "remember_preference": "Additive preference signal, not destructive deletion.",
    "record_investigation": "Creates a new local playbook record with a fresh pb_ UUID.",
    "setup_confirmation_secret": "Bootstraps the confirmation secret; protected by its own validation and no overwrite.",
    "set_compartment": "Changes current context only.",
    "set_namespace": "Changes current context only.",
    "update_tenancy_context": "Curational metadata update; no deletion of managed resources.",
    "deliver_report": "Outbound delivery is opt-in and read-only blocked, but not secret-gated.",
    "send_to_slack": "Outbound notification is opt-in and read-only blocked, but not secret-gated.",
    "send_to_telegram": "Outbound notification is opt-in and read-only blocked, but not secret-gated.",
}
```

The exact wording can be shorter in code, but every exemption should be reviewable in one place.

Add drift tests:

- every tool in `MUTATING_TOOLS` is either in `GUARDED_TOOLS` or in `NON_DESTRUCTIVE_MUTATION_EXEMPTIONS`
- every registered tool in `handlers.py` whose name starts with `delete_` is in `MUTATING_TOOLS`
- every registered `delete_*` tool is in `GUARDED_TOOLS`, unless explicitly exempted
- `delete_playbook` returns `confirmation_required` on first call
- a valid token + current secret executes `delete_playbook`
- wrong secret, changed args, token reuse, and missing secret fail closed
- `setup_confirmation_secret` returns `already_configured` when a valid secret already exists

## Tool Behavior

### `generate_incident_report`

Input gains one optional field:

```python
title: str | None = None
```

If omitted, `metadata.title` defaults to `"Incident Report"`. Callers should pass
a useful title, such as `"24-hour failures and issues report"`, when the report
will be listed or delivered later.

Output changes:

- `artifacts` is no longer always empty.
- `metadata` gains path-friendly fields if useful, but existing metadata keys must remain stable.
- `metadata.title` is set from the optional `title` input, or `"Incident Report"` when not provided.
- The returned Markdown still appears inline so the assistant can summarize it immediately.

Error behavior:

- If persistence fails, return a structured error instead of pretending the report was saved.
- Do not silently fall back to volatile-only reports. The whole point of this change is reliable manual access.

### `get_incident_report`

Input:

```json
{ "report_id": "rpt_<32 hex>" }
```

Errors:

- invalid ID format -> `invalid_report_id`
- not found -> `report_not_found`
- malformed stored files -> `report_store_corrupt`

### `list_incident_reports`

Input:

```json
{ "limit": 20 }
```

Rules:

- default `limit` is 20
- clamp `limit` to a small maximum, such as 100
- sort newest first by metadata `generated_at`
- skip corrupt entries but include a warning count in the response

### `deliver_report`

Input schema changes from requiring `report.markdown` to requiring `report`, where `report` must contain at least one of:

- `markdown`
- `report_id`

Errors:

- neither provided -> `missing_report`
- both provided -> `conflicting_report_inputs`
- invalid report ID -> `invalid_report_id`
- report ID not found -> `report_not_found`
- delivery config errors stay in the existing delivery response shape

ONS delivery is still selected by:

```json
"channels": ["email"]
```

The tool should not infer ONS delivery from report generation.

## Retention Policy

P0 keeps reports indefinitely under `settings.report_delivery.artifact_dir / "store"`. This matches the manual-read/download goal and avoids surprising data loss.

The persisted metadata must include `generated_at`, `report_id`, paths, and report size information so a future retention feature can safely prune old reports.

Deferred retention follow-up:

- configurable `max_report_age_days`
- configurable `max_report_count`
- optional `delete_incident_report(report_id)` protected by 2FA
- optional `prune_incident_reports(dry_run=True)` protected by 2FA when `dry_run=False`

## Security And Privacy

- Validate `report_id` before path joins.
- Never accept path input from the user for report lookup.
- Write files with UTF-8 text encoding.
- Avoid logging raw report Markdown in audit entries.
- Audit `get_incident_report` and `list_incident_reports` through the existing handler-level `invoked` audit entry. Do not add report Markdown to audit args.
- Keep `deliver_report` recipient redaction as-is.
- Keep confirmation secrets out of audit entries.

## Backward Compatibility

- Existing clients using inline Markdown delivery continue to work.
- Existing `generate_incident_report` callers still receive `report_id`, `markdown`, `html`, `metadata`, and `artifacts`.
- `generate_incident_report` gains optional `title`; callers that omit it get `"Incident Report"`.
- `artifacts` now contains useful paths and persistence failures become visible.
- `deliver_report(report={"markdown": ...})` remains valid.
- `deliver_report` with both `markdown` and `report_id` is rejected because ambiguous delivery content is unsafe.

## Acceptance Criteria

- A generated incident report can be fetched later by `report_id`.
- `get_incident_report` returns Markdown content and local paths.
- `list_incident_reports` returns useful titles and a corruption warning count.
- `deliver_report` can send a saved report through ONS/email with `channels=["email"]`.
- `deliver_report` rejects calls that provide both inline Markdown and `report_id`.
- `deliver_report` is never triggered automatically by report generation.
- Report writes are atomic enough that list/read tools do not expose half-written reports.
- `delete_playbook` requires confirmation token + secret before deleting.
- All destructive registered `delete_*` tools are 2FA guarded or explicitly exempted.
- All mutating tools are classified as guarded or exempted by a drift test.
- Existing report generation and delivery tests still pass.

## Test Strategy

- Unit-test `ReportStore` path validation, atomic save, load, list, corrupt-entry handling, and not-found errors.
- Handler-test `generate_incident_report` persistence, optional title, and artifact paths.
- Handler-test `get_incident_report` and `list_incident_reports`, including `warnings.corrupt_count`.
- Delivery-test `deliver_report` with `report_id` resolves stored Markdown and calls existing delivery code.
- Delivery-test `deliver_report` rejects both `markdown` and `report_id` with `conflicting_report_inputs`.
- Safety-test `delete_playbook` confirmation flow.
- Drift-test `MUTATING_TOOLS` against `GUARDED_TOOLS` plus named exemptions.
- Drift-test every registered `delete_*` tool is classified in `MUTATING_TOOLS`.
- Regression-test `setup_confirmation_secret` refuses overwrite when a valid secret exists.
- Regression-test inline Markdown delivery.

## Open Decisions For Implementation Plan

- Whether `ReportStore` should live in `src/oci_logan_mcp/report_store.py` or be folded into `report_generator.py`. Recommendation: separate file.
- Whether `list_incident_reports` should include the first executive-summary line. Recommendation: include only metadata in P0 to keep list calls small.
- Whether report persistence should be optional. Recommendation: no; make persistence the default and fail visibly if it cannot write.
