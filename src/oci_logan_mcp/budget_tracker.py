"""Per-session query budget enforcement (N5)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict


class BudgetExceededError(Exception):
    """Raised when a query would push per-session usage over a limit."""


@dataclass
class BudgetLimits:
    enabled: bool = True
    max_queries_per_session: int = 100
    max_bytes_per_session: int = 10 * 1024**3
    max_cost_usd_per_session: float = 5.00


@dataclass
class BudgetUsage:
    queries: int = 0
    bytes: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {"queries": self.queries, "bytes": self.bytes, "cost_usd": round(self.cost_usd, 4)}


class BudgetTracker:
    def __init__(self, session_id: str, limits: BudgetLimits) -> None:
        self.session_id = session_id
        self.limits = limits
        self._usage = BudgetUsage()
        self._lock = threading.Lock()

    def snapshot(self) -> BudgetUsage:
        with self._lock:
            return BudgetUsage(
                queries=self._usage.queries,
                bytes=self._usage.bytes,
                cost_usd=self._usage.cost_usd,
            )

    def remaining(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "queries": max(0, self.limits.max_queries_per_session - self._usage.queries),
                "bytes": max(0, self.limits.max_bytes_per_session - self._usage.bytes),
                "cost_usd": round(max(0.0, self.limits.max_cost_usd_per_session - self._usage.cost_usd), 4),
            }

    def check(
        self,
        *,
        estimated_bytes: int = 0,
        estimated_cost_usd: float = 0.0,
        override: bool = False,
    ) -> None:
        if not self.limits.enabled or override:
            return
        with self._lock:
            if self._usage.queries + 1 > self.limits.max_queries_per_session:
                raise BudgetExceededError(
                    f"Session query count limit reached: "
                    f"{self._usage.queries}/{self.limits.max_queries_per_session}. "
                    f"Use budget_override=True with confirmation, or start a new session."
                )
            if self._usage.bytes + estimated_bytes > self.limits.max_bytes_per_session:
                raise BudgetExceededError(
                    f"Session bytes budget would be exceeded: "
                    f"{self._usage.bytes + estimated_bytes} > {self.limits.max_bytes_per_session}."
                )
            if self._usage.cost_usd + estimated_cost_usd > self.limits.max_cost_usd_per_session:
                raise BudgetExceededError(
                    f"Session cost budget would be exceeded: "
                    f"${self._usage.cost_usd + estimated_cost_usd:.2f} > "
                    f"${self.limits.max_cost_usd_per_session:.2f}."
                )

    def record(self, *, actual_bytes: int, actual_cost_usd: float) -> None:
        with self._lock:
            self._usage.queries += 1
            self._usage.bytes += max(0, int(actual_bytes))
            self._usage.cost_usd += max(0.0, float(actual_cost_usd))
