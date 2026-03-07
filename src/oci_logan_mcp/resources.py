"""MCP resource definitions for Log Analytics."""

from typing import List, Dict, Any


def get_resources() -> List[Dict[str, Any]]:
    """Get all MCP resource definitions."""
    return [
        {
            "uri": "loganalytics://schema",
            "name": "Log Analytics Schema",
            "description": (
                "Complete schema information including log sources, fields, "
                "entities, parsers, and labels."
            ),
            "mimeType": "application/json",
        },
        {
            "uri": "loganalytics://query-templates",
            "name": "Query Templates",
            "description": "Common query patterns and templates for Log Analytics.",
            "mimeType": "application/json",
        },
        {
            "uri": "loganalytics://syntax-guide",
            "name": "Query Syntax Guide",
            "description": (
                "Log Analytics query language reference with operators and functions."
            ),
            "mimeType": "text/markdown",
        },
        {
            "uri": "loganalytics://recent-queries",
            "name": "Recent Queries",
            "description": "Last 10 successful queries for reference.",
            "mimeType": "application/json",
        },
    ]


def get_query_templates() -> Dict[str, Any]:
    """Get query templates."""
    return {
        "templates": [
            {
                "name": "errors_last_hour",
                "description": "Find all errors in the last hour",
                "query": "'Error' or 'Critical' | timestats count by 'Log Source'",
            },
            {
                "name": "top_n_by_field",
                "description": "Get top N records by a field",
                "query": "* | stats count by '{field}' | sort -count | head {n}",
            },
            {
                "name": "trend_over_time",
                "description": "Show trend over time",
                "query": "* | timestats count span=1hour",
            },
            {
                "name": "search_keyword",
                "description": "Search for a keyword in logs",
                "query": "'{keyword}' | fields 'Log Source', 'Entity', 'Message', 'Time'",
            },
            {
                "name": "filter_by_severity",
                "description": "Filter logs by severity level",
                "query": "'Severity' = '{level}' | stats count by 'Host Name'",
            },
            {
                "name": "error_by_host",
                "description": "Count errors by host",
                "query": "'Severity' = 'Error' | stats count by 'Host Name' | sort -count",
            },
            {
                "name": "recent_logs",
                "description": "Get most recent logs",
                "query": "* | fields 'Time', 'Log Source', 'Message' | sort -'Time' | head 100",
            },
            {
                "name": "log_volume_by_source",
                "description": "Log volume by source",
                "query": "* | stats count by 'Log Source' | sort -count",
            },
        ]
    }


def get_syntax_guide() -> str:
    """Get query syntax guide."""
    return """# Log Analytics Query Syntax Guide

## Basic Search
- `*` - Match all records
- `'keyword'` - Search for keyword
- `'Error' or 'Warning'` - Boolean operators (or, and, not)
- `'field' = 'value'` - Exact field match

## Pipe Commands

### Filtering
- `| where 'Field' = 'value'` - Filter by field value
- `| where 'Field' like '%pattern%'` - Pattern matching
- `| where 'Field' in ('a', 'b')` - Match any of values

### Aggregation
- `| stats count` - Count all records
- `| stats count by 'Field'` - Count grouped by field
- `| stats sum('Field')` - Sum numeric field
- `| stats avg('Field')` - Average of numeric field
- `| stats min('Field'), max('Field')` - Min/max values

### Time-based Aggregation
- `| timestats count span=1hour` - Count per hour
- `| timestats count span=1day` - Count per day
- `| timestats avg('Field') span=15min` - Average per 15 minutes

### Sorting and Limiting
- `| sort -count` - Sort descending
- `| sort 'Field'` - Sort ascending
- `| head 10` - First N records
- `| tail 10` - Last N records

### Field Selection
- `| fields 'Field1', 'Field2'` - Select specific fields
- `| fields -'Field'` - Exclude field

### Field Manipulation
- `| rename 'Old' as 'New'` - Rename field
- `| eval NewField = expression` - Create calculated field

## Common Patterns

### Find Errors
```
'Error' or 'Critical'
```

### Count by Field
```
* | stats count by 'Log Source'
```

### Time Trend
```
* | timestats count span=1hour
```

### Top N
```
* | stats count by 'Host Name' | sort -count | head 10
```

### Filter and Aggregate
```
'Severity' = 'Error' | stats count by 'Log Source', 'Host Name'
```

## Reference
Full documentation: https://docs.oracle.com/en-us/iaas/log-analytics/doc/command-reference.html
"""
