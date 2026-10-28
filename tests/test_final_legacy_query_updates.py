"""A legacy task ID must update the query consumers actually read and execute."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.config import Settings
from oci_logan_mcp.query_engine import QueryEngine
from oci_logan_mcp.saved_search import SavedSearchService

NATIVE = "ocid1.managementsavedsearch.oc1..synthetic"
LEGACY = "ocid1.loganalyticsscheduledtask.oc1..synthetic"
OLD = "* | stats count as old"
NEW = "* | stats count as revised"


@pytest.mark.asyncio
async def test_legacy_update_changes_both_readbacks_and_execution_after_warm_caches():
    # Updating only data_config, or evicting only the task alias, leaves the old query visible.
    settings = Settings()
    settings.log_analytics.namespace = "example"
    settings.log_analytics.default_compartment_id = "ocid1.compartment.example"
    with patch("oci_logan_mcp.client.get_signer", return_value=({"region": "us-ashburn-1"}, object())):
        client = OCILogAnalyticsClient(settings)
    state = SimpleNamespace(id=NATIVE, display_name="Synthetic", compartment_id=client.compartment_id,
                            lifecycle_state="ACTIVE", data_config=[{"query": OLD}],
                            ui_config={"queryString": OLD, "visualizationType": "bar"})
    executed = []

    def read_search(**kwargs):
        assert kwargs["management_saved_search_id"] == NATIVE
        return SimpleNamespace(data=state)

    def update_search(**kwargs):
        assert kwargs["management_saved_search_id"] == NATIVE
        details = kwargs["update_management_saved_search_details"]
        if details.data_config is not None:
            state.data_config = details.data_config
        if details.ui_config is not None:
            state.ui_config = details.ui_config
        return SimpleNamespace(data=state)

    def query(**kwargs):
        executed.append(kwargs["query_details"].query_string)
        column = SimpleNamespace(display_name="count", internal_name="count", value_type="INTEGER")
        return SimpleNamespace(data=SimpleNamespace(columns=[column], items=[{"count": 1}],
                               total_count=1, is_partial_result=False), has_next_page=False)

    client._dashx_client = SimpleNamespace(get_management_saved_search=read_search,
                                         update_management_saved_search=update_search)
    client._la_client = SimpleNamespace(query=query, get_scheduled_task=lambda **kwargs: SimpleNamespace(
        data=SimpleNamespace(id=LEGACY, display_name="Synthetic task", lifecycle_state="ACTIVE",
                             action=SimpleNamespace(saved_search_id=NATIVE), saved_search_query="")))
    cache = CacheManager()
    service = SavedSearchService(client, cache)
    assert (await service.get_search_by_id(NATIVE))["query"] == OLD
    assert (await service.get_search_by_id(LEGACY))["query"] == OLD

    result = await service.update_search(LEGACY, query=NEW)

    assert state.data_config == [{"query": NEW}]
    assert state.ui_config == {"queryString": NEW, "visualizationType": "bar"}
    assert result["id"] == LEGACY
    for identifier in (NATIVE, LEGACY):
        saved = await service.get_search_by_id(identifier)
        assert saved["query"] == NEW
        response = await QueryEngine(client, cache, MagicMock()).execute(saved["query"], use_cache=False)
        assert response["data"]["rows"] == [[1]]
    assert executed == [NEW, NEW]
