"""OCI Management Dashboard CRUD service."""
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

import oci

from .client import OCILogAnalyticsClient
from .cache import CacheManager
from ._mss_payload import build_mss_details, FEATURES_CONFIG

logger = logging.getLogger(__name__)

VALID_VIZ_TYPES = {
    "bar", "vertical_bar", "line", "pie", "table",
    "tile", "area", "treemap", "heatmap", "histogram",
}

VIZ_TYPE_MAP = {
    "bar": "bar",
    "vertical_bar": "vertical_bar",
    "line": "line",
    "pie": "pie",
    "table": "summary_table",
    "tile": "tile",
    "area": "area",
    "treemap": "treemap",
    "heatmap": "heatmap",
    "histogram": "records_histogram",
}

# Dashboard-level parameters config — filter bar with LA scope tiles
# (from oracle-quickstart/oci-o11y-solutions IAM Domain Audit dashboard)
DASH_PARAMS_CONFIG = [
    {"name": "log-analytics-loggroup-filter", "displayName": "Log Group Compartment",
     "savedSearchId": "OOBSS-management-dashboard-filter-4a", "state": "DEFAULT", "width": 4,
     "localStorageKey": "log-analytics-loggroup-filter"},
    {"name": "log-analytics-entity-filter", "displayName": "Entity",
     "savedSearchId": "OOBSS-management-dashboard-filter-2a", "state": "DEFAULT", "width": 6,
     "localStorageKey": "log-analytics-entity-filter"},
    {"name": "regionFilter", "displayName": "Region",
     "savedSearchId": "OOBSS-management-dashboard-region-filter", "state": "DEFAULT", "width": 2,
     "localStorageKey": "regionFilter"},
    {"name": "time", "src": "$(context.time)"},
]

# Tile-level parameter mapping — wires dashboard filters to widget inputs
TILE_PARAMS_MAP = {
    "time": "$(dashboard.params.time)",
    "log-analytics-log-group-compartment": "$(dashboard.params.log-analytics-loggroup-filter)",
    "log-analytics-entity": "$(dashboard.params.log-analytics-entity-filter)",
    "log-analytics-region": "$(dashboard.params.regionFilter)",
}


class DashboardService:
    def __init__(self, oci_client: OCILogAnalyticsClient, cache: CacheManager):
        self.oci_client = oci_client
        self.cache = cache

    def _compute_tile_positions(self, tiles: List[Dict[str, Any]]) -> List[Dict[str, int]]:
        """Compute grid positions for tiles in a 12-column layout.

        Arranges tiles left-to-right, top-to-bottom. When a tile doesn't
        fit in the remaining columns of the current row, it moves to the
        next row.
        """
        positions = []
        current_row = 0
        current_col = 0
        row_height = 0
        for tile in tiles:
            w = tile.get("width", 12)
            h = tile.get("height", 4)
            # If tile doesn't fit in remaining space, move to next row
            if current_col + w > 12:
                current_row += row_height
                current_col = 0
                row_height = 0
            positions.append({"row": current_row, "column": current_col, "height": h, "width": w})
            current_col += w
            row_height = max(row_height, h)
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
        tenancy_id = self.oci_client.tenancy_id

        group_id = str(uuid4())
        base_tags = {"logan_managed": "true", "logan_group_id": group_id,
                     "logan_kind": "dashboard_saved_search"}
        positions = self._compute_tile_positions(tiles)
        created_search_ids = []

        try:
            for i, tile in enumerate(tiles):
                viz = VIZ_TYPE_MAP.get(tile["visualization_type"], tile["visualization_type"])
                mss_details = build_mss_details(
                    display_name=tile["title"],
                    query=tile["query"],
                    compartment_id=cid,
                    tenancy_id=tenancy_id,
                    visualization_type=viz,
                    is_dashboard_tile=True,
                    description=tile.get("description", tile["title"]),
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
                        ui_config={},
                        data_config=[],
                        state="DEFAULT",
                        drilldown_config=[],
                        parameters_map=TILE_PARAMS_MAP,
                    )
                )

            dash_details = oci.management_dashboard.models.CreateManagementDashboardDetails(
                display_name=display_name,
                description=description or display_name,
                compartment_id=cid,
                is_oob_dashboard=False,
                is_show_in_home=False,
                is_favorite=False,
                is_show_description=False,
                type="NORMAL",
                provider_id="log-analytics",
                provider_name="Log Analytics",
                provider_version="3.0.0",
                metadata_version="2.0",
                tiles=tile_details,
                nls={},
                ui_config={"isFilteringEnabled": True, "isRefreshEnabled": True,
                           "isTimeFilterEnabled": True, "timeSelection": {"timePeriod": "l7d"}},
                data_config=[],
                screen_image=" ",
                parameters_config=DASH_PARAMS_CONFIG,
                drilldown_config=[],
                features_config=FEATURES_CONFIG,
                freeform_tags={"logan_managed": "true", "logan_group_id": group_id,
                               "logan_kind": "dashboard"},
                defined_tags={},
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
        tenancy_id = self.oci_client.tenancy_id
        viz = VIZ_TYPE_MAP.get(visualization_type, visualization_type)

        new_search_id = None
        try:
            mss_details = build_mss_details(
                display_name=title,
                query=query,
                compartment_id=cid,
                tenancy_id=tenancy_id,
                visualization_type=viz,
                is_dashboard_tile=True,
                description=title,
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
                ui_config={},
                data_config=[],
                state="DEFAULT",
                drilldown_config=[],
                parameters_map=TILE_PARAMS_MAP,
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
                    ui_config={},
                    data_config=[],
                    state="DEFAULT",
                    drilldown_config=[],
                    parameters_map=TILE_PARAMS_MAP,
                ))
            all_tiles.append(new_tile)

            update_details = oci.management_dashboard.models.UpdateManagementDashboardDetails(
                display_name=dashboard.get("display_name", ""),
                description=dashboard.get("description", ""),
                tiles=all_tiles,
                is_oob_dashboard=False,
                provider_id="log-analytics",
                provider_name="Log Analytics",
                provider_version="3.0.0",
                metadata_version="2.0",
                nls={},
                parameters_config=DASH_PARAMS_CONFIG,
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
