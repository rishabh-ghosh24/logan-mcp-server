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

    def reserve(
        self,
        *,
        estimated_bytes: int = 0,
        estimated_cost_usd: float = 0.0,
        override: bool = False,
    ) -> None:
        """Atomically verify the request fits the budget AND commit the reservation.

        Race-safe: check and increment happen under a single lock acquisition, so
        concurrent callers cannot both pass a check that only one of them would.

        - On over-limit: raises ``BudgetExceededError``; counters unchanged.
        - On success: counters are incremented with the estimated values. The
          caller MUST call :meth:`release` with the same values if the query
          subsequently fails, to roll back the reservation.
        - ``override=True`` skips limit enforcement but still records the usage.
        - Disabled budget: no-op (no enforcement, no tracking).
        """
        if not self.limits.enabled:
            return
        with self._lock:
            if not override:
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
            self._usage.queries += 1
            self._usage.bytes += max(0, int(estimated_bytes))
            self._usage.cost_usd += max(0.0, float(estimated_cost_usd))

    def release(self, *, bytes: int, cost_usd: float) -> None:
        """Roll back a prior :meth:`reserve` (call this when the query failed).

        Counters floor at 0 so a mismatched release never underflows.
        Disabled budget: no-op.
        """
        if not self.limits.enabled:
            return
        with self._lock:
            self._usage.queries = max(0, self._usage.queries - 1)
            self._usage.bytes = max(0, self._usage.bytes - max(0, int(bytes)))
            self._usage.cost_usd = max(0.0, self._usage.cost_usd - max(0.0, float(cost_usd)))
