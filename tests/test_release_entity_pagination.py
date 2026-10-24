"""Entity discovery must keep complete results without blocking MCP dispatch."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from oci_logan_mcp.client import OCILogAnalyticsClient


@pytest.mark.asyncio
async def test_entity_pages_are_bounded_and_fetched_off_the_event_loop(monkeypatch):
    client=object.__new__(OCILogAnalyticsClient)
    client._service_lock=threading.RLock()
    client._la_client=SimpleNamespace(list_log_analytics_entities=lambda:None)
    client._namespace='qa'
    client._compartment_id='ocid1.compartment.qa'
    client._rate_limiter=SimpleNamespace(acquire=AsyncMock(),reset=lambda:None)
    caller=threading.get_ident()
    observed={}
    entities=[SimpleNamespace(name='entity-'+str(i),entity_type_name='Host',management_agent_id=None,lifecycle_state='ACTIVE') for i in range(201)]
    def paginate(operation,**kwargs):
        observed.update(thread=threading.get_ident(),kwargs=kwargs)
        return SimpleNamespace(data=entities)
    monkeypatch.setattr('oci_logan_mcp.client.list_call_get_all_results',paginate)
    result=await client.list_entities(entity_type='Host')
    assert len(result)==201 and result[-1]['name']=='entity-200'
    assert observed['thread']!=caller
    assert observed['kwargs']=={'namespace_name':'qa','compartment_id':'ocid1.compartment.qa','entity_type_name':['Host'],'limit':100}
