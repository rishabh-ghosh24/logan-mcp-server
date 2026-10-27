# OCI Log Analytics MCP Server

Use an MCP-compatible assistant to investigate **OCI logs and metrics in one
conversation**. Ask questions in plain language, inspect the evidence, build
charts and reports, and manage saved searches, dashboards and log-based alerts.

The server provides **69 tools over stdio**. Run it on your computer or on an OCI
VM through SSH. Logs use OCI Log Analytics; metrics use OCI Monitoring. You can
use either service independently, with access controlled by your OCI identity.

## What can I ask?

- "Which log sources have data in the last 24 hours?"
- "What metrics are available for this compute instance? List just the names."
- "Show this instance's CPU utilization over the last day."
- "Compare errors in the last hour with the previous hour."
- "Investigate these application errors and generate a report."
- "Create a dashboard for this query, then preview an alert for it."

The assistant uses the tools to discover data and run queries. Results depend on
what your tenancy collects and what your OCI identity can access. Investigation
results support diagnosis; correlation alone does not establish root cause.

## Quick Start

You need Git, Python **3.11 through 3.14**, an MCP client that supports local
stdio servers, and access to Log Analytics or Monitoring data. The installer
resolves the package's SDK requirements: MCP `>=1.22.0,<2` and OCI `>=2.104.2`.

### 1. Install

These commands select `mcp-publish`, the branch for the 1.3.0 candidate. Cloning
without `--branch` selects the repository's default branch, which may be older.

```bash
git clone --branch mcp-publish https://github.com/rishabh-ghosh24/logan-mcp-server.git
cd logan-mcp-server
python3 -m venv venv
source venv/bin/activate
python -m pip install -e .
```

Use a supported interpreter if `python3` points to another version. These shell
commands use Linux/macOS paths. Native Windows setup has not been verified.
For an OCI VM and remote clients, follow the [team deployment guide](docs/team-deployment.md).

### 2. Configure OCI access

Choose the identity used by the server process:

| Where the server runs | Authentication | What to prepare |
|---|---|---|
| Your computer | `config_file` | An OCI API-key profile in `~/.oci/config` |
| An OCI compute instance | `instance_principal` | A dynamic group and IAM policies for that instance |

For API-key setup, use Oracle's [SDK configuration instructions](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/pythonsdk.htm).
Your administrator must grant the [IAM access](#iam-access) needed by the tools you use.

**For logs, or logs plus metrics**, run:

```bash
oci-logan-mcp --setup
```

Choose the authentication method in the wizard, then confirm the Log Analytics
namespace and default compartment. The wizard saves
`~/.oci-logan-mcp/config.yaml`. It configures logs; add the `monitoring` section
below if you also want explicit Monitoring defaults.

**For metrics only**, create the directory, then save the configuration below as
`~/.oci-logan-mcp/config.yaml`:

```bash
mkdir -p ~/.oci-logan-mcp
```

This laptop example uses the default OCI API-key profile and needs no Log
Analytics section:

```yaml
oci:
  auth_type: config_file
monitoring:
  default_compartment_id: "<compartment OCID>"
  region: "<OCI region>"
```

On an OCI VM, use `auth_type: instance_principal`. Replace the placeholders with
your scope. For combined operation, keep the wizard's `oci` and `log_analytics`
settings and add `monitoring`. See the [Monitoring guide](docs/monitoring.md) for
scope precedence, response limits and environment overrides.

### 3. Connect your assistant

Use the **absolute path** to the executable in `venv/bin`. Replace `your.name`
with a stable user name for your query history and preferences. The name selects
local storage; it does not authenticate an OCI user.

**Claude Desktop:** merge this entry into its configuration file:

```json
{
  "mcpServers": {
    "oci-log-analytics": {
      "command": "/absolute/path/to/logan-mcp-server/venv/bin/oci-logan-mcp",
      "args": ["--user", "your.name"]
    }
  }
}
```

The macOS file is `~/Library/Application Support/Claude/claude_desktop_config.json`.
See the [official local-server guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
for other platforms and client troubleshooting.

**Claude Code:** add the server from a terminal:

```bash
claude mcp add --transport stdio logan -- /absolute/path/to/logan-mcp-server/venv/bin/oci-logan-mcp --user your.name
```

See [Claude Code's MCP documentation](https://code.claude.com/docs/en/mcp) for
configuration scopes and connection status.

**Codex:** add this entry to `~/.codex/config.toml`:

```toml
[mcp_servers.logan-mcp]
command = "/absolute/path/to/logan-mcp-server/venv/bin/oci-logan-mcp"
args = ["--user", "your.name"]
```

See the [official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
Other stdio MCP clients use the same executable and arguments. For a remote
server, use the [SSH configuration examples](docs/team-deployment.md#remote-vm-via-ssh).

### 4. Connect and verify

Reconnect the MCP server or restart your assistant, then ask:

> Test my connection to logs and metrics separately. Show any setup or permission errors.

The assistant can call `test_connection` with `service: "logs"`, `"metrics"` or
`"all"`. Omitting `service` tests logs. Metrics inspection does not prove query
permission; follow it with a small data query.

Then try:

- **Logs:** "List the log sources with data in the last day."
- **Metrics:** "List the available metric namespaces, then the metric names for Compute."

After upgrades, reconnect again so the client loads the new tools and schemas.
If it has no reconnect control, close and reopen the client.

## IAM access

Grant access for the features you use. Metrics do not require Log Analytics,
Compute-instance management, or administrative permissions.

For discovery and metric queries, an example is:

```text
Allow group <your-group> to read metrics in compartment <compartment-name>
```

`inspect metrics` permits discovery only; `read metrics` includes discovery and
data queries. Use `dynamic-group` instead of `group` for an instance principal.
See Oracle's [Monitoring policy reference](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/monitoringpolicyreference.htm).

For logs, use the [Log Analytics policy catalog](https://docs.oracle.com/en-us/iaas/log-analytics/doc/iam-policies-catalog-logging-analytics.html)
to choose grants for queries and resources. Feature-family policies require
tenancy scope; resource-family policies can be compartment scoped. Dashboards,
alarms and Notifications each need their own grants. The [team guide](docs/team-deployment.md#instance-principal-setup)
contains a broader example for deployments that use those features; it is not a
combined requirement for reading logs or metrics.

## Tools by task

| Task | Representative tools | What you get |
|---|---|---|
| Discover data | `get_log_summary`, `list_log_sources`, `list_fields`, `list_entities`, `list_metrics` | Available sources, fields, entities and metric names |
| Query and compare | `run_query`, `run_batch_queries`, `query_metrics`, `query_catalog_metric`, `diff_time_windows` | Log results, metric time series and changes between windows |
| Investigate | `investigate_incident`, `ingestion_health`, `parser_failure_triage`, `pivot_on_entity`, `trace_request_id` | First-cut findings and the supporting evidence |
| Present results | `visualize`, `export_results`, `generate_incident_report` | Log charts, CSV/JSON exports and Markdown/HTML reports |
| Reuse work | `get_query_examples`, `save_learned_query`, `record_investigation`, `list_playbooks` | Query examples, personal learning and recorded investigation steps |
| Manage OCI resources | `create_saved_search`, `create_dashboard`, `create_alert`, `update_alert`, `delete_alert` | Saved searches, dashboards and log-based alarms |
| Deliver reports | `prepare_report_delivery`, `deliver_report`, `list_notification_topics` | Confirmed Notifications summaries and configured delivery channels |
| Control the session | `test_connection`, `get_current_context`, `set_compartment`, `get_session_budget` | Service readiness, scope and remaining query allowance |

The [Monitoring guide](docs/monitoring.md) explains names-first discovery, MQL,
reviewed catalog keys and response caps. The [investigation guide](docs/investigation-tools.md)
contains tool examples, report delivery, sample-log onboarding and playbook recording.

## Learning

Successful complex log queries can be learned automatically. Explicit saves
rank ahead of automatic variants. Each named user has separate query, preference
and history files. MQL is kept out of Log Analytics learning.

Sharing is an administrator-run promotion step for trusted teams, with content
sanitization and recorded usage counts. Those counts do not prove that a query
is correct for another tenancy. See [learning and retention](docs/operations.md#learning)
and [shared-template promotion](docs/team-deployment.md#promoting-shared-templates).

## Destructive Operation Safety

**Writes are enabled by default.** Guarded operations, including OCI resource
creation, updates and deletion, return a preview before execution. The follow-up
requires a single-use token and the user's confirmation secret. Scope and
arguments must still match the preview.

For an optional read-only session, add `--read-only` to the client arguments:

```json
["--user", "your.name", "--read-only"]
```

Read-only mode blocks OCI mutations, outbound delivery and preference/learning
changes. It also blocks `generate_incident_report`, `record_investigation`,
`prepare_report_delivery`, and the session scope changes `set_compartment`,
`set_namespace` and `update_tenancy_context`.

Queries, charts and `export_results` still work. You can pass supported
compartment scope arguments per query without changing the session default.
Cache, history and audit files may still be written. All 69 tools remain visible.

The confirmation flow helps prevent accidental operations by a well-behaved
client. It is not an independent approval channel against a compromised client.
OCI IAM and host access controls determine what the process can access.

Other safeguards include:

- Refusing saved-search deletion while a detection rule still depends on it.
- Showing dependent alerts before query changes and preserving console-authored
  alarm conditions.
- Applying per-session query limits, shared by logs and metrics. The default is
  100 query slots; discovery pages consume slots too.
- Separating private query history from audit records, with configurable audit
  text retention. Query results can contain sensitive data.

Read the [confirmation, budget and audit details](docs/operations.md), the
[trusted-team deployment boundaries](docs/team-deployment.md#user-identity), and
[security reporting policy](SECURITY.md).

## Known limitations

- **Transport:** stdio and SSH are supported. Hosted HTTPS/OAuth is deferred.
- **SSH disconnects:** network interruptions or client IP changes can drop SSH
  connections. The Claude app needs a restart after a drop. Local installations
  are unaffected by this SSH limitation; HTTP transport is the planned fix.
- **Delivery:** real Slack and Telegram sends are unverified. Contract tests and
  safe error handling passed; Notifications email delivery was verified.
- **Platforms:** GitHub CI passes on Linux and macOS with Python 3.11 through
  3.14, using simulated OCI calls. Live OCI release checks ran on Oracle Linux
  with Python 3.11 and 3.14. Native Windows remains unverified.
- **Client caching:** a client can retain old tools or schemas until reconnecting.
- **Metric output:** series, point and byte caps can omit whole series. Results
  disclose this; do not rank a capped subset as the whole fleet. Metrics charts,
  CSV export and automatic MQL learning/sharing are deferred.
- **Investigations:** drill-down uses the seed's pre-pipe filter. Put required
  scoping there; later pipeline filters do not narrow every drill-down.
- **Shared hosts:** user names separate files, not OCI permissions. Connections
  using the same OS account share that account and its configured OCI principal.

## Documentation and development

- [Monitoring and MQL](docs/monitoring.md)
- [Investigation, reports and onboarding](docs/investigation-tools.md)
- [Learning, budgets, confirmation and audit](docs/operations.md)
- [SSH and team deployment](docs/team-deployment.md)
- [Development and verification](docs/development.md)
- [Release notes](CHANGELOG.md)

## License

[MIT](LICENSE)
