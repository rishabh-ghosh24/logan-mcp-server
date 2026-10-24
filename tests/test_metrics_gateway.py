"""Use installed OCI models and strict fakes only at the SDK request boundary."""
import asyncio
from datetime import datetime, timezone
import threading
from types import SimpleNamespace
import oci
import pytest
from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
from oci_logan_mcp.config import Settings
from oci_logan_mcp.metrics.models import MetricScope, QueryPlan, MetricsError


class SdkBoundary:
    def __init__(self, parent_thread):
        self.parent_thread = parent_thread
        self.calls = []
    def list_metrics(self, compartment_id, list_metrics_details, **kwargs):
        assert threading.get_ident() != self.parent_thread
        assert isinstance(list_metrics_details, oci.monitoring.models.ListMetricsDetails)
        assert set(kwargs) == {'compartment_id_in_subtree','limit','page','retry_strategy'}
        assert isinstance(kwargs['retry_strategy'],oci.retry.NoneRetryStrategy)
        self.calls.append(('list',compartment_id,list_metrics_details,kwargs))
        return SimpleNamespace(data=['visible-stream'], headers={'opc-next-page':'opaque-token'})
    def summarize_metrics_data(self, compartment_id, summarize_metrics_data_details, **kwargs):
        assert threading.get_ident() != self.parent_thread
        assert isinstance(summarize_metrics_data_details,oci.monitoring.models.SummarizeMetricsDataDetails)
        assert set(kwargs) == {'compartment_id_in_subtree','retry_strategy'}
        assert isinstance(kwargs['retry_strategy'],oci.retry.NoneRetryStrategy)
        self.calls.append(('query',compartment_id,summarize_metrics_data_details,kwargs))
        return SimpleNamespace(data=[17],headers={})


def setup():
    from oci_logan_mcp.metrics.gateway import MonitoringGateway
    from oci_logan_mcp.metrics.runtime import MonitoringRuntime
    sdk=SdkBoundary(threading.get_ident())
    clients=[]
    def regional(region, *, timeout_seconds):
        clients.append((region,timeout_seconds))
        return sdk
    client=SimpleNamespace(settings=Settings(),monitoring_client_for_region=regional)
    budget=BudgetTracker('metrics',BudgetLimits())
    runtime=MonitoringRuntime()
    gateway=MonitoringGateway(client,runtime,budget)
    return gateway,sdk,clients,budget


@pytest.mark.asyncio
async def test_namespace_discovery_uses_one_native_page_and_namespace_grouping():
    gateway,sdk,clients,budget=setup()
    scope=MetricScope('ocid1.compartment.child','us-phoenix-1',True,None)
    response=await gateway.discover(scope,group_namespaces=True,limit=7,page_token='incoming',deadline=asyncio.get_running_loop().time()+3)
    kind,compartment,details,kwargs=sdk.calls[0]
    assert kind=='list' and compartment==scope.compartment_id
    assert details.group_by==['namespace'] and details.dimension_filters is None
    assert details.namespace is None and details.name is None
    assert kwargs['limit']==7 and kwargs['page']=='incoming' and kwargs['compartment_id_in_subtree'] is True
    assert response.data==['visible-stream'] and response.headers['opc-next-page']=='opaque-token'
    assert clients==[('us-phoenix-1',60)] and budget.snapshot().queries==1 and len(sdk.calls)==1


@pytest.mark.asyncio
async def test_filtered_discovery_has_no_grouping_or_implicit_enumeration():
    gateway,sdk,clients,budget=setup()
    scope=MetricScope('chosen','us-ashburn-1')
    result=await gateway.discover(scope,namespace='custom',metric_name='Latency',resource_group='app',
        dimension_filters={'host':'a" b'},deadline=asyncio.get_running_loop().time()+3)
    details=sdk.calls[0][2]
    assert details.namespace=='custom' and details.name=='Latency' and details.resource_group=='app'
    assert details.dimension_filters=={'host':'a" b'} and details.group_by is None
    assert sdk.calls[0][3]['limit']==50 and sdk.calls[0][3]['compartment_id_in_subtree'] is False
    assert result.data==['visible-stream'] and budget.snapshot().queries==1


@pytest.mark.asyncio
async def test_query_forwards_every_pinned_parameter_and_shares_runtime_across_regions():
    gateway,sdk,clients,budget=setup()
    start=datetime(2026,10,24,10,tzinfo=timezone.utc)
    end=datetime(2026,10,24,11,tzinfo=timezone.utc)
    plan=QueryPlan('custom',('Latency[5m].mean()',),start,end,'1m','explicit',resource_group='app')
    scopes=[MetricScope('first','us-ashburn-1'),MetricScope('second','us-phoenix-1',True,None)]
    responses=await asyncio.gather(*(gateway.query(scope,plan,plan.queries[0],deadline=asyncio.get_running_loop().time()+3) for scope in scopes))
    assert [r.data for r in responses]==[[17],[17]] and budget.snapshot().queries==2
    for call,scope in zip(sdk.calls,scopes):
        kind,compartment,details,kwargs=call
        assert kind=='query' and compartment==scope.compartment_id
        assert details.namespace=='custom' and details.query=='Latency[5m].mean()'
        assert details.start_time==start and details.end_time==end
        assert details.resolution=='1m' and details.resource_group=='app'
        assert kwargs['compartment_id_in_subtree']==scope.include_subcompartments
    assert clients==[('us-ashburn-1',60),('us-phoenix-1',60)]


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [
    {'group_namespaces':True,'dimension_filters':{'host':'secret'}},
    {'group_namespaces':True,'namespace':'custom'}, {'limit':True}, {'limit':101},
])
async def test_invalid_discovery_does_not_enter_sdk(arguments):
    gateway,sdk,clients,budget=setup()
    with pytest.raises(MetricsError):
        await gateway.discover(MetricScope('chosen','us-ashburn-1'),**arguments,deadline=asyncio.get_running_loop().time()+3)
    assert sdk.calls==[] and clients==[] and budget.snapshot().queries==0
