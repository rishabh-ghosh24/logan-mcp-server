"""Bad Monitoring settings block only Monitoring and retain actionable diagnostics."""
import asyncio
import json
from pathlib import Path

import pytest
import yaml

from oci_logan_mcp.config import load_config, save_config
from oci_logan_mcp.metrics.models import MetricScope, MetricsError


def config_file(tmp_path, monitoring):
    path=tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump({'log_analytics':{'namespace':'logs','default_compartment_id':'chosen'},
                                   'monitoring':monitoring}))
    return path


@pytest.mark.parametrize('source,value',[('environment','abc'),('environment','500'),('yaml','abc'),('yaml',500)])
@pytest.mark.asyncio
async def test_bad_metrics_setting_keeps_real_stdio_log_queries_working(tmp_path,source,value):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    from tests.test_protocol_smoke import _params, GOOD_CONFIG

    config=GOOD_CONFIG
    if source=='yaml':
        config+='monitoring:\n  max_series: '+str(value)+'\n'
    params=_params(tmp_path,config,fake_oci=True)
    if source=='environment':
        params.env['OCI_LOGAN_MONITORING_MAX_SERIES']=value
    with (tmp_path/'server.stderr').open('w') as errors:
        async with asyncio.timeout(20):
            async with stdio_client(params,errlog=errors) as (read,write):
                async with ClientSession(read,write) as session:
                    initialized=await session.initialize()
                    assert initialized.serverInfo.name=='oci-log-analytics'
                    response=await session.call_tool('run_query',{'query':'* | stats count','time_range':'last_15_min'})
                    assert response.isError is False
                    assert json.loads(response.content[0].text)['data']['rows']==[[42,'smoke']]
    assert 'monitoring.max_series' in (tmp_path/'server.stderr').read_text()


@pytest.mark.parametrize('source,value',[('environment','private-marker'),('environment','500'),('yaml','private-marker'),('yaml',500)])
@pytest.mark.asyncio
async def test_bad_config_is_a_safe_flagged_metrics_error_before_budget_or_sdk(tmp_path,monkeypatch,caplog,source,value):
    from tests.test_metrics_clients import RecordingSdk, make_client
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.metrics.gateway import MonitoringGateway
    from oci_logan_mcp.metrics.runtime import MonitoringRuntime
    from oci_logan_mcp.metrics.serialization import metrics_content
    from oci_logan_mcp.protocol_results import build_call_tool_result

    path=config_file(tmp_path,{'max_series':value if source=='yaml' else 20})
    if source=='environment':
        monkeypatch.setenv('OCI_LOGAN_MONITORING_MAX_SERIES',str(value))
    settings=load_config(path,isolate_monitoring_errors=True)
    assert settings.log_analytics.namespace=='logs'
    assert settings.monitoring_config_errors==('monitoring.max_series',)
    sdk=RecordingSdk()
    client=make_client(monkeypatch,sdk,settings)
    budget=BudgetTracker('isolation',BudgetLimits())
    gateway=MonitoringGateway(client,MonitoringRuntime(),budget)
    scope=MetricScope('chosen','us-phoenix-1')
    with pytest.raises(MetricsError) as caught:
        await gateway.discover(scope,group_namespaces=True,deadline=asyncio.get_running_loop().time()+3)
    assert caught.value.code=='monitoring_config_invalid'
    assert caught.value.details=={'settings':['monitoring.max_series']}
    assert 'monitoring.max_series' in str(caught.value)
    response=build_call_tool_result(metrics_content({'error':caught.value.code,'detail':str(caught.value),
        **caught.value.details},is_error=True))
    assert response.isError is True
    assert json.loads(response.content[0].text)['error']=='monitoring_config_invalid'
    assert 'private-marker' not in response.content[0].text+caplog.text
    assert sdk.monitor_calls==[] and budget.snapshot().queries==0
    with pytest.raises(ValueError,match='monitoring.max_series'):
        client.monitoring_client_for_region('us-phoenix-1',timeout_seconds=60)
    assert sdk.monitor_calls==[]


def test_good_environment_override_repairs_the_matching_yaml_error(tmp_path,monkeypatch):
    path=config_file(tmp_path,{'max_series':'private-marker','region':'us-phoenix-1'})
    monkeypatch.setenv('OCI_LOGAN_MONITORING_MAX_SERIES','25')
    settings=load_config(path,isolate_monitoring_errors=True)
    assert settings.monitoring_config_errors==()
    assert settings.require_monitoring_config().max_series==25
    assert settings.monitoring.region=='us-phoenix-1'


def test_invalid_yaml_is_not_laundered_into_valid_defaults_on_a_log_config_save(tmp_path):
    bad={'max_series':'private-marker','region':'us-phoenix-1'}
    path=config_file(tmp_path,bad)
    settings=load_config(path,isolate_monitoring_errors=True)
    settings.log_analytics.namespace='updated'
    save_config(settings,path)
    written=yaml.safe_load(path.read_text())
    assert written['log_analytics']['namespace']=='updated'
    assert written['monitoring']==bad
    assert 'monitoring_config_errors' not in written
    with pytest.raises(ValueError,match='monitoring.max_series'):
        load_config(path)


def test_invalid_environment_value_is_not_saved_into_yaml(tmp_path,monkeypatch):
    path=config_file(tmp_path,{'max_series':24})
    monkeypatch.setenv('OCI_LOGAN_MONITORING_MAX_SERIES','private-marker')
    settings=load_config(path,isolate_monitoring_errors=True)
    settings.log_analytics.namespace='updated'
    save_config(settings,path)
    written=yaml.safe_load(path.read_text())
    assert written['log_analytics']['namespace']=='updated'
    assert written['monitoring']['max_series']==24
    assert 'private-marker' not in path.read_text()
    assert settings.monitoring_config_errors==('monitoring.max_series',)


@pytest.mark.parametrize('monitoring',[False,0,[],['private-marker'],'private-marker'])
def test_malformed_monitoring_section_is_scoped_without_exposing_values(tmp_path,monitoring):
    path=config_file(tmp_path,monitoring)
    settings=load_config(path,isolate_monitoring_errors=True)
    assert settings.log_analytics.namespace=='logs'
    assert settings.monitoring_config_errors==('monitoring',)
    with pytest.raises(ValueError,match='monitoring') as caught:
        settings.require_monitoring_config()
    assert 'private-marker' not in str(caught.value)


@pytest.mark.asyncio
async def test_invalid_config_blocks_metric_queries_before_budget_or_sdk(tmp_path,monkeypatch):
    from tests.test_metrics_clients import RecordingSdk, make_client
    from oci_logan_mcp.budget_tracker import BudgetTracker, BudgetLimits
    from oci_logan_mcp.metrics.gateway import MonitoringGateway
    from oci_logan_mcp.metrics.runtime import MonitoringRuntime
    from oci_logan_mcp.metrics.validation import build_raw_plan

    settings=load_config(config_file(tmp_path,{'max_datapoints':0}),isolate_monitoring_errors=True)
    sdk=RecordingSdk()
    client=make_client(monkeypatch,sdk,settings)
    budget=BudgetTracker('isolation',BudgetLimits())
    gateway=MonitoringGateway(client,MonitoringRuntime(),budget)
    plan=build_raw_plan({'namespace':'custom','query':'Load[5m].mean()'})
    with pytest.raises(MetricsError) as caught:
        await gateway.query(MetricScope('chosen','us-phoenix-1'),plan,plan.queries[0],
                            deadline=asyncio.get_running_loop().time()+3)
    assert caught.value.code=='monitoring_config_invalid'
    assert caught.value.details=={'settings':['monitoring.max_datapoints']}
    assert sdk.monitor_calls==[] and budget.snapshot().queries==0


def test_strict_loader_traceback_does_not_echo_an_invalid_environment_value(tmp_path,monkeypatch):
    import traceback
    path=config_file(tmp_path,{'max_series':24})
    monkeypatch.setenv('OCI_LOGAN_MONITORING_MAX_SERIES','private-marker')
    with pytest.raises(ValueError,match='monitoring.max_series') as caught:
        load_config(path)
    assert 'private-marker' not in ''.join(traceback.format_exception(caught.value))
