"""Orchestrate bounded Monitoring operations without Log Analytics dependencies."""
import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone

from ..budget_tracker import BudgetExceededError
from .catalog import MetricCatalog
from .gateway import MonitoringGateway
from .models import MetricResult, MetricsError
from .planner import plan_catalog_query
from .scope import resolve_metric_scope
from .serialization import fit_series, format_discovery, normalize_metric_series, result_size
from .validation import build_raw_plan, limits_from_args, validate_plan


class MetricsService:
    def __init__(self, settings, client, budget, runtime, *, catalog_loader=MetricCatalog.from_package):
        self.settings, self.client, self.budget = settings, client, budget
        self.gateway = MonitoringGateway(client, runtime, budget)
        self._catalog_loader = catalog_loader
        self._catalog = None
        self._last_tested = None
        self._inspect_authorized = None
        self._read_authorized = None
        self._inspect_scope = None
        self._read_scope = None

    def _load_catalog(self):
        if self._catalog is None:
            try:
                self._catalog = self._catalog_loader()
            except Exception:
                raise MetricsError('catalog_unavailable', 'The reviewed metric catalog is unavailable. Raw MQL remains available.') from None
        return self._catalog

    def readiness(self) -> dict:
        errors = list(self.settings.monitoring_config_errors)
        return {'configured': not errors, 'config_errors': errors,
                'client_created': bool(self.client.__dict__.get('_regional_monitoring_clients')),
                'last_tested': self._last_tested,
                'inspect_authorized': self._inspect_authorized, 'inspect_scope': self._inspect_scope,
                'read_authorized': self._read_authorized, 'read_scope': self._read_scope}

    def _scope(self, arguments, shared_compartment_id):
        return resolve_metric_scope(arguments, self.settings, shared_compartment_id=shared_compartment_id,
            oci_config=self.client._config, signer=self.client._signer)

    def _budget_context(self):
        return {'cost_estimate': {'service': 'monitoring', 'estimated': False},
                'budget': self.budget.snapshot().to_dict(), 'budget_remaining': self.budget.remaining(),
                'budget_estimate_scope': 'Session bytes and cost estimate Log Analytics work; Monitoring calls consume query slots without a cost estimate.'}

    @staticmethod
    def _failure(error):
        if isinstance(error, BudgetExceededError):
            error = MetricsError('budget_exceeded', 'The session request budget is exhausted. Start a new session or review its limits.')
        if not isinstance(error, MetricsError):
            error = MetricsError('monitoring_unavailable', 'Monitoring could not complete the operation. Check configuration and service access, then retry explicitly.')
        return {'code': error.code, 'message': str(error), 'status': error.status,
                'request_id': error.request_id, **error.details}

    async def call(self, name: str, arguments: Mapping, *, shared_compartment_id: str | None) -> MetricResult:
        try:
            # Invalid Monitoring settings take precedence over scope or budget failures.
            config = self.gateway.require_configuration()
            if name == 'search_metric_catalog':
                catalog = self._load_catalog()
                payload = catalog.search(arguments.get('query'), namespace=arguments.get('namespace'),
                    resource_group=arguments.get('resource_group'), limit=arguments.get('limit', 25))
                payload['status'] = 'success' if payload['matches'] else 'no_data'
                if result_size(payload) > config.max_response_bytes:
                    raise MetricsError('response_limit_exceeded', 'Narrow the catalog search or lower its limit.')
                return MetricResult(payload)
            scope = self._scope(arguments, shared_compartment_id)
            limits = limits_from_args(arguments, config)
            deadline = asyncio.get_running_loop().time() + config.timeout_seconds
            if name in ('list_metric_namespaces', 'list_metrics'):
                namespaces = name == 'list_metric_namespaces'
                filters = {key: arguments.get(key) for key in ('namespace', 'metric_name', 'resource_group', 'dimension_filters')}
                namespace = filters['namespace']
                if not namespaces and (not isinstance(namespace, str) or not namespace.strip()
                        or len(namespace) > 256 or any(ord(char) < 32 for char in namespace)):
                    raise MetricsError('invalid_namespace', 'Provide a metric namespace from discovery.')
                response = await self.gateway.discover(scope, **filters, limit=arguments.get('limit', 50),
                    page_token=arguments.get('page_token'), group_namespaces=namespaces, deadline=deadline)
                self._inspect_authorized = True
                self._inspect_scope = asdict(scope)
                payload = format_discovery(response, scope=scope, namespace=filters['namespace'], filters=filters,
                    group_namespaces=namespaces, limits=limits)
                payload.update(self._budget_context(), status='success' if payload['returned_count'] else 'no_data')
                if result_size(payload) > limits.max_response_bytes:
                    raise MetricsError('response_limit_exceeded', 'Lower the discovery limit to fit the complete page.')
                return MetricResult(payload)
            definition = None
            if name == 'query_catalog_metric':
                catalog = self._load_catalog()
                definition = catalog.require(arguments.get('metric_key'))
                filters = arguments.get('dimension_filters', {})
                if isinstance(filters, Mapping) and any(
                        isinstance(value, str) and any(char in ('"', '\\') or ord(char) < 32 or ord(char) == 127
                            for char in value) for value in filters.values()):
                    raise MetricsError('unsupported_exact_filter',
                        'Catalog exact filters do not support quotes, backslashes or control characters. Use raw MQL for expert expressions.')
                plan = plan_catalog_query(definition, arguments, catalog_version=catalog.version)
            elif name == 'query_metrics':
                plan = build_raw_plan(arguments)
            else:
                raise MetricsError('unknown_metric_tool', 'Choose an advertised Monitoring tool.')
            validate_plan(plan, limits, now=datetime.now(timezone.utc), max_window_hours=config.max_window_hours)
            series, completed, expressions, failure = [], [], [], None
            for query in plan.queries:
                try:
                    response = await self.gateway.query(scope, plan, query, deadline=deadline)
                    rows = normalize_metric_series(response, scope=scope, plan=plan, query=query, definition=definition)
                    expression = {'query': query, 'request_id': (getattr(response, 'headers', None) or {}).get('opc-request-id')}
                    if definition is not None:
                        expression.update(catalog_key=plan.catalog_key, catalog_version=plan.catalog_version,
                            catalog_unit=definition.unit, source_urls=list(definition.source_urls))
                    expression_index = len(expressions)
                    expressions.append(expression)
                    for row in rows:
                        for key in ('query', 'request_id', 'catalog_key', 'catalog_version', 'catalog_unit', 'source_urls'):
                            row.pop(key, None)
                        row['expression_index'] = expression_index
                    series.extend(rows)
                    completed.append(query)
                    self._read_authorized = True
                    self._read_scope = asdict(scope)
                except Exception as error:
                    failure = self._failure(error)
                    if failure['status'] == 403:
                        self._read_authorized = False
                        self._read_scope = asdict(scope)
                    break
            context = {'scope': asdict(scope), 'namespace': plan.namespace, 'queries': list(plan.queries),
                'time_start': plan.start.isoformat().replace('+00:00', 'Z'),
                'time_end': plan.end.isoformat().replace('+00:00', 'Z'),
                'resolution': plan.resolution, 'resolution_source': plan.resolution_source,
                'resource_group': plan.resource_group, 'catalog_key': plan.catalog_key,
                'catalog_version': plan.catalog_version, 'completed_queries': completed, 'expressions': expressions,
                'complete': failure is None, **self._budget_context()}
            if failure:
                context.update(error=failure, status='partial' if completed else 'error')
                if not completed:
                    context.pop('queries')
                    context.pop('resource_group')
            # Format completed evidence even when the OCI deadline has expired.
            # Caps bound the response; formatting must not discard partial data.
            payload = await asyncio.to_thread(fit_series, series, context=context, limits=limits)
            return MetricResult(payload, is_error=failure is not None)
        except Exception as error:
            return MetricResult({'status': 'error', 'complete': False, 'query_language': 'mql',
                'error': self._failure(error), **self._budget_context()}, is_error=True)

    async def check_connection(self, *, shared_compartment_id: str | None) -> MetricResult:
        result = await self.call('list_metric_namespaces', {'limit': 1}, shared_compartment_id=shared_compartment_id)
        timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        scope = result.payload.get('scope')
        self._last_tested = {'at': timestamp, 'status': 'error' if result.is_error else 'success', 'scope': scope}
        if result.is_error and scope is not None:
            self._inspect_authorized = False
            self._inspect_scope = scope
        payload = {**result.payload, 'service': 'metrics',
            'inspect_authorized': self._inspect_authorized if scope and scope == self._inspect_scope else None,
            'read_authorized': self._read_authorized if scope and scope == self._read_scope else None,
            'permission_note': 'This probe tests metric inspection. Query read permission requires an actual successful query in the same scope.'}
        return MetricResult(payload, result.is_error)
