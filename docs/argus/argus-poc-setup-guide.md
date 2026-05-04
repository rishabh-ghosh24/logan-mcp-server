# Argus POC Setup Guide

This guide captures the POC setup target for Argus, the Hermes-based monitoring assistant running on a new VM.

The current POC goal is intentionally constrained:

- Argus monitors OCI through Telegram.
- Argus uses two remote MCP servers:
  - Logan MCP for OCI Log Analytics.
  - OCI-Mon MCP for OCI Monitoring.
- Argus may read, investigate, summarize, notify, generate reports, and create new Argus-owned artifacts where allowed.
- Argus must not update, delete, remediate, restart, suppress, acknowledge, or modify existing operational state unless the user explicitly approves that exact action.
- Autonomous remediation is out of scope for the POC.

## Current Starting Point

Hermes is freshly installed on a new VM. The setup screen showed the core tools available and several optional API-key-backed tools missing.

For this project, the missing optional web/RL tools are not blockers. The critical setup items are:

1. Main LLM provider configured.
2. Telegram gateway configured.
3. Logan MCP reachable from Hermes.
4. OCI-Mon MCP reachable from Hermes.
5. ONS email delivery configured for reports.
6. Argus POC tool policy applied.
7. Audit and state design reviewed before any implementation.

## Architecture

```text
Telegram user/group
  |
  v
Hermes / Argus on new VM
  |
  |-- Logan MCP remote VM
  |     - OCI Log Analytics queries
  |     - investigation tools
  |     - reports and ONS email delivery
  |
  |-- OCI-Mon MCP remote VM
        - OCI Monitoring alarms
        - metric and compartment discovery
```

Argus should be treated as an operator-facing monitoring assistant, not as a generic chat bot. Its main loop is:

```text
observe -> investigate -> correlate -> explain -> recommend -> notify/report
```

For the POC, the loop stops before remediation.

## Naming And Identity

Use the name `Argus` for the assistant.

Recommended identity conventions:

| Item | Recommendation |
|---|---|
| Telegram bot display name | `Argus` or `Argus OCI` |
| Telegram handle examples | `argus_oci_bot`, `argus_ops_bot`, `argus_monitor_bot` |
| VM service user | `argus` or existing least-privilege service user |
| Artifact prefix | `argus-` |
| Audit reference prefix | `argus-action-` or `argus-incident-` |

Use dedicated accounts/tokens for Argus where possible. Do not reuse personal tokens for long-running unattended operation.

## POC Policy

```text
Reads: allowed.
Telegram notifications: allowed.
ONS email reports: allowed.
Argus-owned creates: allowed with naming, description metadata, tags where available,
rate limits, and audit.
Special creates: require explicit in-chat consent.
Updates/deletes: require explicit approval and Logan confirmation secret where supported.
Autonomous remediation: not allowed.
Everything: audited.
```

### Operation Classification

| Operation type | POC behavior |
|---|---|
| Read, list, get, describe, query | Allowed without approval |
| Telegram response/notification from Argus | Allowed without approval |
| Generate/send ONS email report | Allowed without approval after delivery path is configured |
| Create Argus-owned saved search/dashboard/tile/report/export | Allowed with naming, metadata, rate limits, and audit |
| Create alert | Explicit in-chat consent required because it can create ongoing notification noise |
| Save learned query / remember preference | Explicit "save this" or "remember this" required because it changes future behavior |
| Create log source from sample | Explicit data-review acknowledgement required because it uploads sample data |
| Update existing resource | Explicit approval plus Logan confirmation secret where supported |
| Delete existing resource | Explicit approval plus Logan confirmation secret where supported |
| Restart, stop, suppress, acknowledge, remediate | Not allowed in POC |

## Argus-Owned Artifact Rules

Any artifact Argus creates should be easy to find, audit, and clean up.

Naming:

```text
argus-<resource_type>-<short_purpose>-<YYYYMMDD>
```

Examples:

```text
argus-search-oke-crashloops-20260503
argus-dashboard-vcn-flow-anomalies-20260503
argus-report-daily-health-20260503
```

Description text should include:

```text
[argus_audit_ref=<audit_ref>]
Created by Argus for <purpose>.
Created during <incident/session/ref>.
```

Tags, where supported:

```text
argus_owned: true
argus_audit_ref: <audit_ref>
argus_session: <session_id>
argus_purpose: <human-readable reason>
```

For the POC, Argus should create only in approved non-production or POC compartments unless explicitly instructed otherwise.

## Logan MCP Current Behavior To Remember

This branch is based on the current Logan MCP code, not an abstract design. The current code already has several safety features:

- The server uses the official MCP Python SDK low-level `mcp.server.Server` API in `src/oci_logan_mcp/server.py`.
- It does not currently use the third-party `fastmcp` package.
- It has `AuditLogger` in `src/oci_logan_mcp/audit.py`.
- It has per-user confirmation secrets through `SecretStore`.
- It has confirmation gating through `ConfirmationManager`.
- It has read-only blocking through `read_only_guard.py`.
- It currently marks the following as guarded in `confirmation.GUARDED_TOOLS`:
  - `create_alert`
  - `create_saved_search`
  - `create_dashboard`
  - `create_log_source_from_sample`
  - `add_dashboard_tile`
  - `update_alert`
  - `update_saved_search`
  - `delete_alert`
  - `delete_saved_search`
  - `delete_dashboard`
  - `delete_playbook`

This means the POC guide and the final Hermes config must account for the current Logan behavior. If a create tool is included for Argus, Logan may still return a confirmation flow depending on the current server-side guard policy.

## Hermes Tool Policy

Do not paste this blindly into Hermes until the installed Hermes config schema is verified.

First verify the installed syntax:

```bash
hermes --version
hermes config show
```

Then check local Hermes docs on the VM if present:

```bash
find ~/.hermes -iname '*mcp*' -o -iname '*config*'
```

The policy target is:

### Logan MCP Allowed By Default

Read and diagnostic tools:

```text
run_query
run_saved_search
run_batch_queries
list_log_sources
list_log_groups
list_fields
list_entities
list_compartments
list_labels
list_parsers
list_saved_searches
list_dashboards
list_alerts
list_playbooks
list_incident_reports
list_notification_topics
get_log_summary
get_current_context
get_playbook
get_preferences
get_query_examples
get_incident_report
get_session_budget
get_report_delivery_options
find_compartment
find_rare_events
related_dashboards_and_searches
parser_failure_triage
pivot_on_entity
trace_request_id
diff_time_windows
explain_query
validate_query
investigate_incident
ingestion_health
test_connection
why_did_this_fire
visualize
```

Report and export tools allowed for POC, but audited as creates:

```text
generate_incident_report
investigate_and_generate_report
prepare_report_delivery
deliver_report
export_results
export_transcript
record_investigation
```

Argus-owned persistent creates allowed with naming/rate-limit/audit rules:

```text
create_saved_search
create_dashboard
add_dashboard_tile
```

### Logan MCP Excluded Or Consent-Gated

Special creates requiring explicit in-chat consent:

```text
create_alert
create_log_source_from_sample
save_learned_query
remember_preference
update_tenancy_context
```

Manual/bootstrap only:

```text
setup_confirmation_secret
```

Updates and deletes requiring explicit approval plus Logan confirmation secret where supported:

```text
update_alert
update_saved_search
delete_alert
delete_saved_search
delete_dashboard
delete_playbook
```

Do not use Logan MCP notification tools for Argus chat delivery:

```text
send_to_slack
send_to_telegram
```

Argus should use Hermes's Telegram gateway as its native communication channel.

### Compartment And Namespace Context

Prefer explicit scope arguments on tool calls.

Avoid `set_compartment` and `set_namespace` for Argus in the POC unless a tool has no explicit scope alternative. These mutate server-side context and can confuse multi-client use if the MCP server process is shared.

## OCI-Mon MCP Policy

OCI-Mon currently has a smaller surface. For POC, allow read and context bootstrap tools only:

```text
monitoring_assistant
discover_accessible_compartments
list_saved_templates
setup_default_context
change_default_context
```

Keep auth-changing tools excluded:

```text
configure_auth_fallback
use_instance_principals
```

If OCI-Mon later adds update/delete tools, port the Logan confirmation-secret pattern before exposing those tools to Argus.

## Telegram Setup

Telegram is the POC channel. Minimum requirements:

1. Create a Telegram bot through BotFather.
2. Store the bot token only on the Argus VM.
3. Restrict Argus to approved chat IDs.
4. Reject all messages from unknown chats.
5. Avoid sending raw large log payloads to Telegram.
6. Redact secrets, tokens, credentials, and sensitive customer data before sending excerpts.

Suggested environment variables on the Argus VM:

```bash
ARGUS_TELEGRAM_BOT_TOKEN=<stored locally, not in git>
ARGUS_TELEGRAM_ALLOWED_CHAT_IDS=<comma-separated chat ids>
ARGUS_MODE=poc
ARGUS_AUTONOMY=approval_only
```

Do not paste Telegram tokens into Codex, Claude, or any chat transcript.

## ONS Email Reports

For POC, create a dedicated OCI Notifications topic for Argus reports:

```text
argus-poc-reports
```

Subscribe only yourself at first. Confirm the email subscription before relying on delivery.

Recommended report cadence:

| Cadence | Trigger | Content |
|---|---|---|
| Real-time | High-severity Argus finding | Formal incident report corresponding to Telegram notification |
| Daily | 09:00 IST | What Argus checked, what it found, what it created, what failed |
| Weekly | Monday 09:00 IST | Trends, recurring issues, notification usefulness, audit summary |

Logan's `prepare_report_delivery` and `deliver_report` can support ONS email delivery, but the exact default topic behavior should be verified on the deployed Logan MCP server before relying on it.

## Audit Requirements

Argus needs two audit layers:

1. MCP-side audit.
   - Captures every MCP tool call that reaches Logan or OCI-Mon, regardless of caller.
   - This includes Argus, direct CLI use, Codex, Claude Desktop, or future users.

2. Argus-side decision audit.
   - Captures Telegram messages, reasoning summaries, policy decisions, consent requests, and blocked-by-Hermes intent.
   - This is needed because client-side excluded tools may never reach MCP audit.

Both layers should share:

```text
trace_id
audit_ref
actor
client
timestamp
```

POC audit destination:

- Local JSONL first.
- Ship to OCI Log Analytics as custom sources once parser/source setup is ready.

Suggested source names:

```text
Argus MCP Audit
Argus Decision Audit
```

## SQLite State

SQLite is Argus's local operational state, not the audit system and not a replacement for OCI data.

Use it for:

- deduplication
- notification cooldowns
- active findings
- user approvals/declines
- created artifact tracking
- cron run history
- MCP health status
- noise patterns

Suggested location:

```text
~/.hermes/argus/argus_state.db
```

This should be implemented only after the audit middleware design is reviewed.

## Initial Smoke Test

After Hermes, Telegram, and MCP configs are in place, the first smoke should be read-only:

```text
/health
```

Expected checks:

- Hermes process is running.
- Telegram inbound/outbound works.
- Logan MCP `test_connection` succeeds.
- OCI-Mon MCP health/default context succeeds.
- ONS topic discovery or report delivery options can be read.

Then test:

```text
/alarms
/daily_summary
/investigate parser failures in the last 24 hours
```

Do not test create/update/delete in the first smoke.

## POC Success Bar

Before raising autonomy:

- 30 days of operation.
- At least 50 proactive notifications.
- At least 90 percent of notifications are useful.
- Zero false critical/high-severity claims.
- At least 80 percent of recommendations are ones the user would have approved.
- No unaudited creates.
- No updates/deletes without explicit approval and confirmation.

## Explicit Non-Goals

For the POC, Argus must not:

- restart services
- stop or terminate compute
- change IAM
- modify VCN/security lists/NSGs
- modify Management Agent config
- suppress or acknowledge alarms
- update alarm thresholds
- delete artifacts
- perform autonomous remediation
