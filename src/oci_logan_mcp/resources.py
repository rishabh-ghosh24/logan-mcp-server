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
        {
            "uri": "loganalytics://tenancy-context",
            "name": "Tenancy Context",
            "description": (
                "Environment-specific context including known log sources, "
                "fields, entities, compartments, and operational notes. "
                "Refreshed at every server start."
            ),
            "mimeType": "application/json",
        },
        {
            "uri": "loganalytics://reference-docs",
            "name": "Reference Documentation",
            "description": (
                "Links to official Oracle Log Analytics documentation "
                "for query syntax, operators, and functions."
            ),
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

### String Functions (use in eval)
- `concat('Field1', ' ', 'Field2')` - Concatenate values
- `substr('Field', start, length)` - Extract substring
- `trim('Field')` - Remove whitespace
- `upper('Field')` / `lower('Field')` - Case conversion
- `length('Field')` - String length
- `replace('Field', 'old', 'new')` - Replace text

### Regular Expressions
- `| where 'Field' regex '\\d+\\.\\d+\\.\\d+\\.\\d+'` - Match IP addresses
- `| eval extracted = regex('Field', '(\\d+)ms')` - Extract patterns
- `| where Message regex '(?i)error'` - Case-insensitive regex

### Conditional Logic
- `| eval status = if(count > 100, 'high', 'low')` - If/else
- `| eval tier = case(count > 1000, 'critical', count > 100, 'warning', 'normal')` - Case/when

### Time Functions
- `| eval hour = formatDate('Time', 'HH')` - Extract hour
- `| eval day = formatDate('Time', 'EEEE')` - Day of week
- `| eval elapsed = dateDiff('End Time', 'Start Time', 'SECONDS')` - Time difference

### Advanced Commands
- `| distinct 'Field'` - Unique values
- `| dedup 'Field1', 'Field2'` - Remove duplicates
- `| addfields count as volume` - Add computed fields
- `| link 'Field1', 'Field2'` - Link analysis between fields
- `| cluster` - Auto-cluster similar log entries
- `| classify` - Auto-classify log patterns
- `| eventstats count by 'Field'` - Stats without collapsing rows
- `| delta 'Field'` - Compute difference between consecutive values
- `| lookup table='LookupTable' 'Key'` - Lookup table joins
- `| nlp` - Natural language processing on text fields

## Reference
- Command Reference: https://docs.oracle.com/en-us/iaas/log-analytics/doc/command-reference.html
- Knowledge Content: https://docs.oracle.com/en-us/iaas/log-analytics/doc/knowledge-content-reference.html
- Appendices: https://docs.oracle.com/en-us/iaas/log-analytics/doc/appendices.html
"""


def get_reference_docs() -> Dict[str, Any]:
    """Get links to official Oracle Log Analytics documentation."""
    return {
        "documentation": [
            {
                "name": "Command Reference",
                "url": "https://docs.oracle.com/en-us/iaas/log-analytics/doc/command-reference.html",
                "description": (
                    "Complete reference for all query commands including stats, "
                    "where, eval, timestats, sort, head, tail, fields, rename, "
                    "link, cluster, classify, lookup, and more."
                ),
            },
            {
                "name": "Knowledge Content Reference",
                "url": "https://docs.oracle.com/en-us/iaas/log-analytics/doc/knowledge-content-reference.html",
                "description": (
                    "Reference for built-in log sources, parsers, fields, labels, "
                    "and entity types available in OCI Log Analytics."
                ),
            },
            {
                "name": "Appendices",
                "url": "https://docs.oracle.com/en-us/iaas/log-analytics/doc/appendices.html",
                "description": (
                    "Additional reference material including query language "
                    "operators, functions, and advanced features."
                ),
            },
            {
                "name": "Query Language Overview",
                "url": "https://docs.oracle.com/en-us/iaas/log-analytics/doc/query-language-overview.html",
                "description": "Introduction to the Log Analytics query language with examples.",
            },
        ],
        "tip": "Use these docs when you need detailed syntax for advanced operators like eval, link, cluster, classify, or lookup.",
    }
