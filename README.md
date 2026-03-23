# OCI Log Analytics MCP Server

An MCP server that connects AI assistants (Claude, Codex, etc.) to OCI Log Analytics. Query, visualize, and export log data through natural language.

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

Choose your authentication method, then run the setup wizard:

```bash
# For local/laptop use (reads ~/.oci/config)
export OCI_LA_AUTH_TYPE=config_file

# For OCI VMs (no config file needed — uses IAM policies)
export OCI_LA_AUTH_TYPE=instance_principal

# Run the interactive setup wizard
oci-logan-mcp --setup
```

Or set environment variables directly:

```bash
export OCI_LA_NAMESPACE=your-namespace
export OCI_LA_COMPARTMENT=ocid1.compartment.oc1..xxxxx
```

### 3. Connect your AI assistant

Add to your MCP client configuration:

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "oci-log-analytics": {
      "command": "/path/to/logan-mcp-server/venv/bin/oci-logan-mcp"
    }
  }
}
```

**Claude Code** (`~/.claude.json` or project settings):

```json
{
  "mcpServers": {
    "oci-log-analytics": {
      "command": "/path/to/logan-mcp-server/venv/bin/oci-logan-mcp"
    }
  }
}
```

**Remote VM via SSH** (for instance principal auth on OCI VMs):

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

> **Note:** Always use the `oci-logan-mcp` entry point, not `python -m oci_logan_mcp`.

## What You Can Do

| Capability | Tools | Examples |
|---|---|---|
| **Query logs** | `run_query`, `run_batch_queries`, `run_saved_search` | Search logs, run multiple queries in parallel, execute saved searches |
| **Explore schema** | `list_log_sources`, `list_fields`, `list_entities`, `list_parsers`, `list_labels` | Discover what log data is available |
| **Visualize** | `visualize` | Generate pie, bar, line, area, table, tile, treemap, heatmap, histogram charts |
| **Export** | `export_results` | Export query results to CSV or JSON |
| **Manage scope** | `set_compartment`, `set_namespace`, `find_compartment`, `list_compartments` | Switch compartments, query across tenancy |
| **Validate** | `validate_query`, `get_query_examples` | Check syntax, get example queries by category |
| **Remember** | `save_learned_query`, `list_learned_queries`, `update_tenancy_context` | Save working queries for reuse across sessions |
| **Monitor** | `test_connection`, `get_current_context`, `get_log_summary` | Check connectivity, see current config, view log volume |

## Deploying on an OCI VM

### Fresh VM (Oracle Linux 9)

```bash
# Full bootstrap: installs Python, OCI CLI, Docker, Java, and the MCP server
curl -fsSL https://raw.githubusercontent.com/rishabh-ghosh24/logan-mcp-server/main/scripts/oci-initial-setup.sh | bash
```

### Existing VM (Python 3.10+ available)

```bash
git clone https://github.com/rishabh-ghosh24/logan-mcp-server.git
cd logan-mcp-server
./scripts/setup_oel9.sh
```

### Setting Up Instance Principal Auth

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

**Step 3:** Add IAM Policies at the tenancy level:

```
Allow dynamic-group logan-mcp-dg to use loganalytics-features-family in tenancy
Allow dynamic-group logan-mcp-dg to use loganalytics-resources-family in tenancy
Allow dynamic-group logan-mcp-dg to manage management-dashboard-family in tenancy
Allow dynamic-group logan-mcp-dg to read compartments in tenancy
Allow dynamic-group logan-mcp-dg to manage management-agents in tenancy
Allow dynamic-group logan-mcp-dg to manage management-agent-install-keys in tenancy
Allow dynamic-group logan-mcp-dg to read metrics in tenancy
Allow dynamic-group logan-mcp-dg to read users in tenancy
Allow dynamic-group logan-mcp-dg to {BUCKET_UPDATE, BUCKET_READ} in tenancy
Allow service loganalytics to read loganalytics-features-family in tenancy
```

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

### Authentication Methods

| Method | Use Case | Config |
|---|---|---|
| `config_file` | Local/laptop development | Reads `~/.oci/config` |
| `instance_principal` | OCI compute instances | IAM policies via Dynamic Group |
| `resource_principal` | OCI Functions, managed services | Automatic via service |

## License

MIT
