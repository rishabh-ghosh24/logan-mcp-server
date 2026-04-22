"""parser_failure_triage — surface top parser failures with sample raw lines."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _build_stats_query(top_n: int) -> str:
    """Build query to find top N parser failures by count.

    Filters to records where `'Parse Failed' = 1` — the per-record flag Log
    Analytics sets when parsing a raw line fails. Groups by 'Parser Name'
    and 'Log Source' so each result row identifies both the broken parser
    and the originating source it was attached to.
    """
    return (
        "'Parse Failed' = 1 | "
        "stats count as failure_count, "
        "earliest('Time') as first_seen, "
        "latest('Time') as last_seen "
        "by 'Parser Name', 'Log Source' | "
        f"sort -failure_count | head {top_n}"
    )


def _build_samples_query(parser_source_pairs: List[Tuple[str, str]]) -> str:
    """Fetch raw failure lines for the given (parser_name, source) pairs.

    The caller passes the exact `(Parser Name, Log Source)` tuples from the
    stats result. A parser attached to multiple sources appears as multiple
    tuples, and the caller is expected to merge samples keyed on the same
    tuple so sample lines never bleed across sources.

    The filter intentionally uses `IN` on each dimension independently rather
    than a tuple-IN (which Logan does not support); this can over-fetch the
    cross product, but the per-key cap in `_parse_samples_response` contains
    the result set and the merge function ignores unmatched cross-product
    rows.

    The `head` limit is a global best-effort cap
    (`len(parser_source_pairs) * 3`); per-pair capping to 3 lines happens in
    `_parse_samples_response`. If one pair dominates the first N rows,
    low-volume pairs may receive fewer than 3 samples (or none). This is
    acceptable per spec ("up to 3 samples").
    """
    parser_names = sorted({p for p, _ in parser_source_pairs})
    sources = sorted({s for _, s in parser_source_pairs})
    escaped_parsers = ", ".join(
        f"'{p.replace(chr(39), chr(39) * 2)}'" for p in parser_names
    )
    escaped_sources = ", ".join(
        f"'{s.replace(chr(39), chr(39) * 2)}'" for s in sources
    )
    return (
        f"'Parse Failed' = 1 AND "
        f"'Parser Name' in ({escaped_parsers}) AND "
        f"'Log Source' in ({escaped_sources}) | "
        "fields 'Parser Name', 'Log Source', 'Original Log Content' | "
        f"head {len(parser_source_pairs) * 3}"
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


def _parse_samples_response(
    response: Dict[str, Any],
) -> Dict[Tuple[str, str], List[str]]:
    """Parse samples query response into dict of (parser, source) -> [raw lines].

    Keys on `(Parser Name, Log Source)` so samples from the same parser
    attached to different sources are kept separate. Caps at 3 samples per
    key. Returns an empty dict if the response is malformed or missing
    required columns.
    """
    data = response.get("data", {}) or {}
    columns = [c.get("name") for c in data.get("columns", [])]
    rows = data.get("rows", [])
    required = {"Parser Name", "Log Source", "Original Log Content"}
    if not required.issubset(columns):
        return {}
    pn_idx = columns.index("Parser Name")
    src_idx = columns.index("Log Source")
    raw_idx = columns.index("Original Log Content")
    out: Dict[Tuple[str, str], List[str]] = {}
    for row in rows:
        if not row:
            continue
        key = (str(row[pn_idx]), str(row[src_idx]))
        raw = str(row[raw_idx]) if row[raw_idx] is not None else ""
        bucket = out.setdefault(key, [])
        if len(bucket) < 3:
            bucket.append(raw)
    return out


def _merge_results(
    stats: List[Dict[str, Any]],
    samples: Dict[Tuple[str, str], List[str]],
) -> List[Dict[str, Any]]:
    """Merge stats and samples into final result records.

    Looks up samples by `(parser_name, source)` so a parser attached to
    multiple sources gets each row's samples drawn from its own source only.
    """
    out = []
    for entry in stats:
        key = (entry["parser_name"], entry["source"])
        out.append({
            **entry,
            "sample_raw_lines": samples.get(key, []),
        })
    return out


class ParserTriageTool:
    """Run two Logan queries to surface top parser failures with sample lines."""

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

        pairs = [(s["parser_name"], s["source"]) for s in stats]
        samples_query = _build_samples_query(pairs)
        samples_resp = await self._engine.execute(
            query=samples_query,
            time_range=time_range,
        )
        samples = _parse_samples_response(samples_resp)

        failures = _merge_results(stats, samples)
        total = sum(f["failure_count"] for f in failures)
        return {"failures": failures, "total_failure_count": total}
