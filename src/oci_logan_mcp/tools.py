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
                        "description": "If true, include logs from all sub-compartments of the current compartment. Default: true",
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
                        "enum": ["pie", "bar", "vertical_bar", "line", "area", "table", "tile", "treemap", "heatmap", "histogram"],
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
                        "description": "If true, include logs from all sub-compartments of the current compartment. Default: true",
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
                        "description": "If true, include logs from all sub-compartments of the current compartment. Default: true",
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
        {
            "name": "setup_confirmation_secret",
            "description": (
                "Create your confirmation secret for destructive operations like update or "
                "delete. Use this the first time a guarded tool asks for a confirmation "
                "secret. This is for initial setup only; use --reset-secret from the CLI "
                "if you need to replace an existing secret."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["confirmation_secret", "confirmation_secret_confirm"],
                "properties": {
                    "confirmation_secret": {
                        "type": "string",
                        "description": (
                            "Your new confirmation secret. Minimum 8 characters."
                        ),
                    },
                    "confirmation_secret_confirm": {
                        "type": "string",
                        "description": "Re-enter the same secret to confirm it.",
                    },
                },
            },
        },
        # Memory & Context Tools
        {
            "name": "save_learned_query",
            "description": (
                "Save a working query for future reference. Call this after a query "
                "succeeds and proves useful. The query will be persisted across sessions "
                "and available as a template in future conversations."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short descriptive name for the query",
                    },
                    "query": {
                        "type": "string",
                        "description": "The exact query text that worked",
                    },
                    "description": {
                        "type": "string",
                        "description": "What this query does and when to use it",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category for the query",
                        "enum": ["security", "errors", "performance", "network", "audit", "general"],
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for searchability",
                    },
                },
                "required": ["name", "query", "description"],
            },
        },
        {
            "name": "list_learned_queries",
            "description": (
                "List all previously saved learned queries. These are queries that "
                "were saved from successful executions in past sessions. Filter by "
                "category or tag."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category",
                        "enum": ["security", "errors", "performance", "network", "audit", "general", "all"],
                    },
                    "tag": {
                        "type": "string",
                        "description": "Filter by tag",
                    },
                },
            },
        },
        {
            "name": "update_tenancy_context",
            "description": (
                "Update the persistent tenancy context with discovered information. "
                "Use this to save environment-specific notes, confirmed field names, "
                "or quirks that should be remembered across sessions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Environment-specific notes or quirks to remember",
                    },
                    "confirmed_fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "data_type": {"type": "string"},
                                "known_values": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["name"],
                        },
                        "description": "Fields confirmed to work in this environment",
                    },
                },
            },
        },
        {
            "name": "delete_learned_query",
            "description": "Delete a previously saved learned query by name.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the learned query to delete",
                    },
                },
                "required": ["name"],
            },
        },
        # Preference Tools
        {
            "name": "get_preferences",
            "description": (
                "Get learned user preferences including common fields per log source "
                "and suggested time ranges."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "log_source": {
                        "type": "string",
                        "description": "Log source name to get preferences for",
                    },
                },
            },
        },
        {
            "name": "remember_preference",
            "description": (
                "Save a disambiguation preference (e.g., 'when I say PostgreSQL errors, "
                "I mean Log Source = OCI PostgreSQL Service Logs')."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "intent_key": {
                        "type": "string",
                        "description": "The intent phrase (e.g., 'postgresql_errors')",
                    },
                    "resolved_value": {
                        "type": "string",
                        "description": "The resolved filter or value",
                    },
                },
                "required": ["intent_key", "resolved_value"],
            },
        },
        # ── Alert tools ────────────────────────────────────────────────
        {
            "name": "create_alert",
            "description": (
                "Create an OCI-native autonomous alert from a Log Analytics query. "
                "The alert fires 24/7 via OCI Monitoring, independent of the MCP server. "
                "Requires a numeric aggregation query (e.g. '| stats count'). "
                "APPROVAL REQUIRED: This tool creates OCI resources. Confirm with the user before invoking."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["display_name", "query", "destination_topic_id"],
                "properties": {
                    "display_name": {"type": "string", "description": "Alert name."},
                    "query": {"type": "string", "description": "Log Analytics query (must include | stats aggregation)."},
                    "destination_topic_id": {"type": "string", "description": "ONS topic OCID for notifications."},
                    "schedule": {"type": "string", "description": "Cron schedule (5-field). Default: '0 */15 * * *'"},
                    "threshold_value": {"type": "integer", "description": "Numeric threshold. Default: 0"},
                    "threshold_operator": {"type": "string", "enum": ["gt", "gte", "eq", "lt", "lte"], "description": "Comparison operator. Default: 'gt'"},
                    "severity": {"type": "string", "enum": ["CRITICAL", "ERROR", "WARNING", "INFO"], "description": "Alert severity. Default: 'CRITICAL'"},
                    "compartment_id": {"type": "string", "description": "Compartment OCID override."},
                },
            },
        },
        {
            "name": "list_alerts",
            "description": "List all Logan-managed OCI autonomous alerts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "compartment_id": {"type": "string", "description": "Compartment OCID override."},
                },
            },
        },
        {
            "name": "update_alert",
            "destructive": True,
            "description": (
                "Update an existing autonomous alert. Each parameter targets only the affected OCI resource. "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token and summary. "
                "To execute, re-invoke with confirmation_token and your confirmation secret."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["alert_id"],
                "properties": {
                    "alert_id": {"type": "string", "description": "Monitoring alarm OCID (returned by create_alert or list_alerts)."},
                    "display_name": {"type": "string"},
                    "query": {"type": "string"},
                    "schedule": {"type": "string"},
                    "threshold_value": {"type": "integer"},
                    "threshold_operator": {"type": "string", "enum": ["gt", "gte", "eq", "lt", "lte"]},
                    "severity": {"type": "string", "enum": ["CRITICAL", "ERROR", "WARNING", "INFO"]},
                    "destination_topic_id": {"type": "string"},
                    "confirmation_token": {"type": "string", "description": "Server-generated token from the confirmation step. Omit on first call."},
                    "confirmation_secret": {"type": "string", "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret."},
                },
            },
        },
        {
            "name": "delete_alert",
            "destructive": True,
            "description": (
                "Delete an autonomous alert and all its backing OCI resources. "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token and summary. "
                "To execute, re-invoke with confirmation_token and your confirmation secret."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["alert_id"],
                "properties": {
                    "alert_id": {"type": "string", "description": "Monitoring alarm OCID."},
                    "confirmation_token": {"type": "string", "description": "Server-generated token from the confirmation step. Omit on first call."},
                    "confirmation_secret": {"type": "string", "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret."},
                },
            },
        },
        # ── Saved search CRUD ──────────────────────────────────────────
        {
            "name": "create_saved_search",
            "description": (
                "Create a new Log Analytics saved search. "
                "APPROVAL REQUIRED: This tool creates an OCI resource. Confirm with the user before invoking."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["display_name", "query"],
                "properties": {
                    "display_name": {"type": "string"},
                    "query": {"type": "string"},
                    "description": {"type": "string"},
                    "compartment_id": {"type": "string"},
                    "category": {"type": "string"},
                },
            },
        },
        {
            "name": "update_saved_search",
            "destructive": True,
            "description": (
                "Update an existing saved search. "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token and summary. "
                "To execute, re-invoke with confirmation_token and your confirmation secret."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["saved_search_id"],
                "properties": {
                    "saved_search_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "query": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                    "confirmation_token": {"type": "string", "description": "Server-generated token from the confirmation step. Omit on first call."},
                    "confirmation_secret": {"type": "string", "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret."},
                },
            },
        },
        {
            "name": "delete_saved_search",
            "destructive": True,
            "description": (
                "Delete a saved search. "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token and summary. "
                "To execute, re-invoke with confirmation_token and your confirmation secret."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["saved_search_id"],
                "properties": {
                    "saved_search_id": {"type": "string"},
                    "confirmation_token": {"type": "string", "description": "Server-generated token from the confirmation step. Omit on first call."},
                    "confirmation_secret": {"type": "string", "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret."},
                },
            },
        },
        # ── Dashboard tools ────────────────────────────────────────────
        {
            "name": "create_dashboard",
            "description": (
                "Create an OCI Management Dashboard with visualization tiles. "
                "APPROVAL REQUIRED: This tool creates OCI resources. Confirm with the user before invoking."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["display_name", "tiles"],
                "properties": {
                    "display_name": {"type": "string"},
                    "tiles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["title", "query", "visualization_type"],
                            "properties": {
                                "title": {"type": "string"},
                                "query": {"type": "string"},
                                "visualization_type": {
                                    "type": "string",
                                    "enum": ["bar", "vertical_bar", "line", "pie", "table",
                                             "tile", "area", "treemap", "heatmap", "histogram"],
                                },
                                "width": {"type": "integer"},
                                "height": {"type": "integer"},
                            },
                        },
                    },
                    "description": {"type": "string"},
                    "compartment_id": {"type": "string"},
                },
            },
        },
        {
            "name": "list_dashboards",
            "description": "List OCI Management Dashboards.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "compartment_id": {"type": "string"},
                },
            },
        },
        {
            "name": "add_dashboard_tile",
            "destructive": True,
            "description": (
                "Add a new tile to an existing dashboard. "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token and summary. "
                "To execute, re-invoke with confirmation_token and your confirmation secret."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["dashboard_id", "title", "query", "visualization_type"],
                "properties": {
                    "dashboard_id": {"type": "string"},
                    "title": {"type": "string"},
                    "query": {"type": "string"},
                    "visualization_type": {
                        "type": "string",
                        "enum": ["bar", "vertical_bar", "line", "pie", "table",
                                 "tile", "area", "treemap", "heatmap", "histogram"],
                    },
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "confirmation_token": {"type": "string", "description": "Server-generated token from the confirmation step. Omit on first call."},
                    "confirmation_secret": {"type": "string", "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret."},
                },
            },
        },
        {
            "name": "delete_dashboard",
            "destructive": True,
            "description": (
                "Delete a dashboard and all its tile data sources. "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token and summary. "
                "To execute, re-invoke with confirmation_token and your confirmation secret."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["dashboard_id"],
                "properties": {
                    "dashboard_id": {"type": "string"},
                    "confirmation_token": {"type": "string", "description": "Server-generated token from the confirmation step. Omit on first call."},
                    "confirmation_secret": {"type": "string", "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret."},
                },
            },
        },
        # ── Notification tools ─────────────────────────────────────────
        {
            "name": "send_to_slack",
            "description": (
                "Send a message or query results to Slack via configured webhook. "
                "Provide at least one of 'message' or 'query'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "query": {"type": "string", "description": "Log Analytics query to execute and include in message."},
                    "time_range": {"type": "string", "description": "Time range for query. Default: 'last_1_hour'"},
                    "format": {"type": "string", "enum": ["summary", "full"], "description": "Result format. Default: 'summary'"},
                },
            },
        },
        {
            "name": "send_to_telegram",
            "description": (
                "Send a message or query results to Telegram via configured bot. "
                "Provide at least one of 'message' or 'query'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "query": {"type": "string", "description": "Log Analytics query to execute and include in message."},
                    "time_range": {"type": "string", "description": "Time range for query. Default: 'last_1_hour'"},
                    "format": {"type": "string", "enum": ["summary", "full"], "description": "Result format. Default: 'summary'"},
                    "chat_id": {"type": "string", "description": "Override default chat ID."},
                },
            },
        },
    ]
