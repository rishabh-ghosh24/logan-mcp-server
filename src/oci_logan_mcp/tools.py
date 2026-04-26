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
        {
            "name": "create_log_source_from_sample",
            "destructive": True,
            "description": (
                "Create a Log Analytics parser and log source from JSON/NDJSON or CSV sample logs, "
                "upload the sample workload to OCI Log Analytics, and verify parse failures. "
                "Only provide logs you are allowed to upload; remove secrets, tokens, PII, "
                "and customer-sensitive values before continuing. "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token "
                "and summary. To execute, re-invoke with confirmation_token and your "
                "confirmation secret."
            ),
            "inputSchema": {
                "type": "object",
                "required": [
                    "source_name",
                    "sample_logs",
                    "log_group_id",
                    "acknowledge_data_review",
                ],
                "properties": {
                    "source_name": {
                        "type": "string",
                        "description": "Name of the Log Analytics source to create.",
                    },
                    "sample_logs": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Non-sensitive JSON/NDJSON or CSV sample logs to upload and verify.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["json_ndjson", "csv"],
                        "description": (
                            "Sample format. Use json_ndjson for one JSON object per line, "
                            "or csv for comma-delimited data with a header row. Default: json_ndjson."
                        ),
                    },
                    "log_group_id": {
                        "type": "string",
                        "description": "Log Analytics log group OCID used for sample upload.",
                    },
                    "acknowledge_data_review": {
                        "type": "boolean",
                        "description": (
                            "Must be true to confirm the sample logs were reviewed "
                            "and stripped of secrets, tokens, PII, and sensitive values."
                        ),
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": (
                            "Overwrite an existing parser/source with the same names. "
                            "Default: false; name collisions return CONFLICT."
                        ),
                    },
                    "parser_name": {
                        "type": "string",
                        "description": "Optional parser internal name. Defaults to a sanitized source-based name.",
                    },
                    "parser_display_name": {
                        "type": "string",
                        "description": "Optional parser display name.",
                    },
                    "field_mappings": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Optional mapping of inferred sample keys to Log Analytics field internal names.",
                    },
                    "entity_type": {
                        "type": "string",
                        "description": "Entity type for the source. Default: omc_host_linux.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Filename used for the sample upload. Default: sample.ndjson for JSON/NDJSON, sample.csv for CSV.",
                    },
                    "upload_name": {
                        "type": "string",
                        "description": "Optional OCI Log Analytics upload name.",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Optional entity OCID associated with the sample upload.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Optional timezone for log timestamps without explicit timezone.",
                    },
                    "log_set": {
                        "type": "string",
                        "description": "Optional Log Analytics log set for the sample upload.",
                    },
                    "char_encoding": {
                        "type": "string",
                        "description": "Character encoding for the sample upload. Default: UTF-8.",
                    },
                    "verification_time_range": {
                        "type": "string",
                        "enum": [
                            "last_15_min",
                            "last_1_hour",
                            "last_24_hours",
                            "last_7_days",
                            "last_30_days",
                        ],
                        "description": "Time range used for verification queries. Default: last_30_days.",
                    },
                    "field_check_limit": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 20,
                        "description": "Maximum number of mapped fields to verify for non-null values. Default: 20.",
                    },
                    "poll_attempts": {
                        "type": "integer",
                        "description": "Number of ingestion polling attempts. Default: 6.",
                    },
                    "poll_interval_seconds": {
                        "type": "number",
                        "description": "Seconds between ingestion polling attempts. Default: 10.",
                    },
                    "confirmation_token": {
                        "type": "string",
                        "description": "Server-generated token from the confirmation step. Omit on first call.",
                    },
                    "confirmation_secret": {
                        "type": "string",
                        "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret.",
                    },
                },
            },
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
                    "budget_override": {
                        "type": "boolean",
                        "description": "If true, bypass the per-session budget check for this query. Requires confirmation_token and confirmation_secret (two-factor). Use only when the user explicitly acknowledges the budget override.",
                    },
                    "confirmation_token": {
                        "type": "string",
                        "description": "Server-generated token from the confirmation step. Omit on first call.",
                    },
                    "confirmation_secret": {
                        "type": "string",
                        "description": "Your confirmation secret. Required with token to execute a budget_override. You MUST ask the user for this value each time — NEVER reuse a previously provided secret.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "run_saved_search",
            "description": (
                "Execute a saved search by name or ID. Provide at least one of "
                "`name` or `id` (if both are given, `id` wins). If you don't "
                "know what exists, call list_saved_searches first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the saved search (case-sensitive).",
                    },
                    "id": {
                        "type": "string",
                        "description": "OCID of the saved search.",
                    },
                },
                "anyOf": [
                    {"required": ["name"]},
                    {"required": ["id"]},
                ],
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
        {
            "name": "diff_time_windows",
            "description": (
                "Compare the same query across two time windows. Returns per-dimension "
                "deltas (spike/drop/new/disappeared) plus a one-line summary. Cheapest "
                "triage primitive: 'what's different about this hour vs. yesterday?'"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Base Log Analytics query run in both windows",
                    },
                    "current_window": {
                        "type": "object",
                        "description": "Current window: {time_range: '...'} OR {time_start, time_end} (ISO 8601)",
                    },
                    "comparison_window": {
                        "type": "object",
                        "description": "Comparison window, same shape as current_window",
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional. Fields to break out by. If omitted, extracted from the query's 'by' clause; else a scalar total delta.",
                    },
                },
                "required": ["query", "current_window", "comparison_window"],
            },
        },
        {
            "name": "pivot_on_entity",
            "description": (
                "Pull everything about an entity (host/user/IP/request-id) across all "
                "matching log sources in one call. Runs source discovery, then queries "
                "each source for the entity value. Returns per-source rows and a "
                "merged, time-ordered cross-source timeline."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": ["host", "user", "request_id", "ip", "custom"],
                        "description": "Type of entity to pivot on",
                    },
                    "entity_value": {
                        "type": "string",
                        "description": "Entity value to search for (e.g. 'web-01', 'alice')",
                    },
                    "time_range": {
                        "type": "object",
                        "description": "Time window: {time_range: '...'} OR {time_start, time_end} (ISO 8601)",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional. Limit to these log sources. If omitted, auto-discovers sources with matching data.",
                    },
                    "max_rows_per_source": {
                        "type": "integer",
                        "description": "Max rows to return per source. Default: 100",
                    },
                    "field_name": {
                        "type": "string",
                        "description": "Required when entity_type='custom'. The field name to filter on.",
                    },
                },
                "required": ["entity_type", "entity_value", "time_range"],
            },
        },
        {
            "name": "ingestion_health",
            "description": (
                "Check whether log ingestion is currently working. Runs one aggregate "
                "probe query and classifies each source as healthy (emitted recently), "
                "stopped (last record older than threshold), or unknown (no records in "
                "probe window). Answers 'is ingestion even working right now?' and is "
                "a foundational input to investigate_incident."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "compartment_id": {
                        "type": "string",
                        "description": "Optional compartment OCID. Uses default if not specified.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional. Limit the probe to these source names. If omitted, all discovered sources are probed.",
                    },
                    "severity_filter": {
                        "type": "string",
                        "enum": ["all", "warn", "critical"],
                        "description": "Drop findings below this severity tier. Default: 'warn' (shows warn + critical).",
                    },
                },
            },
        },
        {
            "name": "parser_failure_triage",
            "description": (
                "Surface the top log sources with parse failures, ranked by "
                "failure count. Returns up to top_n sources, each with "
                "failure_count, first/last seen timestamps, and up to 3 "
                "sample raw lines that failed to parse. Each source in OCI "
                "Log Analytics has one parser configured, so this tells you "
                "which parser needs fixing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "time_range": {
                        "type": "string",
                        "enum": [
                            "last_15_min", "last_30_min",
                            "last_1_hour", "last_3_hours", "last_6_hours",
                            "last_12_hours", "last_24_hours",
                            "last_2_days", "last_7_days", "last_14_days",
                            "last_30_days",
                        ],
                        "description": (
                            "Time window to scan for parser failures. "
                            "Default: 'last_24_hours'."
                        ),
                    },
                    "top_n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50000,
                        "description": (
                            "Maximum number of log sources to return, ranked "
                            "by failure count descending. Each source has one "
                            "parser, so this caps the number of broken "
                            "parsers surfaced. Default: 20. Must be in "
                            "[1, 50000] (Logan's LIMIT bound)."
                        ),
                    },
                },
            },
        },
        {
            "name": "investigate_incident",
            "description": (
                "Flagship triage tool. Given a seed Logan query and a time "
                "range, returns a structured first-cut investigation: which "
                "sources are stopped (J1), which parsers are failing (J2), "
                "which sources are anomalous vs. the prior equal-length "
                "window (A2), and for each of the top_k anomalous sources: "
                "top error clusters (Logan `cluster`), top entities "
                "(host/user/request_id by count), and a recent-events "
                "timeline. Merged cross-source timeline + next-step "
                "suggestions round out the report. Budget exhaustion and "
                "per-source errors yield a partial InvestigationReport with "
                "`partial: true` and specific `partial_reasons` — A1 never "
                "raises BudgetExceededError out of its boundary."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Seed Logan query. Only the pre-pipe search "
                            "clause is used for drill-down scoping (later "
                            "pipeline stages are dropped). A seed of '*' "
                            "degrades to unscoped investigation and is "
                            "reported via `seed.seed_filter_degraded`."
                        ),
                    },
                    "time_range": {
                        "type": "string",
                        "enum": [
                            "last_15_min", "last_30_min",
                            "last_1_hour", "last_3_hours", "last_6_hours",
                            "last_12_hours", "last_24_hours",
                            "last_2_days", "last_7_days", "last_14_days",
                            "last_30_days",
                        ],
                        "description": (
                            "Investigation window. Default: 'last_1_hour'. "
                            "A2 compares this window to the prior "
                            "equal-length window."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "description": (
                            "Number of anomalous sources to drill into. "
                            "Default: 3. P0 clamp is [1, 3] to match the "
                            "~20s p95 latency guarantee."
                        ),
                    },
                    "compartment_id": {
                        "type": "string",
                        "description": "Optional compartment OCID. Uses default if not specified.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "generate_incident_report",
            "description": (
                "Generate a deterministic Markdown incident report, and optional "
                "HTML rendering, from an A1 investigate_incident response. P0 is "
                "template-first and does not call an internal LLM."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "investigation": {
                        "type": "object",
                        "description": (
                            "A1 InvestigationReport object returned by "
                            "investigate_incident."
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "html"],
                        "description": (
                            "Output renderer. Markdown is always returned; html "
                            "adds an HTML rendering."
                        ),
                        "default": "markdown",
                    },
                    "include_sections": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "executive_summary",
                                "timeline",
                                "top_findings",
                                "evidence",
                                "recommended_next_steps",
                                "appendix",
                            ],
                        },
                        "description": (
                            "Optional ordered section allowlist. Defaults to all "
                            "sections."
                        ),
                    },
                    "summary_length": {
                        "type": "string",
                        "enum": ["short", "standard", "detailed"],
                        "description": "Executive summary sentence cap. Default: standard.",
                        "default": "standard",
                    },
                },
                "required": ["investigation"],
            },
        },
        {
            "name": "deliver_report",
            "description": (
                "Deliver a generated incident report via Telegram, Slack, or OCI "
                "Notifications email-topic delivery. P0 accepts inline markdown "
                "report content only; report_id lookup is deferred until report "
                "persistence exists."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["report"],
                "properties": {
                    "report": {
                        "type": "object",
                        "required": ["markdown"],
                        "properties": {
                            "markdown": {"type": "string"},
                            "title": {"type": "string"},
                        },
                    },
                    "channels": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["telegram", "email", "slack"],
                        },
                        "default": ["telegram"],
                    },
                    "recipients": {
                        "type": "object",
                        "properties": {
                            "telegram_chat_id": {"type": "string"},
                            "email_topic_ocid": {"type": "string"},
                        },
                    },
                    "format": {
                        "type": "string",
                        "enum": ["pdf", "markdown", "both"],
                        "default": "pdf",
                    },
                    "title": {"type": "string"},
                },
            },
        },
        {
            "name": "why_did_this_fire",
            "description": (
                "For Logan-managed monitoring alarms, explain why the alarm "
                "fired by returning the stored Logan query, the historical "
                "fire window, the trigger query result over that window, and "
                "up to 50 scoped top contributing rows when the seed query is "
                "safely scoped."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "alarm_ocid": {
                        "type": "string",
                        "description": "OCID of the Logan-managed monitoring alarm.",
                    },
                    "fire_time": {
                        "type": "string",
                        "description": "ISO-8601 timestamp when the alarm fired.",
                    },
                    "window_before_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Optional lookback window. Defaults to the alarm's "
                            "pending_duration when present, else 300 seconds."
                        ),
                    },
                    "window_after_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Optional post-fire padding window in seconds. "
                            "Default: 60."
                        ),
                    },
                },
                "required": ["alarm_ocid", "fire_time"],
            },
        },
        {
            "name": "find_rare_events",
            "description": (
                "Find low-frequency values for a field within a source using "
                "Logan's native `rare` command, then annotate them with "
                "history counts and first/last seen timestamps."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Log source name to scope the rare-value search to.",
                    },
                    "field": {
                        "type": "string",
                        "description": "Field to score by low frequency (for example Severity or Entity).",
                    },
                    "time_range": {
                        "type": "object",
                        "description": "Time window: {time_range: '...'} OR {time_start, time_end} (ISO 8601)",
                    },
                    "rarity_threshold_percentile": {
                        "type": "number",
                        "description": "Only include values whose rare percent is <= this threshold. Default: 5.0.",
                    },
                    "history_days": {
                        "type": "integer",
                        "description": "How many days of history to annotate with. Default: 30.",
                    }
                },
                "required": ["source", "field", "time_range"],
            },
        },
        {
            "name": "trace_request_id",
            "description": (
                "Search common request-id and trace-id fields across all "
                "matching log sources and return a single ordered event stream."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "Request or trace id value to search for.",
                    },
                    "time_range": {
                        "type": "object",
                        "description": "Time window: {time_range: '...'} OR {time_start, time_end} (ISO 8601)",
                    },
                    "id_fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional. Override the default field probe order.",
                    },
                },
                "required": ["request_id", "time_range"],
            },
        },
        {
            "name": "related_dashboards_and_searches",
            "description": (
                "Suggest existing dashboards, saved searches, and learned "
                "queries related to a source, entity, or field."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Optional log source name to search for.",
                    },
                    "entity": {
                        "type": "object",
                        "description": "Optional entity to search for.",
                        "properties": {
                            "type": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["type", "value"],
                    },
                    "field": {
                        "type": "string",
                        "description": "Optional field name to search for.",
                    },
                },
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
                "Save a query so future LLM suggestions will prefer this working pattern. "
                "Trigger when the user explicitly says things like 'save this query', "
                "'remember these from this session', 'keep the good ones', or similar. "
                "The save is infrastructure — the user won't see a list of saved queries; "
                "they'll just notice better query suggestions over time. "
                "If the name collides with a built-in or community query, a collision_warning "
                "is returned — retry with force: true to override or rename_to: '<new_name>' "
                "to choose a different name."
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
                    "force": {
                        "type": "boolean",
                        "description": "If true, save even if the name collides with a built-in or community (shared) query. Default false.",
                        "default": False,
                    },
                    "rename_to": {
                        "type": "string",
                        "description": "If provided, save under this name instead (useful to avoid a collision).",
                    },
                },
                "required": ["name", "query", "description"],
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
            "destructive": True,
            "description": (
                "Create an OCI-native autonomous alert from a Log Analytics query. "
                "The alert fires 24/7 via OCI Monitoring, independent of the MCP server. "
                "Requires a numeric aggregation query (e.g. '| stats count'). "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token and summary. "
                "To execute, re-invoke with confirmation_token and your confirmation secret."
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
                    "confirmation_token": {"type": "string", "description": "Server-generated token from the confirmation step. Omit on first call."},
                    "confirmation_secret": {"type": "string", "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret."},
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
            "destructive": True,
            "description": (
                "Create a new Log Analytics saved search. "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token and summary. "
                "To execute, re-invoke with confirmation_token and your confirmation secret."
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
                    "confirmation_token": {"type": "string", "description": "Server-generated token from the confirmation step. Omit on first call."},
                    "confirmation_secret": {"type": "string", "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret."},
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
            "destructive": True,
            "description": (
                "Create an OCI Management Dashboard with visualization tiles. "
                "TWO-FACTOR CONFIRMATION REQUIRED: First call returns a confirmation token and summary. "
                "To execute, re-invoke with confirmation_token and your confirmation secret."
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
                    "confirmation_token": {"type": "string", "description": "Server-generated token from the confirmation step. Omit on first call."},
                    "confirmation_secret": {"type": "string", "description": "Your confirmation secret. Required with token to execute. You MUST ask the user for this value each time — NEVER reuse a previously provided secret."},
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
        # ── Estimation + Budget tools ──────────────────────────────────────
        {
            "name": "explain_query",
            "description": "Estimate cost, bytes scanned, and runtime for a query before running it. Returns estimated_bytes, estimated_cost_usd, estimated_eta_seconds, confidence, and a human-readable rationale.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The Log Analytics query."},
                    "time_range": {
                        "type": "string",
                        "enum": ["last_15_min", "last_1_hour", "last_24_hours", "last_7_days", "last_30_days"],
                    },
                    "time_start": {"type": "string"},
                    "time_end": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_session_budget",
            "description": "Return the current session's query budget usage and remaining allowance.",
            "inputSchema": {"type": "object", "properties": {}},
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
        {
            "name": "export_transcript",
            "description": "Export the current (or specified) session's tool-call transcript as JSONL. Returns the file path and event count. Pass session_id='current' for the running process's session.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session id to export, or 'current' for the running process.",
                        "default": "current",
                    },
                    "include_results": {
                        "type": "boolean",
                        "description": "Include result_summary fields. Default true.",
                        "default": True,
                    },
                    "redact": {
                        "type": "boolean",
                        "description": "Run PII/secret redaction patterns over the output. Default false.",
                        "default": False,
                    },
                },
            },
        },
        {
            "name": "record_investigation",
            "description": (
                "Record the current process's audit events in a time window as "
                "a named investigation playbook. P0 records only; it does not replay."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable playbook name.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description.",
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Inclusive ISO-8601 start time. Defaults to server "
                            "process start."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": "Inclusive ISO-8601 end time. Defaults to now.",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "list_playbooks",
            "description": "List recorded investigation playbooks for the current user.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_playbook",
            "description": "Return one recorded investigation playbook with its full step list.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "playbook_id": {
                        "type": "string",
                        "description": "Playbook id returned by list_playbooks.",
                    }
                },
                "required": ["playbook_id"],
            },
        },
        {
            "name": "delete_playbook",
            "description": "Delete one recorded investigation playbook for the current user.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "playbook_id": {
                        "type": "string",
                        "description": "Playbook id to delete.",
                    }
                },
                "required": ["playbook_id"],
            },
        },
    ]
