"""MCP resource definitions for Log Analytics."""

import functools
import importlib.resources
from typing import List, Dict, Any

import yaml


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


@functools.lru_cache(maxsize=1)
def get_query_templates() -> Dict[str, Any]:
    """Load built-in query templates from packaged YAML.

    These are generic, battle-tested query patterns validated against
    real OCI Log Analytics environments.  They ship with the server
    so any LLM/IDE gets working examples out-of-the-box.
    """
    data_file = importlib.resources.files("oci_logan_mcp") / "data" / "builtin_queries.yaml"
    raw = data_file.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    return {"templates": data.get("queries", [])}


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
**IMPORTANT**: Use spaces around `=` in span clause.
- `| timestats span = 1hour count` - Count per hour
- `| timestats span = 1day count` - Count per day
- `| timestats span = 15min avg('Field')` - Average per 15 minutes
- `| timestats span = 1hour count by 'Log Source'` - Per hour per source

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
* | timestats span = 1hour count
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

## Important Syntax Notes (Lessons Learned)
- **TIMESTATS span**: MUST use spaces around `=` sign
  - Correct:  `timestats span = 1hour count`
  - WRONG:    `timestats count span=1hour`
- **Sort descending**: Use `-` prefix on field name → `sort -'Field Name'`
- **Alias with quotes**: `count as 'My Alias'` (single quotes for aliases with spaces)
- **Log Source filter**: `'Log Source' = 'Exact Name'` or `'Log Source' != null` for all
- **Scope**: Use `tenancy` scope for org-wide queries across all compartments
- **String matching**: `where Message contains 'text'` or `where Message like '%pattern%'`
- **Field names**: Always quote multi-word field names: `'Log Source'`, `'Host Name'`

## Chart Type Recommendations
Choose the right chart type for your query pattern:
- **Pie chart**: `stats count by 'Field' | head 10` — limit to 10 slices for readability
- **Bar / Vertical bar**: `stats count by 'Field' | sort -count | head 10-20`
- **Line / Area**: `timestats span = 1hour count by 'Field'` — time series trends
- **Table**: Any `stats` query — clean tabular display of results
- **Tile**: Single aggregate → `stats count as 'Label'` — big KPI number
- **Heatmap**: Two group-by dimensions → `stats count by 'Field1', 'Field2'`
- **Histogram**: Numeric value distribution → `stats count by 'Field' | head 20`

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
            {
                "name": "Log Analytics Home",
                "url": "https://docs.oracle.com/en-us/iaas/log-analytics/home.htm",
                "description": (
                    "Log Analytics documentation home page. Start here for "
                    "concepts, getting started guides, and feature overviews."
                ),
            },
            {
                "name": "REST API Reference",
                "url": "https://docs.oracle.com/en-us/iaas/api/#/en/logan-api-spec/20200601/",
                "description": (
                    "Complete REST API reference for Log Analytics. Covers all "
                    "API endpoints, request/response schemas, and error codes."
                ),
            },
            {
                "name": "OCI CLI — Log Analytics Commands",
                "url": "https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/log-analytics.html",
                "description": (
                    "OCI CLI command reference for log-analytics. Useful for "
                    "understanding available operations and parameter names."
                ),
            },
        ],
        "tip": (
            "Use these docs when you need detailed syntax for advanced operators, "
            "API parameter names, or to verify correct field names and operations. "
            "Read the reference-docs resource on demand — do not preload."
        ),
    }
