"""Saved search operations service."""

import oci
from typing import Optional, List, Dict, Any

from .client import OCILogAnalyticsClient
from .cache import CacheManager
from .saved_search_config import build_scope_filters, build_ui_config
from .alert_links import CONSOLE_STAMP, find_alerts_for_saved_search
from .saved_search_dependencies import (
    DependencyInspectionError, dependency_snapshot, inspect_dependencies, require_unused,
)

PROVIDER_ID = "log-analytics"
PROVIDER_NAME = "Log Analytics"
PROVIDER_VERSION = "3.0.0"

# OCI Management Dashboard API requires crossService in featuresConfig
FEATURES_CONFIG = {"crossService": {"shared": True}}


class SavedSearchService:
    """Service for managing saved searches."""

    def __init__(self, oci_client: OCILogAnalyticsClient, cache: CacheManager, alarm_service=None):
        """Initialize saved search service."""
        self.oci_client = oci_client
        self.cache = cache
        self.alarm_service = alarm_service

    async def _refresh_linked_alerts(self, saved_search_id: str, query: str) -> List[Dict[str, Any]]:
        """After a query change, refresh every alarm that depends on this search."""
        if self.alarm_service is None:
            return []
        linked = await find_alerts_for_saved_search(self.oci_client, saved_search_id)
        updated = []
        for alert in linked:
            alarm = await self.oci_client.get_alarm(alert.alarm_id)
            stamp = None
            if alert.created_by == "console":
                stamp = {**CONSOLE_STAMP,
                         "logan_backing_saved_search_id": alert.backing_saved_search_id or saved_search_id,
                         "logan_backing_metric_task_id": alert.backing_metric_task_id or ""}
            fields = self.alarm_service.refresh_alarm_for_query(alarm, query, stamp=stamp)
            await self.oci_client.update_alarm(
                alert.alarm_id, oci.monitoring.models.UpdateAlarmDetails(**fields),
            )
            updated.append({"alarm_id": alert.alarm_id, "display_name": alert.display_name,
                            "created_by": alert.created_by})
        return updated

    async def list_searches(self) -> List[Dict[str, Any]]:
        """List all saved searches."""
        cache_key = "saved_searches"
        cached = self.cache.get(cache_key)
        if cached is None:
            searches = [
                *await self.oci_client.list_management_saved_searches(),
                *await self.oci_client.list_saved_searches(),
            ]
            # Exclude Logan-managed backing resources (alert tasks, etc.).
            cached = [s for s in searches
                      if s.get("freeform_tags", {}).get("logan_managed") != "true"]
            self.cache.set(cache_key, cached)
        try:
            snapshot = await dependency_snapshot(self.oci_client)
        except DependencyInspectionError:
            return [{**row, 'usage': 'unknown', 'dependent_rules': None,
                     'dependent_alerts': None, 'dependency_status': 'unknown'} for row in cached]
        return [{**row, **snapshot.markers(row['id'])} for row in cached]

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
        """Store a reusable query without creating a scheduled streaming job."""
        cid = compartment_id or self.oci_client.compartment_id

        mss_details = oci.management_dashboard.models.CreateManagementSavedSearchDetails(
            display_name=display_name,
            compartment_id=cid,
            description=description or "",
            is_oob_saved_search=False,
            type="SEARCH_SHOW_IN_DASHBOARD",
            provider_id=PROVIDER_ID,
            provider_name=PROVIDER_NAME,
            provider_version=PROVIDER_VERSION,
            metadata_version="2.0",
            nls={},
            data_config=[{"query": query}],
            ui_config=build_ui_config(query, "summary_table", build_scope_filters(
                cid, self.oci_client.tenancy_id, self.oci_client._config["region"],
            )),
            screen_image=" ",
            widget_template="visualizations/chartWidgetTemplate.html",
            widget_vm="jet-modules/dashboards/widgets/lxSavedSearchWidget",
            parameters_config=[],
            drilldown_config=[],
            features_config=FEATURES_CONFIG,
        )
        mss = await self.oci_client.create_management_saved_search(mss_details)

        self.cache.delete("saved_searches")
        return {
            "id": mss["id"],
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
        if search_id.startswith("ocid1.managementsavedsearch."):
            mss_update_kwargs = {}
            if "display_name" in kwargs:
                mss_update_kwargs["display_name"] = kwargs["display_name"]
            if "query" in kwargs:
                mss_update_kwargs["data_config"] = [{"query": kwargs["query"]}]
                current = await self.oci_client.get_saved_search(search_id)
                mss_update_kwargs["ui_config"] = {
                    **(current["ui_config"] if "ui_config" in current else {}),
                    "queryString": kwargs["query"],
                }
            mss_update = oci.management_dashboard.models.UpdateManagementSavedSearchDetails(
                **mss_update_kwargs
            )
            await self.oci_client.update_management_saved_search(search_id, mss_update)
            self.cache.delete("saved_searches")
            self.cache.delete(f"saved_search:{search_id}")
            result = {
                "id": search_id,
                "updated": list(kwargs.keys()),
                **{k: v for k, v in kwargs.items()},
            }
            if "query" in kwargs:
                linked = await self._refresh_linked_alerts(search_id, kwargs["query"])
                if linked:
                    result["linked_alerts_updated"] = linked
            return result

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
        result = {"id": search_id, "updated": list(kwargs.keys()), **{k: v for k, v in kwargs.items()}}
        if "query" in kwargs and backing_mss_id:
            linked = await self._refresh_linked_alerts(backing_mss_id, kwargs["query"])
            if linked:
                result["linked_alerts_updated"] = linked
        return result

    async def deletion_dependencies(self, search_id: str, compartment_id: Optional[str] = None) -> Dict:
        """Read current dependencies without relying on the base catalog cache."""
        return await inspect_dependencies(self.oci_client, search_id, compartment_id)

    async def delete_search(self, search_id: str, compartment_id: Optional[str] = None) -> None:
        """Delete only after a fresh dependency check, including after confirmation."""
        report = await self.deletion_dependencies(search_id, compartment_id)
        require_unused(report)
        if search_id.startswith("ocid1.managementsavedsearch."):
            await self.oci_client.delete_management_saved_search(search_id)
            self.cache.delete("saved_searches")
            self.cache.delete(f"saved_search:{search_id}")
            return
        backing_mss_id = report['saved_search_id']
        await self.oci_client.delete_scheduled_task(search_id)
        if backing_mss_id:
            try:
                await self.oci_client.delete_management_saved_search(backing_mss_id)
            except Exception:
                pass
        self.cache.delete("saved_searches")
        self.cache.delete(f"saved_search:{search_id}")
