"""Shared Log Analytics widget and scope settings for saved searches."""


def build_scope_filters(compartment_id: str, tenancy_id: str, region: str = "us-ashburn-1") -> dict:
    """Build the scope filters expected by a Log Analytics widget."""
    region_label = "US East (Ashburn)" if region == "us-ashburn-1" else region
    base = [
        {"type": "LogGroup", "flags": {"IncludeSubCompartments": True},
         "values": [{"value": tenancy_id, "label": "root"}]},
        {"type": "MetricCompartment", "flags": {}, "values": []},
        {"type": "Entity", "flags": {"IncludeDependents": True, "ScopeCompartmentId": compartment_id}, "values": []},
        {"type": "LogSet", "flags": {}, "values": []},
        {"type": "ResourceCompartment", "flags": {"IncludeSubCompartments": True},
         "values": [{"value": tenancy_id, "label": "root"}]},
        {"type": "Region", "flags": {}, "values": [{"value": region, "label": region_label}]},
    ]
    result = {"filters": base, "isGlobal": False}
    for item in base:
        result[item["type"]] = item
    return result


def build_ui_config(query: str, viz_type: str, scope_filters: dict) -> dict:
    """Include the query and required widget metadata in uiConfig."""
    return {
        "timeSelection": {"timePeriod": "l7d"},
        "showTitle": True,
        "visualizationType": viz_type,
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
