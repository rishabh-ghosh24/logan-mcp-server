# OCI Log Analytics MCP Server

An MCP (Model Context Protocol) server that connects AI assistants to Oracle Cloud Infrastructure (OCI) Log Analytics. Query, visualize, and export log data through natural language conversations.

## Features

- **20 MCP Tools**: Query execution, schema exploration, visualization, export, configuration management
- **Cross-Compartment Queries**: Query across your entire OCI tenancy with `scope=tenancy`
- **Intelligent Validation**: Query syntax checking with fuzzy field name suggestions
- **Visualization**: Generate pie, bar, line, area, table, and tile charts from query results
- **Export**: Export results to CSV or JSON format
- **Caching**: In-memory caching with TTL for improved performance
- **Rate Limiting**: Automatic rate limiting with exponential backoff for OCI API calls
- **Query Audit Logging**: All queries logged with rotating file handler

## Prerequisites

- Python 3.10+
- OCI account with Log Analytics enabled
- OCI CLI configured (`~/.oci/config`) or instance principal authentication

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/rishabh-ghosh24/logan-mcp-server.git
cd logan-mcp-server

python3 -m venv venv
source venv/bin/activate
pip install -e .
```

### 2. Configure

Run the interactive setup wizard:

```bash
oci-logan-mcp
```

Or set environment variables:

```bash
export OCI_LA_NAMESPACE=your-namespace
export OCI_LA_COMPARTMENT=ocid1.compartment.oc1..xxxxx
export OCI_LA_AUTH_TYPE=config_file  # or instance_principal
```

### 3. Connect to Claude Desktop

Add to your Claude Desktop MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "oci-log-analytics": {
      "command": "/path/to/logan-mcp-server/venv/bin/python",
      "args": ["-m", "oci_logan_mcp"],
      "env": {
        "OCI_LA_NAMESPACE": "your-namespace",
        "OCI_LA_COMPARTMENT": "ocid1.compartment.oc1..xxxxx"
      }
    }
  }
}
```

### Remote VM Deployment (SSH Tunnel)

For running on an OCI VM with instance principal auth:

```json
{
  "mcpServers": {
    "oci-log-analytics": {
      "command": "ssh",
      "args": [
        "-i", "~/.ssh/your-key",
        "opc@your-vm-ip",
        "cd /path/to/logan-mcp-server && source venv/bin/activate && python -m oci_logan_mcp"
      ]
    }
  }
}
```

## OEL 9 VM Setup

```bash
# On the OCI VM
git clone https://github.com/rishabh-ghosh24/logan-mcp-server.git
cd logan-mcp-server
chmod +x scripts/setup_oel9.sh
./scripts/setup_oel9.sh
```

## Available Tools

| Tool | Description |
|------|-------------|
| `run_query` | Execute Log Analytics queries |
| `run_batch_queries` | Execute multiple queries concurrently |
| `run_saved_search` | Execute a saved search by name or ID |
| `validate_query` | Validate query syntax before execution |
| `visualize` | Generate charts from query results |
| `export_results` | Export results to CSV or JSON |
| `list_log_sources` | List available log sources |
| `list_fields` | List queryable fields |
| `list_entities` | List monitored entities |
| `list_parsers` | List available log parsers |
| `list_labels` | List log classification labels |
| `list_saved_searches` | List saved searches |
| `list_log_groups` | List log groups |
| `list_compartments` | List OCI compartments |
| `get_current_context` | Show current configuration |
| `set_compartment` | Change target compartment |
| `set_namespace` | Change Log Analytics namespace |
| `test_connection` | Test OCI connectivity |
| `find_compartment` | Search compartments by name |
| `get_query_examples` | Get example queries by category |
| `get_log_summary` | Get log volume summary |

## Running Tests

```bash
# Unit tests
pip install -e ".[dev]"
pytest tests/ -v

# Integration tests (requires OCI access)
python run_tests.py
```

## Project Structure

```
logan-mcp-server/
├── src/oci_logan_mcp/
│   ├── __init__.py          # Package init
│   ├── __main__.py          # python -m entry point
│   ├── server.py            # MCP server setup and lifecycle
│   ├── config.py            # Configuration dataclasses and loading
│   ├── wizard.py            # Interactive setup wizard
│   ├── auth.py              # OCI authentication handlers
│   ├── client.py            # OCI Log Analytics API client
│   ├── cache.py             # In-memory caching
│   ├── rate_limiter.py      # API rate limiting
│   ├── query_logger.py      # Query audit logging
│   ├── tools.py             # MCP tool definitions
│   ├── handlers.py          # MCP request handlers
│   ├── resources.py         # MCP resource providers
│   ├── query_engine.py      # Query execution service
│   ├── schema_manager.py    # Schema exploration service
│   ├── validator.py         # Query validation
│   ├── saved_search.py      # Saved search management
│   ├── export.py            # CSV/JSON export
│   ├── visualization.py     # Chart generation
│   ├── time_parser.py       # Time range parsing
│   ├── fuzzy_match.py       # Fuzzy string matching
│   └── templates/
│       └── query_templates.yaml
├── tests/
├── scripts/
│   ├── setup_oel9.sh
│   ├── run.sh
│   └── update.sh
├── run_tests.py             # Integration test suite
├── pyproject.toml
└── .env.example
```

## Authentication

Supported authentication methods:

- **config_file** (default): Uses `~/.oci/config`
- **instance_principal**: For OCI compute instances with dynamic group policies
- **resource_principal**: For OCI Functions and other managed services

## License

MIT
