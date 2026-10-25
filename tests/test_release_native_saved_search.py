"""Native Management Saved Search IDs work alongside legacy task IDs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
import pytest
from oci_logan_mcp.saved_search import SavedSearchService
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.cache import CacheManager

NATIVE='ocid1.managementsavedsearch.qa'

@pytest.mark.asyncio
async def test_listing_includes_native_searches_and_legacy_tasks():
    client=SimpleNamespace(list_saved_searches=AsyncMock(return_value=[{'id':'legacy'}]),
        list_management_saved_searches=AsyncMock(return_value=[{'id':NATIVE}]))
    result=await SavedSearchService(client,CacheManager()).list_searches()
    assert {x['id'] for x in result}=={'legacy',NATIVE}

@pytest.mark.asyncio
async def test_native_delete_never_addresses_scheduled_tasks():
    client=SimpleNamespace(delete_management_saved_search=AsyncMock(),delete_scheduled_task=AsyncMock(),get_saved_search=AsyncMock(return_value={}))
    client.namespace='testns'
    client.compartment_id='ocid1.compartment.test'
    client.list_scheduled_tasks_for_saved_search=AsyncMock(return_value=[])
    await SavedSearchService(client,CacheManager()).delete_search(NATIVE)
    client.delete_management_saved_search.assert_awaited_once_with(NATIVE)
    client.delete_scheduled_task.assert_not_called()

@pytest.mark.asyncio
async def test_native_update_keeps_widget_options_and_updates_both_query_locations():
    client=SimpleNamespace(get_saved_search=AsyncMock(return_value={'ui_config':{'visualizationType':'bar','queryString':'old'}}),
        update_management_saved_search=AsyncMock())
    result=await SavedSearchService(client,CacheManager()).update_search(NATIVE,query='* | stats count')
    assert result['id']==NATIVE
    details=client.update_management_saved_search.call_args.args[1]
    assert details.data_config==[{'query':'* | stats count'}]
    assert details.ui_config=={'visualizationType':'bar','queryString':'* | stats count'}

@pytest.mark.asyncio
async def test_native_client_reads_the_stored_query_without_task_api():
    client=object.__new__(OCILogAnalyticsClient)
    client._rate_limiter=SimpleNamespace(acquire=AsyncMock(),reset=lambda:None)
    native=SimpleNamespace(id=NATIVE,display_name='qa',lifecycle_state='ACTIVE',data_config=[{'query':'* | stats count'}],ui_config={'queryString':'* | stats count'})
    client._dashx_client=SimpleNamespace(get_management_saved_search=MagicMock(return_value=SimpleNamespace(data=native)))
    result=await client.get_saved_search(NATIVE)
    assert result['id']==NATIVE and result['query']=='* | stats count'
    assert result['ui_config']==native.ui_config
