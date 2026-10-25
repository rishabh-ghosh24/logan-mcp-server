"""Saved query reads and alert edits retain the query actually used by OCI."""
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock
import pytest
from oci_logan_mcp.alarm_service import AlarmService
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.cache import CacheManager

@pytest.mark.asyncio
async def test_legacy_task_query_is_read_from_its_native_backing_search():
    client=object.__new__(OCILogAnalyticsClient)
    client._service_lock=threading.RLock();client._namespace='qa'
    client._rate_limiter=SimpleNamespace(acquire=AsyncMock(),reset=lambda:None)
    task=SimpleNamespace(id='task-id',display_name='qa',lifecycle_state='ACTIVE',action=SimpleNamespace(saved_search_id='ocid1.managementsavedsearch.qa'))
    native=SimpleNamespace(id='ocid1.managementsavedsearch.qa',display_name='qa',lifecycle_state='ACTIVE',ui_config={'queryString':'* | stats count'},data_config=[])
    client._la_client=SimpleNamespace(get_scheduled_task=MagicMock(return_value=SimpleNamespace(data=task)))
    client._dashx_client=SimpleNamespace(get_management_saved_search=MagicMock(return_value=SimpleNamespace(data=native)))
    result=await client.get_saved_search('task-id')
    assert result['query']=='* | stats count' and result['_action'] is task.action

@pytest.mark.asyncio
async def test_alert_query_update_keeps_widget_options_and_updates_ui_query():
    client=SimpleNamespace(get_alarm=AsyncMock(return_value={'freeform_tags':{'logan_managed':'true','logan_kind':'monitoring_alarm','logan_backing_saved_search_id':'ocid1.managementsavedsearch.qa'},'query':'metric[1m].count() > 0'}),
        get_saved_search=AsyncMock(return_value={'ui_config':{'queryString':'old','visualizationType':'tile'}}),
        update_management_saved_search=AsyncMock(),update_alarm=AsyncMock(return_value={'id':'alarm-id'}))
    result=await AlarmService(client,CacheManager()).update_alert('alarm-id',query='* | stats count')
    details=client.update_management_saved_search.call_args.args[1]
    assert details.data_config==[{'query':'* | stats count'}]
    assert details.ui_config=={'queryString':'* | stats count','visualizationType':'tile'}
    assert result['alarm_id']=='alarm-id'
