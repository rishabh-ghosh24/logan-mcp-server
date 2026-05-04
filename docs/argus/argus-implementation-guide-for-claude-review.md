# Argus POC Implementation Guide For Claude Review

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans only after Rishabh explicitly approves implementation. This document is review-only until then.

**Goal:** Define the implementation path for Argus POC monitoring support, starting with stronger MCP audit coverage in Logan MCP and later extending to OCI-Mon, Hermes-side state, consent, playbooks, cron monitoring, and reports.

**Architecture:** Keep Logan and OCI-Mon on the official MCP Python SDK. Logan currently uses the low-level `mcp.server.Server` API, not third-party `fastmcp`. Implement audit by extending the existing Logan handler/audit structure first, prove it, then port the pattern to OCI-Mon.

**Tech Stack:** Python 3.10+, official `mcp` Python SDK, current Logan MCP code, JSONL audit files, SQLite for Argus state, OCI Log Analytics for shipped audit, Hermes Telegram gateway.

---

## Design Constraints

1. No implementation before explicit approval.
2. No worktree unless there is a clear advantage. Current branch is `feat/argus-poc-audit-setup`.
3. Stay on the official MCP Python SDK.
4. Do not migrate to the third-party `fastmcp` package.
5. Start with Logan MCP because it has the richer and riskier tool surface.
6. Treat current Logan behavior as source of truth:
   - `src/oci_logan_mcp/server.py` uses `mcp.server.Server`.
   - `src/oci_logan_mcp/audit.py` already has `AuditLogger`.
   - `src/oci_logan_mcp/confirmation.py` already has `ConfirmationManager`.
   - `src/oci_logan_mcp/read_only_guard.py` already classifies mutating tools.
   - `src/oci_logan_mcp/handlers.py` already logs `invoked` for all tools and richer outcomes for guarded calls.
7. Do not break existing confirmation-secret flow.
8. Do not remove existing audit/playbook behavior.

## Current Logan Facts

### MCP SDK Usage

Logan currently registers tools through the official SDK low-level server:

```python
from mcp.server import Server
```

The tool dispatch path is:

```text
OCILogAnalyticsMCPServer._setup_handlers()
  -> @self.server.call_tool()
  -> self.handlers.handle_tool_call(name, arguments)
```

So the first audit insertion point is `MCPHandlers.handle_tool_call`, not FastMCP middleware.

### Existing Audit

Current `AuditLogger` writes JSONL to:

```text
~/.oci-logan-mcp/logs/audit.log
```

It currently stores:

- timestamp
- session_id
- user
- pid
- tool
- args with confirmation secrets removed
- outcome
- optional result_summary
- optional error

The current handler already logs:

- `invoked` before every gate
- `read_only_blocked`
- `confirmation_unavailable`
- `confirmation_requested`
- `confirmation_failed`
- `confirmed`
- `executed`
- `execution_failed`
- `unknown_tool`
- `audit_blocked`

Current branch status: non-guarded tools now receive terminal completion/failure audit events with compact result summaries.

### Existing Confirmation Gate

Current `confirmation.GUARDED_TOOLS` includes:

```text
delete_alert
delete_saved_search
delete_dashboard
delete_playbook
update_alert
update_saved_search
add_dashboard_tile
create_alert
create_saved_search
create_dashboard
create_log_source_from_sample
```

Also, `run_query` is context-guarded when `budget_override=true`.

Important review point: our preferred POC policy allows some Argus-owned creates, but current Logan server-side policy guards multiple create tools. Any implementation must decide whether to keep that stricter server behavior or introduce a separate Argus consent/policy layer later.

### Existing Read-Only Guard

`read_only_guard.MUTATING_TOOLS` already blocks many state-changing tools when `OCI_LOGAN_MCP_READ_ONLY` is enabled. That list includes session/context mutations, reports, notifications, creates, updates, and deletes.

Argus POC is not the same as global Logan read-only mode:

- Argus POC allows reports and some Argus-owned creates.
- Global `read_only` blocks mutating tools for all clients.
- Global `read_only` must also avoid hidden local learning writes from reader tools.

Do not confuse these two policies.

## Final POC Policy

```text
Reads: allowed.
Telegram notifications: allowed through Hermes.
Telegram reports: allowed through Hermes.
Persisted incident report artifacts: allowed as an explicit POC exception.
ONS email reports: later phase / consent-gated until cadence and rate limits are proven.
Persistent Argus-owned creates: consent-gated during Phase 0/1.
Special creates: explicit in-chat consent.
Updates/deletes: explicit approval plus Logan confirmation secret where supported.
Autonomous remediation: not allowed.
Everything: audited.
```

Phase posture:

| Phase | Policy |
|---|---|
| Phase 0 / Smoke | Reads, health checks, Telegram test report by explicit request; no persistent creates |
| Phase 1 / Observation | Reads, Telegram notifications/reports, persisted local incident report artifacts for laptop review; saved searches/dashboards/tiles still consent-gated |
| Phase 2 / Assisted artifacts | Argus-owned saved searches/dashboards/tiles may be enabled with naming, rate limits, and audit |

Hard POC rules:

- No raw logs in Telegram/email by default.
- Do not mutate shared MCP compartment/namespace context except explicit one-time bootstrap.
- The Logan confirmation secret is a second factor, not approval by itself.
- Ambiguous action requests require clarification before any tool call.
- Every user-visible notification/report includes `audit_ref`, short `trace_id`, time window, queried data sources, severity, confidence, and whether creates happened.

## Build Sequence

```text
1. Logan MCP audit coverage v1.
2. Port the audit pattern to OCI-Mon MCP.
3. Argus SQLite state DB.
4. Argus-side decision audit.
5. Telegram consent/approval skill.
6. First investigation playbook skill.
7. Cron-driven proactive monitoring.
8. Daily/weekly digest reports.
```

Only step 1 should be considered for the first implementation review.

## Phase 1: Logan MCP Audit Coverage v1

### Objective

Make Logan audit complete enough that every tool call that reaches Logan has a start and terminal event, with useful actor, trace, redaction, result summary, and strictness behavior.

### Review Findings Incorporated

The current branch incorporates the following review-driven requirements:

- `args_redacted` must run through structured redaction, not merely confirmation-field stripping.
- `audit_write_status` is omitted because a successful JSONL entry can only represent a successful write; failed writes are surfaced through handler return paths and local warnings.
- Required-audit tools fail closed when audit logging fails or when no audit logger is configured.
- Confirmation tokens are requested only after the `confirmation_requested` audit event is durable.
- Read-only mode suppresses local learning side effects from reader tools such as `run_query`, `run_batch_queries`, `visualize`, and `export_results`.
- `investigate_and_generate_report` intentionally remains allowed in read-only mode as a required-audit report-persistence exception, because Rishabh wants generated report docs persisted for laptop review.
- Raw result previews are summarized and pattern-redacted; Argus policy still forbids raw log payloads in user-facing messages by default.

### Audit Raw Args Compatibility

`AuditLogger` still writes the legacy `args` field for compatibility with playbook/transcript consumers. That field is privileged forensic data and can contain raw query text for general tools.

For dashboards, SIEM exports, shared reviews, and any user-visible summaries, use `args_redacted`. Do not treat legacy `args` as safe-to-share.

### Files Expected To Change

| File | Expected responsibility |
|---|---|
| `src/oci_logan_mcp/audit.py` | Extend audit schema and write API while preserving current `log()` compatibility |
| `src/oci_logan_mcp/handlers.py` | Add universal terminal audit logging around handler execution |
| `src/oci_logan_mcp/config.py` | Add optional audit config if needed |
| `src/oci_logan_mcp/server.py` | Initialize any new audit/actor config if needed |
| `tests/test_audit.py` | Add unit tests for schema, redaction, strictness, write failures |
| `tests/test_handlers.py` | Add handler-level audit outcome tests |
| `tests/test_read_only_guard.py` | Update drift tests only if classification changes |
| `docs/argus or users/rishabh/argus` | Update setup docs after behavior is verified |

Do not add a new shared `mcp-audit` package first. Prove the pattern inside Logan, then extract or port later if useful.

### Event Model

Add or evolve audit events toward this shape:

```json
{
  "ts": "2026-05-03T07:42:13Z",
  "event_id": "evt_<uuid>",
  "trace_id": "trace_<uuid>",
  "audit_ref": "argus-action-2026-05-03-1042-C",
  "actor": "rishabh",
  "actor_source": "logan_user",
  "client_metadata": {
    "claimed_client": "unknown",
    "claimed_version": "unknown",
    "source_ip_truncated": null
  },
  "event_type": "mcp_call",
  "tool": "logan.run_query",
  "args_redacted": {},
  "args_sensitivity": "low",
  "result_summary": {
    "success": true,
    "row_count": 42,
    "execution_ms": 1240
  },
  "blocked": false,
  "block_reason": null,
  "audit_strictness": "best_effort",
  "outcome": "executed"
}
```

Compatibility requirement: existing playbook and transcript logic that consumes `AuditLogger.log()` entries must keep working.

### Actor Identity

Do not trust `X-MCP-Client` as identity.

For Logan stdio/SSH deployments, the existing reliable identity is currently:

```text
UserStore.user_id
```

`UserStore` derives from:

```text
explicit user_id argument, LOGAN_USER, USER, default
```

For v1:

- Keep `UserStore.user_id` as the actor source.
- Add optional client metadata later only if Hermes can send trusted headers or environment values through the transport.
- Do not implement bearer-token auth until we verify the actual transport path. HTTP/SSE may support headers; stdio-over-SSH may not.

Claude review question: should v1 actor identity stay with `LOGAN_USER` for stdio deployments, or should HTTP deployments add token-based actor mapping now?

### Trace ID

Trace ID should be accepted from args or context only if there is a safe propagation path. For v1:

- If caller passes `trace_id` or `audit_ref` in arguments for tools that support metadata, preserve it in audit if it is not part of the OCI operation payload.
- Otherwise generate a `trace_<uuid>` inside Logan per tool call.
- Later, Argus decision audit should generate trace IDs at the start of a Telegram or cron operation and propagate them into MCP calls.

Do not add arbitrary `trace_id` fields to tool input schemas until reviewed, because unknown fields may be passed to underlying services or rejected inconsistently.

### Audit Strictness

Use two strictness levels:

```text
best_effort
required
```

Suggested v1 classification:

| Tool category | Strictness |
|---|---|
| Read/list/get/query/diagnostic | `best_effort` |
| Report generation/delivery/export | `required` |
| Argus-owned create | `required` |
| Special create | `required` |
| Update/delete | `required` |
| Secret setup | `required` |
| Outbound notification | `required` |

If audit write fails:

| Tool type | Behavior |
|---|---|
| Read/query | Proceed, warn locally |
| Telegram/Slack notification through Logan | Block if audit required |
| Report/export/create/update/delete | Block before executing |

Important: this is stricter than the existing `AuditLogger.log()` behavior. Implement carefully so reads are not accidentally blocked by transient audit failures.

Deployment decision: if `audit_logger` is unavailable, required-audit tools must fail closed. Best-effort read tools may proceed and log a local warning.

### Redaction

Preserve forensic usefulness while removing secrets.

Always redact:

- `confirmation_token`
- `confirmation_secret`
- `confirmation_secret_confirm`
- API keys
- bearer tokens
- passwords
- private keys
- Telegram bot tokens

Special cases already present:

- `deliver_report` redacts recipients and inline report Markdown.
- `create_log_source_from_sample` redacts sample logs and stores count/hash.

Keep or summarize:

- compartment OCID
- resource OCID for target resources
- display name
- query text when needed for reproducibility
- row count, execution time, success/failure

Hashing PII with persistent salt can be Phase 2. Do not block v1 on it unless Claude insists.

Known Phase 1 boundary: result previews use pattern-based redaction, not a full DLP engine. Add negative tests for obvious sensitive patterns such as OCIDs, emails, IPs, password/token keywords, long IDs, and JWT-like tokens. Raw log payloads remain forbidden in user-visible Argus output by policy.

### Terminal Outcomes To Capture

Every tool call that reaches Logan should have:

1. `invoked`
2. one terminal event:
   - `executed`
   - `execution_failed`
   - `read_only_blocked`
   - `confirmation_requested`
   - `confirmation_failed`
   - `confirmation_unavailable`
   - `unknown_tool`
   - `audit_blocked`

Current code does not fully terminal-log non-guarded success/failure. That is the main v1 gap.

### Result Summary

Do not log full raw tool output. Produce small summaries:

| Result type | Summary |
|---|---|
| JSON list | `row_count`, first few keys, byte length |
| JSON object | top-level keys, status, count fields if present |
| text | byte length and first 200 chars after redaction |
| image | mime type and byte length |
| error | error class and sanitized message |

### Tests Required

Add or update tests to prove:

1. Non-guarded successful tool logs terminal `executed`.
2. Non-guarded failing tool logs terminal `execution_failed`.
3. Unknown tool logs terminal `unknown_tool`.
4. Read-only blocked tool logs terminal `read_only_blocked`.
5. Confirmation requested and confirmation failed still log as today.
6. Guarded successful execution logs terminal `executed`.
7. Secrets are redacted from args.
8. `deliver_report` does not log inline Markdown or recipients.
9. `create_log_source_from_sample` logs sample count/hash but not sample content.
10. Audit write failure does not block read tools.
11. Audit write failure blocks required-strictness tools before execution.
12. Required-audit tools block if `audit_logger` is not configured.
13. Read-only mode does not write local learned-query, user success/failure, preference, or auto-save state.
14. Every registered handler success path emits `invoked` and exactly one terminal audit event.

Suggested commands:

```bash
PYTHONPATH=src python3 -m pytest tests/test_audit.py tests/test_handlers.py tests/test_read_only_guard.py -q
PYTHONPATH=src python3 -m pytest -q
```

Use `MPLCONFIGDIR=/private/tmp/mpl-cache` if matplotlib cache warnings appear.

## Phase 2: OCI-Mon Audit Port

Do this only after Logan v1 is reviewed and passing.

Requirements:

- Same event field names where possible.
- Same actor/trace/audit_ref concepts.
- Same strictness policy.
- Same local JSONL first, OCI LA shipping later.
- No update/delete OCI-Mon tools should be exposed until confirmation/approval gating exists there.

Open question for Claude review: whether OCI-Mon should duplicate the audit module or depend on a small shared internal package after Logan proves the pattern.

## Phase 3: Argus SQLite State

SQLite is Argus operational memory, not audit.

Suggested file:

```text
~/.hermes/argus/argus_state.db
```

Suggested tables:

```sql
CREATE TABLE findings (
    id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_notified TEXT,
    notification_count INTEGER DEFAULT 0,
    severity TEXT,
    status TEXT,
    cooldown_until TEXT,
    metadata_json TEXT
);

CREATE TABLE approvals (
    audit_ref TEXT PRIMARY KEY,
    requested_at TEXT NOT NULL,
    proposed_action TEXT NOT NULL,
    target TEXT,
    user TEXT,
    response TEXT,
    responded_at TEXT,
    trace_id TEXT
);

CREATE TABLE created_artifacts (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    ocid TEXT,
    name TEXT NOT NULL,
    compartment_id TEXT,
    purpose TEXT,
    status TEXT DEFAULT 'active'
);

CREATE TABLE cron_runs (
    id TEXT PRIMARY KEY,
    cron_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT,
    findings_count INTEGER,
    notifications_sent INTEGER,
    error TEXT
);

CREATE TABLE mcp_health (
    server_name TEXT PRIMARY KEY,
    last_check TEXT NOT NULL,
    status TEXT NOT NULL,
    consecutive_failures INTEGER DEFAULT 0,
    last_error TEXT
);

CREATE TABLE noise_patterns (
    fingerprint TEXT PRIMARY KEY,
    dismissal_count INTEGER NOT NULL,
    first_dismissed TEXT NOT NULL,
    last_dismissed TEXT NOT NULL,
    suppress_until TEXT,
    user_note TEXT
);

CREATE TABLE create_rate_log (
    ts TEXT NOT NULL,
    artifact_type TEXT NOT NULL
);
```

## Phase 4: Argus Decision Audit

This is Hermes/Argus-side, not Logan-side.

It must capture:

- Telegram inbound summary.
- Telegram outbound summary.
- model decision summary.
- tool intent.
- blocked-by-Hermes-config intent.
- consent request.
- consent response.
- cron run start/end.

This is necessary because Hermes tool allowlists can hide excluded tools from the LLM and the MCP server may never see attempted forbidden actions.

## Phase 5: Telegram Consent Skill

Implement only after audit and state basics.

Consent categories:

| Category | Required response |
|---|---|
| Special create | explicit in-chat consent |
| Update/delete | explicit approval plus Logan confirmation secret where supported |
| Remediation | not allowed in POC |

Approval message template:

```text
Proposed action: <tool/action>
Target: <resource, compartment, OCID/display name>
Reason: <why Argus recommends it>
Confidence: <low/medium/high and evidence>
Risk: <what could go wrong>
Blast radius: <affected resources/users>
Rollback: <how to undo>
Audit ref: <argus-action-...>

Reply "go ahead" to proceed, "no" to skip, or "explain more" for details.
```

For update/delete, also ask for the Logan confirmation secret only after the server returns a confirmation token. Do not reuse a previously provided secret.

## Phase 6: First Investigation Playbook

Pick one common, low-risk scenario first:

```text
OCI alarm fired -> explain alarm -> check related LA logs -> recommend action
```

The playbook should:

- gather alarm context from OCI-Mon.
- gather log context from Logan.
- create no persistent resources unless the user asks.
- send a concise Telegram summary.
- optionally generate and deliver an ONS email report.

## Phase 7: Cron Proactive Monitoring

Initial cadence:

```text
every 30 or 60 minutes
```

Initial checks:

- MCP health.
- active OCI Monitoring alarms.
- alarm state changes.
- parser failures.
- ingestion health.
- rare/new high-severity log patterns.
- repeated auth/security failures if query patterns are known.

Notification rule:

```text
Notify only when the finding is new, changed, high severity, or no longer suppressed by cooldown.
```

## Phase 8: Daily And Weekly Digests

Daily report:

- checks performed
- findings surfaced
- notifications sent
- reports generated
- artifacts created
- approvals requested/declined
- update/delete count, expected zero in POC
- MCP health
- audit health

Weekly report:

- recurring findings
- noise candidates
- usefulness statistics
- recommendation quality
- cost/volume estimate
- readiness to continue POC or tune

## Claude Review Checklist

Ask Claude to review these specific points:

1. Is `MCPHandlers.handle_tool_call` the right insertion point for Logan v1, given the current low-level official SDK usage?
2. Should actor identity stay based on `UserStore.user_id` for v1, or should token-based actor mapping be implemented immediately?
3. Should current create tools remain server-side confirmation gated, or should some Argus-owned creates move to Hermes-side consent only?
4. Is `required` audit strictness for reports/exports too strict for POC, or appropriate?
5. Are the redaction rules sufficient without persistent-salt hashing in v1?
6. Does adding universal terminal audit events risk breaking `record_investigation` or transcript export consumers?
7. Should OCI-Mon copy the Logan audit module first or wait for a shared package extraction?

## Implementation Stop Conditions

Stop and ask before coding if any of these are true:

- Hermes cannot pass actor identity or trace metadata in the expected way.
- Current Logan confirmation behavior conflicts with the desired Argus create policy.
- Audit write failure handling would require invasive changes to handlers.
- Existing tests imply report/playbook behavior depends on current audit shape.
- OCI-Mon uses a materially different MCP framework or transport.

## Out Of Scope For First Implementation

- autonomous remediation
- update/delete workflows
- TOTP replacement for static confirmation secret
- OAuth/mTLS actor identity
- audit signing or tamper-evident chain
- real-time audit streaming
- Langfuse or LLM cost tracing
- production customer multi-tenancy
