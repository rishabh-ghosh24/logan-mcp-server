"""Query execution service for Log Analytics."""

import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime

from .client import OCILogAnalyticsClient
from .cache import CacheManager
from .query_logger import QueryLogger
from .time_parser import parse_time_range
from .next_steps import suggest as _suggest_next_steps
from .query_estimator import QueryEstimator, QueryEstimate
from .budget_tracker import BudgetTracker, BudgetExceededError


class QueryEngine:
    """Handles query execution and result processing."""

    def __init__(
        self,
        oci_client: OCILogAnalyticsClient,
        cache: CacheManager,
        logger: QueryLogger,
        estimator: Optional[QueryEstimator] = None,
        budget_tracker: Optional[BudgetTracker] = None,
    ):
        """Initialize query engine."""
        self.oci_client = oci_client
        self.cache = cache
        self.logger = logger
        self.estimator = estimator
        self.budget_tracker = budget_tracker

    @staticmethod
    def _flatten_estimate(response: Dict[str, Any], estimate_dict: Optional[Dict]) -> None:
        if not estimate_dict:
            return
        response["estimated_bytes"] = estimate_dict.get("estimated_bytes")
        response["estimated_rows"] = estimate_dict.get("estimated_rows")
        response["estimated_cost_usd"] = estimate_dict.get("estimated_cost_usd")
        response["estimated_eta_seconds"] = estimate_dict.get("estimated_eta_seconds")
        response["estimate_confidence"] = estimate_dict.get("confidence")
        response["estimate_rationale"] = estimate_dict.get("rationale")

    async def execute(
        self,
        query: str,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
        time_range: Optional[str] = None,
        max_results: Optional[int] = None,
        include_subcompartments: bool = True,
        use_cache: bool = True,
        compartment_id: Optional[str] = None,
        budget_override: bool = False,
    ) -> Dict[str, Any]:
        """Execute a Log Analytics query."""
        return await self._execute_inner(
            query=query,
            time_start=time_start,
            time_end=time_end,
            time_range=time_range,
            max_results=max_results,
            include_subcompartments=include_subcompartments,
            use_cache=use_cache,
            compartment_id=compartment_id,
            skip_budget=False,
            budget_override=budget_override,
        )

    async def _execute_inner(
        self,
        query: str,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
        time_range: Optional[str] = None,
        max_results: Optional[int] = None,
        include_subcompartments: bool = True,
        use_cache: bool = True,
        compartment_id: Optional[str] = None,
        skip_budget: bool = False,
        budget_override: bool = False,
    ) -> Dict[str, Any]:
        """Internal execute with cache-first ordering."""
        # Parse time parameters
        start, end = parse_time_range(time_start, time_end, time_range)

        # Determine which compartment to use
        effective_compartment = compartment_id or self.oci_client.compartment_id

        # --- Cache-first: check cache before any estimation or OCI calls ---
        cache_key = self._make_cache_key(query, start, end, include_subcompartments, effective_compartment)
        if use_cache:
            cached_bundle = self.cache.get(cache_key)
            if cached_bundle is not None:
                # Support both old format (raw result) and new format ({result, estimate})
                if isinstance(cached_bundle, dict) and "result" in cached_bundle:
                    cached_result = cached_bundle["result"]
                    cached_estimate = cached_bundle.get("estimate")
                else:
                    # Legacy cache format — raw result, no estimate
                    cached_result = cached_bundle
                    cached_estimate = None

                response = {
                    "source": "cache",
                    "data": cached_result,
                    "metadata": {
                        "query": query,
                        "compartment_id": effective_compartment,
                        "time_start": start.isoformat(),
                        "time_end": end.isoformat(),
                        "include_subcompartments": include_subcompartments,
                    },
                }
                self._flatten_estimate(response, cached_estimate)
                response["next_steps"] = [s.to_dict() for s in _suggest_next_steps(query, response)]
                return response

        # --- Live path: estimate first, then budget check, then execute ---
        estimate: Optional[QueryEstimate] = None
        if self.estimator is not None:
            estimate = await self.estimator.estimate(
                query=query,
                time_range=time_range,
                time_start=time_start,
                time_end=time_end,
                compartment_id=effective_compartment,
                include_subcompartments=include_subcompartments,
            )

        if not skip_budget and self.budget_tracker is not None and estimate is not None:
            self.budget_tracker.check(
                estimated_bytes=estimate.estimated_bytes,
                estimated_cost_usd=float(estimate.estimated_cost_usd or 0.0),
                override=budget_override,
            )

        # Execute query
        start_time = datetime.now()
        try:
            result = await self.oci_client.query(
                query_string=query,
                time_start=start.isoformat(),
                time_end=end.isoformat(),
                max_results=max_results,
                include_subcompartments=include_subcompartments,
                compartment_id=compartment_id,
            )

            execution_time = (datetime.now() - start_time).total_seconds()

            if use_cache:
                cache_payload = {
                    "result": result,
                    "estimate": estimate.to_dict() if estimate is not None else None,
                }
                self.cache.set(cache_key, cache_payload)

            self.logger.log_query(
                query=query,
                time_start=start,
                time_end=end,
                execution_time=execution_time,
                result_count=len(result.get("rows", [])),
                success=True,
            )

            # Budget: record actual usage after successful live execution
            if not skip_budget and self.budget_tracker is not None and estimate is not None:
                self.budget_tracker.record(
                    actual_bytes=int(estimate.estimated_bytes),
                    actual_cost_usd=float(estimate.estimated_cost_usd or 0.0),
                )

            response = {
                "source": "live",
                "data": result,
                "metadata": {
                    "query": query,
                    "compartment_id": effective_compartment,
                    "time_start": start.isoformat(),
                    "time_end": end.isoformat(),
                    "include_subcompartments": include_subcompartments,
                    "execution_time_seconds": execution_time,
                },
            }
            self._flatten_estimate(response, estimate.to_dict() if estimate is not None else None)
            response["next_steps"] = [s.to_dict() for s in _suggest_next_steps(query, response)]
            return response

        except Exception as e:
            execution_time = (datetime.now() - start_time).total_seconds()
            self.logger.log_query(
                query=query,
                time_start=start,
                time_end=end,
                execution_time=execution_time,
                result_count=0,
                success=False,
                error=str(e),
            )
            raise

    async def execute_batch(
        self,
        queries: List[Dict[str, Any]],
        include_subcompartments: bool = True,
        compartment_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Execute multiple queries concurrently. Budget is NOT enforced for batch (P0)."""
        tasks = [
            self._execute_inner(
                query=q["query"],
                time_start=q.get("time_start"),
                time_end=q.get("time_end"),
                time_range=q.get("time_range"),
                max_results=q.get("max_results"),
                include_subcompartments=q.get("include_subcompartments", include_subcompartments),
                compartment_id=q.get("compartment_id", compartment_id),
                skip_budget=True,
            )
            for q in queries
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        return [
            {"success": True, "result": r}
            if not isinstance(r, Exception)
            else {"success": False, "error": str(r)}
            for r in results
        ]

    def _make_cache_key(
        self,
        query: str,
        start: datetime,
        end: datetime,
        include_subcompartments: bool = True,
        compartment_id: Optional[str] = None,
    ) -> str:
        """Generate cache key for a query."""
        sub_flag = "sub" if include_subcompartments else "nosub"
        comp = compartment_id or "default"
        return f"{query}:{start.isoformat()}:{end.isoformat()}:{sub_flag}:{comp}"
