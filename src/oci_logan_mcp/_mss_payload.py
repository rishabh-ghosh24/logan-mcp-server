"""Shared payload builder for OCI ManagementSavedSearch (MSS) resources.

Two code paths in this MCP server create MSSes against the OCI Management
Dashboard API:

  - ``saved_search.create_search``        — standalone, user-visible saved search.
  - ``dashboard_service.create_dashboard`` — per-tile backing MSS, hidden by the
                                             ``logan_managed`` tag.

They share the same OCI API call (``create_management_saved_search``) and the
same payload shape, but for a long time each maintained its own inline payload
construction. The dashboard path stayed correct; the standalone path rotted —
shipping with no ``ui_config`` and hitting ``Invalid uiConfig`` from OCI.

This module is the single source of truth for the payload. Both call sites must
go through ``build_mss_details``. If OCI's contract changes, the fix lands here.
"""
from typing import Any, Dict, Optional

import oci

# Matches the working pg_dashboard_builder.py payload.
FEATURES_CONFIG: Dict[str, Any] = {"crossService": {"shared": True}}

# Saved-search parameters config — Log Analytics-specific scope filter tiles.
# Sourced from oracle-quickstart/oci-o11y-solutions IAM Domain Audit dashboard.
SS_PARAMS_CONFIG = [
    {
        "name": "log-analytics-log-group-compartment",
        "displayName": "Log Group Compartment",
        "required": True,
        "defaultFilterIds": ["OOBSS-management-dashboard-filter-4a"],
        "editUi": {
            "inputType": "savedSearch",
            "filterTile": {"filterId": "OOBSS-management-dashboard-filter-4a"},
        },
        "valueFormat": {"type": "object"},
    },
    {
        "name": "log-analytics-entity",
        "displayName": "Entity",
        "required": True,
        "defaultFilterIds": ["OOBSS-management-dashboard-filter-2a"],
        "editUi": {
            "inputType": "savedSearch",
            "filterTile": {"filterId": "OOBSS-management-dashboard-filter-2a"},
        },
        "valueFormat": {"type": "object"},
    },
    {
        "name": "log-analytics-region",
        "displayName": "Region",
        "required": False,
        "defaultFilterIds": ["OOBSS-management-dashboard-region-filter"],
        "editUi": {
            "inputType": "savedSearch",
            "filterTile": {"filterId": "OOBSS-management-dashboard-region-filter"},
        },
        "valueFormat": {"type": "array"},
    },
    {"name": "time", "displayName": "$(bundle.globalSavedSearch.TIME)", "required": True, "hidden": True},
]


def build_scope_filters(
    compartment_id: str, tenancy_id: str, region: str = "us-ashburn-1"
) -> Dict[str, Any]:
    """Build the scopeFilters block. Mirrors the working dashboard path."""
    region_label = "US East (Ashburn)" if region == "us-ashburn-1" else region
    base = [
        {"type": "LogGroup", "flags": {"IncludeSubCompartments": True},
         "values": [{"value": tenancy_id, "label": "root"}]},
        {"type": "MetricCompartment", "flags": {}, "values": []},
        {"type": "Entity", "flags": {"IncludeDependents": True, "ScopeCompartmentId": compartment_id},
         "values": []},
        {"type": "LogSet", "flags": {}, "values": []},
        {"type": "ResourceCompartment", "flags": {"IncludeSubCompartments": True},
         "values": [{"value": tenancy_id, "label": "root"}]},
        {"type": "Region", "flags": {}, "values": [{"value": region, "label": region_label}]},
    ]
    result: Dict[str, Any] = {"filters": base, "isGlobal": False}
    for f in base:
        result[f["type"]] = f
    return result


def build_ui_config(
    query: str, visualization_type: str, scope_filters: Dict[str, Any]
) -> Dict[str, Any]:
    """Build the ui_config block. ``queryString`` is where OCI reads the query."""
    return {
        "timeSelection": {"timePeriod": "l7d"},
        "showTitle": True,
        "visualizationType": visualization_type,
        "visualizationOptions": {
            "customVizOpt": {
                "primaryFieldIname": "mbody",
                "primaryFieldDname": "Original Log Content",
            }
        },
        "queryString": query,
        "scopeFilters": scope_filters,
        "vizType": "lxSavedSearchWidgetType",
        "enableWidgetInApp": True,
    }


def build_mss_details(
    *,
    display_name: str,
    query: str,
    compartment_id: str,
    tenancy_id: str,
    region: str = "us-ashburn-1",
    visualization_type: str = "summary_table",
    is_dashboard_tile: bool = False,
    description: Optional[str] = None,
    freeform_tags: Optional[Dict[str, str]] = None,
) -> "oci.management_dashboard.models.CreateManagementSavedSearchDetails":
    """Build a CreateManagementSavedSearchDetails payload.

    The OCI API requires ``ui_config['queryString']`` to be set. ``data_config``
    stays empty — the working dashboard path uses ``[]`` and OCI reads the query
    from ``ui_config``.

    Args:
        display_name: User-visible name.
        query: The OCI LA query string. Goes into ``ui_config['queryString']``.
        compartment_id: Compartment OCID for the MSS.
        tenancy_id: Tenancy OCID (used in scope filters).
        region: OCI region for the scope filters.
        visualization_type: OCI internal viz name (e.g. ``summary_table``,
            ``line``, ``bar``, ``tile``).
        is_dashboard_tile: If True, payload type is ``WIDGET_SHOW_IN_DASHBOARD``
            (hidden backing resource). If False, ``SEARCH_SHOW_IN_DASHBOARD``
            (user-visible standalone).
        description: Optional description; defaults to empty string.
        freeform_tags: Optional tags. Dashboard tiles set ``logan_managed=true``
            here so ``list_saved_searches`` can filter them out.
    """
    scope_filters = build_scope_filters(compartment_id, tenancy_id, region)
    mss_type = "WIDGET_SHOW_IN_DASHBOARD" if is_dashboard_tile else "SEARCH_SHOW_IN_DASHBOARD"
    return oci.management_dashboard.models.CreateManagementSavedSearchDetails(
        display_name=display_name,
        compartment_id=compartment_id,
        description=description or "",
        is_oob_saved_search=False,
        type=mss_type,
        provider_id="log-analytics",
        provider_name="Log Analytics",
        provider_version="3.0.0",
        metadata_version="2.0",
        nls={},
        data_config=[],
        ui_config=build_ui_config(query, visualization_type, scope_filters),
        screen_image=" ",
        widget_template="visualizations/chartWidgetTemplate.html",
        widget_vm="jet-modules/dashboards/widgets/lxSavedSearchWidget",
        parameters_config=SS_PARAMS_CONFIG,
        drilldown_config=[],
        features_config=FEATURES_CONFIG,
        freeform_tags=freeform_tags or {},
    )
