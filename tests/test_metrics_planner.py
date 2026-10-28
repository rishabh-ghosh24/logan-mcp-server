"""Reviewed catalog inputs compile to exact, separate MQL expressions."""
from dataclasses import replace
import pytest
from oci_logan_mcp.metrics.catalog import MetricCatalog
from oci_logan_mcp.metrics.models import MetricsError


def compile_plan(key='cpu', definition=None, **arguments):
    from oci_logan_mcp.metrics.planner import plan_catalog_query
    return plan_catalog_query(definition or MetricCatalog.from_package().require(key),
        {'time_start':'2026-10-24T10:00:00Z','time_end':'2026-10-24T11:00:00Z',**arguments},
        catalog_version='fixture-version')


def test_cpu_defaults_are_explicit():
    plan = compile_plan()
    assert plan.namespace == 'oci_computeagent'
    assert plan.queries == ('CpuUtilization[5m].mean()',)
    assert plan.resolution == '5m' and plan.resolution_source == 'catalog_interval'
    assert plan.catalog_key == 'cpu' and plan.catalog_version == 'fixture-version'
    assert plan.resource_group is None
    assert plan.start.isoformat() == '2026-10-24T10:00:00+00:00'


@pytest.mark.parametrize('arguments,resolution,source,query', [
    ({'interval':'1h'},'1h','catalog_interval','CpuUtilization[1h].mean()'),
    ({'interval':'1h','resolution':'1h'},'1h','explicit','CpuUtilization[1h].mean()'),
    ({'interval':'5m','resolution':'1m','aggregation':'max'},'1m','explicit','CpuUtilization[5m].max()'),
])
def test_explicit_interval_and_resolution_preserve_the_requested_meaning(arguments,resolution,source,query):
    plan = compile_plan(**arguments)
    assert plan.queries == (query,) and plan.resolution == resolution and plan.resolution_source == source


@pytest.mark.parametrize('arguments', [
    {'aggregation':'p99'}, {'interval':'10m'}, {'resolution':'1h'},
    {'dimension_filters':{'compartmentId':'secret'}}, {'group_by':['compartmentId']},
    {'group_by':['resourceId','resourceId']}, {'group_by':'resourceId'},
    {'dimension_filters':{'resourceId':None}}, {'dimension_filters':{'resourceId':3}},
    {'namespace':'other'}, {'resource_group':'other'}, {'group_by':[True]},
])
def test_unreviewed_or_malformed_planner_choices_fail_before_execution(arguments):
    with pytest.raises(MetricsError):
        compile_plan(**arguments)


@pytest.mark.parametrize('change', [
    {'metric_names':('A','B','C','D','E')}, {'metric_names':()},
    {'metric_names':('Cpu;evil',)}, {'namespace':'oci_computeagent | evil'},
    {'allowed_filter_dimensions':('resourceId | evil',)},
])
def test_unusable_definition_identifiers_are_withheld(change):
    definition = replace(MetricCatalog.from_package().require('cpu'),**change)
    with pytest.raises(MetricsError):
        compile_plan(definition=definition)


def test_cpu_memory_preserves_two_independent_metrics():
    plan = compile_plan('cpu_memory')
    assert plan.queries == ('CpuUtilization[5m].mean()','MemoryUtilization[5m].mean()')
    assert plan.catalog_key == 'cpu_memory' and len(plan.queries)==2


def test_filters_sort_and_group_order_is_preserved():
    plan = compile_plan(dimension_filters={'resourceId':'id','resourceDisplayName':'host'},
        group_by=['resourceDisplayName','resourceId'])
    assert plan.queries == ('CpuUtilization[5m]{resourceDisplayName="host",resourceId="id"}.groupBy(resourceDisplayName,resourceId).mean()',)


def test_hostile_dimension_value_stays_a_single_quoted_value():
    hostile = 'x"} | evil[1m].sum() {name="\\tail'
    plan = compile_plan(dimension_filters={'resourceDisplayName':hostile})
    assert plan.queries == (r'CpuUtilization[5m]{resourceDisplayName="x\"} | evil[1m].sum() {name=\"\\tail"}.mean()',)
    from oci_logan_mcp.metrics.validation import parse_mql_intervals
    assert [x.total_seconds() for x in parse_mql_intervals(plan.queries[0])] == [300]
