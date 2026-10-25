"""Run the public metrics surface over actual MCP stdio outside the repository."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from tests.test_protocol_smoke import _params, BAD_CONFIG, GOOD_CONFIG

METRICS_CONFIG=('oci:\n  config_path: /fake/oci/config\n  auth_type: config_file\n'
    'monitoring:\n  default_compartment_id: compartment-A\n  region: us-phoenix-1\n')


@asynccontextmanager
async def metrics_session(tmp_path,*,mode='success',combined=False,auth=True,la_fail=False,budget=100):
    config=(GOOD_CONFIG if combined else METRICS_CONFIG) if auth else BAD_CONFIG
    config+=f'budget:\n  max_queries_per_session: {budget}\n'
    params=_params(tmp_path,config,auth)
    barrier=tmp_path/'metrics-barrier'
    barrier.mkdir()
    params.env['LOGAN_FAKE_METRICS_DIR']=str(barrier)
    params.env['LOGAN_FAKE_METRICS_MODE']=mode
    params.env['LOGAN_FAKE_LA_FAIL']='1' if la_fail else '0'
    with (tmp_path/'metrics-server.stderr').open('w') as errors:
        async with asyncio.timeout(25):
            async with stdio_client(params,errlog=errors) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    yield session,barrier


async def metric_call(session,name='query_metrics',arguments=None):
    response=await session.call_tool(name,arguments or {'namespace':'oci_computeagent',
        'query':'CpuUtilization[5m].mean()','time_range':'last_1_hour'})
    assert response.content and response.content[0].type=='text'
    return json.loads(response.content[0].text),response.isError


def invocations(barrier):
    return [json.loads(line) for line in (barrier/'requests.jsonl').read_text().splitlines()]


@pytest.mark.asyncio
@pytest.mark.parametrize('combined,la_fail',[(False,False),(False,True),(True,False)])
async def test_stdio_metrics_work_without_la_and_return_observed_sdk_data(tmp_path,combined,la_fail):
    async with metrics_session(tmp_path,combined=combined,la_fail=la_fail) as (session,barrier):
        assert len((await session.list_tools()).tools)==69
        namespaces,error=await metric_call(session,'list_metric_namespaces',{'limit':1})
        assert not error and namespaces['namespaces']==['oci_computeagent'] and namespaces['more_available'] is True
        definitions,error=await metric_call(session,'list_metrics',{'namespace':'oci_computeagent','limit':2})
        assert not error and definitions['metrics'][0]['name']=='CpuUtilization'
        catalog,error=await metric_call(session,'search_metric_catalog',{'query':'cpu'})
        assert not error and catalog['matches'][0]['metric_key']=='cpu'
        for name,args in [('query_metrics',None),('query_catalog_metric',{'metric_key':'cpu','time_range':'last_1_hour'})]:
            payload,error=await metric_call(session,name,args)
            assert not error and payload['status']=='success' and payload['returned_datapoint_count']==2
            assert payload['series'][0]['points'][0][1]==0 and payload['series'][0]['points'][1][1]==73.5
            assert payload['series'][0]['expression_index']==0
            assert payload['expressions'][0]['query']=='CpuUtilization[5m].mean()'
            assert 'query' not in payload['series'][0] and 'request_id' not in payload['series'][0]
        calls=invocations(barrier)
        assert [entry['operation'] for entry in calls]==['list','list','query','query']
        assert all(entry['namespace']=='oci_computeagent' for entry in calls[1:])
        if combined:
            logs,error=await metric_call(session,'run_query',{'query':'* | stats count','time_range':'last_15_min'})
            assert not error and logs['data']['rows']==[[42,'smoke']]


@pytest.mark.asyncio
async def test_stdio_all_resources_and_offline_monitoring_help(tmp_path):
    online=tmp_path/'online';online.mkdir()
    async with metrics_session(online,combined=True) as (session,barrier):
        resources=(await session.list_resources()).resources
        assert len(resources)==8
        contents={}
        for resource in resources:
            response=await session.read_resource(resource.uri)
            assert response.contents and response.contents[0].text
            contents[str(resource.uri)]=response.contents[0].text
        assert 'MQL' in contents['monitoring://syntax-guide']
        assert json.loads(contents['monitoring://query-templates'])['query_language']=='mql'
        assert 'stats' in contents['loganalytics://syntax-guide']
        digest=json.loads(contents['loganalytics://schema'])
        assert digest['scope']['window']=='last_24_hours' and 'log_sources' not in digest
    offline=tmp_path/'offline';offline.mkdir()
    async with metrics_session(offline,auth=False) as (session,barrier):
        for name,args in [('query_metrics',None),('search_metric_catalog',{'query':'cpu'})]:
            payload,error=await metric_call(session,name,args)
            assert error and payload['error']=='server_not_initialized' and payload['how_to_fix']
        for resource in (await session.list_resources()).resources:
            response=await session.read_resource(resource.uri)
            text=response.contents[0].text
            if str(resource.uri).startswith('monitoring://'):
                assert 'MQL' in text or json.loads(text)['query_language']=='mql'
            else:
                assert json.loads(text)['error']=='server_not_initialized'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,code',[('no_data',None),('denied','NotAuthorizedOrNotFound'),('invalid','InvalidParameter')])
async def test_stdio_empty_data_and_safe_service_errors(tmp_path,mode,code):
    async with metrics_session(tmp_path,mode=mode) as (session,barrier):
        payload,error=await metric_call(session)
        if code:
            assert error and payload['error']['code']==code and payload['status']=='error'
            assert 'PRIVATE-OCI-FAILURE' not in json.dumps(payload)
        else:
            assert not error and payload['status']=='no_data' and payload['series']==[]
        assert len(invocations(barrier))==1


@pytest.mark.asyncio
async def test_stdio_budget_stops_the_next_sdk_request(tmp_path):
    async with metrics_session(tmp_path,budget=1) as (session,barrier):
        first,error=await metric_call(session)
        assert not error and first['returned_datapoint_count']==2
        second,error=await metric_call(session)
        assert error and second['error']['code']=='budget_exceeded'
        assert len(invocations(barrier))==1


@pytest.mark.asyncio
async def test_stdio_metrics_scope_barrier_pins_old_scope_then_applies_setter(tmp_path):
    async with metrics_session(tmp_path,mode='gated') as (session,barrier):
        query=asyncio.create_task(metric_call(session))
        try:
            async with asyncio.timeout(6):
                while not (barrier/'started').exists():
                    if query.done():
                        await query
                    await asyncio.sleep(.02)
            assert (barrier/'started').read_text()=='compartment-A'
            setter=asyncio.create_task(session.call_tool('set_compartment',{'compartment_id':'compartment-B'}))
            await asyncio.sleep(.15)
            assert not query.done() and not setter.done()
        finally:
            (barrier/'release').touch()
        first,error=await query
        changed=await setter
        assert not error and not changed.isError
        assert first['series'][0]['identity']['compartment_id']=='compartment-A'
        second,error=await metric_call(session)
        assert not error and second['series'][0]['identity']['compartment_id']=='compartment-B'
        assert [entry['compartment_id'] for entry in invocations(barrier)]==['compartment-A','compartment-B']
