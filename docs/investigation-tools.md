# Investigation and reporting tools

[Back to the README](../README.md)

### `ingestion_health`: is ingestion even working?

Probe log-source freshness in one call. Classifies every source as `healthy`, `stopped`, or `unknown` based on how recently it last emitted a record. Cheapest signal-quality primitive.

```json
{
  "tool": "ingestion_health",
  "sources": ["Linux Syslog", "Apache Access"],
  "severity_filter": "warn"
}
```

Returns `{summary, checked_at, findings: [...]}` where each finding carries `status`, `severity`, `last_log_ts`, `age_seconds`, and a human-readable `message`.

Configurable via `ingestion_health.stoppage_threshold_seconds` (default 600s) and `ingestion_health.freshness_probe_window` (default `last_1_hour`) in `config.yaml`.

### `create_log_source_from_sample`: onboard JSON/NDJSON, CSV, or regex-text logs

Create a custom Log Analytics parser/source from non-sensitive JSON/NDJSON sample lines, CSV content with a header row, or line-oriented text with an explicit regex pattern and ordered capture-field keys. The tool uploads the same sample workload and verifies that upload batch with a `'Parse Failed'` check plus per-field populated counts. It is guarded because it creates OCI resources and uploads log content.

```json
{
  "tool": "create_log_source_from_sample",
  "source_name": "BlueCat Edge DNS Logs",
  "sample_logs": ["{\"eventType\":\"query-response\",\"sourceAddress\":\"192.0.2.10\"}"],
  "format": "json_ndjson",
  "log_group_id": "ocid1.loganalyticsloggroup.oc1..example",
  "acknowledge_data_review": true
}
```

For regex text, use `format="regex_text"` and provide both `regex_pattern` and `regex_field_keys`. `regex_field_keys[0]` maps to capture group 1, `regex_field_keys[1]` maps to capture group 2, and so on. v1 regex support is deliberately explicit; it does not infer arbitrary plain-text formats.

```json
{
  "tool": "create_log_source_from_sample",
  "source_name": "App Text Logs",
  "sample_logs": [
    "INFO user=alice ip=192.0.2.10 action=login",
    "WARN user=bob ip=192.0.2.11 action=logout"
  ],
  "format": "regex_text",
  "regex_pattern": "^(\\w+) user=(\\S+) ip=(\\S+) action=(\\S+)$",
  "regex_field_keys": ["severity", "user", "sourceip", "action"],
  "log_group_id": "ocid1.loganalyticsloggroup.oc1..example",
  "acknowledge_data_review": true
}
```

`acknowledge_data_review` must be true: the caller is responsible for providing logs that are safe to upload and stripped of secrets, tokens, PII, and customer-sensitive values. Existing parser/source names return `CONFLICT` unless `overwrite=true` is explicitly supplied. Verification uses the generated/uploaded `'Upload Name'` so parse-failure checks are scoped to this sample workload, not every record in the tenancy, and uses `last_30_days` by default to avoid false negatives from pasted samples whose event time is older than the upload time.

### `parser_failure_triage`: which parsers are broken?

Surface the top log sources with parse failures, ranked by volume. Returns up to 20 sources, each with failure count, first/last seen timestamps, and up to 3 sample raw lines that failed to parse. Each source has one parser configured, so this tells you which parser needs fixing before investigating an incident.

```json
{
  "tool": "parser_failure_triage",
  "time_range": "last_24_hours",
  "top_n": 10
}
```

Returns `{failures: [...], total_failure_count: N}` where each entry carries `source`, `failure_count`, `first_seen`, `last_seen`, and `sample_raw_lines` (up to 3).

If the session budget is exhausted between the stats and samples queries, the ranked failure list is preserved and the response adds `partial: true, partial_reason: "samples_budget_exceeded"` with empty `sample_raw_lines` on each entry: so you still see which sources are failing even when samples can't be fetched.

### `investigate_incident`: one-call first-cut triage

The flagship. Given a seed Logan query + time range, returns a structured investigation: stopped sources (J1), parser failures (J2), top_k anomalous sources vs. the prior equal-length window (A2), with per-source top error clusters (Logan `cluster`), top entities (host/user/request_id), and a recent-events timeline. Multi-query investigations can return partial results when a deadline, budget or backend call prevents completion.

```json
{
  "tool": "investigate_incident",
  "query": "'Event' = 'error'",
  "time_range": "last_1_hour",
  "top_k": 3
}
```

Returns `{summary, seed, ingestion_health, parser_failures, anomalous_sources: [{source, pct_change, top_error_clusters, top_entities, timeline, errors}], cross_source_timeline, next_steps, budget, partial, partial_reasons, elapsed_seconds}`.

Investigation intent routing:
- User wording such as "investigate", "investigation mode", "investigator mode", "root cause", "what happened", "likely story", "triage this incident", "troubleshoot", "find the issue(s)", "what is wrong", "diagnose", "what went wrong", "why did this happen", "RCA", and "postmortem" should start with `investigate_incident`.
- Supporting subagents or manual `run_query` calls can deepen evidence, but they do not replace `investigate_incident`.
- If the user expects a written artifact, call `generate_incident_report` after `investigate_incident`, or use `investigate_and_generate_report` to run both steps in one tool call.
- Email delivery is a separate confirmed action: ask whether the user wants OCI Notifications email delivery, resolve/reuse a topic with `prepare_report_delivery`, ask its final confirmation prompt, then call `deliver_report` with the returned `delivery_confirmation_token`.

**Current limitations:**
- Only the seed's pre-pipe search clause is used for drill-down scoping. A seed like `'Event' = 'error' | where Severity = 'critical'` investigates **all** `'Event' = 'error'` rows: the `where` narrowing is dropped for drill-down. Fix: put all scoping in the pre-pipe filter.
- `top_k` is clamped to `[1, 3]`: bounds the number of drill-down queries; elapsed time still depends on OCI and the selected data.
- `alarm_ocid` seed and NL-to-query `description` seed are deferred (A6 will own alarm-OCID).
- J1's freshness is evaluated over its configured `freshness_probe_window`, which may differ from the investigation `time_range`; the report's `ingestion_health.note` explains the difference.

**Partial responses.** A1 never raises `BudgetExceededError` out of its boundary: instead, the report comes back with `partial: true` and specific `partial_reasons`:
- `"budget_exceeded"`: session budget ran out mid-investigation
- `"timeline_omitted"`: one or more per-source timeline queries errored
- `"entity_discovery_partial"`: one or more entity fields weren't valid for a source (`InvalidParameter`)
- `"source_errors"`: a non-budget, non-field-variance infrastructure failure occurred somewhere in a per-source branch (cluster query 5xx, unexpected ServiceError in entity discovery, transport timeout, or a whole-branch-level exception). Unlike `timeline_omitted`, this also covers failures in the cluster/entity sub-phases even if the rest of the branch completed: the sub-phase's empty result is accompanied by an `errors` entry naming the failure.

Each condition is accompanied by `anomalous_sources[*].errors` entries describing exactly what went wrong per source.

### `investigate_and_generate_report`: investigation plus report

For investigation-mode requests where the user expects a report, this convenience workflow runs `investigate_incident` first, then feeds that result to `generate_incident_report`:

```json
{
  "tool": "investigate_and_generate_report",
  "query": "'Event' = 'error'",
  "time_range": "last_24_hours",
  "top_k": 3,
  "format": "markdown"
}
```

Returns `{status: "report_generated", investigation, report, delivery_options}`. It does not send email. Use the returned `delivery_options` to ask whether the user wants OCI Notifications delivery and whether to reuse a saved topic.

### `why_did_this_fire`: replay a Logan alarm fire window

For Logan-managed monitoring alarms only. Given an alarm OCID and the historical `fire_time`, the tool replays the stored Logan query over the computed context window, returns the alarm metadata used for evaluation, and surfaces up to 50 scoped top contributing rows.

```json
{
  "tool": "why_did_this_fire",
  "alarm_ocid": "ocid1.alarm.oc1..exampleuniqueID",
  "fire_time": "2026-04-23T10:00:00Z"
}
```

Returns `{alarm, evaluation, window, seed, trigger_query_result, top_contributing_rows, related_saved_search_id, dashboard_id}`.

Current constraints:
- Logan-managed monitoring alarms only. Non-Logan alarms return structured error codes.
- `window_before_seconds` defaults to the alarm's `pending_duration`; if that metadata is absent or unparsable, it falls back to 300 seconds.
- Raw top-row expansion is suppressed when the stored Logan query degrades to an unscoped `*`. In that case the response sets `seed.seed_filter_degraded: true`, returns `top_contributing_rows: []`, and explains the omission via `top_contributing_rows_omitted_reason: "unscoped_seed_filter"`.
- `dashboard_id` is always `null` in this version; explicit dashboard linkage is deferred.

### `find_rare_events`: surface low-frequency field values

Wrapper around Logan's native `rare` command. Given a source, a field, and a current time window, the tool returns low-frequency values plus one history annotation query for `count_in_history`, `first_seen`, and `last_seen`.

```json
{
  "tool": "find_rare_events",
  "source": "Linux Syslog Logs",
  "field": "Severity",
  "time_range": {"time_range": "last_24_hours"}
}
```

Returns `{source, field, time_range, history_days, rarity_threshold_percentile, rare_values}` where each entry in `rare_values` carries:
- `value`
- `count_in_range`
- `percent_in_range`
- `count_in_history`
- `first_seen`
- `last_seen`

Current behavior:
- Uses Logan's native `rare limit = -1 showcount = true showpercent = true <field>` command for the current window.
- Uses one follow-up `stats` query for history annotation.
- `rarity_threshold_percentile` defaults to `5.0`; rows above that percentage are filtered out.
- `history_days` defaults to `30`, with built-in relative windows for `1`, `2`, `7`, `14`, and `30` days and an absolute fallback for other positive values.
- Live validation is checked with `scripts/validate_rare_query.py`; the parser aligns sparse grouped responses by declared columns before the wrapper consumes them.

### `trace_request_id`: stitch request-id events across sources

Given a request-id or trace-id value and a time window, the tool probes common id fields (`Request ID`, `Trace ID`, `traceId`, `x-request-id`) and returns one merged, de-duplicated event stream.

```json
{
  "tool": "trace_request_id",
  "request_id": "abc-123-def",
  "time_range": {"time_range": "last_1_hour"}
}
```

Returns `{request_id, events, sources_matched}` where:
- `events` is a single ordered cross-source timeline
- duplicates across multiple id-field probes are removed
- `sources_matched` lists the unique sources that contributed events

### `related_dashboards_and_searches`: reuse existing dashboards and queries

Given a source, entity, or field, suggest the most relevant existing dashboards, saved searches, and learned queries so an operator can pivot into already-curated views instead of starting from scratch.

```json
{
  "tool": "related_dashboards_and_searches",
  "field": "Request ID"
}
```

Returns `{dashboards, saved_searches, learned_queries}` where each bucket contains up to 5 entries with `{id, name, score, reason}`.

Current behavior:
- Requires at least one of `source`, `entity`, or `field`.
- Learned-query results come from personal + shared catalogs only; builtin and starter templates are excluded.
- Saved searches are shortlisted from listing metadata, then the top 10 candidates are rescored using fetched query text.
- Matching uses existing normalization and fuzzy helpers from `fuzzy_match.py`, so field and source naming variations can still surface relevant resources.

### `generate_incident_report`: deterministic incident report

Convert an `investigate_incident` response into a deterministic Markdown incident report, with optional HTML rendering:

```json
{
  "tool": "generate_incident_report",
  "investigation": {
    "summary": "Apache errors spiked and parser failures were present.",
    "anomalous_sources": []
  },
  "format": "html",
  "summary_length": "standard",
  "title": "Apache error investigation"
}
```

Returns `{report_id, markdown, html, metadata, artifacts}`. Markdown is always returned. `html` is populated only when `format="html"`.

Current behavior:
- Template-first and deterministic; no internal LLM provider is called.
- Source is an A1 `InvestigationReport` object only.
- The response includes `delivery_options.email` with the OCI Notifications email workflow, any saved per-user topic, and any server-configured default topic.
- Each report is persisted under `report_delivery.artifact_dir/store/rpt_<id>/` and `artifacts` contains local paths for manual read/download.
- Supported sections are `executive_summary`, `timeline`, `top_findings`, `evidence`, `recommended_next_steps`, and `appendix`.
- Playbook-run reports and session-id reports are deferred.

Stored report helpers:

- `get_incident_report(report_id="rpt_...")` returns the stored Markdown, paths, and metadata.
- `list_incident_reports(limit=20)` lists recent stored reports and includes `warnings.corrupt_count`.
- `prepare_report_delivery(report_id="rpt_...", channel="email", recipients={"email_topic_ocid": "ocid1.onstopic..."})` stores the selected topic and returns a final-confirmation prompt plus token.
- `deliver_report(report={"report_id": "rpt_..."}, channels=["email"], delivery_confirmation_token="<token>")` sends a stored report only after `prepare_report_delivery` and final user confirmation.

### `get_report_delivery_options` and `list_notification_topics`

After report generation, ask:

> Do you want this report delivered by OCI Notifications email?

If the user says yes, use `get_report_delivery_options` or the report's returned `delivery_options`:
- If `saved_topic` exists, ask whether to reuse it.
- If no saved topic should be reused, call `list_notification_topics` to show available topics.
- Call `prepare_report_delivery` with the selected topic. It stores the topic and returns `confirmation_prompt` plus `confirmation_token`.
- Ask the returned final confirmation prompt before sending.
- Call `deliver_report` with the returned `delivery_confirmation_token`. If the topic changes, call `prepare_report_delivery` again; the token is bound to the prepared topic.

`list_notification_topics` defaults to the current compartment and accessible subcompartments, and can be scoped with `compartment_id`, `include_subcompartments`, and `lifecycle_state`.

### `deliver_report`: PDF + Telegram / ONS / Slack delivery

Deliver generated report Markdown to the channels users already monitor. Telegram is the default IM path and receives the full PDF when `format` is `pdf` or `both`.

```json
{
  "tool": "deliver_report",
  "report": {
    "report_id": "rpt_0123456789abcdef0123456789abcdef"
  },
  "channels": ["telegram", "email"],
  "format": "both",
  "recipients": {
    "telegram_chat_id": "-100999",
    "email_topic_ocid": "ocid1.onstopic.oc1..example"
  },
  "delivery_confirmation_token": "token_from_prepare_report_delivery"
}
```

Returns `{status, delivered, pdf_path}`. `status` is `sent`, `partial`, or `failed` depending on per-channel results. `delivered[]` contains one row per requested channel with `{channel, status, message_id, artifact, recipient, error?}`.

Current behavior:
- Input accepts exactly one of inline `report.markdown` or stored `report.report_id`.
- `format="pdf"` generates a local PDF and sends it to Telegram as a document. Email and Slack receive the inline summary.
- `format="markdown"` skips PDF generation and sends inline summaries/messages only.
- `format="both"` sends the PDF to Telegram and inline summaries to email/Slack.
- Telegram uploads are rejected before send if the PDF exceeds the 50 MB Bot API document limit.
- ONS email delivery publishes an inline Markdown/plaintext summary to an OCI Notifications topic. ONS does not attach the PDF and no Object Storage/PAR link is generated in this version.
- Email delivery requires a stored `report.report_id` and a valid `delivery_confirmation_token` from `prepare_report_delivery`.
- If email is prepared and `recipients.email_topic_ocid` is omitted, the workflow reuses the current user's saved report topic, then the server-configured `notifications.ons.default_topic_ocid`. Successful email delivery saves the effective topic as the user's future default.
- The assistant/client must call `prepare_report_delivery`, ask its explicit final confirmation prompt, and pass its token to `deliver_report`; topic reuse only removes repeated topic selection.
- Slack is optional and summary-only via the existing webhook integration. This is suitable for testing with a private/free Slack registration before later Oracle Slack workspace rollout.

Configure defaults in `~/.oci-logan-mcp/config.yaml`:

```yaml
notifications:
  telegram:
    bot_token: "<telegram-bot-token>"
    default_chat_id: "-100999"
  ons:
    default_topic_ocid: "ocid1.onstopic.oc1..example"
  slack:
    webhook_url: "https://hooks.slack.com/services/..."

report_delivery:
  artifact_dir: "/home/opc/.oci-logan-mcp/reports"
  max_email_body_chars: 8000
```

Environment overrides:

```bash
export TELEGRAM_BOT_TOKEN="<telegram-bot-token>"
export TELEGRAM_CHAT_ID="-100999"
export OCI_LOGAN_ONS_TOPIC_OCID="ocid1.onstopic.oc1..example"
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export OCI_LOGAN_REPORT_ARTIFACT_DIR="/home/opc/.oci-logan-mcp/reports"
export OCI_LOGAN_REPORT_MAX_EMAIL_CHARS=8000
```

End-to-end incident flow:

```json
{"tool": "investigate_incident", "query": "'Log Source' = 'Apache Access' | stats count by Status", "time_range": "last_1_hour"}
```

```json
{"tool": "generate_incident_report", "investigation": {"summary": "Example investigation summary", "anomalous_sources": []}, "format": "markdown"}
```

The `investigation` value must be the full object returned by
`investigate_incident`, not a quoted JSON string. The shortened object above
illustrates its shape; replace it with the actual investigation result.

```json
{"tool": "get_report_delivery_options"}
```

```json
{"tool": "list_notification_topics", "include_subcompartments": true, "lifecycle_state": "ACTIVE"}
```

```json
{"tool": "prepare_report_delivery", "report_id": "rpt_<id from generate_incident_report>", "channel": "email", "recipients": {"email_topic_ocid": "ocid1.onstopic.oc1..selected"}}
```

```json
{"tool": "deliver_report", "report": {"report_id": "rpt_<id from generate_incident_report>"}, "channels": ["telegram", "email"], "format": "both", "delivery_confirmation_token": "<token from prepare_report_delivery>"}
```

### `record_investigation`: save a playbook from the audit trail

Capture the current server process's audited tool calls into a named playbook:

```json
{
  "tool": "record_investigation",
  "name": "CPU spike triage",
  "description": "Queries and pivots used for the April 24 CPU incident",
  "since": "2026-04-24T09:00:00Z",
  "until": "2026-04-24T10:00:00Z"
}
```

Returns `{id, name, description, owner, created_at, source_process_session_id, window, steps, warning}`. Each step includes the audited `tool`, sanitized `args`, timestamp, and outcome. If no audit events match the requested window, the playbook is still saved with an empty `steps` list and a warning.

Use `list_playbooks` for compact metadata, `get_playbook(playbook_id=...)` for the full step list, and `delete_playbook(playbook_id=...)` to remove one.

Current behavior:
- Playbooks are stored per user under the Logan base directory.
- The `record_investigation` call itself is omitted from captured steps because it is the capture boundary, not part of the investigation.
- Playbooks are record/catalog only. Replay, parameterization, and report generation are deferred.
