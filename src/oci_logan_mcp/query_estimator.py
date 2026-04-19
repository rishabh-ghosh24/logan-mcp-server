"""Query cost + ETA estimation service (H1).

P0 strategy: probe-based volume estimate. For each Log Source referenced in
the query, issue a cheap count-over-last-hour probe against OCI, scale to
the query's window, apply a conservative filter-selectivity discount, and
convert bytes → cost and bytes → ETA via config constants. Probes are cached
per-source with a configurable TTL.

Estimator NEVER raises. On any failure it returns a safe `confidence="low"`
estimate so callers can decide without exception-handling boilerplate.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import Settings
from .time_parser import parse_time_range

logger = logging.getLogger(__name__)


@dataclass
class QueryEstimate:
    estimated_bytes: int
    estimated_rows: Optional[int]
    estimated_cost_usd: Optional[float]
    estimated_eta_seconds: float
    confidence: str  # "high" | "medium" | "low"
    rationale: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "estimated_bytes": self.estimated_bytes,
            "estimated_rows": self.estimated_rows,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_eta_seconds": self.estimated_eta_seconds,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


_SOURCE_EQ_RE = re.compile(r"'Log Source'\s*=\s*'([^']+)'", re.IGNORECASE)
_SOURCE_IN_RE = re.compile(r"'Log Source'\s+in\s*\(([^)]+)\)", re.IGNORECASE)


class QueryEstimator:
    def __init__(self, oci_client, settings: Settings) -> None:
        self.oci_client = oci_client
        self.settings = settings
        self._probe_cache: Dict[str, tuple] = {}

    @staticmethod
    def _extract_sources(query: str) -> List[str]:
        sources: List[str] = []
        for m in _SOURCE_EQ_RE.finditer(query or ""):
            sources.append(m.group(1))
        for m in _SOURCE_IN_RE.finditer(query or ""):
            for raw in m.group(1).split(","):
                name = raw.strip().strip("'").strip('"')
                if name:
                    sources.append(name)
        seen = set()
        out: List[str] = []
        for s in sources:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    @staticmethod
    def _has_filters(query: str) -> bool:
        """Return True if the query has additional filters beyond just the Log Source selector."""
        if not query:
            return False
        # Strip out the 'Log Source' = / in clauses and check for remaining filters
        stripped = re.sub(r"'Log Source'\s*(=\s*'[^']*'|in\s*\([^)]*\))", "", query, flags=re.IGNORECASE)
        if re.search(r"\bwhere\b", stripped, re.IGNORECASE):
            return True
        # Look for comparison operators in remaining query (not pipe commands)
        if re.search(r"\b\w+\s*[=<>!]=?\s*'[^']*'", stripped, re.IGNORECASE):
            return True
        if re.search(r"\band\b|\bor\b", stripped, re.IGNORECASE):
            return True
        return False

    def _window_hours(self, time_range: Optional[str]) -> float:
        try:
            start, end = parse_time_range(None, None, time_range or "last_1_hour")
            return max(0.1, (end - start).total_seconds() / 3600.0)
        except Exception:
            return 1.0

    async def estimate(
        self,
        query: str,
        time_range: Optional[str] = None,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
    ) -> QueryEstimate:
        """Return an estimate. Never raises."""
        try:
            return await self._estimate_inner(query, time_range, time_start, time_end)
        except Exception as e:
            logger.warning("QueryEstimator failed unexpectedly: %s", e)
            return self._safe_default("internal_error")

    def _safe_default(self, reason: str) -> QueryEstimate:
        return QueryEstimate(
            estimated_bytes=0,
            estimated_rows=None,
            estimated_cost_usd=None,
            estimated_eta_seconds=0.0,
            confidence="low",
            rationale=f"No estimate available ({reason}).",
        )

    async def _estimate_inner(
        self,
        query: str,
        time_range: Optional[str],
        time_start: Optional[str],
        time_end: Optional[str],
    ) -> QueryEstimate:
        sources = self._extract_sources(query)
        if not sources:
            return self._safe_default("no source filter")

        if time_start and time_end:
            try:
                start, end = parse_time_range(time_start, time_end, None)
                hours = max(0.1, (end - start).total_seconds() / 3600.0)
            except Exception:
                hours = 1.0
        else:
            hours = self._window_hours(time_range)

        total_bytes = 0.0
        confidences: List[str] = []
        rationales: List[str] = []
        for source in sources:
            bph = await self._probe_bytes_per_hour(source)
            if bph is None:
                rationales.append(f"{source}: probe failed")
                confidences.append("low")
                continue
            total_bytes += bph * hours
            confidences.append("medium")
            rationales.append(f"{source}: ~{int(bph)} bytes/hr × {hours:.1f}h")

        if self._has_filters(query):
            total_bytes *= self.settings.cost.filter_selectivity_discount
            rationales.append(f"× {self.settings.cost.filter_selectivity_discount} filter discount")

        cost = (total_bytes / (1024**3)) * self.settings.cost.cost_per_gb_usd
        throughput_bps = self.settings.cost.eta_throughput_mbps * 1024 * 1024
        eta = total_bytes / throughput_bps if throughput_bps > 0 else 0.0

        confidence = "medium" if confidences and all(c == "medium" for c in confidences) else "low"
        if total_bytes == 0:
            confidence = "low"

        return QueryEstimate(
            estimated_bytes=int(total_bytes),
            estimated_rows=None,
            estimated_cost_usd=round(cost, 4),
            estimated_eta_seconds=round(eta, 2),
            confidence=confidence,
            rationale="; ".join(rationales),
        )

    async def _probe_bytes_per_hour(self, source: str) -> Optional[float]:
        ttl = self.settings.cost.probe_ttl_seconds
        now = time.time()
        cached = self._probe_cache.get(source)
        if cached and (now - cached[1]) < ttl:
            return cached[0]
        try:
            probe_result = await self.oci_client.query(
                query_string=f"'Log Source' = '{source}' | stats count",
                time_start=None,
                time_end=None,
                max_results=1,
                include_subcompartments=True,
            )
            rows = (probe_result or {}).get("rows", [])
            count = 0
            if rows and isinstance(rows[0], list) and rows[0]:
                try:
                    count = int(rows[0][0] or 0)
                except (TypeError, ValueError):
                    count = 0
            bytes_per_hour = float(count) * 500.0
            self._probe_cache[source] = (bytes_per_hour, now)
            return bytes_per_hour
        except Exception as e:
            logger.info("probe failed for source=%s: %s", source, e)
            return None
