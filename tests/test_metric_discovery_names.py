"""Names-first discovery must stay scoped, paginated and budgeted."""

import json
from types import SimpleNamespace

import oci
import pytest

from tests.test_metrics_service import make_handlers, call
from tests.test_metrics_protocol import metrics_session, metric_call


def metric(name, resource='instance-a', *, zone='a', group='qa'):
    return oci.monitoring.models.Metric(
        namespace='oci_computeagent', name=name, compartment_id='metrics-default',
        resource_group=group, dimensions={'resourceId': resource, 'zone': zone})


class DiscoveryBoundary:
    """Model OCI grouping's documented filter hazard at the request boundary."""

    def __init__(self, items=None):
        self.items = items if items is not None else [
            metric('CpuUtilization'), metric('CpuUtilization', zone='b'),
            metric('MemoryUtilization', 'instance-b')]
        self.calls = []
        self.error = None

    def list_metrics(self, *, compartment_id, list_metrics_details, **kwargs):
        details = list_metrics_details
        self.calls.append((compartment_id, details, kwargs))
        if self.error:
            raise self.error
        items = list(self.items)
        if details.name:
            items = [item for item in items if item.name == details.name]
        if details.resource_group:
            items = [item for item in items if item.resource_group == details.resource_group]
        if details.dimension_filters and not details.group_by:
            items = [item for item in items if all(item.dimensions.get(key) == value
                     for key, value in details.dimension_filters.items())]
        if details.group_by == ['name']:
            items = [metric(name) for name in sorted({item.name for item in items})]
        offset = int((kwargs.get('page') or 'native:0').split(':')[-1])
        end = offset + kwargs['limit']
        headers = {'opc-request-id': 'qualification-discovery'}
        if end < len(items):
            headers['opc-next-page'] = 'native:' + str(end)
        return SimpleNamespace(data=items[offset:end], headers=headers)


def handlers_with_discovery(tmp_path, monkeypatch, items=None):
    handlers, _ = make_handlers(tmp_path, monkeypatch)
    backend = DiscoveryBoundary(items)
    handlers.oci_client._monitoring_factory = lambda **kwargs: backend
    return handlers, backend


async def names_page(handlers, **arguments):
    payload, error = await call(handlers, 'list_metrics',
                                {'namespace': 'oci_computeagent', **arguments})
    assert not error, payload
    assert payload['discovery_mode'] == 'names'
    assert all(set(item) == {'name'} for item in payload['metrics'])
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize('filters', [None, {}])
async def test_overview_groups_exact_names_and_costs_one_native_page(tmp_path, monkeypatch, filters):
    handlers, backend = handlers_with_discovery(tmp_path, monkeypatch)
    arguments = {} if filters is None else {'dimension_filters': filters}
    result = await names_page(handlers, **arguments)
    assert result['metrics'] == [{'name': 'CpuUtilization'}, {'name': 'MemoryUtilization'}]
    assert result['returned_count'] == 2 and result['more_available'] is False
    assert result['scope']['compartment_id'] == 'metrics-default'
    assert result['budget']['queries'] == 1 and result['budget_remaining']['queries'] == 99
    assert len(backend.calls) == 1 and backend.calls[0][1].group_by == ['name']
    assert backend.calls[0][1].sort_by == 'NAME' and backend.calls[0][1].sort_order == 'ASC'
    assert 'total_count' not in result


@pytest.mark.asyncio
async def test_native_identifiers_are_not_relabelled_or_case_folded(tmp_path, monkeypatch):
    items = [metric('CPUUtilization'), metric('CpuUtilization'),
             metric('request_latency_bucket'), metric('request_latency_bucket')]
    handlers, _ = handlers_with_discovery(tmp_path, monkeypatch, items)
    result = await names_page(handlers)
    assert result['metrics'] == [{'name': 'CPUUtilization'}, {'name': 'CpuUtilization'},
                                 {'name': 'request_latency_bucket'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('resource,wanted', [
    ('instance-a', ['CpuUtilization']), ('instance-b', ['MemoryUtilization']), ('missing', [])])
async def test_instance_filter_never_turns_into_whole_compartment_names(tmp_path, monkeypatch, resource, wanted):
    handlers, backend = handlers_with_discovery(tmp_path, monkeypatch)
    positive = await names_page(handlers)
    assert positive['returned_count'] == 2
    result = await names_page(handlers, dimension_filters={'resourceId': resource}, resource_group='qa')
    assert [row['name'] for row in result['metrics']] == wanted
    assert result['status'] == ('success' if wanted else 'no_data')
    details = backend.calls[-1][1]
    assert details.group_by is None and details.dimension_filters == {'resourceId': resource}
    assert details.resource_group == 'qa' and result['budget']['queries'] == 2


@pytest.mark.asyncio
async def test_named_followup_keeps_complete_stream_dimensions(tmp_path, monkeypatch):
    handlers, backend = handlers_with_discovery(tmp_path, monkeypatch)
    overview = await names_page(handlers)
    assert overview['returned_count'] == 2
    result, error = await call(handlers, 'list_metrics', {
        'namespace': 'oci_computeagent', 'metric_name': 'CpuUtilization',
        'dimension_filters': {'resourceId': 'instance-a'}, 'resource_group': 'qa'})
    assert not error and result['discovery_mode'] == 'streams'
    assert result['returned_count'] == 2
    assert [row['dimensions'] for row in result['metrics']] == [
        {'resourceId': 'instance-a', 'zone': 'a'}, {'resourceId': 'instance-a', 'zone': 'b'}]
    assert all(row['resource_group'] == 'qa' and row['name'] == 'CpuUtilization'
               for row in result['metrics'])
    assert backend.calls[-1][1].group_by is None


async def collect_names_with_reserve(handlers, **arguments):
    names, pages, token = set(), 0, None
    while True:
        payload = await names_page(handlers, **arguments, **({'page_token': token} if token else {}))
        pages += 1
        names.update(row['name'] for row in payload['metrics'])
        token = payload['next_page_token']
        if not payload['more_available']:
            return sorted(names), True, pages
        if payload['budget_remaining']['queries'] <= 5:
            return sorted(names), False, pages


@pytest.mark.asyncio
@pytest.mark.parametrize('filtered', [False, True])
async def test_later_pages_are_required_and_no_hidden_enumeration_occurs(tmp_path, monkeypatch, filtered):
    items = [metric('CpuUtilization'), metric('CpuUtilization', zone='b'), metric('MemoryUtilization')]
    handlers, backend = handlers_with_discovery(tmp_path, monkeypatch, items)
    arguments = {'limit': 1, **({'dimension_filters': {'resourceId': 'instance-a'}} if filtered else {})}
    names, complete, pages = await collect_names_with_reserve(handlers, **arguments)
    assert names == ['CpuUtilization', 'MemoryUtilization'] and complete
    assert pages == (3 if filtered else 2)
    assert len(backend.calls) == pages == handlers._budget_tracker.snapshot().queries
    assert backend.calls[1][2]['page'] == 'native:1'
    assert all(item[0] == 'metrics-default' for item in backend.calls)
    assert all(item[1].dimension_filters == ({'resourceId': 'instance-a'} if filtered else None)
               for item in backend.calls)


@pytest.mark.asyncio
async def test_compliant_page_consumer_stops_with_five_slots_and_reports_incomplete(tmp_path, monkeypatch):
    items = [metric('CpuUtilization', zone=str(index)) for index in range(4)] + [metric('MemoryUtilization')]
    handlers, backend = handlers_with_discovery(tmp_path, monkeypatch, items)
    handlers._budget_tracker.limits.max_queries_per_session = 8
    names, complete, pages = await collect_names_with_reserve(
        handlers, limit=1, dimension_filters={'resourceId': 'instance-a'})
    assert names == ['CpuUtilization'] and complete is False and pages == 3
    assert len(backend.calls) == 3 and handlers._budget_tracker.remaining()['queries'] == 5


@pytest.mark.asyncio
async def test_empty_names_are_no_data_with_a_positive_control(tmp_path, monkeypatch):
    handlers, backend = handlers_with_discovery(tmp_path, monkeypatch)
    assert (await names_page(handlers))['returned_count'] == 2
    backend.items = []
    result = await names_page(handlers)
    assert result['status'] == 'no_data' and result['metrics'] == []
    assert result['returned_count'] == 0 and result['next_page_token'] is None
    assert result['budget']['queries'] == 2


@pytest.mark.asyncio
async def test_backend_failure_is_not_misreported_as_an_empty_inventory(tmp_path, monkeypatch):
    handlers, backend = handlers_with_discovery(tmp_path, monkeypatch)
    assert (await names_page(handlers))['returned_count'] == 2
    backend.error = oci.exceptions.ServiceError(403, 'NotAuthorizedOrNotFound', {}, 'private test detail')
    result, error = await call(handlers, 'list_metrics', {'namespace': 'oci_computeagent'})
    assert error and result['error']['status'] == 403 and result['status'] == 'error'
    assert 'private test detail' not in json.dumps(result) and 'metrics' not in result


@pytest.mark.asyncio
async def test_existing_hard_budget_still_blocks_before_sdk(tmp_path, monkeypatch):
    handlers, backend = handlers_with_discovery(tmp_path, monkeypatch)
    handlers._budget_tracker.limits.max_queries_per_session = 1
    assert (await names_page(handlers))['budget_remaining']['queries'] == 0
    result, error = await call(handlers, 'list_metrics', {'namespace': 'oci_computeagent'})
    assert error and result['error']['code'] == 'budget_exceeded'
    assert len(backend.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['', None, 42])
async def test_invalid_native_name_is_an_explicit_error_not_a_made_up_label(tmp_path, monkeypatch, invalid):
    handlers, backend = handlers_with_discovery(tmp_path, monkeypatch)
    assert (await names_page(handlers))['returned_count'] == 2
    backend.items = [metric(invalid)]
    result, error = await call(handlers, 'list_metrics', {
        'namespace': 'oci_computeagent', 'dimension_filters': {'resourceId': 'instance-a'}})
    assert error and result['error']['code'] == 'invalid_metrics_response'
    assert result['status'] == 'error' and 'metrics' not in result


@pytest.mark.asyncio
async def test_real_stdio_names_then_details_preserves_paging_and_budget(tmp_path):
    async with metrics_session(tmp_path) as (session, _):
        names, error = await metric_call(session, 'list_metrics', {'namespace': 'oci_computeagent'})
        assert not error and names['discovery_mode'] == 'names'
        assert names['metrics'] == [{'name': 'CpuUtilization'}]
        assert names['next_page_token'] == 'fake-next-page' and names['budget']['queries'] == 1
        details, error = await metric_call(session, 'list_metrics', {
            'namespace': 'oci_computeagent', 'metric_name': 'CpuUtilization'})
        assert not error and details['discovery_mode'] == 'streams'
        assert details['metrics'][0]['dimensions'] == {'resourceId': 'fake-instance'}
        assert details['budget']['queries'] == 2
