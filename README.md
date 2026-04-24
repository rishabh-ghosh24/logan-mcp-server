# OCI Log Analytics MCP Server

An MCP server that connects AI assistants (Claude, Codex, etc.) to [OCI Log Analytics](https://docs.oracle.com/en-us/iaas/log-analytics/home.htm). Query, visualize, and export log data through natural language.

## Quick Start

### 1. Install

```bash
git clone https://github.com/rishabh-ghosh24/logan-mcp-server.git
cd logan-mcp-server
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

### 2. Configure

**Choose your auth method:**

| Method | When to use | Setup |
|---|---|---|
| `instance_principal` | Running on an OCI VM (recommended) | [Instance principal setup](#instance-principal-setup) |
| `config_file` | Running on your laptop | Uses `~/.oci/config` — [OCI CLI setup guide](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm) |

```bash
# Set your auth method
export OCI_LA_AUTH_TYPE=instance_principal   # or config_file

# Run the interactive setup wizard
oci-logan-mcp --setup
```

The wizard will prompt for your Log Analytics namespace and default compartment.

Or set environment variables directly:

```bash
export OCI_LA_NAMESPACE=your-namespace
export OCI_LA_COMPARTMENT=ocid1.compartment.oc1..xxxxx
```

### 3. Connect your AI assistant

> **Important:** Always use the `oci-logan-mcp` entry point, not `python -m oci_logan_mcp`.

#### Local (server runs on same machine)

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

#### Remote VM via SSH

For running on an OCI VM with instance principal auth. This config goes in your **local** MCP client:

```json
{
  "mcpServers": {
    "oci-log-analytics": {
      "command": "ssh",
      "args": [
        "-i", "~/.ssh/your-key",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
        "opc@your-vm-ip",
        "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user firstname.lastname"
      ]
    }
  }
}
```

> **SSH keepalive:** The `ServerAliveInterval=60` and `ServerAliveCountMax=3` options send a keepalive packet every 60 seconds and disconnect after 3 missed responses. Without these, idle SSH connections can be silently dropped by firewalls or NAT gateways, causing the MCP server to disconnect unexpectedly.

#### Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.oci-log-analytics]
command = "ssh"
args = ["-i", "~/.ssh/your-key", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3", "opc@your-vm-ip", "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user firstname.lastname"]
```

#### Codex App

In the Codex app, go to **MCP settings → Connect to a custom MCP** and fill in:

| Field | Value |
|---|---|
| **Name** | `oci-log-analytics` |
| **Type** | `STDIO` |
| **Command to launch** | `ssh` |
| **Argument 1** | `-i` |
| **Argument 2** | `/path/to/.ssh/your-key` |
| **Argument 3** | `-o` |
| **Argument 4** | `StrictHostKeyChecking=no` |
| **Argument 5** | `-o` |
| **Argument 6** | `ServerAliveInterval=60` |
| **Argument 7** | `-o` |
| **Argument 8** | `ServerAliveCountMax=3` |
| **Argument 9** | `opc@your-vm-ip` |
| **Argument 10** | `cd /home/opc/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user firstname.lastname` |

Click **Save**, then start a new Codex session to connect.

> **Windows users:** Try the standard SSH config above first. If the server connects but immediately disconnects, Windows OpenSSH may not be handling stdio correctly. In that case, use PuTTY's `plink.exe` instead — see the [Windows setup guide](docs/windows-setup.md).

## What You Can Do

| Capability | Tools | Examples |
|---|---|---|
| **Query logs** | `run_query`, `run_batch_queries`, `run_saved_search` | Search logs, run multiple queries in parallel, execute saved searches |
| **Triage diffs** | `diff_time_windows`, `pivot_on_entity`, `ingestion_health`, `parser_failure_triage`, `investigate_incident`, `why_did_this_fire`, `find_rare_events`, `trace_request_id`, `related_dashboards_and_searches` | Compare a query across two time windows; pull all events for an entity across sources; probe per-source ingestion freshness; surface top parser failures; one-call first-cut investigation orchestrator; replay a Logan-managed alarm's historical fire window; surface low-frequency field values for a source; search common request-id / trace-id fields across sources; find existing dashboards and saved searches related to a source, entity, or field |
| **Investigation playbooks** | `record_investigation`, `list_playbooks`, `get_playbook`, `delete_playbook` | Save the current session's audited tool calls as a named playbook, then list, fetch, or delete recorded playbooks |
| **Reports** | `generate_incident_report` | Convert an investigation result into deterministic Markdown, with optional HTML rendering |
| **Explore schema** | `list_log_sources`, `list_fields`, `list_entities`, `list_parsers`, `list_labels` | Discover what log data is available |
| **Visualize** | `visualize` | Generate pie, bar, line, area, table, tile, treemap, heatmap, histogram charts |
| **Dashboards** | `create_dashboard`, `add_dashboard_tile`, `list_dashboards`, `delete_dashboard` | Create OCI Management Dashboards with LA widgets, grid layout, and scope filters |
| **Alerts** | `create_alert`, `update_alert`, `list_alerts`, `delete_alert` | Create OCI-native alarms from LA queries with metric extraction and ONS notifications |
| **Saved searches** | `create_saved_search`, `update_saved_search`, `list_saved_searches`, `delete_saved_search` | Manage LA saved searches backed by scheduled tasks |
| **Export** | `export_results` | Export query results to CSV or JSON |
| **Manage scope** | `set_compartment`, `set_namespace`, `find_compartment`, `list_compartments` | Switch compartments, query across tenancy |
| **Validate** | `validate_query`, `get_query_examples` | Check syntax, get curated starter examples by category (included with install) |
| **Remember** | `save_learned_query`, `get_preferences`, `remember_preference` | Save queries for improved future suggestions, learn field preferences and time ranges per log source |
| **Monitor** | `test_connection`, `get_current_context`, `get_log_summary` | Check connectivity, see current config, view log volume |

## Investigation Toolkit

### `ingestion_health` — is ingestion even working?

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

### `parser_failure_triage` — which parsers are broken?

Surface the top log sources with parse failures, ranked by volume. Returns up to 20 sources, each with failure count, first/last seen timestamps, and up to 3 sample raw lines that failed to parse. Each source has one parser configured, so this tells you which parser needs fixing before investigating an incident.

```json
{
  "tool": "parser_failure_triage",
  "time_range": "last_24_hours",
  "top_n": 10
}
```

Returns `{failures: [...], total_failure_count: N}` where each entry carries `source`, `failure_count`, `first_seen`, `last_seen`, and `sample_raw_lines` (up to 3).

If the session budget is exhausted between the stats and samples queries, the ranked failure list is preserved and the response adds `partial: true, partial_reason: "samples_budget_exceeded"` with empty `sample_raw_lines` on each entry — so you still see which sources are failing even when samples can't be fetched.

### `investigate_incident` — one-call first-cut triage

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

**P0 limitations** (documented honestly, not magic):
- Only the seed's pre-pipe search clause is used for drill-down scoping. A seed like `'Event' = 'error' | where Severity = 'critical'` investigates **all** `'Event' = 'error'` rows — the `where` narrowing is dropped for drill-down. Fix: put all scoping in the pre-pipe filter.
- `top_k` is clamped to `[1, 3]` — matches the ~21-query budget and ≤20s latency guarantee.
- `alarm_ocid` seed and NL-to-query `description` seed are deferred (A6 will own alarm-OCID).
- J1's freshness is evaluated over its configured `freshness_probe_window`, which may differ from the investigation `time_range`; the report's `ingestion_health.note` explains the difference.

**Partial responses.** A1 never raises `BudgetExceededError` out of its boundary — instead, the report comes back with `partial: true` and specific `partial_reasons`:
- `"budget_exceeded"` — session budget ran out mid-investigation
- `"timeline_omitted"` — one or more per-source timeline queries errored
- `"entity_discovery_partial"` — one or more entity fields weren't valid for a source (`InvalidParameter`)
- `"source_errors"` — a non-budget, non-field-variance infrastructure failure occurred somewhere in a per-source branch (cluster query 5xx, unexpected ServiceError in entity discovery, transport timeout, or a whole-branch-level exception). Unlike `timeline_omitted`, this also covers failures in the cluster/entity sub-phases even if the rest of the branch completed — the sub-phase's empty result is accompanied by an `errors` entry naming the failure.

Each condition is accompanied by `anomalous_sources[*].errors` entries describing exactly what went wrong per source.

### `why_did_this_fire` — replay a Logan alarm fire window

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

### `find_rare_events` — surface low-frequency field values

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

### `trace_request_id` — stitch request-id events across sources

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

### `related_dashboards_and_searches` — reuse existing dashboards and queries

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

### `generate_incident_report` — deterministic incident report

Convert an `investigate_incident` response into a deterministic Markdown incident report, with optional HTML rendering:

```json
{
  "tool": "generate_incident_report",
  "investigation": {
    "summary": "Apache errors spiked and parser failures were present.",
    "anomalous_sources": []
  },
  "format": "html",
  "summary_length": "standard"
}
```

Returns `{report_id, markdown, html, metadata, artifacts}`. Markdown is always returned. `html` is populated only when `format="html"`.

P0 behavior:
- Template-first and deterministic; no internal LLM provider is called.
- Source is an A1 `InvestigationReport` object only.
- Supported sections are `executive_summary`, `timeline`, `top_findings`, `evidence`, `recommended_next_steps`, and `appendix`.
- Playbook-run reports, session-id reports, PDF generation, and report delivery are separate follow-ups.

### `record_investigation` — save a playbook from the audit trail

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

## Multi-User Learning

The server learns from usage and improves over time. Each user gets isolated storage, and the best queries are promoted to benefit everyone.

### How it works

- **Per-user query storage** — Each user's saved queries and preferences are stored separately (`--user` flag or `LOGAN_USER` env var)
- **Auto-learning** — The server tracks which fields you use with each log source (field affinity), your preferred time ranges, and disambiguation choices
- **Shared templates** — An admin runs the promotion script to promote high-quality queries to a shared library available to all users

### User identity

Each MCP connection identifies itself with a username. The server automatically creates a storage directory for new users on first connection — no manual setup needed.

**Use `firstname.lastname` format** to avoid conflicts (e.g., `--user david.smith` not `--user david`).

The username is resolved in this order:
1. `--user <name>` flag (highest priority)
2. `LOGAN_USER` environment variable
3. System `$USER` (default fallback — usually `opc` on shared VMs, so always set `--user` explicitly)

**Example:** In your MCP client SSH config, append `--user firstname.lastname` to the remote command:

```
cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user david.smith
```

For Codex CLI (`~/.codex/config.toml`):

```toml
[mcp_servers.oci-log-analytics]
command = "ssh"
args = ["-i", "~/.ssh/your-key", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3", "opc@your-vm-ip", "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user david.smith"]
```

Each user's queries and preferences are stored under `~/.oci-logan-mcp/users/<username>/`. When a second user connects with a different name, they get their own isolated storage.

### Promoted query learning

Queries saved via `save_learned_query` (or auto-saved when the server detects a successful complex query) contribute to each user's personal learned catalog. The promotion pipeline evaluates personal queries against quality thresholds and promotes the best ones to a shared catalog visible to all users of the server.

**How promotion works:**

- The shared catalog feeds the `query-templates` MCP resource and the `get_query_examples` onboarding surface (top-N community favorites appear alongside curated starter examples)
- Promotion is invisible to end users — they just get progressively better LLM suggestions over time as the shared catalog grows
- Sensitive data (OCIDs, IPs, emails, secrets) is automatically redacted before promotion

**Promotion thresholds:**

| Scenario | interest_score | success_rate |
|---|---|---|
| Single-user query | >= 4 | >= 0.8 |
| Multi-user query (same query saved by multiple users) | >= 3 | >= 0.7 |

**Collision policy:** If a query name matches an existing builtin or community entry, `save_learned_query` returns a `collision_warning`. Supply `force=true` to overwrite or `rename_to` to save under a different name.

### Promoting shared templates

Promotion is a single-writer admin task — not something each user session runs. Run it from one place (cron, manually, etc.) to avoid conflicts.

```bash
# Run once manually
oci-logan-mcp --promote-and-exit

# With explicit base directory
oci-logan-mcp --promote-and-exit --base-dir /home/opc/.oci-logan-mcp
```

For scheduled automation see [`docs/cron-scheduling.md`](docs/cron-scheduling.md).

## Deploying on an OCI VM

### Fresh VM (Oracle Linux 9)

```bash
curl -fsSL https://raw.githubusercontent.com/rishabh-ghosh24/logan-mcp-server/main/scripts/oci-initial-setup.sh | bash
```

This installs Python, OCI CLI, and the MCP server in one step.

### Existing VM (Python 3.10+ available)

```bash
git clone https://github.com/rishabh-ghosh24/logan-mcp-server.git
cd logan-mcp-server
./scripts/setup_oel9.sh   # Installs Python, pip, creates venv, and installs the MCP server
```

### Instance Principal Setup

Instance principal is the recommended auth method for OCI VMs — no config files to manage.

**Step 1:** Get your compute instance OCID:

```bash
curl -s -H "Authorization: Bearer Oracle" \
  http://169.254.169.254/opc/v2/instance/ | jq -r '.id'
```

**Step 2:** Create a Dynamic Group (e.g., `logan-mcp-dg`) in the OCI Console:

```
ANY {instance.id = '<your-compute-instance-OCID>'}
```

**Step 3:** Add IAM policies at the tenancy level:

```
Allow dynamic-group logan-mcp-dg to use loganalytics-features-family in tenancy
Allow dynamic-group logan-mcp-dg to use loganalytics-resources-family in tenancy
Allow dynamic-group logan-mcp-dg to manage management-dashboard-family in tenancy
Allow dynamic-group logan-mcp-dg to read compartments in tenancy
Allow dynamic-group logan-mcp-dg to manage alarms in tenancy
Allow dynamic-group logan-mcp-dg to read metrics in tenancy
Allow dynamic-group logan-mcp-dg to manage ons-topics in tenancy
Allow dynamic-group logan-mcp-dg to use streams in tenancy
```

> **New tenancy?** If Log Analytics is not yet enabled, an administrator must first add this one-time tenancy-level policy:
> ```
> Allow service loganalytics to read loganalytics-features-family in tenancy
> ```

**Step 4:** Configure and run:

```bash
export OCI_LA_AUTH_TYPE=instance_principal
oci-logan-mcp --setup
```

## Cost + ETA estimation

Every `run_query` response now carries flat estimate fields at the top level: `estimated_bytes`, `estimated_rows`, `estimated_cost_usd`, `estimated_eta_seconds`, `estimate_confidence`, `estimate_rationale`.

Use `explain_query` to get the full estimate **without** running the query. Cache hits replay the last known estimate for the same query/time range — no additional OCI calls.

## Session query budget

Per-session caps prevent runaway agent loops:

| Limit | Default |
|---|---|
| `max_queries_per_session` | 100 |
| `max_bytes_per_session` | 10 GiB |
| `max_cost_usd_per_session` | $5.00 |

Call `get_session_budget` any time to see usage and remaining allowance.

**Scope of enforcement (P0):** budget is enforced on `run_query` only. Cache hits are **free**. `run_batch_queries` is **unbudgeted** in P0 (concurrent execution would race under per-call checks; budgeting for batch is tracked for P1).

> **P0 limitation — source-less queries:** queries without a `'Log Source'` filter cannot be estimated (no source to probe). They return `estimate_confidence="low"` and `estimated_bytes=0`, so the bytes and cost limits are not checked. The **query-count limit still applies**. Full-scan cost estimation requires an OCI "explain" API that does not yet exist.

To exceed a budget in a specific call, pass `budget_override=true` to `run_query`. This is a guarded follow-up pattern: the first call returns a confirmation request, and a second call with `confirmation_token` plus `confirmation_secret` executes. Override does not exempt usage recording.

Configure in `~/.oci-logan-mcp/config.yaml`:

```yaml
budget:
  enabled: true
  max_queries_per_session: 100
  max_bytes_per_session: 10737418240
  max_cost_usd_per_session: 5.00
```

Disable entirely with `budget.enabled: false`.

## Destructive Operation Safety

### Read-only mode

Start the server without any ability to mutate OCI resources or external systems:

```bash
oci-logan-mcp --read-only
# or
OCI_LOGAN_MCP_READ_ONLY=1 oci-logan-mcp
```

In read-only mode the following tools return a `read_only_blocked` error instead
of executing:

- `create_alert`, `update_alert`, `delete_alert`
- `create_saved_search`, `update_saved_search`, `delete_saved_search`
- `create_dashboard`, `add_dashboard_tile`, `delete_dashboard`
- `send_to_slack`, `send_to_telegram`
- `set_compartment`, `set_namespace`, `update_tenancy_context`
- `save_learned_query`, `remember_preference`, `setup_confirmation_secret`
- `record_investigation`, `delete_playbook`

All query, validation, listing, visualization, `export_results`, `list_playbooks`,
and `get_playbook` tools remain available. Use this mode when giving an
untrusted agent, a newcomer, or an automated process access to the server.

All delete and update operations on OCI resources (alerts, dashboards, saved searches) are protected by **two-factor server-side confirmation**. This prevents any MCP client — Claude, Codex, or others — from accidentally modifying or destroying resources.

### Guarded Tools

| Tool | Action |
|------|--------|
| `delete_alert` | Destroys alarm + backing OCI resources |
| `delete_saved_search` | Destroys saved search |
| `delete_dashboard` | Destroys dashboard + tile data sources |
| `update_alert` | Modifies an existing alert |
| `update_saved_search` | Modifies an existing saved search |
| `add_dashboard_tile` | Modifies an existing dashboard |

`create_*` tools are **not** guarded — they are additive and don't affect existing resources.

### Per-User Confirmation Secrets

Each user has their own confirmation secret. Secrets are:

- **Minimum 8 characters**
- **Hashed with `hashlib.scrypt`** — the plaintext is never stored
- **Stored in the user's directory** at `~/.oci-logan-mcp/users/<username>/confirmation_secret.hash`

The server now starts even if a user has no secret yet. Read-only and additive tools work immediately. The first time a user attempts a guarded action, the MCP client can call `setup_confirmation_secret` in-band to create the secret. There is no shared env var — `OCI_LA_CONFIRMATION_SECRET` has been removed.

`setup_confirmation_secret` is for first-time setup only. If a secret is already configured and you need to replace it, use `--reset-secret`.

**Forgotten secret?** Use the `--reset-secret` CLI flag to re-enter a new secret:

```bash
oci-logan-mcp --user firstname.lastname --reset-secret
```

**Admin recovery:** If `--reset-secret` is unavailable (e.g., non-interactive session), delete the hash file manually and restart:

```bash
rm ~/.oci-logan-mcp/users/<username>/confirmation_secret.hash
```

Then either restart and use `setup_confirmation_secret`, or run `--reset-secret` interactively to create a new one immediately.

### How It Works

1. **First call** — returns a human-readable summary of the action + a single-use confirmation token
2. **Second call** — requires the token + your secret to execute

The token is bound to the exact tool and resource. A token issued for `delete_alert(id=A)` cannot authorize `delete_alert(id=B)` or any other tool — reusing a token for a different resource is rejected outright.

Optionally configure token expiry in `config.yaml` (default: 300 seconds):

```yaml
guardrails:
  token_expiry_seconds: 300
```

### Audit Log

All guarded tool interactions are logged as JSON-lines to a shared audit log at:

```
~/.oci-logan-mcp/logs/audit.log
```

Each entry records:

- **who** — the username (`--user` flag or `LOGAN_USER`)
- **what** — tool name and arguments
- **when** — UTC timestamp
- **outcome** — `confirmed`, `confirmation_failed`, `token_expired`, `confirmation_unavailable`, etc.

This gives administrators a full history of which users attempted or executed destructive operations.

Every tool call — including read-only tools — emits an `invoked` entry before any guard runs, giving a complete call-by-call trace.

### Transcript export

Every tool call is recorded in the audit log with a process-scoped `session_id`. Export the current session's trail as JSONL:

```
export_transcript(session_id="current")
```

Output file lands under `~/.oci-logan-mcp/transcripts/` by default (override via `transcript_dir` in config).

Flags:
- `include_results=false` — omit `result_summary` fields (useful when sharing).
- `redact=true` — apply built-in PII/secret masking before writing.

> **Note:** `session_id` is process-scoped — one id per server process. Long-lived servers aggregate many logical investigations under one id. Per-investigation session boundaries are a future enhancement.

> **P0 limitation:** The `--promote-and-exit` path does not emit audit entries; `export_transcript` against a promotion run returns `event_count: 0` by design. Full promotion-run audit coverage is planned for P1.

### Fail-Closed Design

- **No secret set** → guarded tools return `confirmation_unavailable` and point the user to `setup_confirmation_secret`
- **Invalid/corrupt secret file** → guarded tools return `confirmation_unavailable` with recovery guidance
- **Wrong secret** → `confirmation_failed`
- **Token reuse** → rejected (single-use)
- **Token expired** → rejected
- **Resource mismatch** (token for A used on B) → rejected
- **Arguments changed** → rejected

## Development

### Running Tests

```bash
pip install -e ".[dev]"

# Unit tests
pytest tests/ -v

# Integration tests (requires OCI access)
python run_tests.py
```

## Version History

### 1.2.0 (2026-04-17)

#### Internal architecture
- Unified query catalog across builtin templates, starter examples, personal learned queries, and shared promoted queries. Single `UnifiedCatalog` module with surface-specific precedence rules.
- Stable `entry_id` (UUID4) on every personal learned query with legacy backfill migration.
- Cross-user promotion dedup now uses canonical key `(name.lower(), normalized_query_text)` — fixes the multi-user aggregation bug where two users saving the same query were counted separately.
- Shared-catalog lock (`shared/catalog.lock`) protects promoted-queries writes from racing with concurrent user saves.

#### User-visible
- `save_learned_query` gains optional `force` and `rename_to` parameters for resolving collisions with built-in or community queries.
- Onboarding (`get_query_examples`) now includes top-N community favorites alongside starter examples.
- Scheduled promotion guide at `docs/cron-scheduling.md`.

#### Removed
- `list_learned_queries` MCP tool — learning is now invisible infrastructure.
- `delete_learned_query` MCP tool — orphaned without list.
- `ContextManager.{save,list,delete}_learned_query`, `get_all_templates`, and `record_query_usage` — replaced by `UserStore` + `UnifiedCatalog`.
- `src/oci_logan_mcp/templates/query_templates.yaml` — unwired dead file.

#### Behavior changes
- `QueryAutoSaver` now requires `user_store` (no fallback to ContextManager).
- Servers running without user_store configured will fail fast at startup.

---

| Version | Summary |
|---|---|
| **0.5.0** | **Dashboard creation:** Programmatic OCI Management Dashboard creation with proper LA widget wiring, 2-column grid layout, scope filter integration (`parametersMap` with `$(dashboard.params.*)` references), and dashboard delete/update with cleanup. Visualization types: bar, line, pie, table, area, treemap, heatmap, histogram. |
| **0.4.0** | **Alarms & safety:** OCI-native autonomous alerts from Log Analytics queries (metric extraction + OCI Monitoring alarms + ONS notifications). Two-factor confirmation for destructive operations with per-user hashed secrets, audit logging, and secret redaction in all log output. |
| **0.3.0** | Multi-user learning: per-user query storage, preference tracking, shared query promotion with sensitive data sanitization, thread-safe file locking. |
| **0.2.0** | Cluster query accuracy fix, compact cluster output formatting, compartment persistence, startup responsiveness (deferred schema refresh), `--setup` and `--user` CLI flags, Windows setup guide. |
| **0.1.0** | Initial release: 24 MCP tools, query execution, schema exploration, visualization, export, cross-compartment queries, caching, rate limiting, query auto-save. |

## License

MIT
