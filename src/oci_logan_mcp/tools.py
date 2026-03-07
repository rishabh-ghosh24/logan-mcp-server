"""MCP tool definitions for Log Analytics operations."""

from typing import List, Dict, Any


def get_tools() -> List[Dict[str, Any]]:
    """Get all MCP tool definitions.

    Returns:
        List of tool definition dictionaries.
    """
    return [
        # Schema Exploration Tools
        {
            "name": "list_log_sources",
            "description": (
                "List all available log sources in OCI Log Analytics. "
                "Returns source names, descriptions, and associated entity types."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "compartment_id": {
                        "type": "string",
                        "description": "Optional compartment OCID. Uses default if not specified.",
                    }
                },
            },
        },
        {
            "name": "list_fields",
            "description": (
                "List fields available for querying. Includes field types, possible values, "
                "and semantic hints to help construct accurate queries."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": "Optional log source name to filter fields.",
                    }
                },
            },
        },
        {
            "name": "list_entities",
            "description": "List monitored entities (hosts, applications, databases, etc.)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "description": "Optional entity type filter (e.g., 'Host', 'Database')",
                    }
                },
            },
        },
        {
            "name": "list_parsers",
            "description": "List available log parsers.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_labels",
            "description": "List label definitions used for log classification.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_saved_searches",
            "description": "List all saved searches available in Log Analytics.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_log_groups",
            "description": "List log groups in the current compartment.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        # Query Execution Tools
        {
            "name": "validate_query",
            "description": (
                "Validate a Log Analytics query before execution. "
                "Returns errors, warnings, suggestions, and estimated cost."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The Log Analytics query to validate",
                    },
                    "time_start": {
                        "type": "string",
                        "description": "Start time (ISO 8601 format)",
                    },
                    "time_end": {
                        "type": "string",
                        "description": "End time (ISO 8601 format)",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "run_query",
            "description": "Execute a Log Analytics query and return results.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The Log Analytics query to execute",
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Relative time range: last_15_min, last_1_hour, last_24_hours, last_7_days, last_30_days",
                        "enum": ["last_15_min", "last_1_hour", "last_24_hours", "last_7_days", "last_30_days"],
                    },
                    "time_start": {
                        "type": "string",
                        "description": "Absolute start time (ISO 8601). Overrides time_range.",
                    },
                    "time_end": {
                        "type": "string",
                        "description": "Absolute end time (ISO 8601). Overrides time_range.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return",
                    },
                    "include_subcompartments": {
                        "type": "boolean",
                        "description": "If true, include logs from all sub-compartments of the current compartment. Default: false",
                    },
                    "compartment_id": {
                        "type": "string",
                        "description": "Optional compartment OCID to query. If not specified, uses default compartment from config.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["default", "tenancy"],
                        "description": "Query scope: 'default' uses your configured compartment, 'tenancy' queries ALL compartments across the entire tenancy. Use 'tenancy' when user asks for logs 'across all compartments', 'entire tenancy', 'organization-wide', etc. When scope='tenancy', include_subcompartments is automatically set to true.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "run_saved_search",
            "description": "Execute a saved search by name or ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the saved search",
                    },
                    "id": {
                        "type": "string",
                        "description": "OCID of the saved search",
                    },
                },
            },
        },
        {
            "name": "run_batch_queries",
            "description": "Execute multiple queries concurrently.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "time_range": {"type": "string"},
                                "include_subcompartments": {"type": "boolean"},
                                "compartment_id": {"type": "string"},
                            },
                            "required": ["query"],
                        },
                        "description": "Array of query objects",
                    },
                    "include_subcompartments": {
                        "type": "boolean",
                        "description": "Default for all queries: if true, include logs from sub-compartments. Can be overridden per-query.",
                    },
                    "compartment_id": {
                        "type": "string",
                        "description": "Default compartment OCID for all queries. Can be overridden per-query.",
                    },
                },
                "required": ["queries"],
            },
        },
        # Visualization Tools
        {
            "name": "visualize",
            "description": (
                "Generate a visualization (chart) from a query. "
                "Returns a PNG image and raw data."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The Log Analytics query",
                    },
                    "chart_type": {
                        "type": "string",
                        "description": "Type of chart to generate",
                        "enum": ["pie", "bar", "line", "area", "table", "tile", "treemap"],
                    },
                    "title": {
                        "type": "string",
                        "description": "Chart title",
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Relative time range",
                        "enum": ["last_15_min", "last_1_hour", "last_24_hours", "last_7_days", "last_30_days"],
                    },
                    "time_start": {
                        "type": "string",
                        "description": "Absolute start time (ISO 8601). Overrides time_range.",
                    },
                    "time_end": {
                        "type": "string",
                        "description": "Absolute end time (ISO 8601). Overrides time_range.",
                    },
                    "include_subcompartments": {
                        "type": "boolean",
                        "description": "If true, include logs from all sub-compartments. Default: false",
                    },
                    "compartment_id": {
                        "type": "string",
                        "description": "Optional compartment OCID to query. If not specified, uses default compartment from config.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["default", "tenancy"],
                        "description": "Query scope: 'default' uses your configured compartment, 'tenancy' queries ALL compartments across the entire tenancy.",
                    },
                },
                "required": ["query", "chart_type"],
            },
        },
        # Export Tools
        {
            "name": "export_results",
            "description": "Export query results to CSV or JSON format.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The Log Analytics query",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["csv", "json"],
                        "description": "Export format",
                    },
                    "time_range": {
                        "type": "string",
                        "description": "Relative time range",
                        "enum": ["last_15_min", "last_1_hour", "last_24_hours", "last_7_days", "last_30_days"],
                    },
                    "time_start": {
                        "type": "string",
                        "description": "Absolute start time (ISO 8601). Overrides time_range.",
                    },
                    "time_end": {
                        "type": "string",
                        "description": "Absolute end time (ISO 8601). Overrides time_range.",
                    },
                    "include_subcompartments": {
                        "type": "boolean",
                        "description": "If true, include logs from all sub-compartments. Default: false",
                    },
                    "compartment_id": {
                        "type": "string",
                        "description": "Optional compartment OCID to query. If not specified, uses default compartment from config.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["default", "tenancy"],
                        "description": "Query scope: 'default' uses your configured compartment, 'tenancy' queries ALL compartments across the entire tenancy.",
                    },
                },
                "required": ["query", "format"],
            },
        },
        # Configuration Tools
        {
            "name": "set_compartment",
            "description": "Change the current compartment context.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "compartment_id": {
                        "type": "string",
                        "description": "Compartment OCID",
                    }
                },
                "required": ["compartment_id"],
            },
        },
        {
            "name": "set_namespace",
            "description": "Change the current Log Analytics namespace.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Log Analytics namespace",
                    }
                },
                "required": ["namespace"],
            },
        },
        {
            "name": "get_current_context",
            "description": "Get the current namespace, compartment, and configuration context.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_compartments",
            "description": "List available compartments.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        # Helper Tools
        {
            "name": "test_connection",
            "description": (
                "Test the connection to OCI Log Analytics. Use this FIRST to verify "
                "the server is properly configured and can connect to OCI. Returns "
                "connection status, namespace, compartment, and a sample query result."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "find_compartment",
            "description": (
                "Find a compartment by name (fuzzy match). Use this when user mentions "
                "a compartment by name like 'Production', 'Development', 'shared-infra', etc."
                "Returns matching compartments with their OCIDs that can be used in queries."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Compartment name or partial name to search for",
                    }
                },
                "required": ["name"],
            },
        },
        {
            "name": "get_query_examples",
            "description": (
                "Get example Log Analytics queries for common use cases. Use this to help "
                "construct queries when unsure of the syntax. Categories include: basic, "
                "security, performance, errors, and statistics."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["basic", "security", "performance", "errors", "statistics", "all"],
                        "description": "Category of examples to return. Use 'all' for complete reference.",
                    }
                },
            },
        },
        {
            "name": "get_log_summary",
            "description": (
                "Get a summary of available log data - which log sources have data and "
                "approximate counts. Use this to understand what data is available before "
                "constructing queries. Helps avoid querying empty log sources."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "time_range": {
                        "type": "string",
                        "enum": ["last_1_hour", "last_24_hours", "last_7_days"],
                        "description": "Time range to check for data. Default: last_24_hours",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["default", "tenancy"],
                        "description": "Scope: 'default' for current compartment, 'tenancy' for all compartments.",
                    },
                },
            },
        },
    ]
