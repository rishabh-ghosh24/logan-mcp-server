# OCI Log Analytics MCP Server

Connect an MCP-compatible assistant to [OCI Log Analytics](https://docs.oracle.com/en-us/iaas/log-analytics/home.htm) and [OCI Monitoring](https://docs.oracle.com/en-us/iaas/Content/Monitoring/home.htm) to investigate logs and metrics, build visualizations, manage saved searches and dashboards, and create log-based alerts. The server exposes 69 tools over stdio.

## Quick Start

### 1. Install

Use Python 3.11 to 3.14. In the commands below, `python3` must point to a supported version.

Installation requires MCP SDK 1.22.0 or later within version 1, and OCI SDK 2.104.2 or later. Older versions lack APIs used by the server.

```bash
git clone https://github.com/rishabh-ghosh24/logan-mcp-server.git
cd logan-mcp-server
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

Alternatively, with [uv installed](https://docs.astral.sh/uv/guides/tools/), run the setup command from Git:

```bash
uvx --from git+https://github.com/rishabh-ghosh24/logan-mcp-server oci-logan-mcp --setup
```

The Git command uses the repository's default branch.

### 2. Configure

| Method | When to use | Setup |
|---|---|---|
| `config_file` | Your laptop | Uses `~/.oci/config` from the [OCI CLI setup](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm) |
| `instance_principal` | An OCI compute instance | Uses the instance's dynamic group and IAM policies |

For a local installation:

```bash
export OCI_LA_AUTH_TYPE=config_file
oci-logan-mcp --setup
```

The wizard saves your authentication settings, Log Analytics namespace and default compartment. Run setup before connecting a client. After setup, environment variables can override values:

```bash
export OCI_LA_NAMESPACE=your-namespace
export OCI_LA_COMPARTMENT=ocid1.compartment.oc1..xxxxx
export OCI_CONFIG_PROFILE=DEFAULT
```

`OCI_LA_MCP_CONFIG` selects a different configuration file.

#### IAM access

Ask your tenancy administrator to grant access for the features you use. The following broad example preserves the project's existing policy set. Dashboard, alarm, notification and stream grants support those features; they are not a combined administrative requirement for reading logs.

```text
Allow group <your-group> to use loganalytics-features-family in tenancy
Allow group <your-group> to use loganalytics-resources-family in tenancy
Allow group <your-group> to manage management-dashboard-family in tenancy
Allow group <your-group> to read compartments in tenancy
Allow group <your-group> to manage alarms in tenancy
Allow group <your-group> to read metrics in tenancy
Allow group <your-group> to manage ons-topics in tenancy
Allow group <your-group> to use streams in tenancy
```

Use the [Log Analytics policy catalog](https://docs.oracle.com/en-us/iaas/log-analytics/doc/iam-policies-catalog-logging-analytics.html) and [access examples](https://docs.oracle.com/en-us/iaas/log-analytics/doc/enable-access-logging-analytics-its-resources.html) to select narrower grants. Feature-family policies require tenancy scope; resource-family policies can use compartment scope. Alarm and metric permissions are described in the [Monitoring policy reference](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/monitoringpolicyreference.htm). Dynamic groups use the same verbs with `Allow dynamic-group <name>`.

### 3. Connect your AI assistant

#### Claude Desktop or Claude Code


Works with Claude Desktop, Claude Code, or any MCP client:

```json
{
  "mcpServers": {
    "oci-log-analytics": {
      "command": "/path/to/logan-mcp-server/venv/bin/oci-logan-mcp",
      "args": ["--user", "firstname.lastname"]
    }
  }
}
```

**Where to put this:**

- **Claude Desktop:** `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows)
- **Claude Code:** `~/.claude.json` or project `.mcp.json`

#### Codex CLI

Add a local server to `~/.codex/config.toml`:

```toml
[mcp_servers.logan-mcp]
command = "/path/to/logan-mcp-server/venv/bin/oci-logan-mcp"
args = ["--user", "firstname.lastname"]
```

For the uvx installation, use the absolute path to `uvx` as the client command and these arguments:

```json
["--from", "git+https://github.com/rishabh-ghosh24/logan-mcp-server", "oci-logan-mcp", "--user", "firstname.lastname"]
```

Start a new MCP session and call `test_connection`, then use `get_log_summary` or `list_log_sources` to discover available data. For SSH connections and shared OCI VMs, see the [team deployment guide](docs/team-deployment.md).

## What You Can Do

| Capability | Tools | Examples |
|---|---|---|
| **Query logs** | `run_query`, `run_batch_queries`, `run_saved_search` | Search logs, run multiple queries in parallel, execute saved searches |
| **Query metrics** | `list_metric_namespaces`, `list_metrics`, `search_metric_catalog`, `query_catalog_metric`, `query_metrics` | Discover Monitoring streams, find reviewed metric keys and run bounded MQL |
| **Triage diffs** | `diff_time_windows`, `pivot_on_entity`, `ingestion_health`, `parser_failure_triage`, `investigate_incident`, `investigate_and_generate_report`, `why_did_this_fire`, `find_rare_events`, `trace_request_id`, `related_dashboards_and_searches` | Compare a query across two time windows; pull all events for an entity across sources; probe per-source ingestion freshness; surface top parser failures; one-call first-cut investigation orchestrator; run investigation plus report generation; replay a Logan-managed alarm's historical fire window; surface low-frequency field values for a source; search common request-id / trace-id fields across sources; find existing dashboards and saved searches related to a source, entity, or field |
| **Investigation playbooks** | `record_investigation`, `list_playbooks`, `get_playbook`, `delete_playbook` | Save the current session's audited tool calls as a named playbook, then list, fetch, or delete recorded playbooks |
| **Reports** | `generate_incident_report`, `get_report_delivery_options`, `list_notification_topics`, `deliver_report` | Convert an investigation result into deterministic Markdown/HTML, then generate a PDF and deliver it through Telegram, ONS email-topic summary, or optional Slack summary |
| **Explore schema** | `list_log_sources`, `list_fields`, `list_entities`, `list_parsers`, `list_labels` | Discover what log data is available |
| **Onboard sources** | `create_log_source_from_sample` | Generate a JSON/NDJSON, CSV, or regex-text parser and source from sample logs, upload the sample workload, and verify parse failures |
| **Visualize** | `visualize` | Generate pie, bar, line, area, table, tile, treemap, heatmap, histogram charts |
| **Dashboards** | `create_dashboard`, `add_dashboard_tile`, `list_dashboards`, `delete_dashboard` | Create OCI Management Dashboards with LA widgets, grid layout, and scope filters |
| **Alerts** | `create_alert`, `update_alert`, `list_alerts`, `delete_alert` | Create OCI-native alarms from LA queries with metric extraction and ONS notifications |
| **Saved searches** | `create_saved_search`, `update_saved_search`, `list_saved_searches`, `delete_saved_search` | Manage native LA saved searches; existing scheduled-task IDs remain supported |
| **Export** | `export_results` | Export query results to CSV or JSON |
| **Manage scope** | `set_compartment`, `set_namespace`, `find_compartment`, `list_compartments` | Switch compartments, query across tenancy |
| **Validate** | `validate_query`, `get_query_examples` | Check syntax, get curated starter examples by category (included with install) |
| **Remember** | `save_learned_query`, `get_preferences`, `remember_preference` | Save queries for improved future suggestions, learn field preferences and time ranges per log source |
| **Monitor** | `test_connection`, `get_current_context`, `get_log_summary` | Check connectivity, see current config, view log volume |

Saved-search usage markers follow the catalog cache, which lasts five minutes by
default. They help you browse the catalog. Deletion checks always read current OCI
dependencies and refuse while a rule still uses the search. Alert reads remain live.

A saved-search query update checks linked alerts before writing. If a later alert
refresh fails, the result reports `partial_failure`, the successful updates and
the remaining alert IDs. Review those alerts before retrying.

## Monitoring metrics

Logs and metrics use one server and the same OCI identity. Each service initializes
when needed. Metrics work without a Log Analytics namespace; a service-specific
failure leaves the other service usable. Common authentication failure affects
both. The two `monitoring://` help resources remain available without working
authentication.

For metrics only, create `~/.oci-logan-mcp/config.yaml` with your authentication
method and Monitoring defaults. This instance-principal example needs no Log
Analytics section:

```yaml
oci:
  auth_type: instance_principal
monitoring:
  default_compartment_id: <compartment OCID>
  region: <OCI region>
  max_window_hours: 24
  timeout_seconds: 60
  max_series: 20
  max_datapoints: 4000
  max_response_bytes: 262144
```

On a laptop, use `auth_type: config_file` and the OCI config/profile described in
Quick Start. For combined operation, retain your existing `log_analytics` section
and add `monitoring`. The setup wizard continues to configure logs; edit this
section directly for metrics.

Environment overrides use the prefix `OCI_LOGAN_MONITORING_` with suffixes
`COMPARTMENT`, `REGION`, `MAX_WINDOW_HOURS`, `TIMEOUT`, `MAX_SERIES`,
`MAX_DATAPOINTS` and `MAX_RESPONSE_BYTES`. Invalid Monitoring settings block metrics
with a safe setting-name error while logs remain usable.

The explicit compartment wins, followed by the user's remembered/session choice,
Monitoring default, then Log Analytics default. Region comes from the call,
Monitoring config, authenticated OCI config, then signer. Subtree is false by
default and requires a tenancy-root scope when the tenancy is known. `set_namespace`
changes only the Log Analytics namespace. No service silently changes region or
compartment after a failure.

### Monitoring permissions

Grant discovery with `inspect metrics` and data queries with `read metrics`.
`read metrics` includes inspection. For example, an instance dynamic group that
needs both can use:

```text
Allow dynamic-group <dynamic-group-name> to read metrics in compartment <compartment-name>
```

Use `group` instead of `dynamic-group` for an API-key user's group. These tools
need no Compute-instance read, alarm read or administrative permission. Existing
log and alarm tools retain their separate IAM requirements. See the official
[Monitoring policy reference](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/monitoringpolicyreference.htm).

### Discovery and queries

Start with `test_connection({"service":"metrics"})`; inspection success does not
prove data-query permission. `get_current_context` reports configuration, created
clients and the scope of previous permission checks without probing OCI. Use
`test_connection({"service":"all"})` for separate logs and metrics diagnostics.
Omitting `service` preserves the existing logs check.

```text
list_metric_namespaces({"limit":50})
list_metrics({"namespace":"oci_computeagent","metric_name":"CpuUtilization"})
search_metric_catalog({"query":"cpu"})
query_catalog_metric({"metric_key":"cpu","time_range":"last_1_hour"})
query_metrics({"namespace":"oci_computeagent","query":"CpuUtilization[5m].mean()","time_range":"last_24_hours"})
```

Discovery returns one native page. When `more_available` is true, pass its
`next_page_token` with the same scope and filters. Search is local, reports
ambiguity and never executes a query. Catalog queries require an exact reviewed
key. Raw MQL supports any authorized namespace, including custom metrics outside
the catalog. The catalog adapts reviewed data from Argus and OCI-Mon; source
commits, official URLs and withheld definitions ship in its manifest. Source
review does not establish live coverage in your tenancy.

Catalog filters refuse quotes, backslashes and control characters until their
escaped meaning is verified against a matching live stream. Literal `*` and `|`
remain supported with equality. Use expert raw MQL for explicit `=~` fuzzy matching
or other advanced expressions.

The query interval and resolution are different. Raw resolution defaults to the
largest supported value no greater than every operand interval; catalog
resolution follows its interval. An explicit resolution cannot exceed an operand
interval. The response states which value was used. The server allows at most
24 hours per call. OCI retention measured from now is 7 days at 1m, 30 days at 5m
and 90 days at 1h or 1d. See the [MQL reference](https://docs.oracle.com/en-us/iaas/Content/Monitoring/Reference/mql.htm).

Results label compact `[timestamp,value]` pairs with `point_columns`. Zeros stay
zero and gaps stay gaps. Per-expression facts appear once in `expressions`; each
stream has an `expression_index` and retains its full identity and units. Defaults
are 20 series and 4,000 points across the response, with hard maxima of 100 series
and 10,000 points. The configured byte cap also applies. A window that cannot fit
one whole series is rejected before OCI with compatible alternatives. Runtime
caps omit whole series and disclose counts and reasons. A capped subset cannot
establish fleet-wide worst-resource or top-N claims.

For 24 hours, a 5m interval returns about 289 points per stream. Choose explicit
1h aggregation and resolution when hourly fleet trends answer the question:

```text
query_catalog_metric({"metric_key":"cpu","interval":"1h","resolution":"1h","aggregation":"mean","time_range":"last_24_hours"})
```

State that hourly means can hide short spikes, and report interval and coverage.
Narrow dimensions before increasing caps. A smaller response can reduce tokens
per call, but extra queries to recover omitted data can increase total tokens.
The [Monitoring guide](src/oci_logan_mcp/data/monitoring_syntax.md) and
[starter calls](src/oci_logan_mcp/data/monitoring_templates.yaml) also explain load
balancer component levels and recovery.

Monitoring requests share the session query budget with logs. Timeouts keep their
charge because OCI work may still run. Session byte and cost estimates describe
estimated Log Analytics work; Monitoring responses explicitly say their service
cost is not estimated. MQL and dimension values enter audit as hashes and lengths
under both retention modes. MQL never enters the Log Analytics learner or query
history. Authorized dimensions still appear in tool results.

Correlate logs and metrics using resource id, region, verified compartment and the
same window. Matching timestamps alone prove neither cause nor an incident.
Metrics charts/CSV, automatic MQL learning/sharing and alarm-history expansion
are deferred. Existing log workflows continue unchanged.

### Repeatable live validation

From an installed environment, use the repository's read-only validator:

```bash
python scripts/validate_metrics_live.py --config ~/.oci-logan-mcp/config.yaml \
  --python venv/bin/python --compartment-id <compartment-OCID> \
  --region <OCI-region> --output-dir /tmp/monitoring-validation-new
```

It uses a new private HOME and stores, compares one selected stream with a direct
SDK request over the same fixed UTC window and records each applicable starter.
Keep `private-results.json`, copied settings and `server.stderr` private. Share
`review-summary.json` for coverage. Exit 0 means required live gates passed, 1
means a query or comparison failed, and 2 means authorization/data prerequisites
remain pending. A CPU result does not certify every catalog row.

## Investigation Toolkit

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

The flagship. Given a seed Logan query + time range, returns a structured investigation: stopped sources (J1), parser failures (J2), top_k anomalous sources vs. the prior equal-length window (A2), with per-source top error clusters (Logan `cluster`), top entities (host/user/request_id), and a recent-events timeline. Target: ≤20s p95 on dogfood data.

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

**P0 limitations** (documented honestly, not magic):
- Only the seed's pre-pipe search clause is used for drill-down scoping. A seed like `'Event' = 'error' | where Severity = 'critical'` investigates **all** `'Event' = 'error'` rows: the `where` narrowing is dropped for drill-down. Fix: put all scoping in the pre-pipe filter.
- `top_k` is clamped to `[1, 3]`: matches the ~21-query budget and ≤20s latency guarantee.
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

P0 constraints:
- Logan-managed monitoring alarms only. Non-Logan alarms return structured error codes.
- `window_before_seconds` defaults to the alarm's `pending_duration`; if that metadata is absent or unparsable, it falls back to 300 seconds.
- Raw top-row expansion is suppressed when the stored Logan query degrades to an unscoped `*`. In that case the response sets `seed.seed_filter_degraded: true`, returns `top_contributing_rows: []`, and explains the omission via `top_contributing_rows_omitted_reason: "unscoped_seed_filter"`.
- `dashboard_id` is always `null` in P0; explicit dashboard linkage is deferred.

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

P0 behavior:
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

P0 behavior:
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

P0 behavior:
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

P0 behavior:
- Input accepts exactly one of inline `report.markdown` or stored `report.report_id`.
- `format="pdf"` generates a local PDF and sends it to Telegram as a document. Email and Slack receive the inline summary.
- `format="markdown"` skips PDF generation and sends inline summaries/messages only.
- `format="both"` sends the PDF to Telegram and inline summaries to email/Slack.
- Telegram uploads are rejected before send if the PDF exceeds the 50 MB Bot API document limit.
- ONS email delivery publishes an inline Markdown/plaintext summary to an OCI Notifications topic. ONS does not attach the PDF and no Object Storage/PAR link is generated in P0.
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
{"tool": "generate_incident_report", "investigation": "<A1 response>", "format": "markdown"}
```

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

P0 behavior:
- Playbooks are stored per user under the Logan base directory.
- The `record_investigation` call itself is omitted from captured steps because it is the capture boundary, not part of the investigation.
- P0 is record/catalog only. Replay, parameterization, and report generation are deferred.

## Learning

Each user has separate query and preference files under `~/.oci-logan-mcp/users/<username>/`. Select a name with `--user firstname.lastname` or `LOGAN_USER`; the system user is the fallback.

- Successful complex queries are scored and auto-saved. Trivial queries are skipped.
- Automatic learning keeps at most 3 variants per query shape and 20 automatic entries per log source. It removes entries with the fewest recorded successes, then the lowest usage and oldest use.
- The personal store holds at most 200 queries. Automatic eviction protects manual saves; a store filled with manual queries skips additional automatic saves.
- `save_learned_query` marks an explicit save as manual and gives it a promotion-eligible interest score. Within the relevant learned examples, explicit saves rank ahead of automatic saves in `get_query_examples`.
- Preferences track field usage, time ranges and disambiguation choices. `set_compartment` remembers your default compartment without rewriting the shared configuration file.
- Shared examples show `source`, `user_count` and `success_count`. Execution counts describe recorded use, not proof that a query is correct for another environment.

If a name conflicts with a built-in or shared query, `save_learned_query` returns a `collision_warning`. Use `rename_to` for another name or `force=true` to override the collision.

Shared-template promotion is an administrator-run feature for trusted team deployments. See [promoting shared templates](docs/team-deployment.md#promoting-shared-templates) for thresholds, scheduling and content review.

## Cost + ETA estimation

`run_query` responses carry `estimated_bytes`, `estimated_rows`, `estimated_cost_usd`, `estimated_eta_seconds`, `estimate_confidence` and `estimate_rationale`.

`explain_query` estimates the requested query instead of executing it in full. Estimation can issue count probes against OCI and caches those probes. A cached query response reuses its recorded estimate. Estimates are advisory and do not represent an OCI bill.

A whole-query deadline covers SDK calls, pagination, rate-limit waits and retries. Set `query.timeout_seconds` in the configuration. A timeout releases the caller, but an SDK thread and OCI request may continue until the HTTP call returns. The server does not add an explicit in-flight query cap beyond its existing rate limiting. Timed-out queries retain their budget reservation because work may still be running.

## Session query budget

The query execution path applies these per-session limits:

| Limit | Default |
|---|---|
| `max_queries_per_session` | 100 |
| `max_bytes_per_session` | 10 GiB |
| `max_cost_usd_per_session` | $5.00 |

Call `get_session_budget` to inspect usage and remaining allowance. Cached query responses do not consume a new query reservation. Several investigation and visualization tools use the same query path.

`run_batch_queries` accepts at most 10 queries and reserves budget atomically for each live item. Cache hits reuse results without another reservation. A refusal appears in that item's result as `BudgetExceededError`; a batch has no budget override. Use a confirmed `run_query` call to override an individual query. Do not treat the session budget as a tenancy spending limit.

Queries without a `'Log Source'` filter have low-confidence volume estimates and may report zero estimated bytes and cost. Their query-count limit still applies, but the byte and cost limits cannot bound an unknown scan size.

A `run_query` call with `budget_override=true` follows the confirmation flow. An override still records usage.

```yaml
budget:
  enabled: true
  max_queries_per_session: 100
  max_bytes_per_session: 10737418240
  max_cost_usd_per_session: 5.00
```

Set `budget.enabled: false` to disable these limits.

## Destructive Operation Safety

All 69 tools remain advertised. Writes are enabled by default, with confirmation required for guarded operations.

### Read-only mode

```bash
oci-logan-mcp --read-only
# Environment variable alternative:
OCI_LOGAN_MCP_READ_ONLY=1 oci-logan-mcp
```

Read-only mode blocks OCI resource changes, outbound delivery, context/preference changes, learned-query saves, confirmation-secret setup, and playbook creation/deletion. Queries, listing, validation, visualization and permitted local exports/reports remain available. Cache, history and audit files can still be written. OCI IAM controls the data the process can access.

Tool hints follow this same policy: OCI calls stay within the authenticated tenancy, only external delivery is open-world, and tools that write local reports, exports or transcripts remain read-only.

### Guarded operations

Guarded tools include alert, saved-search and dashboard creation, updates and deletion; dashboard tile changes; log-source onboarding; and playbook deletion. A budget override also requires confirmation.

1. The first call returns an action summary and a single-use confirmation token.
2. The follow-up supplies that token and the user's confirmation secret.

This is **two-step server-side confirmation**. It helps a well-behaved client avoid accidental execution. It does not provide an independent approval channel against a compromised client that can submit both values.

Tokens validate the tool, submitted arguments, and effective namespace, compartment and subcompartment setting. Scope changes wait for running operations. Changing the default scope between preview and execution requires a new confirmation token.

Token expiry defaults to 300 seconds:

```yaml
guardrails:
  token_expiry_seconds: 300
```

### Per-user confirmation secrets

Secrets require at least eight characters and are stored as salted `hashlib.scrypt` hashes in `~/.oci-logan-mcp/users/<username>/confirmation_secret.hash`.

Startup succeeds without a secret. Unguarded tools remain available; guarded operations require first-time setup through `setup_confirmation_secret`. To replace a forgotten or existing secret, use an interactive terminal:

```bash
oci-logan-mcp --user firstname.lastname --reset-secret
```

The former `OCI_LA_CONFIRMATION_SECRET` variable is no longer used.

### History and audit records

Operational logs redact confirmation secrets on root handlers present at server startup. Embedding applications that add handlers later must apply `install_secret_redaction` to those handlers.

Private query history is stored at `~/.oci-logan-mcp/users/<username>/history/queries.log`, with POSIX mode `0600` and rotation. It does not propagate through the operational logger. `logging.query_logging: false` disables file history; the recent-query resource remains in memory for the session.

Audit records are stored separately at `~/.oci-logan-mcp/logs/audit.log`. New records contain `args_redacted`, not a duplicate raw `args` field. The local single-user default retains redacted argument text so investigation playbooks and transcripts remain useful.

Set `audit.store_arguments: false`, or `OCI_LOGAN_AUDIT_STORE_ARGUMENTS=0`, to replace query/free-text arguments with hashes and lengths, limit result summaries to selected numeric/boolean fields, and fingerprint errors. Such records carry `text_retained: false`. Playbooks recorded from them carry `text_omitted: true` and a warning that omitted arguments require review before replay. This audit setting does not disable private query history.

### Transcript export

Use `export_transcript(session_id="current")` to export the current process's audit trail. Output defaults to `~/.oci-logan-mcp/transcripts/`; `transcript_dir` changes the destination.

- `include_results=false` omits result summaries.
- `redact=true` applies additional built-in masking.
- The export preserves `text_retained` so readers can identify omitted text.

A session ID belongs to a server process and can contain multiple investigations. Promotion runs do not currently emit MCP audit events.

### Failure behavior

Guarded operations fail when the secret is missing, corrupt or wrong, or when a token is expired, reused or does not match the submitted operation. Required audit failures prevent the operation from executing. Read-only calls use best-effort auditing.

Report security issues through the process in [SECURITY.md](SECURITY.md).

## Development

Use the `venv/` environment and install the development dependencies:

```bash
pip install -e ".[dev]"
```

Run the regression suite with a temporary home and Matplotlib cache:

```bash
python - <<'PYTEST'
import os
from pathlib import Path
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix="logan-tests-") as scratch:
    environment = os.environ.copy()
    environment["HOME"] = scratch
    environment["MPLCONFIGDIR"] = str(Path(scratch) / "matplotlib")
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        env=environment, check=True,
    )
PYTEST
```

The suite builds a real wheel and speaks MCP stdio to subprocesses using a fake OCI backend. CI is configured for Linux and macOS with Python 3.11, 3.12, 3.13 and 3.14. Live OCI integration checks are separate and require configured access:

```bash
python run_tests.py
```

## Version History

### 1.3.0 (unreleased)

- Require MCP SDK 1.22.0 and OCI SDK 2.104.2 or later so installations include the APIs used by the server.
- Create native saved searches with their query and widget configuration, and retain support for existing scheduled-task IDs.
- Translate five-field alert schedules to OCI's cron format and reject invalid field ranges before creating resources.
- Compare alert thresholds with the value each scheduled run extracts, over a window that spans the schedule, and run every 15 minutes by default.
- List console-made log alerts next to Logan's, with each alert's live condition and threshold.
- Refresh an alert's stored query and repair it whenever its query or backing saved search changes, and warn before the change about the alerts that depend on it.
- Delete an alert without deleting its saved search, keep a detection rule that other alerts still use, and show who created a console-made alert before changing or deleting it.
- Mark saved searches used by detection rules and refuse deletion until those rules are removed, with a fresh dependency check before each delete.
- Preserve untagged console alarm conditions, recognise every metric in a compound condition, and confirm missing detection rules through OCI inventory.
- Preflight linked alerts before saved-search writes, report partial refreshes, and reuse listing markers with bounded reads during each catalog refresh.
- Fetch entity inventories with larger pages outside the event loop, and use the SDK's supported source filter for field discovery.
- Preserve OCI's native metric units while accepting capitalization variants of percent.
- Report aggregate log counts accurately in connection diagnostics, including zero.
- Return safe generic errors when service-context diagnostics fail.
- Add five Monitoring metric tools and two resources for MQL syntax and starter queries.
- Query metrics without configuring Log Analytics, with separate service initialization.
- Include 23 reviewed metric definitions with source provenance; withhold unverified candidates.
- Check logs, metrics or both with `test_connection(service=...)`.
- Add Monitoring settings and `OCI_LOGAN_MONITORING_*` environment overrides.
- Document separate `inspect metrics` and `read metrics` IAM permissions.
- Fix wheel construction and include both query catalogs.
- Set the Python minimum to 3.11 and report one package version.
- Publish server instructions, tool titles and annotations.
- Expose all supported time presets consistently.
- Run query SDK calls off the event loop with one whole-query deadline.
- Restore resource reads and return actionable initialization errors.
- Remember compartments per user and resolve configuration paths consistently.
- Score explicit saves, preserve manual origin and rank them ahead of automatic saves.
- Apply automatic variant/source caps and make promotion status match the shared file.
- Include the Log Analytics query in an alert's backing saved search.
- Remove raw audit argument duplication, isolate private history and configure audit text retention.
- Show execution/user counts on shared examples.
- Exercise real MCP stdio and configure an eight-job CI matrix with wheel builds.
- Move internal installer/documents out of tracked publication files and add public setup, deployment and security guidance.
- Report blocked, unknown and failed tool calls as MCP errors.
- Limit batches to ten queries and reserve budget for each live item.
- Bind confirmations to the effective scope and wait for active operations before changing scope.
- Sanitize shared metadata, redact IPv6 addresses and withdraw rejected or replaced patterns; remove orphaned patterns only after complete scans.
- Preserve whitespace inside quoted query text and keep learning outcomes separate for different scopes.
- Record execution outcomes for batch, visualization and export queries.
- Reject unsafe user identifiers and migrate legacy queries only to the OS owner's private store, once.
- Redact confirmation secrets on logging handlers, including child logs and handlers attached before startup.
- Fix starter queries and diagnose common Log Analytics syntax errors before execution.
- Use discovered grouping fields in host templates instead of assuming Host Name exists in every tenancy.
- Replace the broad substring starter with keyword matching; counts follow keyword semantics.
- Clarify core tool descriptions and record per-call MCP client claims in audit events.
- Recognize the keyword starter during automatic learning while keeping legacy query semantics distinct.
- Install confirmation-secret redaction once per logging handler.


### 1.2.0 (2026-04-17)

#### Internal architecture
- Unified query catalog across builtin templates, starter examples, personal learned queries, and shared promoted queries. Single `UnifiedCatalog` module with surface-specific precedence rules.
- Stable `entry_id` (UUID4) on every personal learned query with legacy backfill migration.
- Cross-user promotion dedup now uses canonical key `(name.lower(), normalized_query_text)`: fixes the multi-user aggregation bug where two users saving the same query were counted separately.
- Shared-catalog lock (`shared/catalog.lock`) protects promoted-queries writes from racing with concurrent user saves.

#### User-visible
- `save_learned_query` gains optional `force` and `rename_to` parameters for resolving collisions with built-in or community queries.
- Onboarding (`get_query_examples`) now includes top-N community favorites alongside starter examples.
- Scheduled promotion guide at `docs/cron-scheduling.md`.

#### Removed
- `list_learned_queries` MCP tool: learning is now invisible infrastructure.
- `delete_learned_query` MCP tool: orphaned without list.
- `ContextManager.{save,list,delete}_learned_query`, `get_all_templates`, and `record_query_usage`: replaced by `UserStore` + `UnifiedCatalog`.
- `src/oci_logan_mcp/templates/query_templates.yaml`: unwired dead file.

#### Behavior changes
- `QueryAutoSaver` now requires `user_store` (no fallback to ContextManager).
- Servers running without user_store configured will fail fast at startup.

---

| Version | Summary |
|---|---|
| **0.5.0** | **Dashboard creation:** Programmatic OCI Management Dashboard creation with proper LA widget wiring, 2-column grid layout, scope filter integration (`parametersMap` with `$(dashboard.params.*)` references), and dashboard delete/update with cleanup. Visualization types: bar, line, pie, table, area, treemap, heatmap, histogram. |
| **0.4.0** | **Alarms & safety:** OCI-native autonomous alerts from Log Analytics queries (metric extraction + OCI Monitoring alarms + ONS notifications). Two-step confirmation for destructive operations with per-user hashed secrets, audit logging, and query history and audit logging. |
| **0.3.0** | Multi-user learning: per-user query storage, preference tracking, shared query promotion with sensitive data sanitization, thread-safe file locking. |
| **0.2.0** | Cluster query accuracy fix, compact cluster output formatting, compartment persistence, startup responsiveness (deferred schema refresh), `--setup` and `--user` CLI flags, Windows setup guide. |
| **0.1.0** | Initial release: 24 MCP tools, query execution, schema exploration, visualization, export, cross-compartment queries, caching, rate limiting, query auto-save. |

## License

MIT
