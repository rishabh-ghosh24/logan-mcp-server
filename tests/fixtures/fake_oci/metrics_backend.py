"""Test-only Monitoring backend. Every OCI request remains inside the fake shim."""
import json
import os
from pathlib import Path
import time

import oci
from oci.response import Response


def _record(operation, compartment_id, details, **extra):
    directory=os.environ.get('LOGAN_FAKE_METRICS_DIR')
    if directory:
        row={'operation':operation,'compartment_id':compartment_id,'namespace':details.namespace,**extra}
        with (Path(directory)/'requests.jsonl').open('a') as output:
            output.write(json.dumps(row)+'\n')


class MonitoringClient:
    def __init__(self,config,signer,**kwargs):
        self.region=config['region']

    def list_metrics(self,compartment_id,list_metrics_details,**kwargs):
        details=list_metrics_details
        _record('list',compartment_id,details,region=self.region,page=kwargs.get('page'))
        row=oci.monitoring.models.Metric(namespace=details.namespace or 'oci_computeagent',
            name=details.name or 'CpuUtilization',compartment_id=compartment_id,
            resource_group=details.resource_group,dimensions={'resourceId':'fake-instance'})
        return Response(200,{'opc-next-page':'fake-next-page','opc-request-id':'fake-discovery'},[row],None)

    def summarize_metrics_data(self,compartment_id,summarize_metrics_data_details,**kwargs):
        details=summarize_metrics_data_details
        _record('query',compartment_id,details,region=self.region,query=details.query,resolution=details.resolution)
        mode=os.environ.get('LOGAN_FAKE_METRICS_MODE','success')
        if mode=='gated':
            directory=Path(os.environ['LOGAN_FAKE_METRICS_DIR'])
            marker=directory/'starting'
            marker.write_text(compartment_id)
            os.replace(marker,directory/'started')
            deadline=time.monotonic()+10
            while not (directory/'release').exists():
                if time.monotonic()>=deadline:
                    raise TimeoutError('Fake metrics barrier was not released')
                time.sleep(.02)
        if mode in ('denied','invalid'):
            raise oci.exceptions.ServiceError(403 if mode=='denied' else 400,
                'NotAuthorizedOrNotFound' if mode=='denied' else 'InvalidParameter',
                {'opc-request-id':'fake-safe-request'},'PRIVATE-OCI-FAILURE '+details.query)
        rows=[]
        if mode!='no_data':
            rows=[oci.monitoring.models.MetricData(namespace=details.namespace,
                name=details.query.split('[')[0],compartment_id=compartment_id,
                resource_group=details.resource_group,dimensions={'resourceId':'fake-instance'},
                metadata={'unit':'percent'},aggregated_datapoints=[
                    oci.monitoring.models.AggregatedDatapoint(timestamp=details.start_time,value=0),
                    oci.monitoring.models.AggregatedDatapoint(timestamp=details.end_time,value=73.5)])]
        return Response(200,{'opc-request-id':'fake-query'},rows,None)


def install():
    oci.monitoring.MonitoringClient=MonitoringClient
    original=oci.log_analytics.LogAnalyticsClient
    def lazy_la(*args,**kwargs):
        if os.environ.get('LOGAN_FAKE_LA_FAIL')=='1':
            raise RuntimeError('Fake Log Analytics is unavailable')
        return original(*args,**kwargs)
    oci.log_analytics.LogAnalyticsClient=lazy_la
