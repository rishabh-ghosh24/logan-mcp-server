"""parser_failure_triage — surface top parser failures with sample raw lines."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _build_stats_query(top_n: int) -> str:
    """Build query to find top N parser failures by count.

    Returns a Log Analytics query that:
    - Filters to Parser Failure log source
    - Counts failures and tracks first/last seen times per parser
    - Sorts descending by failure count
    - Limits to top N results
    """
    return (
        "'Log Source' = 'Parser Failure' | "
        "stats count as failure_count, "
        "earliest('Time') as first_seen, "
        "latest('Time') as last_seen "
        "by 'Parser Name', 'Log Source' | "
        f"sort -failure_count | head {top_n}"
    )


def _build_samples_query(parser_names: List[str]) -> str:
    """Fetch raw failure lines for the given parsers.

    The `head` limit is a global cap (`len(parser_names) * 3`); per-parser
    capping to 3 lines happens in `_parse_samples_response`.
    """
    escaped = ", ".join(
        f"'{n.replace(chr(39), chr(39) * 2)}'" for n in parser_names
    )
    return (
        f"'Log Source' = 'Parser Failure' AND 'Parser Name' in ({escaped}) | "
        "fields 'Parser Name', 'Original Log Content' | "
        f"head {len(parser_names) * 3}"
    )
