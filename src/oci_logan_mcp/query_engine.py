"""Query execution service for Log Analytics."""

import asyncio
from typing import Optional, List, Dict, Any
from datetime import datetime

from .client import OCILogAnalyticsClient
from .cache import CacheManager
from .query_logger import QueryLogger
from .time_parser import parse_time_range


class QueryEngine:
    """Handles query execution and result processing."""

    def __init__(
        self,
        oci_client: OCILogAnalyticsClient,
        cache: CacheManager,
        logger: QueryLogger,
    ):
        """Initialize query engine."""
        self.oci_client = oci_client
        self.cache = cache
        self.logger = logger

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
    ) -> Dict[str, Any]:
        """Execute a Log Analytics query."""
        # Parse time parameters
        start, end = parse_time_range(time_start, time_end, time_range)

        # Determine which compartment to use
        effective_compartment = compartment_id or self.oci_client.compartment_id

        # Check cache
        cache_key = self._make_cache_key(query, start, end, include_subcompartments, effective_compartment)
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached:
                return {
                    "source": "cache",
                    "data": cached,
                    "metadata": {
                        "query": query,
                        "compartment_id": effective_compartment,
                        "time_start": start.isoformat(),
                        "time_end": end.isoformat(),
                        "include_subcompartments": include_subcompartments,
                    },
                }

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
                self.cache.set(cache_key, result)

            self.logger.log_query(
                query=query,
                time_start=start,
                time_end=end,
                execution_time=execution_time,
                result_count=len(result.get("rows", [])),
                success=True,
            )

            return {
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
        """Execute multiple queries concurrently."""
        tasks = [
            self.execute(
                query=q["query"],
                time_start=q.get("time_start"),
                time_end=q.get("time_end"),
                time_range=q.get("time_range"),
                max_results=q.get("max_results"),
                include_subcompartments=q.get("include_subcompartments", include_subcompartments),
                compartment_id=q.get("compartment_id", compartment_id),
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
