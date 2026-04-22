"""parser_failure_triage — surface top sources with parse failures and sample raw lines."""

from __future__ import annotations

from typing import Any, Dict, List


def _build_stats_query(top_n: int) -> str:
    """Build query to find top N log sources with parse failures.

    Filters to records where `'Parse Failed' = 1` — the per-record flag Log
    Analytics sets when a line fails to parse. Each log source in OCI LA has
    one parser configured, so grouping by 'Log Source' is equivalent to
    grouping by "which parser is broken" (there is no separate 'Parser Name'
    field on these records).
    """
    return (
        "'Parse Failed' = 1 | "
        "stats count as failure_count, "
        "earliest('Time') as first_seen, "
        "latest('Time') as last_seen "
        "by 'Log Source' | "
        f"sort -failure_count | head {top_n}"
    )


def _build_samples_query(sources: List[str]) -> str:
    """Fetch raw failure lines for the given sources.

    The `head` limit is a global best-effort cap (`len(sources) * 3`);
    per-source capping to 3 lines happens in `_parse_samples_response`.
    If one source dominates the first N rows, low-volume sources may
    receive fewer than 3 samples (or none). This is acceptable per spec
    ("up to 3 samples").
    """
    escaped = ", ".join(
        f"'{s.replace(chr(39), chr(39) * 2)}'" for s in sources
    )
    return (
        f"'Parse Failed' = 1 AND 'Log Source' in ({escaped}) | "
        "fields 'Log Source', 'Original Log Content' | "
        f"head {len(sources) * 3}"
    )


def _parse_stats_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse stats query response into list of source failure records.

    Expected columns: Log Source, failure_count, first_seen, last_seen.
    Returns empty list if response is malformed or missing required columns.
    """
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", [])]
    rows = data.get("rows", [])
    required = {"Log Source", "failure_count", "first_seen", "last_seen"}
    if not required.issubset(columns):
        return []
    src_idx = columns.index("Log Source")
    cnt_idx = columns.index("failure_count")
    fs_idx = columns.index("first_seen")
    ls_idx = columns.index("last_seen")
    out = []
    for row in rows:
        if not row:
            continue
        out.append({
            "source": str(row[src_idx]),
            "failure_count": int(row[cnt_idx]) if row[cnt_idx] is not None else 0,
            "first_seen": str(row[fs_idx]) if row[fs_idx] is not None else None,
            "last_seen": str(row[ls_idx]) if row[ls_idx] is not None else None,
        })
    return out


def _parse_samples_response(response: Dict[str, Any]) -> Dict[str, List[str]]:
    """Parse samples query response into dict of source -> [raw lines].

    Keys on 'Log Source'. Caps at 3 samples per source. Returns an empty
    dict if the response is malformed or missing required columns.
    """
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", [])]
    rows = data.get("rows", [])
    required = {"Log Source", "Original Log Content"}
    if not required.issubset(columns):
        return {}
    src_idx = columns.index("Log Source")
    raw_idx = columns.index("Original Log Content")
    out: Dict[str, List[str]] = {}
    for row in rows:
        if not row:
            continue
        src = str(row[src_idx])
        raw = str(row[raw_idx]) if row[raw_idx] is not None else ""
        bucket = out.setdefault(src, [])
        if len(bucket) < 3:
            bucket.append(raw)
    return out


def _merge_results(
    stats: List[Dict[str, Any]],
    samples: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """Attach sample_raw_lines (keyed by source) to each stats entry."""
    out = []
    for entry in stats:
        out.append({
            **entry,
            "sample_raw_lines": samples.get(entry["source"], []),
        })
    return out


class ParserTriageTool:
    """Run two Logan queries to surface top parse-failing sources with samples."""

    def __init__(self, query_engine):
        self._engine = query_engine

    async def run(
        self,
        time_range: str = "last_24_hours",
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

        sources = [s["source"] for s in stats]
        samples_query = _build_samples_query(sources)
        samples_resp = await self._engine.execute(
            query=samples_query,
            time_range=time_range,
        )
        samples = _parse_samples_response(samples_resp)

        failures = _merge_results(stats, samples)
        total = sum(f["failure_count"] for f in failures)
        return {"failures": failures, "total_failure_count": total}
