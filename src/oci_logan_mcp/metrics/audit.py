"""Keep metrics text out of operational audit records under both retention modes."""
from collections.abc import Mapping
import re
from ..sanitize import AuditFingerprint

METRIC_TOOLS = frozenset({
    'list_metric_namespaces', 'list_metrics', 'search_metric_catalog',
    'query_catalog_metric', 'query_metrics',
})


def _fingerprint(value):
    text = value if isinstance(value, str) else str(type(value).__name__)
    return AuditFingerprint(text)


def metric_audit_arguments(name: str, arguments: Mapping) -> dict:
    result = {}
    for key in ('metric_key', 'namespace', 'interval', 'aggregation', 'resolution', 'region', 'service'):
        value = arguments.get(key)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,255}', value):
            result[key] = value
    for key in ('include_subcompartments', 'max_series', 'max_datapoints', 'limit'):
        if type(arguments.get(key)) in (bool, int):
            result[key] = arguments[key]
    for key in ('query', 'resource_group', 'compartment_id', 'page_token', 'time_start', 'time_end', 'time_range'):
        if key in arguments:
            result[key + '_fingerprint'] = _fingerprint(arguments[key])
    filters = arguments.get('dimension_filters')
    if isinstance(filters, Mapping):
        result['dimension_filters'] = {
            key if isinstance(key, str) and re.fullmatch(r'[A-Za-z_]\w{0,255}', key) else
            'key_' + _fingerprint(key)['sha256']: _fingerprint(value)
            for key, value in filters.items()
        }
    return result


def metric_audit_summary(payload: Mapping, *, execution_ms: int) -> dict:
    result = {'execution_ms': execution_ms, 'success': not bool(payload.get('error'))}
    status = payload.get('status')
    if status in ('success', 'no_data', 'partial', 'error', 'cancelled'):
        result['status'] = status
    for key in ('complete', 'returned_series_count', 'returned_datapoint_count', 'returned_count', 'matched_count'):
        if type(payload.get(key)) in (bool, int):
            result[key] = payload[key]
    error = payload.get('error')
    if isinstance(error, Mapping):
        code = error.get('code')
        if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,64}', code):
            result['error_code'] = code
    return result
