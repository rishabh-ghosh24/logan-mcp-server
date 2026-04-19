# Spec — `feat/reports-and-playbooks`

**Branch:** `feat/reports-and-playbooks` (off `main`, started after `feat/triage-toolkit` merges)
**Theme:** T4 — Closing the loop
**Core features:** N1, N3, Report Delivery
**Stretch features:** G1
**Ship order within branch:**
1. N1 (investigation recorder — reuses N6 transcript plumbing)
2. N3 (incident report generator — consumes A1 output)
3. Report Delivery (PDF + Telegram + ONS email)
4. Stretch: G1 PII redaction

**Companion docs:** [../feature-catalog.md](../feature-catalog.md) · [../roadmap.md](../roadmap.md)

> **For agentic workers:** Design-level spec. Produce a TDD plan per feature via `superpowers:writing-plans` before execution.

---

## Goal

Close the investigation loop. After A1 produces a first-cut investigation, turn it into a reusable playbook (N1), synthesize it into a human-readable report (N3), and deliver that report to where the user already is — Telegram and email (Report Delivery). Optionally redact PII before it leaves the server (G1).

## Acceptance criteria for the branch

- `record_investigation(session_id, name)` saves the current session's tool chain as a named replayable playbook.
- `generate_incident_report(investigation)` produces a structured Markdown report.
- `deliver_report(report, channels=["telegram", "email"])` generates a PDF and ships it via Telegram + ONS.
- Stretch: PII redaction policy, when enabled via config, masks matching fields on outbound.
- All existing tests pass.

---

## N1 — Investigation recorder (playbooks)

### Purpose
Capture the tool-call chain of an ad-hoc investigation and replay it as a named playbook. The agent's investigation becomes a reusable runbook.

### Tool interface
```
record_investigation(
  session_id: str = "current",
  name: str,
  description: str | None = None,
  parameterize: bool = True,  # auto-extract values to replay-time params
) -> PlaybookId

replay_investigation(
  playbook_id: str,
  params: dict | None = None,
  dry_run: bool = False,
) -> InvestigationReport

list_playbooks() -> list[PlaybookMetadata]
delete_playbook(playbook_id: str) -> None
```

### Data model
```
Playbook {
  id: str,
  name: str,
  description: str,
  owner: str,
  created_at: datetime,
  steps: list[Step],
  parameters: list[Parameter],  # auto-extracted values: time ranges, entities, sources
  source_session_id: str,
}
Step {
  tool: str,
  args: dict,  # param references as $param_name
  capture_as: str | None,  # variable name for downstream steps
}
```

### Parameterization heuristic
- **Time ranges** → always become params.
- **Entity values** (hosts, users, request IDs) → params if they appear as literal strings in args.
- **Sources, compartments** → optional params (default = same as capture time).

### Files
- Create: `src/oci_logan_mcp/playbook_store.py` — persistent playbook storage (SQLite).
- Create: `src/oci_logan_mcp/playbook_engine.py` — record + replay logic.
- Modify: `src/oci_logan_mcp/audit.py` — ensure session events are queryable by session_id (shared with N6).
- Modify: `src/oci_logan_mcp/tools.py` — register 4 tools.
- Create: `tests/test_playbook_store.py`.
- Create: `tests/test_playbook_engine.py`.

### Test outline
1. Test: record a 3-step session → playbook has 3 steps with correct tools.
2. Test: replay with same params reproduces output on canned data.
3. Test: replay with different time_range param reruns against new window.
4. Test: `dry_run=True` returns the resolved step list without executing.
5. Test: delete_playbook removes it from list.
6. Test: auto-parameterization extracts a time range and an entity value from a recorded session.

### Dependencies
- **Hard on N6 transcript plumbing** from `feat/agent-guardrails` (already merged when this branch starts).

---

## N3 — Auto-generate incident report

### Purpose
Synthesize an A1 investigation (or a replayed playbook, or a user-curated selection of queries) into a human-readable report.

### Tool interface
```
generate_incident_report(
  source: {
    investigation: InvestigationReport | None,  # output of A1
    playbook_run: PlaybookRunResult | None,      # output of N1 replay
    session_id: str | None,                      # fallback: synthesize from session transcript
  },
  format: "markdown" | "html" = "markdown",
  include_sections: list[str] | None = None,  # default: all
  summary_length: "short" | "standard" | "detailed" = "standard",
) -> {
  report_id: str,
  markdown: str,
  html: str | None,
  metadata: {generated_at, source_type, word_count},
  artifacts: list[{name, type, inline_data_or_path}],  # charts, tables as attachments
}
```

### Report sections (default)
1. **Executive summary** (3–5 sentences).
2. **Timeline** — key events in chronological order.
3. **Top findings** — anomalous sources, error clusters, changed entities.
4. **Evidence** — queries run + result snippets (linked).
5. **Recommended next steps** — derived from N2's next_steps hints.
6. **Appendix** — full tool-call chain reference (links to N6 transcript).

### LLM usage
- Report synthesis uses an LLM call for prose sections (exec summary, findings narrative).
- Data sections (timeline, evidence) are templated — no LLM.
- LLM prompt lives in `src/oci_logan_mcp/templates/report_prompt.md`.

### Files
- Create: `src/oci_logan_mcp/report_generator.py` — synthesis orchestrator.
- Create: `src/oci_logan_mcp/templates/report_prompt.md` — LLM prompt.
- Create: `src/oci_logan_mcp/templates/report_skeleton.md` — Markdown skeleton.
- Modify: `src/oci_logan_mcp/tools.py`.
- Create: `tests/test_report_generator.py`.

### Test outline
1. Test: investigation input → report has all default sections.
2. Test: empty investigation → report says "no findings" in exec summary without errors.
3. Test: `summary_length=short` keeps exec summary under N words.
4. Test: artifacts include chart data when investigation has visualizations.
5. Test: LLM failure → template fallback produces a usable report (no crash).

### Dependencies
- A1 output shape (investigation) from `feat/triage-toolkit`.
- N1 playbook-run output.

---

## Report Delivery (M-new) — PDF + Telegram + ONS email

### Purpose
Ship the generated report to where the user already is. Telegram PDF attachment + OCI Notifications email.

### Tool interface
```
deliver_report(
  report: {report_id: str} | {markdown: str, title: str},
  channels: list["telegram" | "email"] = ["telegram"],
  recipients: {
    telegram_chat_id: str | None,     # default: from config
    email_topic_ocid: str | None,     # default: from config
  } = {},
  format: "pdf" | "markdown" | "both" = "pdf",
  title: str | None = None,
) -> {
  delivered: list[{channel: str, status: "sent" | "failed", message_id: str | None}],
  pdf_path: str | None,  # local path if pdf generated
}
```

### PDF generation
- Use `weasyprint` or `reportlab` (prefer `weasyprint` — Markdown → HTML → PDF is cleaner).
- Styling: single simple CSS file, no branding flexibility in P0.
- Max PDF size: 50 MB (Telegram hard limit; email via ONS typically accepts less — validate and truncate with warning).

### Delivery
- **Telegram:** extend existing `notification_service.py` (or whatever hosts `send_to_telegram`) with an `attach_file` pathway. Bot API supports `sendDocument` with attachments.
- **Email via ONS:** existing ONS integration (used by alarms). Attachment handling: ONS notifications don't natively attach files — instead, upload PDF to Object Storage, include pre-authenticated URL in the notification body. Optional; ship without attachments if Object Storage path not configured (include pre-authenticated URL is stretch inside this feature).

### Files
- Create: `src/oci_logan_mcp/report_pdf.py` — Markdown → PDF.
- Create: `src/oci_logan_mcp/report_delivery.py` — channel orchestrator.
- Modify: `src/oci_logan_mcp/notification_service.py` — add file-attachment Telegram path.
- Modify: `src/oci_logan_mcp/config.py` — add `telegram_default_chat_id`, `ons_default_topic_ocid`, `report_artifact_bucket`.
- Modify: `src/oci_logan_mcp/tools.py`.
- Create: `tests/test_report_delivery.py`.
- Create: `tests/test_report_pdf.py`.

### Test outline
1. Test: Markdown input → PDF generated with expected page count.
2. Test: delivery to Telegram mocks out `sendDocument`, verifies correct arguments.
3. Test: delivery to email publishes an ONS notification with body containing link.
4. Test: `channels=["telegram", "email"]` delivers to both; one failure doesn't block the other.
5. Test: PDF > 50MB is rejected with a clear error before calling Telegram.

### Dependencies
- N3 report output.
- Existing Telegram and ONS integrations.

---

## Stretch: G1 — PII redaction (optional, config-driven)

### Purpose
Mask PII in outbound data when operator explicitly enables it. **Off by default** — most workloads need to see emails, usernames, failed-login trails.

### Config model
```yaml
# Loaded from env file or config.yaml
redaction:
  enabled: false              # master switch
  rules:
    - name: email
      pattern: '[\w.-]+@[\w.-]+\.\w+'
      replacement: '<email>'
      apply_to: ["query_results", "reports", "transcripts"]
    - name: ssn
      pattern: '\d{3}-\d{2}-\d{4}'
      replacement: '<ssn>'
      apply_to: ["query_results", "reports", "transcripts"]
    - name: credit_card
      pattern: '\b(?:\d{4}[- ]?){3}\d{4}\b'
      replacement: '<cc>'
  exclude_fields: []          # fields where rules never apply
  include_fields: []          # if set, rules ONLY apply to these fields
```

### Scope
- Applied at **one central chokepoint** — all tool responses pass through redactor before leaving server.
- Applied to report markdown/PDF and transcript exports.
- Disabled when `redaction.enabled=false` (default).

### Files
- Modify: `src/oci_logan_mcp/sanitize.py` — extend existing sanitizer with config-driven rule engine.
- Modify: `src/oci_logan_mcp/config.py` — add redaction config block.
- Modify: `src/oci_logan_mcp/tools.py` — wrap tool responses via sanitizer when enabled.
- Modify: `src/oci_logan_mcp/report_generator.py` + `report_delivery.py` — apply sanitizer on report surfaces.
- Create: `tests/test_redaction_policy.py`.

### Test outline
1. Test: `redaction.enabled=false` → tool responses unchanged.
2. Test: `enabled=true` with email rule → emails in query results are masked.
3. Test: `exclude_fields=["raw_log"]` → rule skipped for that field.
4. Test: report generation applies redaction to the final markdown.
5. Test: malformed regex in config → server logs error and continues without applying that rule (no crash).

### Dependencies
- Existing `sanitize.py` infrastructure.
- Applied to: query_engine responses, report output, transcript export (N6).

---

## Branch merge criteria

- All new and existing tests pass.
- End-to-end demo works: alarm OCID → `investigate_incident` → `generate_incident_report` → `deliver_report(channels=["telegram", "email"])` lands PDF in Telegram and email notification.
- Playbook record → replay roundtrip works with canned data.
- Stretch G1 either shipped (with documented config examples in README) or deferred with PR-description rationale.
- README updated with: investigation workflow guide, sample Telegram/email config, G1 config example (if shipped).
- Acceptance-criteria checklist marked done in PR description.

---

## Cross-branch note

This branch assumes:
- `feat/agent-guardrails` merged (N6, L1, H1, N5 available).
- `feat/triage-toolkit` merged (A1 available).

If either is still open at kickoff time, revisit sequencing — do not start this branch in parallel. Solo dev, linear flow.
