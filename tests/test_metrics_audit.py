"""Metrics privacy applies even when legacy free-text audit retention is enabled."""
import hashlib
import json

import oci
import pytest

from tests.test_metrics_service import make_handlers, raw_arguments, call


@pytest.mark.asyncio
@pytest.mark.parametrize('retained', [True, False])
async def test_metrics_query_fingerprints_and_client_claims_without_plaintext(tmp_path, monkeypatch, caplog, retained):
    handlers, sdk = make_handlers(tmp_path, monkeypatch, retained=retained)
    query='CpuUtilization[5m]{resourceId="PRIVATE-DIMENSION-123"}.mean()'
    args=raw_arguments(query=query, resource_group='PRIVATE-RESOURCE-GROUP')
    claimed={'claimed_client':'metric-client','claimed_version':'7','source_ip_truncated':None}
    before_preferences=list((tmp_path/'users').rglob('preferences*'))
    payload, error=await call(handlers, arguments=args, client_metadata=claimed)
    assert not error and payload['returned_datapoint_count']==2
    sdk.error=oci.exceptions.ServiceError(400,'InvalidParameter',{'opc-request-id':'safe-id'},query)
    failed, error=await call(handlers, arguments=args, client_metadata=claimed)
    assert error and failed['error']['status']==400
    events=list(handlers.audit_logger.iter_entries())
    assert len(events)==4 and [event['outcome'] for event in events]==['invoked','executed','invoked','executed']
    assert all(event['client_metadata']==claimed for event in events)
    encoded=json.dumps(events)+caplog.text+json.dumps(failed)
    assert 'PRIVATE-DIMENSION-123' not in encoded and 'PRIVATE-RESOURCE-GROUP' not in encoded
    for event in events:
        fingerprint=event['args_redacted']['query_fingerprint']
        assert fingerprint['sha256']==hashlib.sha256(query.encode()).hexdigest() and fingerprint['length']==len(query)
    assert handlers.query_logger.get_recent_queries()==[]
    assert handlers.query_logger._history_path.read_text()==''
    assert before_preferences==list((tmp_path/'users').rglob('preferences*'))


def test_audit_helpers_preserve_safe_fields_and_fingerprint_dimension_values():
    from oci_logan_mcp.metrics.audit import metric_audit_arguments, metric_audit_summary
    result=metric_audit_arguments('query_catalog_metric',{'metric_key':'cpu_utilization','interval':'5m',
        'namespace':'oci_computeagent','dimension_filters':{'resourceId':'private-value'},'ignored':'private-extra'})
    assert result['metric_key']=='cpu_utilization' and result['interval']=='5m'
    assert result['dimension_filters']['resourceId']=={'sha256':hashlib.sha256(b'private-value').hexdigest(),'length':13}
    assert 'private-value' not in json.dumps(result) and 'ignored' not in result
    summary=metric_audit_summary({'status':'partial','complete':False,'returned_series_count':2,
        'returned_datapoint_count':17,'query':'private-query','error':{'code':'monitoring_timeout','message':'private-error'}},execution_ms=25)
    assert summary=={'status':'partial','complete':False,'returned_series_count':2,
        'returned_datapoint_count':17,'execution_ms':25,'success':False,'error_code':'monitoring_timeout'}
