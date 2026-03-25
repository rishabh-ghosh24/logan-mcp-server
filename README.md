# OCI Log Analytics MCP Server

An MCP server that connects AI assistants (Claude, Codex, etc.) to [OCI Log Analytics](https://www.oracle.com/cloud/log-analytics/). Query, visualize, and export log data through natural language.

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
      "command": "/path/to/logan-mcp-server/venv/bin/oci-logan-mcp"
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
        "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp"
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
args = ["-i", "~/.ssh/your-key", "-o", "StrictHostKeyChecking=no", "opc@your-vm-ip", "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp"]
```

#### Codex App

In the Codex app, go to MCP settings and add a new server:
- **Command:** `ssh`
- **Arguments:** `-i`, `~/.ssh/your-key`, `-o`, `StrictHostKeyChecking=no`, `opc@your-vm-ip`, `cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp`

> **Windows users:** Windows OpenSSH doesn't handle MCP's stdio transport correctly. Use PuTTY's `plink.exe` instead. See [Windows setup guide](docs/windows-setup.md).

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

Pass `--user <name>` when starting the server, or set the `LOGAN_USER` environment variable. Defaults to the system `$USER`.

In your MCP client SSH config, add the flag:

```
cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user alice
```

### Promoting shared templates

Queries are promoted based on interest score and success rate — not use count. A complex query (interest score >= 4) that works is valuable even if used once.

```bash
# Run the promotion script (as admin)
python scripts/promote_queries.py /path/to/.oci-logan-mcp
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
./scripts/setup_oel9.sh
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

## License

MIT
