"""Freeze existing public contracts while adding five bounded Monitoring tools."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from oci_logan_mcp.tools import get_tools
from oci_logan_mcp.resources import get_resources
from oci_logan_mcp.server import build_tool_list
from oci_logan_mcp.read_only_guard import MUTATING_TOOLS, OUTBOUND_TOOLS

NAMES={'list_metric_namespaces','list_metrics','search_metric_catalog','query_catalog_metric','query_metrics'}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def test_public_metric_schemas_and_read_only_classification():
    tools={tool['name']:tool for tool in get_tools()}
    assert NAMES <= tools.keys() and len(tools)==69
    expected={'list_metric_namespaces':[], 'list_metrics':['namespace'],
        'search_metric_catalog':['query'],'query_catalog_metric':['metric_key'],'query_metrics':['namespace','query']}
    for name,required in expected.items():
        assert tools[name]['inputSchema'].get('required',[])==required
    for tool in build_tool_list():
        if tool.name in NAMES:
            assert tool.title and tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False and tool.annotations.openWorldHint is False
    assert NAMES.isdisjoint(MUTATING_TOOLS | OUTBOUND_TOOLS)
    assert len(json.dumps(get_tools()))<=80000


@pytest.mark.parametrize('name',['query_catalog_metric','query_metrics'])
def test_query_schemas_expose_response_caps_and_no_fixed_resolution_default(name):
    tool=next(tool for tool in get_tools() if tool['name']==name)
    props=tool['inputSchema']['properties']
    assert props['max_datapoints']['default']==4000 and props['max_datapoints']['maximum']==10000
    assert props['max_series']['default']==20 and props['max_series']['maximum']==100
    assert 'default' not in props['resolution']
    assert props['include_subcompartments']['default'] is False
    assert set(props['resolution']['enum'])=={'1m','5m','1h','1d'}


def test_existing_tool_resource_and_policy_contracts_match_reviewed_baseline():
    snapshot=json.loads((Path(__file__).parent/'fixtures'/'legacy_tool_contracts.json').read_text())
    assert snapshot['commit']=='2accd691d0ccdde580e3f35c00a57c9bdf918093'
    tools=deepcopy(get_tools())
    connection=next(tool for tool in tools if tool['name']=='test_connection')
    service=connection['inputSchema']['properties'].pop('service')
    assert service['enum']==['logs','metrics','all'] and service['default']=='logs'
    actual={tool['name']:digest(tool) for tool in tools if tool['name'] not in NAMES}
    assert actual==snapshot['tools'] and len(actual)==64
    legacy={resource['uri']:digest(resource) for resource in get_resources() if resource['uri'].startswith('loganalytics://')}
    assert legacy==snapshot['resources'] and len(get_resources())==8
    assert sorted(MUTATING_TOOLS)==snapshot['mutating_tools'] and sorted(OUTBOUND_TOOLS)==snapshot['outbound_tools']


def test_monitoring_help_is_packaged_separately_from_log_templates():
    from oci_logan_mcp.metrics.resources import read_monitoring_resource
    guide=read_monitoring_resource('monitoring://syntax-guide')
    templates=read_monitoring_resource('monitoring://query-templates')
    assert 'MQL' in guide and 'lbComponent' in guide
    assert templates['query_language']=='mql' and templates['templates']
    assert all(item['query_language']=='mql' and item['source_urls'] for item in templates['templates'])
    lb=[item for item in templates['templates'] if item.get('namespace')=='oci_lbaas']
    assert lb and all(item['arguments']['dimension_filters']['lbComponent']=='loadBalancer' for item in lb)
    with pytest.raises(ValueError):
        read_monitoring_resource('monitoring://unknown')


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments',[{}, {'namespace':True}, {'namespace':''}])
async def test_metric_discovery_requires_namespace_before_budget_or_sdk(tmp_path,monkeypatch,arguments):
    from tests.test_metrics_service import make_handlers,call
    handlers,sdk=make_handlers(tmp_path,monkeypatch)
    payload,error=await call(handlers,'list_metrics',arguments)
    assert error and payload['error']['code']=='invalid_namespace'
    assert sdk.calls==[] and handlers._budget_tracker.snapshot().queries==0
