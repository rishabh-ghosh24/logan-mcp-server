"""OCI Management Dashboard CRUD service."""
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

import oci

from .client import OCILogAnalyticsClient
from .cache import CacheManager

logger = logging.getLogger(__name__)

VALID_VIZ_TYPES = {
    "bar", "vertical_bar", "line", "pie", "table",
    "tile", "area", "treemap", "heatmap", "histogram",
}

# OCI Management Dashboard API requires crossService in featuresConfig
FEATURES_CONFIG = {"crossService": {"shared": True}, "dependencies": []}

VIZ_TYPE_MAP = {
    "bar": "bar",
    "vertical_bar": "vertical_bar",
    "line": "line",
    "pie": "donut",
    "table": "table",
    "tile": "tile",
    "area": "area",
    "treemap": "treemap",
    "heatmap": "heatmap",
    "histogram": "histogram",
}


class DashboardService:
    def __init__(self, oci_client: OCILogAnalyticsClient, cache: CacheManager):
        self.oci_client = oci_client
        self.cache = cache

    def _compute_tile_positions(self, count: int) -> List[Dict[str, int]]:
        positions = []
        for i in range(count):
            positions.append({"row": i, "column": 0, "height": 4, "width": 12})
        return positions

    async def create_dashboard(
        self,
        display_name: str,
        tiles: List[Dict[str, Any]],
        description: Optional[str] = None,
        compartment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not tiles:
            raise ValueError("At least one tile is required to create a dashboard.")
        for tile in tiles:
            if tile.get("visualization_type") not in VALID_VIZ_TYPES:
                raise ValueError(
                    f"Invalid visualization type '{tile.get('visualization_type')}'. "
                    f"Supported: {', '.join(sorted(VALID_VIZ_TYPES))}"
                )

        cid = compartment_id or self.oci_client.compartment_id
        group_id = str(uuid4())
        base_tags = {"logan_managed": "true", "logan_group_id": group_id,
                     "logan_kind": "dashboard_saved_search"}
        positions = self._compute_tile_positions(len(tiles))
        created_search_ids = []

        try:
            for i, tile in enumerate(tiles):
                mss_details = oci.management_dashboard.models.CreateManagementSavedSearchDetails(
                    display_name=tile["title"],
                    description=tile.get("description", tile["title"]),
                    compartment_id=cid,
                    type="WIDGET_SHOW_IN_DASHBOARD",
                    provider_id="log-analytics",
                    provider_name="Logging Analytics",
                    provider_version="3.0.0",
                    metadata_version="2.0",
                    nls={},
                    data_config=[{"query": tile["query"]}],
                    ui_config={"visualizationType": VIZ_TYPE_MAP.get(
                        tile["visualization_type"], tile["visualization_type"]
                    )},
                    screen_image="to-do",
                    widget_template="visualizations/chartWidgetTemplate.html",
                    widget_vm="visualizations/chartWidget",
                    parameters_config=[],
                    drilldown_config=[],
                    features_config=FEATURES_CONFIG,
                    freeform_tags=base_tags,
                )
                mss = await self.oci_client.create_management_saved_search(mss_details)
                created_search_ids.append(mss["id"])

            tile_details = []
            for i, (tile, search_id, pos) in enumerate(zip(tiles, created_search_ids, positions)):
                tile_details.append(
                    oci.management_dashboard.models.ManagementDashboardTileDetails(
                        display_name=tile["title"],
                        saved_search_id=search_id,
                        row=pos["row"],
                        column=pos["column"],
                        height=tile.get("height", pos["height"]),
                        width=tile.get("width", pos["width"]),
                        nls={},
                        data_config=[],
                        state="DEFAULT",
                        drilldown_config=[],
                        parameters_map={},
                    )
                )

            dash_details = oci.management_dashboard.models.CreateManagementDashboardDetails(
                display_name=display_name,
                description=description or "",
                compartment_id=cid,
                is_oob_dashboard=False,
                provider_id="log-analytics",
                provider_name="Logging Analytics",
                provider_version="3.0.0",
                metadata_version="2.0",
                tiles=tile_details,
                nls={},
                parameters_config=[],
                drilldown_config=[],
                is_show_in_home=False,
                features_config=FEATURES_CONFIG,
            )
            dashboard = await self.oci_client.create_management_dashboard(dash_details)

        except Exception:
            for search_id in created_search_ids:
                try:
                    await self.oci_client.delete_management_saved_search(search_id)
                except Exception:
                    pass
            raise

        return {
            "dashboard_id": dashboard["id"],
            "display_name": dashboard.get("display_name", display_name),
            "tile_saved_search_ids": created_search_ids,
            "dashboard_group": group_id,
        }

    async def list_dashboards(self, compartment_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self.oci_client.list_management_dashboards(compartment_id)

    async def add_tile(
        self,
        dashboard_id: str,
        title: str,
        query: str,
        visualization_type: str,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Dict[str, Any]:
        if visualization_type not in VALID_VIZ_TYPES:
            raise ValueError(
                f"Invalid visualization type '{visualization_type}'. "
                f"Supported: {', '.join(sorted(VALID_VIZ_TYPES))}"
            )

        dashboard = await self.oci_client.get_management_dashboard(dashboard_id)
        etag = dashboard.get("_etag")
        existing_tiles = dashboard.get("tiles", [])

        if len(existing_tiles) >= 20:
            raise ValueError("Dashboard already has 20 tiles (maximum). Remove a tile before adding.")

        cid = dashboard.get("compartment_id") or self.oci_client.compartment_id

        new_search_id = None
        try:
            mss_details = oci.management_dashboard.models.CreateManagementSavedSearchDetails(
                display_name=title,
                description=title,
                compartment_id=cid,
                type="WIDGET_SHOW_IN_DASHBOARD",
                provider_id="log-analytics",
                provider_name="Logging Analytics",
                provider_version="3.0.0",
                metadata_version="2.0",
                nls={},
                data_config=[{"query": query}],
                ui_config={"visualizationType": VIZ_TYPE_MAP.get(visualization_type, visualization_type)},
                screen_image="to-do",
                widget_template="visualizations/chartWidgetTemplate.html",
                widget_vm="visualizations/chartWidget",
                parameters_config=[],
                drilldown_config=[],
                features_config=FEATURES_CONFIG,
                freeform_tags={"logan_managed": "true",
                               "logan_kind": "dashboard_saved_search",
                               "logan_dashboard_id": dashboard_id},
            )
            mss = await self.oci_client.create_management_saved_search(mss_details)
            new_search_id = mss["id"]

            if existing_tiles:
                next_row = max(t.get("row", 0) + t.get("height", 4) for t in existing_tiles)
            else:
                next_row = 0
            new_tile = oci.management_dashboard.models.ManagementDashboardTileDetails(
                display_name=title,
                saved_search_id=new_search_id,
                row=next_row,
                column=0,
                height=height or 4,
                width=width or 12,
                nls={},
                data_config=[],
                state="DEFAULT",
                drilldown_config=[],
                parameters_map={},
            )

            all_tiles = []
            for t in existing_tiles:
                all_tiles.append(oci.management_dashboard.models.ManagementDashboardTileDetails(
                    display_name=t.get("display_name", ""),
                    saved_search_id=t.get("saved_search_id", ""),
                    row=t.get("row", 0),
                    column=t.get("column", 0),
                    height=t.get("height", 4),
                    width=t.get("width", 12),
                    nls={},
                    data_config=[],
                    state="DEFAULT",
                    drilldown_config=[],
                    parameters_map={},
                ))
            all_tiles.append(new_tile)

            update_details = oci.management_dashboard.models.UpdateManagementDashboardDetails(
                display_name=dashboard.get("display_name", ""),
                description=dashboard.get("description", ""),
                tiles=all_tiles,
                is_oob_dashboard=False,
                provider_id="log-analytics",
                provider_name="Logging Analytics",
                provider_version="3.0.0",
                metadata_version="2.0",
                nls={},
                parameters_config=[],
                drilldown_config=[],
                features_config=FEATURES_CONFIG,
            )
            await self.oci_client.update_management_dashboard(
                dashboard_id, update_details, if_match=etag
            )

        except Exception:
            if new_search_id:
                try:
                    await self.oci_client.delete_management_saved_search(new_search_id)
                except Exception:
                    pass
            raise

        return {"dashboard_id": dashboard_id, "saved_search_id": new_search_id, "title": title}

    async def delete_dashboard(self, dashboard_id: str) -> Dict[str, Any]:
        dashboard = await self.oci_client.get_management_dashboard(dashboard_id)
        tile_search_ids = [
            t["saved_search_id"]
            for t in dashboard.get("tiles", [])
            if t.get("saved_search_id")
        ]

        deleted = []
        remaining = []

        for search_id in tile_search_ids:
            try:
                await self.oci_client.delete_management_saved_search(search_id)
                deleted.append(search_id)
            except oci.exceptions.ServiceError as e:
                if e.status == 404:
                    deleted.append(search_id)
                else:
                    remaining.append(search_id)

        try:
            await self.oci_client.delete_management_dashboard(dashboard_id)
            deleted.append(dashboard_id)
        except oci.exceptions.ServiceError as e:
            if e.status != 404:
                remaining.append(dashboard_id)

        return {
            "deleted": deleted,
            "remaining": remaining,
            "partial_failure": len(remaining) > 0,
        }
