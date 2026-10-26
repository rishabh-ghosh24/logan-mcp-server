"""Listing snapshots reduce reads without becoming deletion authority."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_rq8_saved_search_dependencies import make_service, rule, SEARCH
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.config import Settings
from oci_logan_mcp.alarm_service import AlarmService
from tests.test_alert_followups import CONSOLE


@pytest.mark.asyncio
async def test_warm_listing_reuses_compact_markers_but_returns_independent_rows():
    service, client = make_service([rule()])
    first = await service.list_searches()
    first[0]['dependent_rules'] = 999
    again = await service.list_searches()
    assert again[0]['dependent_rules'] == 1 and again[1]['dependent_rules'] == 0
    assert client.list_scheduled_tasks_for_saved_search.await_count == 1
    assert client.get_scheduled_task_details.await_count == 1
    assert 'dependent_rules' not in service.cache.get('saved_searches')[0]


@pytest.mark.asyncio
async def test_catalog_expiry_refreshes_markers(monkeypatch):
    rules = []
    service, client = make_service(rules)
    assert (await service.list_searches())[0]['dependent_rules'] == 0
    rules.append(rule())
    client.list_scheduled_tasks_for_saved_search.return_value = [{'id': rules[0]['id']}]
    assert (await service.list_searches())[0]['dependent_rules'] == 0
    future = time.time() + 301
    monkeypatch.setattr('oci_logan_mcp.cache.time.time', lambda: future)
    assert (await service.list_searches())[0]['dependent_rules'] == 1
    assert client.list_management_saved_searches.await_count == 2
    assert client.list_scheduled_tasks_for_saved_search.await_count == 2


@pytest.mark.asyncio
async def test_namespace_change_cannot_reuse_another_scopes_marker_map():
    rules = [rule()]
    service, client = make_service(rules)
    assert (await service.list_searches())[0]['dependent_rules'] == 1
    client.namespace = 'another'
    client.list_scheduled_tasks_for_saved_search.return_value = []
    assert (await service.list_searches())[0]['dependent_rules'] == 0
    assert client.list_management_saved_searches.await_count == 2
    assert client.list_scheduled_tasks_for_saved_search.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_listing_refresh_is_single_flight():
    service, client = make_service([rule()])
    results = await asyncio.gather(*(service.list_searches() for _ in range(4)))
    assert all(rows[0]['dependent_rules'] == 1 for rows in results)
    assert client.list_management_saved_searches.await_count == 1
    assert client.list_scheduled_tasks_for_saved_search.await_count == 1
    assert client.get_scheduled_task_details.await_count == 1


@pytest.mark.asyncio
async def test_detail_reads_overlap_with_at_most_four_requests():
    rules = [rule(f'producer-{i}') for i in range(8)]
    service, client = make_service(rules)
    entered, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    async def read(identifier):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            entered.set()
        try:
            await release.wait()
            return next(row for row in rules if row['id'] == identifier)
        finally:
            active -= 1
    client.get_scheduled_task_details.side_effect = read
    pending = asyncio.create_task(service.list_searches())
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        assert peak == 4 and not pending.done()
    finally:
        release.set()
        rows = await pending
    assert rows[0]['dependent_rules'] == 8 and rows[1]['dependent_rules'] == 0
    assert peak == 4


@pytest.mark.asyncio
async def test_concurrent_client_detail_reads_serialize_rate_acquisition():
    with patch('oci_logan_mcp.client.get_signer', return_value=({'region': 'us-ashburn-1'}, object())):
        client = OCILogAnalyticsClient(Settings())
    entered, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    async def acquire():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        try:
            await release.wait()
        finally:
            active -= 1
    client._rate_limiter.acquire = acquire
    client._la_client = SimpleNamespace(get_scheduled_task=lambda **kwargs: SimpleNamespace(data=SimpleNamespace(
        id=kwargs['scheduled_task_id'], action=None, schedules=[], freeform_tags={})))
    pending = [asyncio.create_task(client.get_scheduled_task_details(identifier)) for identifier in ('first', 'second')]
    try:
        await entered.wait()
        await asyncio.sleep(0)
        assert peak == 1
    finally:
        release.set()
        rows = await asyncio.gather(*pending)
    assert [row['id'] for row in rows] == ['first', 'second']


@pytest.mark.asyncio
async def test_failed_refresh_is_not_cached_as_an_empty_dependency_map():
    service, client = make_service([rule()])
    client.get_scheduled_task_details.side_effect = RuntimeError('temporary failure')
    first = await service.list_searches()
    assert first[0]['dependency_status'] == 'unknown' and first[0]['dependent_rules'] is None
    client.get_scheduled_task_details.side_effect = None
    client.get_scheduled_task_details.return_value = rule()
    second = await service.list_searches()
    assert second[0]['dependent_rules'] == 1
    third = await service.list_searches()
    assert third[0]['dependent_rules'] == 1
    assert client.get_scheduled_task_details.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create', 'update', 'delete'])
async def test_alarm_writes_invalidate_only_the_listing_snapshot(operation):
    rules = [rule()] if operation == 'delete' else []
    service, client = make_service(rules)
    first = await service.list_searches()
    assert first[0]['dependent_rules'] == (1 if operation == 'delete' else 0)
    client.get_alarm.return_value = {**CONSOLE, 'query': 'team_metric[5m].max() > 4', 'freeform_tags': {
        'logan_managed': 'true', 'logan_kind': 'monitoring_alarm',
        'logan_backing_saved_search_id': SEARCH, 'logan_backing_metric_task_id': rule()['id']}}
    client.list_alarms.return_value = [client.get_alarm.return_value]
    alarms = AlarmService(client, service.cache)
    service.cache.set('unrelated-query', {'rows': [[42]]})
    def added(*args, **kwargs):
        rules.append(rule())
        client.list_scheduled_tasks_for_saved_search.return_value = [{'id': rule()['id']}]
        return {'id': 'alarm-a'}
    if operation == 'delete':
        def removed(*args, **kwargs):
            rules.clear()
            client.list_scheduled_tasks_for_saved_search.return_value = []
        client.delete_scheduled_task.side_effect = removed
        await alarms.delete_alert('alarm-a')
    elif operation == 'update':
        client.update_alarm.side_effect = added
        await alarms.update_alert('alarm-a', severity='WARNING')
    else:
        client.tenancy_id = 'ocid1.tenancy.example'
        client._config = {'region': 'us-ashburn-1'}
        client.create_management_saved_search.return_value = {'id': SEARCH}
        client.create_scheduled_task.return_value = {'id': rule()['id']}
        client.create_alarm.side_effect = added
        result = await alarms.create_alert('Example', '* | stats count', 'ocid1.onstopic.example')
        assert result['alarm_id'] == 'alarm-a'
    refreshed = await service.list_searches()
    assert refreshed[0]['dependent_rules'] == (0 if operation == 'delete' else 1)
    assert client.list_management_saved_searches.await_count == 2
    assert service.cache.get('unrelated-query') == {'rows': [[42]]}
