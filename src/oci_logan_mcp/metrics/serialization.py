"""Preserve metric evidence and measure the actual compact MCP result encoding."""
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from typing import Any
from ..protocol_results import build_call_tool_result
from .models import MetricDefinition, MetricScope, MetricsError, QueryPlan, ResponseLimits
from .validation import RESOLUTIONS


def metrics_content(payload: dict, *, is_error: bool=False) -> list[dict]:
    try:
        text = json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    except (TypeError,ValueError):
        raise MetricsError('invalid_metrics_response','Monitoring returned values that cannot be represented safely.') from None
    return [{'type':'text','text':text,'is_error':is_error}]


def result_size(payload: dict, *, is_error: bool=False) -> int:
    result = build_call_tool_result(metrics_content(payload,is_error=is_error))
    return len(result.model_dump_json(by_alias=True,exclude_none=True).encode('utf-8'))


def _dimensions(value):
    if value is None:
        return {}
    if not isinstance(value,Mapping) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in value.items()):
        raise MetricsError('invalid_metrics_response','Monitoring returned invalid dimension metadata.')
    return dict(sorted(value.items()))


def normalize_metric_series(response: Any, *, scope: MetricScope, plan: QueryPlan,
                            query: str, definition: MetricDefinition | None=None) -> list[dict]:
    series = []
    bound = math.ceil((plan.end-plan.start)/RESOLUTIONS[plan.resolution])+1
    request_id = (getattr(response,'headers',None) or {}).get('opc-request-id')
    for item in response.data:
        native_points = item.aggregated_datapoints or []
        if len(native_points)>bound:
            raise MetricsError('backend_bound_mismatch','Monitoring returned more points than the validated series bound.',
                               details={'observed_points':len(native_points),'point_bound':bound})
        points = []
        for point in native_points:
            value, timestamp = point.value, point.timestamp
            if (type(value) not in (int,float) or not math.isfinite(value)
                    or not isinstance(timestamp,datetime) or timestamp.tzinfo is None):
                raise MetricsError('invalid_metrics_response','Monitoring returned invalid datapoints.')
            utc = timestamp.astimezone(timezone.utc)
            points.append((utc,[utc.isoformat().replace('+00:00','Z'),value]))
        points.sort(key=lambda pair:pair[0])
        identity = {'region':scope.region,'compartment_id':item.compartment_id or scope.compartment_id,
                    'namespace':item.namespace or plan.namespace,'metric_name':item.name,
                    'resource_group':item.resource_group,'dimensions':_dimensions(item.dimensions)}
        metadata = item.metadata or {}
        unit = metadata.get('unit') or None
        if unit is not None and not isinstance(unit,str):
            raise MetricsError('invalid_metrics_response','Monitoring returned invalid unit metadata.')
        exact = (definition is not None and identity['namespace']==definition.namespace
                 and identity['metric_name'] in definition.metric_names
                 and identity['resource_group']==definition.resource_group)
        warnings = []
        source = 'oci' if unit else 'unknown'
        if exact:
            if unit and unit!=definition.unit:
                warnings.append('unit_conflict')
            elif not unit:
                unit,source = definition.unit,'catalog'
        if unit is None:
            warnings.append('unit_unknown')
        row = {'identity':identity,'points':[pair[1] for pair in points],
               'unit':unit,'unit_source':source,'metadata':dict(metadata),'warnings':warnings,
               'query':query,'request_id':request_id,
               'compartment_source':'oci' if item.compartment_id else 'request_scope'}
        if exact:
            row.update(catalog_key=definition.metric_key,catalog_version=plan.catalog_version,
                       catalog_unit=definition.unit,source_urls=list(definition.source_urls))
        series.append(row)
    return series


def _identity_key(series):
    identity = series['identity']
    return tuple(identity.get(name) or '' for name in ('region','compartment_id','namespace','metric_name','resource_group')) + (
        tuple(sorted(identity['dimensions'].items())),)


def fit_series(series: Sequence[dict], *, context: dict, limits: ResponseLimits) -> dict:
    ordered = sorted(series,key=_identity_key)
    total_series = len(ordered)
    total_points = sum(len(item['points']) for item in ordered)
    admitted = []
    reasons = set()

    def payload_for(rows):
        point_count = sum(len(item['points']) for item in rows)
        dropped = total_series-len(rows)
        warnings = list(context.get('warnings',[]))
        for item in rows:
            for warning in item.get('warnings',[]):
                if warning not in warnings:
                    warnings.append(warning)
        if dropped:
            warnings.append('Complete-series caps omitted streams; this subset does not describe the whole fleet.')
        return {**context,'query_language':'mql','point_columns':['timestamp','value'],
                'series':list(rows),'status':context.get('status') or ('partial' if dropped else ('success' if point_count else 'no_data')),
                'returned_series_count':len(rows),'total_series_count':total_series,
                'returned_datapoint_count':point_count,'total_datapoint_count':total_points,
                'dropped_series_count':dropped,'dropped_datapoint_count':total_points-point_count,
                'complete':bool(context.get('complete',True)) and not dropped,
                'cap_reasons':sorted(reasons),'warnings':warnings}

    for item in ordered:
        if len(admitted)>=limits.max_series:
            reasons.add('series')
            continue
        if sum(len(row['points']) for row in admitted)+len(item['points'])>limits.max_datapoints:
            reasons.add('points')
            continue
        candidate = payload_for([*admitted,item])
        if result_size(candidate)>limits.max_response_bytes:
            reasons.add('bytes')
            continue
        admitted.append(item)
    payload = payload_for(admitted)
    # A later cap reason can grow the final metadata envelope by a few bytes.
    while admitted and result_size(payload)>limits.max_response_bytes:
        admitted.pop()
        reasons.add('bytes')
        payload = payload_for(admitted)
    if (ordered and not admitted) or result_size(payload)>limits.max_response_bytes:
        raise MetricsError('response_limit_exceeded','No complete metric response fits. Narrow dimensions or the window, or increase a permitted limit.',
                           details={'max_response_bytes':limits.max_response_bytes})
    return payload


def format_discovery(response: Any, *, scope: MetricScope, namespace: str | None,
                     filters: Mapping, group_namespaces: bool,
                     limits: ResponseLimits | None=None) -> dict:
    rows = list(response.data)
    headers = getattr(response,'headers',None) or {}
    payload = {'query_language':'mql','scope':asdict(scope),'namespace':namespace,
               'filters':dict(filters),'returned_count':len(rows),'page_complete':True,
               'next_page_token':headers.get('opc-next-page'),'request_id':headers.get('opc-request-id'),
               'more_available':bool(headers.get('opc-next-page')),
               'warnings':['One native page; no total inventory count is available.']}
    if group_namespaces:
        payload['namespaces'] = [row.namespace for row in rows]
    else:
        payload['metrics'] = [dict(namespace=row.namespace,name=row.name,resource_group=row.resource_group,
                                   compartment_id=row.compartment_id,dimensions=_dimensions(row.dimensions)) for row in rows]
    if result_size(payload)>(limits or ResponseLimits()).max_response_bytes:
        raise MetricsError('response_limit_exceeded','The complete discovery page exceeds the response limit. Lower the discovery limit.')
    return payload
