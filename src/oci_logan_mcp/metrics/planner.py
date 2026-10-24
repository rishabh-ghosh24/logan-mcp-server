"""Compile only reviewed catalog choices, preserving independent metric meaning."""
from collections.abc import Mapping
from dataclasses import replace
import json
import re
from .models import MetricDefinition, MetricsError, QueryPlan
from .validation import build_raw_plan, RESOLUTIONS


def plan_catalog_query(definition: MetricDefinition, arguments: Mapping, *, catalog_version: str) -> QueryPlan:
    identifiers = (definition.namespace, *definition.metric_names,
                   *definition.allowed_filter_dimensions, *definition.group_by_dimensions)
    if (not 1 <= len(definition.metric_names) <= 4 or any(
            not isinstance(value,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',value)
            for value in identifiers)):
        raise MetricsError('invalid_catalog_definition', 'This definition cannot be compiled by the reviewed catalog planner.')
    if 'namespace' in arguments or 'resource_group' in arguments:
        raise MetricsError('invalid_catalog_arguments', 'Catalog namespace and resource group are fixed. Use raw MQL for other values.')
    aggregation = arguments.get('aggregation', definition.default_aggregation)
    interval = arguments.get('interval', definition.default_interval)
    if aggregation not in definition.allowed_aggregations or aggregation not in ('mean','max','min','sum'):
        raise MetricsError('invalid_aggregation', 'Choose an aggregation allowed by this catalog entry.')
    if not isinstance(interval,str) or interval not in RESOLUTIONS:
        raise MetricsError('invalid_interval', 'Choose catalog interval 1m, 5m, 1h or 1d.')
    filters = arguments.get('dimension_filters', {})
    groups = arguments.get('group_by', [])
    if (not isinstance(filters, Mapping) or any(
            key not in definition.allowed_filter_dimensions or not isinstance(value,str)
            for key,value in filters.items())):
        raise MetricsError('invalid_dimension_filter', 'Use reviewed dimension names and exact text values.')
    if (not isinstance(groups,(list,tuple)) or any(not isinstance(key,str) for key in groups)
            or len(set(groups)) != len(groups) or not set(groups).issubset(definition.group_by_dimensions)):
        raise MetricsError('invalid_grouping', 'Choose distinct reviewed grouping dimensions.')
    selector = '{' + ','.join(key+'='+json.dumps(value,ensure_ascii=False) for key,value in sorted(filters.items())) + '}' if filters else ''
    grouping = '.groupBy(' + ','.join(groups) + ')' if groups else ''
    expressions = tuple(f'{name}[{interval}]{selector}{grouping}.{aggregation}()' for name in definition.metric_names)
    raw_arguments = {name:arguments[name] for name in ('time_start','time_end','time_range') if name in arguments}
    raw_arguments.update(namespace=definition.namespace,query=expressions[0],
        resolution=arguments.get('resolution',interval),resource_group=definition.resource_group)
    plan = build_raw_plan(raw_arguments)
    return replace(plan,queries=expressions,catalog_key=definition.metric_key,catalog_version=catalog_version,
                   resolution_source='explicit' if arguments.get('resolution') is not None else 'catalog_interval')
