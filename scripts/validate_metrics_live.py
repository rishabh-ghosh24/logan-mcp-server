#!/usr/bin/env python3
"""Validate Monitoring through MCP and an independent SDK request in private paths."""
import argparse
import asyncio
import configparser
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import oci
import yaml

from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.config import _parse_config
from oci_logan_mcp.metrics.catalog import MetricCatalog
from oci_logan_mcp.metrics.resources import read_monitoring_resource


def packaged_templates():
    document=read_monitoring_resource('monitoring://query-templates')
    catalog=MetricCatalog.from_package()
    if not isinstance(document,dict) or document.get('query_language')!='mql' or not document.get('templates'):
        raise ValueError('Invalid packaged Monitoring templates.')
    for row in document['templates']:
        if (row.get('query_language')!='mql' or not row.get('source_urls')
                or any(not isinstance(url,str) or not url.startswith('https://docs.oracle.com/') for url in row['source_urls'])
                or row.get('tool') not in ('query_metrics','query_catalog_metric')):
            raise ValueError('Unverified Monitoring starter metadata.')
        if row['tool']=='query_catalog_metric':
            try:
                definition=catalog.require(row['arguments']['metric_key'])
            except (KeyError,ValueError):
                raise ValueError('Unreviewed Monitoring starter key.') from None
            if (row.get('catalog_key')!=definition.metric_key or row.get('catalog_version')!=catalog.version
                    or row.get('namespace')!=definition.namespace or row.get('expected_unit')!=definition.unit
                    or not set(row['source_urls']).issubset(definition.source_urls)):
                raise ValueError('Inconsistent Monitoring starter metadata.')
    return document['templates']


def write_private(path,content):
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as output:
        output.write(content)


def private_settings(arguments,home):
    raw=yaml.safe_load(arguments.config.read_text())
    if not isinstance(raw,dict):
        raise ValueError('Provide a YAML configuration object.')
    # Parse without invoking the normal loader's legacy-directory migration.
    settings=_parse_config(raw)
    settings.monitoring.default_compartment_id=arguments.compartment_id
    if arguments.region:
        settings.monitoring.region=arguments.region
    if settings.oci.auth_type=='config_file':
        config=oci.config.from_file(str(settings.oci.config_path.expanduser()),settings.oci.profile)
        profile={key:str(value) for key,value in config.items() if isinstance(value,(str,int,float,bool))}
        for key in ('key_file','security_token_file'):
            if key in profile:
                profile[key]=str(Path(profile[key]).expanduser().resolve())
        parser=configparser.ConfigParser(interpolation=None)
        parser['DEFAULT']=profile
        import io
        stream=io.StringIO();parser.write(stream)
        settings.oci.config_path=home/'oci-config'
        settings.oci.profile='DEFAULT'
        write_private(settings.oci.config_path,stream.getvalue())
    settings.logging.log_path=home/'logs'
    settings.report_delivery.artifact_dir=home/'reports'
    directory=home/'.oci-logan-mcp';directory.mkdir(mode=0o700)
    write_private(directory/'config.yaml',yaml.safe_dump(settings.to_dict(),sort_keys=False))
    return settings


def _signature(rows):
    return sorted(rows,key=lambda row:json.dumps(row['identity'],sort_keys=True))


def sdk_signature(response,*,region,compartment,namespace):
    rows=[]
    for row in response.data:
        identity={'region':region,'compartment_id':row.compartment_id or compartment,
            'namespace':row.namespace or namespace,'metric_name':row.name,
            'resource_group':row.resource_group,'dimensions':dict(sorted((row.dimensions or {}).items()))}
        points=sorted([[point.timestamp.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),point.value]
            for point in row.aggregated_datapoints or []],key=lambda pair:pair[0])
        rows.append({'identity':identity,'points':points})
    return _signature(rows)


async def validate_metrics(arguments,summary,private):
    home=arguments.output_dir/'home';home.mkdir(mode=0o700)
    settings=private_settings(arguments,home)
    client=OCILogAnalyticsClient(settings)
    region=arguments.region or settings.monitoring.region or client._config.get('region') or getattr(client._signer,'region',None)
    scope={'compartment_id':arguments.compartment_id,'region':region,'include_subcompartments':False}
    environment={key:value for key,value in os.environ.items()
        if not key.startswith(('OCI_LA_','OCI_LOGAN_','OCI_CONFIG_')) and key!='LOGAN_SMOKE_COMMAND'}
    environment.update(HOME=str(home),USERPROFILE=str(home),PYTHONDONTWRITEBYTECODE='1',MPLCONFIGDIR=str(home/'matplotlib'))
    params=StdioServerParameters(command=arguments.python,
        args=['-m','oci_logan_mcp','--read-only','--user','metrics-validation'],env=environment,cwd=str(home))
    private['tool_sequence']=[]
    private['calls']=[]
    async def invoke(session,name,args):
        private['tool_sequence'].append(name)
        result=await session.call_tool(name,args)
        payload=json.loads(next(item.text for item in result.content if item.type=='text'))
        private['calls'].append({'tool':name,'arguments':args,'is_error':result.isError,'result':payload})
        return payload,result.isError
    def reason(payload):
        error=payload.get('error')
        return error.get('code','service_error') if isinstance(error,dict) else 'service_unavailable'
    stderr=arguments.output_dir/'server.stderr'
    descriptor=os.open(stderr,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as errors:
        async with asyncio.timeout(300):
            async with stdio_client(params,errlog=errors) as (read,write):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    namespaces,failed=await invoke(session,'list_metric_namespaces',{**scope,'limit':50})
                    if failed or not namespaces.get('namespaces'):
                        summary['prerequisite']='namespace_inspection_unavailable'
                        return 2
                    visible=namespaces['namespaces']
                    namespace='oci_computeagent' if 'oci_computeagent' in visible else visible[0]
                    definitions,failed=await invoke(session,'list_metrics',{**scope,'namespace':namespace,'limit':10})
                    if failed or not definitions.get('metrics'):
                        summary['prerequisite']='metric_definitions_unavailable'
                        return 2
                    stream=definitions['metrics'][0]
                    metric=stream['name']
                    if not re.fullmatch(r'[A-Za-z_]\w*',metric):
                        summary['prerequisite']='no_supported_metric_identifier'
                        return 2
                    filters={key:value for key,value in stream.get('dimensions',{}).items()
                        if re.fullmatch(r'[A-Za-z_]\w*',key) and not any(char in value for char in ('"','\\','*','|'))}
                    selector='{'+','.join(key+'='+json.dumps(value) for key,value in sorted(filters.items()))+'}' if filters else ''
                    query=metric+'[5m]'+selector+'.mean()'
                    end=(datetime.now(timezone.utc)-timedelta(minutes=5)).replace(second=0,microsecond=0)
                    start=end-timedelta(hours=1)
                    window={'time_start':start.isoformat(),'time_end':end.isoformat()}
                    catalog,failed=await invoke(session,'search_metric_catalog',{'query':'cpu'})
                    if failed:
                        summary['catalog_search']='failed'
                        return 1
                    raw={**scope,**window,'namespace':namespace,'query':query,'resolution':'5m'}
                    if stream.get('resource_group') is not None:
                        raw['resource_group']=stream['resource_group']
                    payload,failed=await invoke(session,'query_metrics',raw)
                    if failed:
                        summary['prerequisite']=reason(payload)
                        return 2 if isinstance(payload.get('error'),dict) and payload['error'].get('status') in (401,403,404) else 1
                    if payload.get('returned_datapoint_count',0)>0:
                        summary['positive_query_count']+=1
                        assert payload['scope']['compartment_id']==scope['compartment_id'] and payload['scope']['region']==region
                        assert payload['complete'] is True,'Comparison query must cover its whole selected scope.'
                        sdk=client.monitoring_client_for_region(region,timeout_seconds=settings.monitoring.timeout_seconds)
                        details=oci.monitoring.models.SummarizeMetricsDataDetails(namespace=namespace,query=query,
                            resource_group=raw.get('resource_group'),start_time=start,end_time=end,resolution='5m')
                        response=await asyncio.to_thread(sdk.summarize_metrics_data,compartment_id=arguments.compartment_id,
                            summarize_metrics_data_details=details,compartment_id_in_subtree=False,retry_strategy=oci.retry.NoneRetryStrategy())
                        native=sdk_signature(response,region=region,compartment=arguments.compartment_id,namespace=namespace)
                        protocol=_signature([{'identity':row['identity'],'points':row['points']} for row in payload['series']])
                        equal=native==protocol
                        private['sdk_comparison']={'equal':equal,'point_count':sum(len(row['points']) for row in native),
                            'arguments':raw,'native':native,'protocol':protocol}
                        summary['direct_comparison']='passed' if equal else 'failed'
                    failures=summary['direct_comparison']=='failed'
                    for starter in packaged_templates():
                        row={'name':starter['name'],'status':'unverified','reason':'namespace_not_in_discovered_page'}
                        if starter.get('namespace') in visible and not starter.get('required_substitutions'):
                            result,failed=await invoke(session,starter['tool'],{**scope,**starter['arguments']})
                            if failed:
                                status=(result.get('error') or {}).get('status') if isinstance(result.get('error'),dict) else None
                                row.update(status='unverified' if status in (401,403,404) else 'failed',reason=reason(result))
                                failures=failures or row['status']=='failed'
                            elif result.get('returned_datapoint_count',0)>0:
                                row.update(status='verified',reason='positive_data',complete=bool(result.get('complete')))
                                summary['positive_query_count']+=1
                            else:
                                row['reason']='no_data'
                        summary['starter_results'].append(row)
                    if failures:
                        return 1
                    return 0 if summary['positive_query_count'] and summary['direct_comparison']=='passed' else 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--compartment-id',required=True)
    parser.add_argument('--region')
    parser.add_argument('--output-dir',type=Path,required=True)
    arguments=parser.parse_args(argv)
    interpreter=shutil.which(os.path.expanduser(arguments.python))
    if interpreter is None:
        parser.error('--python must name an executable file or a command on PATH.')
    # Keep virtual-environment symlinks while fixing the caller's working directory.
    arguments.python=os.path.abspath(interpreter)
    arguments.output_dir.mkdir(mode=0o700,parents=True,exist_ok=False)
    summary={'status':'pending','sdk_version':oci.__version__,'positive_query_count':0,
        'direct_comparison':'unverified','starter_results':[],
        'coverage_note':'Live results cover only the recorded streams and windows. Official catalog review is separate.'}
    private={}
    try:
        code=asyncio.run(validate_metrics(arguments,summary,private))
    except Exception as error:
        summary['execution_error']=type(error).__name__
        code=2 if isinstance(error,(oci.exceptions.ServiceError,oci.exceptions.ConfigFileNotFound)) else 1
    summary['status']={0:'passed',1:'failed',2:'pending'}[code]
    write_private(arguments.output_dir/'private-results.json',json.dumps(private,indent=2))
    write_private(arguments.output_dir/'review-summary.json',json.dumps(summary,indent=2))
    print(json.dumps(summary,separators=(',',':')))
    return code


if __name__=='__main__':
    raise SystemExit(main())
