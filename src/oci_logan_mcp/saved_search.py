"""Saved search operations service."""

import oci
from typing import Optional, List, Dict, Any

from .client import OCILogAnalyticsClient
from .cache import CacheManager

PROVIDER_ID = "log-analytics"
PROVIDER_NAME = "Logging Analytics"
PROVIDER_VERSION = "3.0.0"

# OCI Management Dashboard API requires crossService in featuresConfig
FEATURES_CONFIG = {"crossService": False}


class SavedSearchService:
    """Service for managing saved searches."""

    def __init__(self, oci_client: OCILogAnalyticsClient, cache: CacheManager):
        """Initialize saved search service."""
        self.oci_client = oci_client
        self.cache = cache

    async def list_searches(self) -> List[Dict[str, Any]]:
        """List all saved searches."""
        cache_key = "saved_searches"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        searches = await self.oci_client.list_saved_searches()
        # Exclude Logan-managed backing resources (alert tasks, etc.)
        searches = [
            s for s in searches
            if s.get("freeform_tags", {}).get("logan_managed") != "true"
        ]
        self.cache.set(cache_key, searches)
        return searches

    async def get_search_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find a saved search by display name."""
        searches = await self.list_searches()
        for search in searches:
            if search.get("display_name", "").lower() == name.lower():
                return await self.get_search_by_id(search["id"])
        return None

    async def get_search_by_id(self, search_id: str) -> Dict[str, Any]:
        """Get a saved search by ID."""
        cache_key = f"saved_search:{search_id}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        search = await self.oci_client.get_saved_search(search_id)
        self.cache.set(cache_key, search)
        return search

    async def find_searches(
        self, keyword: Optional[str] = None, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Find saved searches matching a keyword."""
        searches = await self.list_searches()

        if keyword:
            keyword_lower = keyword.lower()
            searches = [
                s
                for s in searches
                if keyword_lower in s.get("display_name", "").lower()
            ]

        return searches[:limit]

    async def create_search(
        self,
        display_name: str,
        query: str,
        description: Optional[str] = None,
        compartment_id: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new saved search backed by a ManagementSavedSearch and a ScheduledTask."""
        cid = compartment_id or self.oci_client.compartment_id

        mss_details = oci.management_dashboard.models.CreateManagementSavedSearchDetails(
            display_name=display_name,
            compartment_id=cid,
            description=description or "",
            type="SEARCH_SHOW_IN_DASHBOARD",
            provider_id=PROVIDER_ID,
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            metadata_version="2.0",
            nls={},
            data_config=[{"query": query}],
            screen_image="",
            widget_template="",
            widget_vm="",
            parameters_config=[],
            drilldown_config=[],
            features_config=FEATURES_CONFIG,
        )
        mss = await self.oci_client.create_management_saved_search(mss_details)

        try:
            task_details = oci.log_analytics.models.CreateStandardTaskDetails(
                kind="STANDARD",
                task_type="SAVED_SEARCH",
                display_name=display_name,
                compartment_id=cid,
                action=oci.log_analytics.models.StreamAction(
                    saved_search_id=mss["id"],
                    saved_search_duration="PT1H",
                ),
                schedules=[],
            )
            task = await self.oci_client.create_scheduled_task(task_details)
        except Exception:
            await self.oci_client.delete_management_saved_search(mss["id"])
            raise

        self.cache.delete("saved_searches")
        return {
            "id": task["id"],
            "display_name": display_name,
            "management_saved_search_id": mss["id"],
        }

    async def _get_backing_mss_id(self, search_id: str) -> Optional[str]:
        """Retrieve the ManagementSavedSearch id that backs a ScheduledTask."""
        task_data = await self.oci_client.get_saved_search(search_id)
        action = task_data.get("_action")
        if action and hasattr(action, "saved_search_id"):
            return action.saved_search_id
        return None

    async def update_search(self, search_id: str, **kwargs) -> Dict[str, Any]:
        """Update display_name and/or query of an existing saved search."""
        backing_mss_id = await self._get_backing_mss_id(search_id)

        if "query" in kwargs and backing_mss_id:
            mss_update = oci.management_dashboard.models.UpdateManagementSavedSearchDetails(
                data_config=[{"query": kwargs["query"]}],
            )
            await self.oci_client.update_management_saved_search(backing_mss_id, mss_update)

        if "display_name" in kwargs:
            task_update = oci.log_analytics.models.UpdateStandardTaskDetails(
                kind="STANDARD",
                display_name=kwargs["display_name"],
            )
            await self.oci_client.update_scheduled_task(search_id, task_update)
            if backing_mss_id:
                mss_name_update = oci.management_dashboard.models.UpdateManagementSavedSearchDetails(
                    display_name=kwargs["display_name"],
                )
                await self.oci_client.update_management_saved_search(backing_mss_id, mss_name_update)

        self.cache.delete("saved_searches")
        self.cache.delete(f"saved_search:{search_id}")
        return {"id": search_id, "updated": list(kwargs.keys()), **{k: v for k, v in kwargs.items()}}

    async def delete_search(self, search_id: str) -> None:
        """Delete a saved search and its backing ManagementSavedSearch."""
        backing_mss_id = await self._get_backing_mss_id(search_id)
        await self.oci_client.delete_scheduled_task(search_id)
        if backing_mss_id:
            try:
                await self.oci_client.delete_management_saved_search(backing_mss_id)
            except Exception:
                pass
        self.cache.delete("saved_searches")
        self.cache.delete(f"saved_search:{search_id}")
