"""Verify the query and required UI configuration on real OCI model requests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from oci_logan_mcp.alarm_service import AlarmService
from oci_logan_mcp.saved_search import SavedSearchService
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import CacheConfig

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['saved_search','alert'])
async def test_created_saved_search_keeps_query_and_valid_ui(kind):
    client=SimpleNamespace(compartment_id='ocid1.compartment.qa',tenancy_id='ocid1.tenancy.qa',
        _config={'region':'uk-london-1'},get_topic=AsyncMock(),
        create_management_saved_search=AsyncMock(return_value={'id':'ocid1.managementsavedsearch.qa'}),
        create_scheduled_task=AsyncMock(return_value={'id':'ocid1.scheduledtask.qa'}),
        create_alarm=AsyncMock(return_value={'id':'ocid1.alarm.qa'}))
    cache=CacheManager(CacheConfig())
    query="'Log Source' = 'QA' | stats count"
    if kind=='saved_search':
        result=await SavedSearchService(client,cache).create_search(display_name='QA',query=query)
        assert result['management_saved_search_id']=='ocid1.managementsavedsearch.qa'
    else:
        result=await AlarmService(client,cache).create_alert(display_name='QA',query=query,destination_topic_id='ocid1.onstopic.qa')
        assert result['backing_saved_search_id']=='ocid1.managementsavedsearch.qa'
    client.create_management_saved_search.assert_awaited_once()
    details=client.create_management_saved_search.call_args.args[0]
    assert details.data_config==[{'query':query}]
    assert details.ui_config['queryString']==query
    assert details.ui_config['vizType']=='lxSavedSearchWidgetType'
    assert details.ui_config['visualizationType']=='summary_table'
    scope=details.ui_config['scopeFilters']
    assert scope['Region']['values']==[{'value':'uk-london-1','label':'uk-london-1'}]
    assert scope['Entity']['flags']['ScopeCompartmentId']=='ocid1.compartment.qa'
