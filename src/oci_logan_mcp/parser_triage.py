"""parser_failure_triage — surface top parser failures with sample raw lines."""

from __future__ import annotations

from typing import Any, Dict, List


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

    The `head` limit is a global best-effort cap (`len(parser_names) * 3`);
    per-parser capping to 3 lines happens in `_parse_samples_response`.
    Note: if one parser dominates the first N rows, low-volume parsers
    appearing later may receive fewer than 3 samples (or none). This is
    acceptable per spec ("up to 3 samples") but worth knowing.
    """
    escaped = ", ".join(
        f"'{n.replace(chr(39), chr(39) * 2)}'" for n in parser_names
    )
    return (
        f"'Log Source' = 'Parser Failure' AND 'Parser Name' in ({escaped}) | "
        "fields 'Parser Name', 'Original Log Content' | "
        f"head {len(parser_names) * 3}"
    )


def _parse_stats_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse stats query response into list of parser failure records.

    Expected columns: Parser Name, Log Source, failure_count, first_seen, last_seen
    Returns empty list if response is malformed or missing required columns.
    """
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", [])]
    rows = data.get("rows", [])
    required = {"Parser Name", "Log Source", "failure_count", "first_seen", "last_seen"}
    if not required.issubset(columns):
        return []
    pn_idx = columns.index("Parser Name")
    src_idx = columns.index("Log Source")
    cnt_idx = columns.index("failure_count")
    fs_idx = columns.index("first_seen")
    ls_idx = columns.index("last_seen")
    out = []
    for row in rows:
        if not row:
            continue
        out.append({
            "parser_name": str(row[pn_idx]),
            "source": str(row[src_idx]),
            "failure_count": int(row[cnt_idx]) if row[cnt_idx] is not None else 0,
            "first_seen": str(row[fs_idx]) if row[fs_idx] is not None else None,
            "last_seen": str(row[ls_idx]) if row[ls_idx] is not None else None,
        })
    return out


def _parse_samples_response(response: Dict[str, Any]) -> Dict[str, List[str]]:
    """Parse samples query response into dict of parser -> [raw lines].

    Groups raw log content by parser name, capping at 3 samples per parser.
    Returns empty dict if response is malformed or missing required columns.
    """
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", [])]
    rows = data.get("rows", [])
    if "Parser Name" not in columns or "Original Log Content" not in columns:
        return {}
    pn_idx = columns.index("Parser Name")
    raw_idx = columns.index("Original Log Content")
    out: Dict[str, List[str]] = {}
    for row in rows:
        if not row:
            continue
        name = str(row[pn_idx])
        raw = str(row[raw_idx]) if row[raw_idx] is not None else ""
        bucket = out.setdefault(name, [])
        if len(bucket) < 3:
            bucket.append(raw)
    return out


def _merge_results(
    stats: List[Dict[str, Any]],
    samples: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """Merge stats and samples into final result records.

    Attaches sample_raw_lines from the samples dict to each stats entry.
    """
    out = []
    for entry in stats:
        out.append({
            **entry,
            "sample_raw_lines": samples.get(entry["parser_name"], []),
        })
    return out


class ParserTriageTool:
    """Run two Logan queries to surface top parser failures with sample lines."""

    def __init__(self, query_engine):
        self._engine = query_engine

    async def run(
        self,
        time_range: str = "last_24h",
        top_n: int = 20,
    ) -> Dict[str, Any]:
        stats_query = _build_stats_query(top_n)
        stats_resp = await self._engine.execute(
            query=stats_query,
            time_range=time_range,
        )
        stats = _parse_stats_response(stats_resp)

        if not stats:
            return {"failures": [], "total_failure_count": 0}

        parser_names = [s["parser_name"] for s in stats]
        samples_query = _build_samples_query(parser_names)
        samples_resp = await self._engine.execute(
            query=samples_query,
            time_range=time_range,
        )
        samples = _parse_samples_response(samples_resp)

        failures = _merge_results(stats, samples)
        total = sum(f["failure_count"] for f in failures)
        return {"failures": failures, "total_failure_count": total}
