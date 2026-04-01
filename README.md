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
        "opc@your-vm-ip",
        "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user firstname.lastname"
      ]
    }
  }
}
```

#### Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.oci-log-analytics]
command = "ssh"
args = ["-i", "~/.ssh/your-key", "-o", "StrictHostKeyChecking=no", "opc@your-vm-ip", "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user firstname.lastname"]
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
| **Argument 5** | `opc@your-vm-ip` |
| **Argument 6** | `cd /home/opc/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user firstname.lastname` |

Click **Save**, then start a new Codex session to connect.

> **Windows users:** Try the standard SSH config above first. If the server connects but immediately disconnects, Windows OpenSSH may not be handling stdio correctly. In that case, use PuTTY's `plink.exe` instead — see the [Windows setup guide](docs/windows-setup.md).

## What You Can Do

| Capability | Tools | Examples |
|---|---|---|
| **Query logs** | `run_query`, `run_batch_queries`, `run_saved_search` | Search logs, run multiple queries in parallel, execute saved searches |
| **Explore schema** | `list_log_sources`, `list_fields`, `list_entities`, `list_parsers`, `list_labels` | Discover what log data is available |
| **Visualize** | `visualize` | Generate pie, bar, line, area, table, tile, treemap, heatmap, histogram charts |
| **Export** | `export_results` | Export query results to CSV or JSON |
| **Manage scope** | `set_compartment`, `set_namespace`, `find_compartment`, `list_compartments` | Switch compartments, query across tenancy |
| **Validate** | `validate_query`, `get_query_examples` | Check syntax, get example queries by category |
| **Remember** | `save_learned_query`, `list_learned_queries`, `get_preferences`, `remember_preference` | Save queries, learn field preferences and time ranges per log source |
| **Monitor** | `test_connection`, `get_current_context`, `get_log_summary` | Check connectivity, see current config, view log volume |

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
args = ["-i", "~/.ssh/your-key", "-o", "StrictHostKeyChecking=no", "opc@your-vm-ip", "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user david.smith"]
```

Each user's queries and preferences are stored under `~/.oci-logan-mcp/users/<username>/`. When a second user connects with a different name, they get their own isolated storage.

### Promoting shared templates

Queries are promoted based on interest score and success rate — not use count. A complex query (interest score >= 4) that works is valuable even if used once.

```bash
# Run once manually
python scripts/promote_queries.py /path/to/.oci-logan-mcp

# Or set up a cron job to run every 2 hours
crontab -e
# Add this line:
0 */2 * * * cd /path/to/logan-mcp-server && venv/bin/python scripts/promote_queries.py /home/opc/.oci-logan-mcp >> /var/log/logan-promote.log 2>&1
```

Sensitive data (OCIDs, IPs, emails, secrets) is automatically redacted before promotion.

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

| Version | Summary |
|---|---|
| **0.3.0** | Multi-user learning: per-user query storage, preference tracking, shared query promotion with sensitive data sanitization, thread-safe file locking. |
| **0.2.0** | Cluster query accuracy fix, compact cluster output formatting, compartment persistence, startup responsiveness (deferred schema refresh), `--setup` and `--user` CLI flags, Windows setup guide. |
| **0.1.0** | Initial release: 24 MCP tools, query execution, schema exploration, visualization, export, cross-compartment queries, caching, rate limiting, query auto-save. |

## License

MIT
