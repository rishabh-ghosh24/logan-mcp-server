"""The public exact-filter path refuses unverified quote escapes before OCI."""
import json
import pytest
from tests.test_metrics_service import make_handlers,call,raw_arguments


@pytest.mark.asyncio
@pytest.mark.parametrize('value',['private-marker"name','private-marker\\name','x"} | evil[1m].sum() {name="\\tail',
    'private-marker\nname','private-marker\x7fname'])
async def test_catalog_refuses_unverified_escapes_without_budget_or_sdk(tmp_path,monkeypatch,value):
    handlers,sdk=make_handlers(tmp_path,monkeypatch)
    args={'metric_key':'cpu','time_range':'last_1_hour','dimension_filters':{'resourceDisplayName':value}}
    payload,error=await call(handlers,'query_catalog_metric',args)
    assert error and payload['error']['code']=='unsupported_exact_filter'
    assert sdk.calls==[] and handlers._budget_tracker.snapshot().queries==0
    assert 'private-marker' not in json.dumps(payload) and value not in json.dumps(payload)


@pytest.mark.asyncio
async def test_catalog_keeps_literal_star_pipe_and_space_values(tmp_path,monkeypatch):
    handlers,sdk=make_handlers(tmp_path,monkeypatch)
    payload,error=await call(handlers,'query_catalog_metric',{'metric_key':'cpu','time_range':'last_1_hour',
        'dimension_filters':{'resourceDisplayName':'web server*|other'}})
    assert not error and payload['returned_datapoint_count']==2
    assert sdk.calls[0][2].query=='CpuUtilization[5m]{resourceDisplayName="web server*|other"}.mean()'


@pytest.mark.asyncio
async def test_raw_mql_stays_available_for_expert_filter_expressions(tmp_path,monkeypatch):
    handlers,sdk=make_handlers(tmp_path,monkeypatch)
    query='CpuUtilization[5m]{resourceDisplayName=~"web*|other"}.mean()'
    payload,error=await call(handlers,arguments=raw_arguments(query=query))
    assert not error and payload['returned_datapoint_count']==2 and sdk.calls[0][2].query==query
