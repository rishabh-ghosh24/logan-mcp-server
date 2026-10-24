"""Validate bounded requests without claiming to implement the whole MQL grammar."""
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import math
import re
from ..config import MonitoringConfig
from ..time_parser import parse_time_range
from .models import MetricsError, QueryPlan, ResponseLimits

RESOLUTIONS = {'1m': timedelta(minutes=1), '5m': timedelta(minutes=5),
               '1h': timedelta(hours=1), '1d': timedelta(days=1)}
RETENTION_DAYS = {'1m': 7, '5m': 30, '1h': 90, '1d': 90}


def _unquoted(query: str) -> str:
    output = []
    quote = None
    index = 0
    while index < len(query):
        char = query[index]
        if quote:
            output.append(' ')
            if char == '\\':
                index += 1
                if index < len(query):
                    output.append(' ')
            elif char == quote:
                if index + 1 < len(query) and query[index + 1] == quote:
                    output.append(' ')
                    index += 1
                else:
                    quote = None
        elif char in ('"', "'"):
            quote = char
            output.append(' ')
        else:
            output.append(char)
        index += 1
    if quote:
        raise MetricsError('invalid_mql', 'Close every quoted MQL value before querying.')
    return ''.join(output)


def parse_mql_intervals(query: str) -> tuple[timedelta, ...]:
    if (not isinstance(query, str) or not query.strip() or len(query) > 4096
            or any((ord(c) < 32 and c not in '\n\r\t') or ord(c) == 127 for c in query)):
        raise MetricsError('invalid_mql', 'Provide MQL of at most 4096 characters without control characters.')
    text = _unquoted(query)
    operands = list(re.finditer(r'\b[A-Za-z_]\w*\s*(?:\[([^\]]*)\])?\s*(?:\{[^}]*\}\s*)?\.', text))
    intervals = []
    for operand in operands:
        token = (operand.group(1) or '').strip()
        match = re.fullmatch(r'([1-9]\d*)(m|h|d)', token)
        if not match:
            raise MetricsError('invalid_interval', 'Every metric operand needs a supported numeric interval.')
        count, unit = int(match[1]), match[2]
        if count > {'m':60, 'h':24, 'd':1}[unit]:
            raise MetricsError('invalid_interval', 'Use intervals from 1m to 60m, 1h to 24h, or 1d.')
        intervals.append(timedelta(seconds=count * {'m':60,'h':3600,'d':86400}[unit]))
    if not intervals:
        raise MetricsError('invalid_interval', 'Could not determine metric intervals. Use explicit metric[interval] operands.')
    return tuple(intervals)


def limits_from_args(arguments: Mapping, config: MonitoringConfig) -> ResponseLimits:
    if 'max_response_bytes' in arguments:
        raise MetricsError('invalid_limits', 'Response bytes are controlled by Monitoring configuration.')
    values = {}
    for name, maximum in (('max_series',100), ('max_datapoints',10000)):
        value = arguments.get(name, getattr(config,name))
        if type(value) is not int or not 1 <= value <= maximum:
            raise MetricsError('invalid_limits', f'{name} must be an integer from 1 to {maximum}.')
        values[name] = value
    return ResponseLimits(**values, max_response_bytes=config.max_response_bytes)


def build_raw_plan(arguments: Mapping) -> QueryPlan:
    namespace = arguments.get('namespace')
    if not isinstance(namespace,str) or not namespace.strip() or len(namespace)>256 or any(ord(c)<32 for c in namespace):
        raise MetricsError('invalid_namespace', 'Provide a metric namespace from discovery.')
    query = arguments.get('query')
    intervals = parse_mql_intervals(query)
    start_arg, end_arg = arguments.get('time_start'), arguments.get('time_end')
    if bool(start_arg) != bool(end_arg):
        raise MetricsError('invalid_time_range', 'Provide both time_start and time_end, or a relative time_range.')
    try:
        start, end = parse_time_range(time_start=start_arg, time_end=end_arg, time_range=arguments.get('time_range'))
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise MetricsError('invalid_time_range', 'Provide valid UTC timestamps or a supported relative time_range.') from None
    resolution = arguments.get('resolution')
    source = 'explicit'
    if resolution is None:
        resolution = next((key for key,delta in reversed(RESOLUTIONS.items()) if delta<=min(intervals)), None)
        source = 'query_interval'
    if resolution not in RESOLUTIONS:
        raise MetricsError('invalid_resolution', 'Use resolution 1m, 5m, 1h or 1d.')
    if RESOLUTIONS[resolution] > min(intervals):
        raise MetricsError('resolution_exceeds_interval', 'Resolution cannot exceed any metric operand interval.')
    resource_group = arguments.get('resource_group')
    if resource_group is not None and (not isinstance(resource_group,str) or len(resource_group)>256 or any(ord(c)<32 for c in resource_group)):
        raise MetricsError('invalid_resource_group', 'Provide a valid resource group from discovery.')
    return QueryPlan(namespace,(query,),start,end,resolution,source,resource_group)


def validate_plan(plan: QueryPlan, limits: ResponseLimits, *, now: datetime, max_window_hours: int) -> None:
    if plan.start >= plan.end or plan.end > now:
        raise MetricsError('invalid_time_range', 'Use a nonempty ordered window ending no later than now.')
    duration = plan.end - plan.start
    if duration > timedelta(hours=max_window_hours):
        raise MetricsError('window_exceeded', 'Narrow the metric window to the configured maximum.', details={'max_window_hours':max_window_hours})
    if plan.resolution not in RESOLUTIONS:
        raise MetricsError('invalid_resolution', 'Use resolution 1m, 5m, 1h or 1d.')
    if not 1 <= len(plan.queries) <= 4:
        raise MetricsError('invalid_mql', 'Provide between one and four metric expressions.')
    intervals = tuple(delta for query in plan.queries for delta in parse_mql_intervals(query))
    resolution = RESOLUTIONS[plan.resolution]
    if resolution > min(intervals):
        raise MetricsError('resolution_exceeds_interval', 'Resolution cannot exceed any metric operand interval.')
    if plan.start < now - timedelta(days=RETENTION_DAYS[plan.resolution]):
        raise MetricsError('retention_exceeded', 'The start is outside retention for this resolution.', details={'retention_days':RETENTION_DAYS[plan.resolution]})
    bound = math.ceil(duration / resolution) + 1
    if bound > limits.max_datapoints:
        details = {'point_bound':bound, 'max_datapoints':limits.max_datapoints}
        if bound <= 10000:
            details['required_max_datapoints'] = bound
        for key,delta in RESOLUTIONS.items():
            if (delta > resolution and math.ceil(duration/delta)+1 <= limits.max_datapoints
                    and plan.start >= now-timedelta(days=RETENTION_DAYS[key])):
                details['suggested_resolution'] = key
                if delta > min(intervals):
                    details['required_min_interval'] = key
                break
        raise MetricsError('point_limit_exceeded',
            'One complete series exceeds the point cap. Increase the explicit cap or use the suggested resolution with compatible intervals.', details=details)
