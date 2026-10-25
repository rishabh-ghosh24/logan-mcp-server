"""Exercise metrics through real handlers with only OCI request boundaries replaced."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import oci
import pytest

from oci_logan_mcp.audit import AuditLogger
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.config import Settings
from oci_logan_mcp.context_manager import ContextManager
from oci_logan_mcp.handlers import MCPHandlers, ScopeGuard
from oci_logan_mcp.preferences import PreferenceStore
from oci_logan_mcp.query_logger import QueryLogger
from oci_logan_mcp.secret_store import SecretStore
from oci_logan_mcp.user_store import UserStore


class MonitoringBoundary:
    def __init__(self):
        self.calls = []
        self.error = None
        self.empty = False
        self.started = threading.Event()
        self.release = None
        self.fail_after = None
        self.extra_stream = False

    def list_metrics(self, *, compartment_id, list_metrics_details, **kwargs):
        self.calls.append(('discover', compartment_id, list_metrics_details, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(data=[oci.monitoring.models.Metric(namespace='oci_computeagent',
            name='CpuUtilization', compartment_id=compartment_id, dimensions={'resourceId':'instance-a'})],
            headers={'opc-request-id':'discovery-id', 'opc-next-page':'next-native-page'})

    def summarize_metrics_data(self, *, compartment_id, summarize_metrics_data_details, **kwargs):
        details = summarize_metrics_data_details
        self.calls.append(('query', compartment_id, details, kwargs))
        self.started.set()
        if self.release is not None:
            assert self.release.wait(3), 'Test did not release the SDK worker'
        if self.error or (self.fail_after is not None and len(self.calls)>self.fail_after):
            raise self.error or oci.exceptions.ServiceError(403, 'NotAuthorizedOrNotFound', {}, 'private failure')
        rows = [] if self.empty else [oci.monitoring.models.MetricData(namespace=details.namespace,
            name=details.query.split('[')[0], compartment_id=compartment_id, resource_group=details.resource_group,
            dimensions={'resourceId':'instance-a'}, metadata={'unit':'percent'},
            aggregated_datapoints=[oci.monitoring.models.AggregatedDatapoint(timestamp=details.start_time, value=0),
                oci.monitoring.models.AggregatedDatapoint(timestamp=details.end_time, value=17.25)])]
        if rows and self.extra_stream:
            second=oci.monitoring.models.MetricData(namespace=details.namespace,
                name=details.query.split('[')[0], compartment_id=compartment_id,
                dimensions={'resourceId':'instance-b'}, metadata={'unit':'percent'},
                aggregated_datapoints=rows[0].aggregated_datapoints)
            rows.append(second)
        return SimpleNamespace(data=rows, headers={'opc-request-id':'metric-request-id'})


def make_handlers(tmp_path, monkeypatch, *, retained=True, remembered=None, la_namespace=''):
    settings = Settings()
    settings.read_only = True
    settings.log_analytics.namespace = la_namespace
    settings.log_analytics.default_compartment_id = 'la-default'
    settings.monitoring.default_compartment_id = 'metrics-default'
    settings.monitoring.region = 'us-ashburn-1'
    settings.report_delivery.artifact_dir = tmp_path/'reports'
    monkeypatch.setattr('oci_logan_mcp.client.get_signer', lambda _: ({'region':'us-phoenix-1',
        'tenancy':'ocid1.tenancy.test'}, SimpleNamespace()))
    client = OCILogAnalyticsClient(settings)
    boundary = MonitoringBoundary()
    client._monitoring_factory = lambda **kwargs: boundary
    client._la_factory = Mock(side_effect=RuntimeError('LA constructor must be lazy'))
    client._identity_factory = Mock(side_effect=RuntimeError('Identity constructor must be lazy'))
    user = UserStore(base_dir=tmp_path, user_id='metricuser')
    user_dir = tmp_path/'users'/'metricuser'
    preferences = PreferenceStore(user_dir=user_dir)
    if remembered:
        preferences.remember('default_compartment_id', remembered, confidence=1)
    history = QueryLogger(settings.logging, user_dir=user_dir)
    audit = AuditLogger(tmp_path/'audit', session_id='metrics-service', store_arguments=retained)
    handlers = MCPHandlers(settings, client, CacheManager(settings.cache), history,
        ContextManager(settings, context_dir=tmp_path/'context'), user,
        preference_store=preferences, secret_store=SecretStore(tmp_path/'secret'), audit_logger=audit)
    return handlers, boundary


def raw_arguments(**extra):
    end = datetime.now(timezone.utc)-timedelta(minutes=1)
    return {'namespace':'oci_computeagent', 'query':'CpuUtilization[5m].mean()',
        'time_start':(end-timedelta(hours=1)).isoformat(), 'time_end':end.isoformat(), **extra}


async def call(handlers, name='query_metrics', arguments=None, **kwargs):
    content = await handlers.handle_tool_call(name, raw_arguments() if arguments is None else arguments, **kwargs)
    assert content and content[0]['type']=='text'
    return json.loads(content[0]['text']), bool(content[0].get('is_error'))


@pytest.mark.asyncio
async def test_metrics_only_dispatch_returns_native_points_without_la_or_identity(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch)
    payload, error = await call(handlers)
    assert not error and payload['status']=='success' and payload['complete'] is True
    assert payload['series'][0]['points'][0][1]==0 and payload['series'][0]['points'][1][1]==17.25
    assert payload['scope']['compartment_id']=='metrics-default'
    assert payload['resolution']=='5m' and payload['resolution_source']=='query_interval'
    assert payload['cost_estimate']=={'service':'monitoring','estimated':False}
    assert payload['budget']['queries']==1 and handlers._budget_tracker.snapshot().queries==1
    assert len(sdk.calls)==1 and not handlers.oci_client._la_factory.called and not handlers.oci_client._identity_factory.called


@pytest.mark.asyncio
@pytest.mark.parametrize('remembered', [None, 'remembered-choice'])
async def test_scope_defaults_setter_and_explicit_override_remain_distinct(tmp_path, monkeypatch, remembered):
    handlers, sdk = make_handlers(tmp_path, monkeypatch, remembered=remembered)
    handlers.settings.read_only = False
    first, _ = await call(handlers)
    assert first['scope']['compartment_id']==(remembered or 'metrics-default')
    await handlers.handle_tool_call('set_compartment', {'compartment_id':'chosen-B'})
    await handlers.handle_tool_call('set_namespace', {'namespace':'other-log-namespace'})
    second, error = await call(handlers)
    third, _ = await call(handlers, arguments=raw_arguments(compartment_id='explicit-C', region='us-phoenix-1'))
    assert not error and second['scope']['compartment_id']=='chosen-B'
    assert second['namespace']=='oci_computeagent' and third['scope']['region']=='us-phoenix-1'
    assert [entry[1] for entry in sdk.calls]==[remembered or 'metrics-default','chosen-B','explicit-C']
    assert third['scope']['compartment_id']=='explicit-C'


@pytest.mark.asyncio
async def test_shared_scope_guard_blocks_setter_until_real_metric_worker_finishes(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch, remembered='chosen-A')
    handlers.settings.read_only = False
    sdk.release = threading.Event()
    async with asyncio.timeout(4):
        query = asyncio.create_task(call(handlers))
        while not sdk.started.is_set():
            if query.done():
                await query
            await asyncio.sleep(.01)
        setter = asyncio.create_task(handlers.handle_tool_call('set_compartment', {'compartment_id':'chosen-B'}))
        await asyncio.sleep(.04)
        assert not setter.done()
        sdk.release.set()
        first, error = await query
        await setter
        second, _ = await call(handlers)
    assert not error and first['series'][0]['identity']['compartment_id']=='chosen-A'
    assert second['series'][0]['identity']['compartment_id']=='chosen-B'


@pytest.mark.asyncio
async def test_monitoring_denial_does_not_poison_later_call_or_log_dispatch(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch, la_namespace='logs')
    sdk.error = oci.exceptions.ServiceError(403, 'NotAuthorizedOrNotFound', {}, 'private query detail')
    denied, error = await call(handlers)
    assert error and denied['status']=='error' and denied['error']['status']==403
    sdk.error = None
    success, error = await call(handlers)
    assert not error and success['returned_datapoint_count']==2
    class LogBoundary:
        def query(self, namespace_name, query_details, limit, page=None):
            return oci.response.Response(200, {}, SimpleNamespace(columns=[SimpleNamespace(
                display_name='count', internal_name='count', value_type='LONG')],
                items=[{'count':42}], total_count=1, is_partial_result=False), None)
    handlers.oci_client._la_client = LogBoundary()
    logs, error = await call(handlers, 'run_query', {'query':'* | stats count', 'time_range':'last_1_hour'})
    assert not error and logs['data']['rows'][0][0]==42


@pytest.mark.asyncio
async def test_catalog_loader_failure_is_local_to_catalog_tools(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch)
    def broken():
        raise RuntimeError('private catalog error')
    handlers.metrics_service._catalog_loader = broken
    catalog, error = await call(handlers, 'search_metric_catalog', {'query':'cpu'})
    assert error and catalog['error']['code']=='catalog_unavailable' and 'private' not in json.dumps(catalog)
    raw, error = await call(handlers)
    assert not error and raw['returned_datapoint_count']==2 and len(sdk.calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['empty','cap','invalid','budget'])
async def test_service_error_and_empty_and_cap_contracts(tmp_path, monkeypatch, mode):
    handlers, sdk = make_handlers(tmp_path, monkeypatch)
    args = raw_arguments()
    if mode=='empty':
        sdk.empty = True
    elif mode=='cap':
        args['max_datapoints']=13
    elif mode=='invalid':
        args['query']='CpuUtilization.mean()'
    else:
        handlers._budget_tracker.limits.max_queries_per_session=0
    payload, error = await call(handlers, arguments=args)
    if mode in ('empty','cap'):
        assert not error and payload['status']==('no_data' if mode=='empty' else 'success')
        assert payload['returned_datapoint_count']==(0 if mode=='empty' else 2)
    else:
        assert error and payload['status']=='error' and sdk.calls==[]
        assert payload['error']['code']==('invalid_interval' if mode=='invalid' else 'budget_exceeded')
        assert handlers._budget_tracker.snapshot().queries==0


@pytest.mark.asyncio
async def test_composite_failure_keeps_completed_data_and_shared_budget(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch)
    from oci_logan_mcp.metrics.catalog import MetricCatalog
    from oci_logan_mcp.metrics.models import MetricDefinition
    entry = MetricDefinition('pair','oci_computeagent','Pair',('CpuUtilization','MemoryUtilization'),'percent')
    handlers.metrics_service._catalog_loader = lambda: MetricCatalog([entry], version='test')
    sdk.fail_after = 1
    args = raw_arguments(metric_key='pair')
    args.pop('query'); args.pop('namespace')
    payload, error = await call(handlers,'query_catalog_metric',args)
    assert error and payload['status']=='partial' and payload['complete'] is False
    assert payload['returned_datapoint_count']==2 and payload['completed_queries']==['CpuUtilization[5m].mean()']
    assert len(sdk.calls)==2 and handlers._budget_tracker.snapshot().queries==1


@pytest.mark.asyncio
async def test_local_catalog_search_uses_no_network_or_budget(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch)
    payload, error = await call(handlers,'search_metric_catalog',{'query':'cpu'})
    assert not error and payload['matches'][0]['metric_key']=='cpu'
    assert payload['query_language']=='mql' and sdk.calls==[] and handlers._budget_tracker.snapshot().queries==0


@pytest.mark.asyncio
async def test_readiness_and_metrics_probe_distinguish_inspect_from_read(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch)
    before, error = await call(handlers,'get_current_context',{})
    assert not error and before['services']['monitoring']['client_created'] is False
    assert before['services']['monitoring']['read_authorized'] is None and sdk.calls==[]
    probe, error = await call(handlers,'test_connection',{'service':'metrics'})
    assert not error and probe['status']=='success' and probe['inspect_authorized'] is True
    assert probe['read_authorized'] is None and len(sdk.calls)==1
    assert sdk.calls[0][2].group_by==['namespace'] and sdk.calls[0][3]['limit']==1
    after, _ = await call(handlers,'get_current_context',{})
    assert after['services']['monitoring']['client_created'] is True
    assert after['services']['monitoring']['last_tested']['status']=='success' and len(sdk.calls)==1
    await call(handlers)
    ready = handlers.metrics_service.readiness()
    assert ready['read_authorized'] is True


@pytest.mark.asyncio
async def test_bad_metrics_config_blocks_before_scope_and_is_visible_in_diagnostics(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch)
    handlers.settings.monitoring_config_errors=('max_series',)
    handlers.settings.monitoring.default_compartment_id=''
    handlers.settings.log_analytics.default_compartment_id=''
    bad, error = await call(handlers)
    assert error and bad['error']['code']=='monitoring_config_invalid'
    context, _ = await call(handlers,'get_current_context',{})
    assert context['services']['monitoring']['config_errors']==['max_series']
    probe, error = await call(handlers,'test_connection',{'service':'metrics'})
    assert error and probe['error']['code']=='monitoring_config_invalid'
    assert sdk.calls==[] and handlers._budget_tracker.snapshot().queries==0


@pytest.mark.asyncio
async def test_all_diagnostics_preserve_each_service_and_flag_mixed_failure(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch)
    payload, error = await call(handlers,'test_connection',{'service':'all'})
    assert error and payload['status']=='partial'
    assert payload['services']['metrics']['inspect_authorized'] is True
    assert payload['services']['logs']['status']!='All systems operational'
    assert len(sdk.calls)==1


@pytest.mark.asyncio
async def test_cancelled_dispatch_has_one_terminal_event_despite_late_worker(tmp_path, monkeypatch):
    handlers, sdk = make_handlers(tmp_path, monkeypatch)
    sdk.release=threading.Event()
    task=asyncio.create_task(call(handlers))
    try:
        async with asyncio.timeout(3):
            while not sdk.started.is_set():
                if task.done():
                    await task
                await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        sdk.release.set()
    await asyncio.sleep(.05)
    events=list(handlers.audit_logger.iter_entries())
    assert [event['outcome'] for event in events]==['invoked','cancelled']
    assert events[0]['trace_id']==events[1]['trace_id']
    assert handlers._budget_tracker.snapshot().queries==1


@pytest.mark.asyncio
async def test_aggregate_cap_keeps_whole_stream_and_discloses_omissions(tmp_path, monkeypatch):
    handlers, sdk=make_handlers(tmp_path,monkeypatch)
    sdk.extra_stream=True
    payload,error=await call(handlers,arguments=raw_arguments(max_series=1))
    assert not error and payload['status']=='partial' and payload['complete'] is False
    assert payload['returned_series_count']==1 and payload['dropped_series_count']==1
    assert payload['returned_datapoint_count']==2 and payload['dropped_datapoint_count']==2
    assert payload['cap_reasons']==['series'] and payload['series'][0]['identity']['dimensions']['resourceId']=='instance-a'


@pytest.mark.asyncio
async def test_native_discovery_preserves_page_token_and_exact_filters(tmp_path, monkeypatch):
    handlers,sdk=make_handlers(tmp_path,monkeypatch)
    payload,error=await call(handlers,'list_metrics',{'namespace':'oci_computeagent',
        'metric_name':'CpuUtilization','dimension_filters':{'resourceId':'instance-a'},'page_token':'opaque','limit':2})
    assert not error and payload['metrics'][0]['dimensions']=={'resourceId':'instance-a'}
    assert payload['next_page_token']=='next-native-page' and payload['returned_count']==1
    assert sdk.calls[0][2].dimension_filters=={'resourceId':'instance-a'} and sdk.calls[0][3]['page']=='opaque'
    assert handlers._budget_tracker.snapshot().queries==1


@pytest.mark.asyncio
async def test_timeout_keeps_worker_scope_and_budget_after_setter(tmp_path, monkeypatch):
    handlers,sdk=make_handlers(tmp_path,monkeypatch,remembered='chosen-A')
    handlers.settings.read_only=False
    handlers.settings.monitoring.timeout_seconds=.03
    sdk.release=threading.Event()
    try:
        payload,error=await call(handlers)
        assert error and payload['error']['code']=='monitoring_timeout' and sdk.started.is_set()
        await handlers.handle_tool_call('set_compartment',{'compartment_id':'chosen-B'})
        assert sdk.calls[0][1]=='chosen-A' and sdk.calls[0][2].namespace=='oci_computeagent'
        assert handlers._budget_tracker.snapshot().queries==1
    finally:
        sdk.release.set()
    await asyncio.sleep(.04)
    events=[e for e in handlers.audit_logger.iter_entries() if e['tool']=='query_metrics']
    assert len(events)==2 and [e['outcome'] for e in events]==['invoked','executed']


@pytest.mark.asyncio
async def test_overlapping_metrics_calls_keep_client_claims_with_their_trace(tmp_path, monkeypatch):
    handlers,sdk=make_handlers(tmp_path,monkeypatch)
    sdk.release=threading.Event()
    first_claim={'claimed_client':'first','claimed_version':'1','source_ip_truncated':None}
    second_claim={'claimed_client':'second','claimed_version':'2','source_ip_truncated':None}
    first=asyncio.create_task(call(handlers,client_metadata=first_claim))
    try:
        async with asyncio.timeout(3):
            while not sdk.started.is_set():
                if first.done():
                    await first
                await asyncio.sleep(.01)
            second,error=await call(handlers,'search_metric_catalog',{'query':'cpu'},client_metadata=second_claim)
            assert not error and second['matches'][0]['metric_key']=='cpu'
    finally:
        sdk.release.set()
    payload,error=await first
    assert not error and payload['returned_datapoint_count']==2
    events=list(handlers.audit_logger.iter_entries())
    for name,claims in [('query_metrics',first_claim),('search_metric_catalog',second_claim)]:
        selected=[event for event in events if event['tool']==name]
        assert len(selected)==2 and all(event['client_metadata']==claims for event in selected)
        assert selected[0]['trace_id']==selected[1]['trace_id']


@pytest.mark.asyncio
async def test_composite_uses_one_deadline_and_charges_late_second_request(tmp_path, monkeypatch):
    handlers,sdk=make_handlers(tmp_path,monkeypatch)
    from oci_logan_mcp.metrics.catalog import MetricCatalog
    from oci_logan_mcp.metrics.models import MetricDefinition
    entry=MetricDefinition('pair','oci_computeagent','Pair',('CpuUtilization','MemoryUtilization'),'percent')
    handlers.metrics_service._catalog_loader=lambda: MetricCatalog([entry],version='test')
    handlers.settings.monitoring.timeout_seconds=.25
    original=sdk.summarize_metrics_data
    def delayed(**kwargs):
        time.sleep(.15)
        return original(**kwargs)
    sdk.summarize_metrics_data=delayed
    args=raw_arguments(metric_key='pair')
    args.pop('query'); args.pop('namespace')
    payload,error=await call(handlers,'query_catalog_metric',args)
    assert error and payload['error']['code']=='monitoring_timeout' and payload['complete'] is False
    assert payload['completed_queries']==['CpuUtilization[5m].mean()'] and payload['returned_datapoint_count']==2
    assert handlers._budget_tracker.snapshot().queries==2
    await asyncio.sleep(.1)
    assert len(sdk.calls)==2 and len(list(handlers.audit_logger.iter_entries()))==2


@pytest.mark.asyncio
async def test_probe_does_not_reuse_read_permission_from_a_different_scope(tmp_path, monkeypatch):
    handlers,sdk=make_handlers(tmp_path,monkeypatch)
    handlers.settings.read_only=False
    positive,error=await call(handlers,arguments=raw_arguments(compartment_id='scope-A'))
    assert not error and positive['returned_datapoint_count']==2
    await handlers.handle_tool_call('set_compartment',{'compartment_id':'scope-B'})
    probe,error=await call(handlers,'test_connection',{'service':'metrics'})
    assert not error and probe['scope']['compartment_id']=='scope-B'
    assert probe['inspect_authorized'] is True and probe['read_authorized'] is None
    assert len(sdk.calls)==2
