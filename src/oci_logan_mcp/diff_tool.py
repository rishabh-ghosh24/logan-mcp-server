"""diff_time_windows — before/after delta across two time windows."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

SIGNIFICANCE_THRESHOLD_PCT = 10.0
TOP_K_SUMMARY = 3


# Matches a `by <fields>` clause that follows a `| stats|eventstats|timestats` pipe.
# Anchoring on the pipe prevents matching the word "by" inside filter-value string
# literals (e.g. `'caused by X'`). If multiple stats pipes exist, we take the LAST
# match — that's the final grouping the caller cares about.
_BY_CLAUSE_RE = re.compile(
    r"\|\s*(?:stats|eventstats|timestats)\s+.*?\bby\s+(.+?)(?=\s*\||\s*$)",
    re.IGNORECASE,
)


def _extract_by_clause(query: str) -> List[str]:
    """Extract dimension field names from the final `| stats ... by <fields>` clause.

    Returns the first match's fields when only one stats clause exists; with
    multiple stats pipes, returns the last. Returns [] if no stats clause with
    a `by` is present. Handles single/multiple fields, quoted or unquoted.
    """
    matches = list(_BY_CLAUSE_RE.finditer(query))
    if not matches:
        return []
    raw = matches[-1].group(1).strip()
    return [p.strip().strip("'").strip('"') for p in raw.split(",") if p.strip()]


class DiffTool:
    """Run a query in two time windows and return a per-dimension delta."""

    def __init__(self, query_engine):
        self._engine = query_engine

    async def run(
        self,
        query: str,
        current_window: Dict[str, str],
        comparison_window: Dict[str, str],
        dimensions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        reused_breakout = False
        if dimensions is None:
            dimensions = _extract_by_clause(query)
            reused_breakout = bool(dimensions)

        effective_dims = dimensions or []
        # If we reused the query's own `by` clause, don't re-append stats —
        # the query already aggregates. Otherwise compose the stats pipe.
        composed = query if reused_breakout else self._compose_query(query, effective_dims)

        current_task = self._engine.execute(query=composed, **current_window)
        comparison_task = self._engine.execute(query=composed, **comparison_window)
        current_res, comparison_res = await asyncio.gather(current_task, comparison_task)

        current_rows = self._extract_rows(current_res, effective_dims)
        comparison_rows = self._extract_rows(comparison_res, effective_dims)

        delta = self._compute_delta(current_rows, comparison_rows, effective_dims)
        summary = self._build_summary(delta)

        return {
            "current": {"total": sum(r["count"] for r in current_rows), "rows": current_rows},
            "comparison": {"total": sum(r["count"] for r in comparison_rows), "rows": comparison_rows},
            "delta": delta,
            "summary": summary,
            "metadata": {
                "query": query,
                "composed_query": composed,
                "dimensions": effective_dims,
                "reused_breakout": reused_breakout,
                "current_window": current_window,
                "comparison_window": comparison_window,
            },
        }

    @staticmethod
    def _compose_query(query: str, dimensions: List[str]) -> str:
        if not dimensions:
            return f"{query} | stats count as count"
        dim_list = ", ".join(f"'{d}'" for d in dimensions)
        return f"{query} | stats count as count by {dim_list}"

    @staticmethod
    def _extract_rows(response: Dict[str, Any], dimensions: List[str]) -> List[Dict[str, Any]]:
        data = response.get("data", {}) or {}
        columns = [c.get("name") for c in data.get("columns", [])]
        rows_raw = data.get("rows", [])

        if not dimensions:
            count = 0
            if rows_raw and rows_raw[0]:
                count = int(rows_raw[0][0] or 0)
            return [{"key": ("__total__",), "count": count}]

        # Index by column name for safety (OCI can re-order columns).
        count_idx = columns.index("count") if "count" in columns else len(columns) - 1
        dim_idx = [columns.index(d) for d in dimensions if d in columns]

        extracted: List[Dict[str, Any]] = []
        for row in rows_raw:
            key = tuple(str(row[i]) for i in dim_idx)
            try:
                count = int(row[count_idx] or 0)
            except (TypeError, ValueError):
                count = 0
            extracted.append({"key": key, "count": count})
        return extracted

    @staticmethod
    def _compute_delta(
        current_rows: List[Dict[str, Any]],
        comparison_rows: List[Dict[str, Any]],
        dimensions: List[str],
    ) -> List[Dict[str, Any]]:
        curr = {r["key"]: r["count"] for r in current_rows}
        comp = {r["key"]: r["count"] for r in comparison_rows}
        all_keys = set(curr) | set(comp)

        def _label(key):
            if not dimensions:
                return "__total__"
            return ", ".join(f"{d}={v}" for d, v in zip(dimensions, key))

        rows = []
        for key in all_keys:
            c, p = curr.get(key, 0), comp.get(key, 0)
            if p == 0 and c == 0:
                pct, tag = 0.0, "stable"
            elif p == 0:
                pct, tag = float("inf"), "new"
            elif c == 0:
                pct, tag = -100.0, "disappeared"
            else:
                pct = (c - p) / p * 100.0
                if abs(pct) < SIGNIFICANCE_THRESHOLD_PCT:
                    tag = "stable"
                elif pct > 0:
                    tag = "spike"
                else:
                    tag = "drop"
            rows.append({
                "dimension": _label(key),
                "current": c,
                "comparison": p,
                "pct_change": pct if pct != float("inf") else None,  # JSON-safe
                "tag": tag,
            })
        # Spec contract: `delta` lists only *significant* changes. Stable rows
        # are omitted — callers can still see per-row totals via `current.rows`
        # / `comparison.rows`.
        significant = [r for r in rows if r["tag"] != "stable"]
        significant.sort(key=lambda r: (r["current"] + r["comparison"]), reverse=True)
        return significant

    @staticmethod
    def _build_summary(delta: List[Dict[str, Any]]) -> str:
        # `delta` is already stable-filtered by `_compute_delta`.
        if not delta:
            return "No significant change between windows."

        # Rank by absolute pct_change weighted by volume. Rows tagged "new"
        # carry pct_change=None (infinity isn't JSON-safe), so we can't weight
        # them by pct — put them in a higher tier (1, vol) so any new row
        # outranks any pct-bearing change. Within the new tier they still
        # order by volume.
        def _rank(r):
            pct = r["pct_change"]
            vol = max(r["current"], r["comparison"])
            if pct is None:
                return (1, vol)
            return (0, abs(pct) * vol)

        top = sorted(delta, key=_rank, reverse=True)[:TOP_K_SUMMARY]
        parts = []
        for r in top:
            if r["tag"] == "new":
                parts.append(f"{r['dimension']} is new ({r['current']} events)")
            elif r["tag"] == "disappeared":
                parts.append(f"{r['dimension']} disappeared (was {r['comparison']})")
            else:
                sign = "+" if r["pct_change"] > 0 else ""
                parts.append(f"{r['dimension']} {sign}{r['pct_change']:.0f}%")
        return "Significant change: " + "; ".join(parts) + "."
