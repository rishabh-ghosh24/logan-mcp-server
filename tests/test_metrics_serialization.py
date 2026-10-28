"""Real SDK values survive normalization and caps admit only complete streams."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import oci
import pytest
from mcp.types import CallToolResult, TextContent
from oci_logan_mcp.metrics.catalog import MetricCatalog
from oci_logan_mcp.metrics.models import MetricScope, QueryPlan, ResponseLimits, MetricsError

SCOPE=MetricScope('root','us-ashburn-1',True,'root')
START=datetime(2026,10,24,10,tzinfo=timezone.utc)
PLAN=QueryPlan('oci_computeagent',('CpuUtilization[5m].mean()',),START,START+timedelta(hours=1),'5m','catalog_interval',catalog_key='cpu',catalog_version='fixture')


def native(zone='a', *, metadata=None, points=None, name='CpuUtilization', namespace='oci_computeagent', group=None):
    points=points if points is not None else [(START,0),(START+timedelta(minutes=10),1.2345678901234567)]
    return oci.monitoring.models.MetricData(name=name,namespace=namespace,resource_group=group,
        compartment_id='actual-child',dimensions={'zone':zone,'resourceId':'same-id','resourceDisplayName':'same-name'},
        metadata=metadata,aggregated_datapoints=[oci.monitoring.models.AggregatedDatapoint(timestamp=t,value=v) for t,v in points])


def normalize(items,definition=None):
    from oci_logan_mcp.metrics.serialization import normalize_metric_series
    return normalize_metric_series(SimpleNamespace(data=items,headers={'opc-request-id':'request-73'}),
        scope=SCOPE,plan=PLAN,query=PLAN.queries[0],definition=definition)


def stream(name, count=2, label=None):
    return {'identity':{'region':'us-ashburn-1','compartment_id':'actual-child','namespace':'custom',
        'metric_name':'Load','resource_group':None,'dimensions':{'resourceId':name,'displayName':label or name}},
        'points':[['2026-10-24T10:00:00Z',index] for index in range(count)], 'unit':None,'warnings':[]}


def fit(items,limits=None,context=None):
    from oci_logan_mcp.metrics.serialization import fit_series
    return fit_series(items,context=context or {'query_language':'mql','queries':['Load[5m].mean()']},limits=limits or ResponseLimits())


def test_complete_identity_keeps_streams_with_the_same_resource_and_display_name():
    result=normalize([native('b'),native('a')])
    assert len(result)==2
    assert {s['identity']['dimensions']['zone'] for s in result}=={'a','b'}
    for s in result:
        identity=s['identity']
        assert identity['region']=='us-ashburn-1' and identity['compartment_id']=='actual-child'
        assert identity['namespace']=='oci_computeagent' and identity['metric_name']=='CpuUtilization'
        assert identity['resource_group'] is None
        assert list(identity['dimensions'])==['resourceDisplayName','resourceId','zone']
        assert identity['dimensions']['resourceId']=='same-id'
        assert s['request_id']=='request-73'


def test_points_sort_without_merging_filling_or_rounding():
    fractional=START+timedelta(minutes=5,microseconds=123456)
    points=[(START+timedelta(minutes=10),7),(fractional,1.2345678901234567),(START,0),(fractional,2)]
    result=normalize([native(points=points)])
    payload=fit(result)
    assert payload['point_columns']==['timestamp','value']
    assert payload['series'][0]['points']==[
        ['2026-10-24T10:00:00Z',0],['2026-10-24T10:05:00.123456Z',1.2345678901234567],
        ['2026-10-24T10:05:00.123456Z',2],['2026-10-24T10:10:00Z',7]]
    assert payload['returned_datapoint_count']==4 and payload['total_datapoint_count']==4
    assert payload['complete'] is True


@pytest.mark.parametrize('metadata,definition,unit,source,warning', [
    (None,False,None,'unknown','unit_unknown'),
    ({'unit':'percent'},False,'percent','oci',None),
    (None,True,'percent','catalog',None),
    ({'unit':'bytes'},True,'bytes','oci','unit_conflict'),
])
def test_units_use_actual_metadata_and_exact_reviewed_fallback(metadata,definition,unit,source,warning):
    definition=MetricCatalog.from_package().require('cpu') if definition else None
    item=normalize([native(metadata=metadata)],definition=definition)[0]
    assert item['unit']==unit and item['unit_source']==source
    if warning:
        assert warning in item['warnings']
    else:
        assert item['warnings']==[]


@pytest.mark.parametrize('changes',[{'name':'OtherMetric'},{'namespace':'custom'},{'group':'other'}])
def test_catalog_units_do_not_leak_to_a_different_metric(changes):
    item=normalize([native(**changes)],definition=MetricCatalog.from_package().require('cpu'))[0]
    assert item['unit'] is None and item['unit_source']=='unknown' and 'unit_unknown' in item['warnings']


@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf'),True])
def test_invalid_backend_values_fail_safely_instead_of_emitting_json_extensions(value):
    with pytest.raises(MetricsError) as caught:
        normalize([native(points=[(START,value)])])
    assert caught.value.code=='invalid_metrics_response'


def test_backend_point_bound_is_checked_before_aggregate_truncation():
    points=[(START+timedelta(minutes=i),i) for i in range(14)]
    with pytest.raises(MetricsError) as caught:
        normalize([native(points=points)])
    assert caught.value.code=='backend_bound_mismatch'
    assert caught.value.details=={'observed_points':14,'point_bound':13}


@pytest.mark.parametrize('limits,reason',[ (ResponseLimits(max_series=1),'series'),(ResponseLimits(max_datapoints=3),'points') ])
def test_count_caps_admit_sorted_complete_streams_and_report_known_omissions(limits,reason):
    payload=fit([stream('b'),stream('a')],limits)
    assert [s['identity']['dimensions']['resourceId'] for s in payload['series']]==['a']
    assert payload['series'][0]['points']==[['2026-10-24T10:00:00Z',0],['2026-10-24T10:00:00Z',1]]
    assert payload['returned_series_count']==1 and payload['total_series_count']==2
    assert payload['returned_datapoint_count']==2 and payload['total_datapoint_count']==4
    assert payload['dropped_series_count']==1 and payload['dropped_datapoint_count']==2
    assert payload['complete'] is False and reason in payload['cap_reasons']
    assert 'next_page_token' not in payload


def test_byte_cap_includes_a_long_query_envelope_and_admits_complete_series():
    from oci_logan_mcp.metrics.serialization import result_size
    context={'query_language':'mql','queries':['Load[5m]{name="'+'x'*1000+'"}.mean()']}
    limits=ResponseLimits(max_response_bytes=4000)
    payload=fit([stream('b',label='y'*1500),stream('a',label='x'*1500)],limits,context)
    assert [s['identity']['dimensions']['resourceId'] for s in payload['series']]==['a']
    assert payload['complete'] is False and payload['dropped_series_count']==1
    assert payload['dropped_datapoint_count']==2 and 'bytes' in payload['cap_reasons']
    assert result_size(payload)<=limits.max_response_bytes


def test_serialized_result_size_matches_mcp_for_unicode_and_escaped_json():
    from oci_logan_mcp.metrics.serialization import result_size,metrics_content
    payload={'query_language':'mql','marker':'漢字"\\\n','points':[['2026-10-24T10:00:00.123456Z',1.2345678901234567]]}
    text=json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    expected=CallToolResult(content=[TextContent(type='text',text=text)],isError=False)
    wire=expected.model_dump_json(by_alias=True,exclude_none=True).encode('utf-8')
    assert result_size(payload)==len(wire)
    content=metrics_content(payload)
    assert content[0]['type']=='text' and content[0]['text']==text
    assert json.loads(content[0]['text'])==payload
    assert len(wire)>len(text.encode('utf-8'))


@pytest.mark.parametrize('items,context',[([stream('a',label='x'*5000)],None),([],{'query':'x'*5000})])
def test_no_complete_series_or_metadata_fitting_is_a_flagged_limit_error(items,context):
    from oci_logan_mcp.metrics.serialization import metrics_content
    from oci_logan_mcp.protocol_results import build_call_tool_result
    with pytest.raises(MetricsError) as caught:
        fit(items,ResponseLimits(max_response_bytes=1024),context)
    assert caught.value.code=='response_limit_exceeded'
    response=build_call_tool_result(metrics_content({'error':caught.value.code},is_error=True))
    assert response.isError is True and json.loads(response.content[0].text)['error']=='response_limit_exceeded'


def test_empty_native_result_is_no_data_without_an_invented_total():
    result=normalize([])
    assert result==[]
    payload=fit(result)
    assert payload['status']=='no_data' and payload['complete'] is True
    assert payload['series']==[] and payload['returned_datapoint_count']==0 and payload['total_series_count']==0


def test_existing_partial_context_is_never_upgraded_to_complete():
    payload=fit([stream('a')],context={'complete':False,'status':'backend_error','warnings':['earlier query failed']})
    assert payload['complete'] is False and payload['status']=='backend_error'
    assert payload['series'][0]['identity']['dimensions']['resourceId']=='a'
    assert 'earlier query failed' in payload['warnings']


def test_discovery_preserves_native_items_and_one_next_token_without_total():
    from oci_logan_mcp.metrics.serialization import format_discovery
    items=[oci.monitoring.models.Metric(name='Load',namespace='custom',compartment_id='child',dimensions={'z':'last','a':'first'})]
    response=SimpleNamespace(data=items,headers={'opc-next-page':'opaque+token','opc-request-id':'request-73'})
    payload=format_discovery(response,scope=SCOPE,namespace='custom',filters={'name':'Load'},group_namespaces=False)
    assert payload['returned_count']==1 and payload['page_complete'] is True
    assert payload['next_page_token']=='opaque+token' and payload['request_id']=='request-73'
    assert payload['metrics'][0]['name']=='Load' and payload['metrics'][0]['compartment_id']=='child'
    assert payload['metrics'][0]['dimensions']=={'a':'first','z':'last'}
    assert payload['filters']=={'name':'Load'} and payload['scope']['region']=='us-ashburn-1'
    assert 'total_count' not in payload and 'total_series_count' not in payload
    grouped=format_discovery(response,scope=SCOPE,namespace=None,filters={},group_namespaces=True)
    assert grouped['namespaces']==['custom'] and grouped['returned_count']==1


def test_oversized_discovery_page_is_a_flagged_error_without_skipping_items():
    from oci_logan_mcp.metrics.serialization import format_discovery,metrics_content
    from oci_logan_mcp.protocol_results import build_call_tool_result
    item=oci.monitoring.models.Metric(name='Load',namespace='custom',dimensions={'long':'x'*5000})
    with pytest.raises(MetricsError) as caught:
        format_discovery(SimpleNamespace(data=[item],headers={'opc-next-page':'opaque'}),scope=SCOPE,
            namespace='custom',filters={},group_namespaces=False,limits=ResponseLimits(max_response_bytes=1024))
    assert caught.value.code=='response_limit_exceeded' and 'limit' in str(caught.value)
    result=build_call_tool_result(metrics_content({'error':caught.value.code},is_error=True))
    assert result.isError is True
    assert 'next_page_token' not in json.loads(result.content[0].text)


def test_shared_protocol_builder_is_reexported_unchanged():
    from oci_logan_mcp.protocol_results import build_call_tool_result
    from oci_logan_mcp.server import build_call_tool_result as old_import
    assert old_import is build_call_tool_result
    result=build_call_tool_result([{'type':'text','text':'before'},
        {'type':'image','data':'cG5n','mimeType':'image/png','is_error':True}])
    assert result.isError is True and result.content[0].text=='before'
    assert result.content[1].data=='cG5n' and result.content[1].mimeType=='image/png'
