"""Unit tests for the shared ManagementSavedSearch payload builder.

These tests pin the payload contract that the OCI API requires. Both
saved_search.create_search and dashboard_service.create_dashboard go through
build_mss_details, so any drift between standalone and tile creation is
caught here.
"""
import pytest

from oci_logan_mcp._mss_payload import (
    build_mss_details,
    build_scope_filters,
    build_ui_config,
)


CID = "ocid1.compartment.test"
TID = "ocid1.tenancy.test"
QUERY = "'Log Source' = 'OCI VCN Flow Unified Schema Logs' | stats count as Flows"


class TestBuildMssDetails:
    def test_query_lands_in_ui_config_querystring(self):
        """The OCI API reads the query from ui_config['queryString']."""
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
        )
        assert d.ui_config["queryString"] == QUERY

    def test_data_config_stays_empty(self):
        """data_config must be []; query lives in ui_config only."""
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
        )
        assert d.data_config == []

    def test_standalone_uses_search_type(self):
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
            is_dashboard_tile=False,
        )
        assert d.type == "SEARCH_SHOW_IN_DASHBOARD"

    def test_dashboard_tile_uses_widget_type(self):
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
            is_dashboard_tile=True,
        )
        assert d.type == "WIDGET_SHOW_IN_DASHBOARD"

    def test_default_visualization_is_summary_table(self):
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
        )
        assert d.ui_config["visualizationType"] == "summary_table"

    def test_custom_visualization_passes_through(self):
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
            visualization_type="line",
        )
        assert d.ui_config["visualizationType"] == "line"

    def test_freeform_tags_default_empty(self):
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
        )
        assert d.freeform_tags == {}

    def test_freeform_tags_pass_through(self):
        tags = {"logan_managed": "true", "logan_kind": "dashboard_saved_search"}
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
            freeform_tags=tags,
        )
        assert d.freeform_tags == tags

    def test_scope_filters_use_tenancy_root(self):
        """Scope filters must reference the tenancy at the root LogGroup level."""
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
        )
        sf = d.ui_config["scopeFilters"]
        log_group = next(f for f in sf["filters"] if f["type"] == "LogGroup")
        assert log_group["values"][0]["value"] == TID

    def test_provider_metadata_pins_log_analytics(self):
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
        )
        assert d.provider_id == "log-analytics"
        assert d.provider_name == "Log Analytics"

    def test_widget_template_and_vm_pinned(self):
        """Pin the template/VM strings — drift breaks rendering silently."""
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
        )
        assert d.widget_template == "visualizations/chartWidgetTemplate.html"
        assert d.widget_vm == "jet-modules/dashboards/widgets/lxSavedSearchWidget"

    def test_description_defaults_to_empty_string(self):
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
        )
        assert d.description == ""

    def test_description_pass_through(self):
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
            description="hello",
        )
        assert d.description == "hello"

    def test_features_config_has_crossservice_shared(self):
        d = build_mss_details(
            display_name="x", query=QUERY, compartment_id=CID, tenancy_id=TID,
        )
        assert d.features_config == {"crossService": {"shared": True}}


class TestBuildUiConfig:
    def test_querystring_set(self):
        sf = build_scope_filters(CID, TID)
        ui = build_ui_config(QUERY, "line", sf)
        assert ui["queryString"] == QUERY

    def test_visualization_type_set(self):
        sf = build_scope_filters(CID, TID)
        ui = build_ui_config(QUERY, "treemap", sf)
        assert ui["visualizationType"] == "treemap"


class TestBuildScopeFilters:
    def test_log_group_uses_tenancy_id(self):
        sf = build_scope_filters(CID, TID)
        lg = next(f for f in sf["filters"] if f["type"] == "LogGroup")
        assert lg["values"][0]["value"] == TID

    def test_entity_uses_compartment_id(self):
        sf = build_scope_filters(CID, TID)
        ent = next(f for f in sf["filters"] if f["type"] == "Entity")
        assert ent["flags"]["ScopeCompartmentId"] == CID

    def test_filters_are_indexed_by_type(self):
        """The structure also has top-level keys per filter type for fast lookup."""
        sf = build_scope_filters(CID, TID)
        assert "LogGroup" in sf
        assert "Entity" in sf
