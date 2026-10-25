"""Saved-search safety uses live rule references, even without an alarm."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import CacheConfig, Settings
from oci_logan_mcp.saved_search import SavedSearchService
from oci_logan_mcp.client import OCILogAnalyticsClient

SEARCH = 'ocid1.managementsavedsearch.oc1..example'
FREE = 'ocid1.managementsavedsearch.oc1..independent'
RULE = 'ocid1.loganalyticsscheduledtask.oc1..example'
CID = 'ocid1.compartment.example'


def make_service(rules=(), alarms=()):
    client = AsyncMock()
    client.compartment_id = CID
    client.namespace = 'example'
    client.list_management_saved_searches.return_value = [
        {'id': SEARCH, 'display_name': 'Team search'}, {'id': FREE, 'display_name': 'Reusable search'}]
    client.list_saved_searches.return_value = []
    client.list_scheduled_tasks_for_saved_search.return_value = [{'id': r['id']} for r in rules]
    client.get_scheduled_task_details.side_effect = lambda task_id: next(r for r in rules if r['id'] == task_id)
    client.list_alarms.return_value = list(alarms)
    client.get_saved_search.return_value = {'id': SEARCH, 'query': '* | stats count'}
    return SavedSearchService(client, CacheManager(CacheConfig(enabled=True))), client


def rule(identifier=RULE):
    return {'id': identifier, 'display_name': 'Team detection rule', 'saved_search_id': SEARCH,
            'metric_name': 'team_metric', 'metric_namespace': 'example_metrics',
            'metric_compartment_id': CID, 'lifecycle_state': 'ACTIVE'}


@pytest.mark.asyncio
async def test_listing_marks_rule_without_alarm_and_keeps_rows_compact():
    service, client = make_service([rule()])
    rows = await service.list_searches()
    used, independent = rows
    assert used['usage'] == 'detection_rule'
    assert used['dependent_rules'] == 1 and used['dependent_alerts'] == 0
    assert used['dependency_status'] == 'checked'
    assert independent['usage'] == 'reusable' and independent['dependent_rules'] == 0
    assert not any(isinstance(value, (dict, list)) for value in used.values())
    assert client.list_scheduled_tasks_for_saved_search.await_count == 1


@pytest.mark.asyncio
async def test_warm_catalog_cache_still_sees_new_rules_without_mutating_cached_rows():
    rules = []
    service, client = make_service(rules)
    first = await service.list_searches()
    assert first[0]['dependent_rules'] == 0
    rules.append(rule())
    client.list_scheduled_tasks_for_saved_search.return_value = [{'id': RULE}]
    second = await service.list_searches()
    assert second[0]['dependent_rules'] == 0
    assert first[0]['dependent_rules'] == 0
    with pytest.raises(RuntimeError, match='detection rule'):
        await service.delete_search(SEARCH)
    client.delete_management_saved_search.assert_not_awaited()
    await service.delete_search(FREE)
    client.delete_management_saved_search.assert_awaited_once_with(FREE)
    service.cache.delete('saved_searches')
    refreshed = await service.list_searches()
    assert refreshed[0]['dependent_rules'] == 1
    assert 'dependent_rules' not in service.cache.get('saved_searches')[0]
    assert client.list_management_saved_searches.await_count == 2
    assert client.list_scheduled_tasks_for_saved_search.await_count == 4


@pytest.mark.asyncio
async def test_listing_counts_all_rules_and_unique_alarm_consumers():
    alarm = {'id': 'alarm', 'display_name': 'Team alert', 'namespace': 'example_metrics',
             'metric_compartment_id': CID, 'query': 'team_metric[5m].last() > 0'}
    service, _ = make_service([rule(), rule('second-rule')], [alarm])
    [used, _] = await service.list_searches()
    assert used['dependent_rules'] == 2 and used['dependent_alerts'] == 1


@pytest.mark.asyncio
async def test_missing_metric_identity_does_not_report_an_exact_alarm_count():
    incomplete = {**rule(), 'metric_compartment_id': None}
    service, client = make_service([incomplete])
    [used, independent] = await service.list_searches()
    assert used['usage'] == 'detection_rule' and used['dependent_rules'] == 1
    assert used['dependent_alerts'] is None and used['dependency_status'] == 'unknown'
    assert independent['dependent_alerts'] == 0 and independent['dependency_status'] == 'checked'
    with pytest.raises(RuntimeError, match='detection rule'):
        await service.delete_search(SEARCH)
    client.delete_management_saved_search.assert_not_awaited()
    await service.delete_search(FREE)
    client.delete_management_saved_search.assert_awaited_once_with(FREE)


@pytest.mark.asyncio
async def test_failed_rule_inspection_is_unknown_instead_of_zero():
    service, client = make_service()
    client.list_scheduled_tasks_for_saved_search.side_effect = RuntimeError('private failure text')
    [used, _] = await service.list_searches()
    assert used['dependency_status'] == 'unknown'
    assert used['dependent_rules'] is None and used['dependent_alerts'] is None
    client.list_scheduled_tasks_for_saved_search.side_effect = None
    [checked, _] = await service.list_searches()
    assert checked['dependent_rules'] == 0 and checked['dependency_status'] == 'checked'


@pytest.mark.asyncio
async def test_native_search_deletion_refuses_rule_without_alarm_then_deletes_independent():
    service, client = make_service([rule()])
    with pytest.raises(RuntimeError, match='detection rule'):
        await service.delete_search(SEARCH)
    client.delete_management_saved_search.assert_not_awaited()
    client.delete_scheduled_task.assert_not_awaited()
    await service.delete_search(FREE)
    client.delete_management_saved_search.assert_awaited_once_with(FREE)


@pytest.mark.asyncio
async def test_legacy_rule_id_cannot_delete_its_backing_search():
    service, client = make_service([rule()])
    client.get_saved_search.return_value = {'id': RULE, '_action': SimpleNamespace(saved_search_id=SEARCH)}
    with pytest.raises(RuntimeError, match='detection rule'):
        await service.delete_search(RULE)
    client.delete_management_saved_search.assert_not_awaited()
    client.delete_scheduled_task.assert_not_awaited()
    await service.delete_search(FREE)
    client.delete_management_saved_search.assert_awaited_once_with(FREE)


@pytest.mark.asyncio
async def test_failed_inspection_prevents_delete_and_does_not_expose_failure_text():
    service, client = make_service()
    client.list_scheduled_tasks_for_saved_search.side_effect = RuntimeError('private failure text')
    with pytest.raises(RuntimeError, match='dependencies') as failure:
        await service.delete_search(SEARCH)
    assert 'private failure text' not in str(failure.value)
    client.delete_management_saved_search.assert_not_awaited()
    client.list_scheduled_tasks_for_saved_search.side_effect = None
    await service.delete_search(SEARCH)
    client.delete_management_saved_search.assert_awaited_once_with(SEARCH)


@pytest.mark.asyncio
async def test_incomplete_rule_detail_prevents_deletion_with_positive_control():
    service, client = make_service([rule()])
    client.get_scheduled_task_details.side_effect = None
    client.get_scheduled_task_details.return_value = {'id': RULE}
    with pytest.raises(RuntimeError, match='dependencies'):
        await service.delete_search(FREE)
    client.delete_management_saved_search.assert_not_awaited()
    client.list_scheduled_tasks_for_saved_search.return_value = []
    await service.delete_search(FREE)
    client.delete_management_saved_search.assert_awaited_once_with(FREE)


@pytest.mark.asyncio
async def test_native_search_in_another_compartment_cannot_be_deleted_as_if_checked():
    service, client = make_service()
    client.get_saved_search.return_value = {'id': SEARCH, 'compartment_id': 'ocid1.compartment.other'}
    with pytest.raises(RuntimeError, match='dependencies'):
        await service.delete_search(SEARCH)
    client.delete_management_saved_search.assert_not_awaited()
    client.get_saved_search.return_value = {'id': SEARCH, 'compartment_id': CID}
    await service.delete_search(SEARCH)
    client.delete_management_saved_search.assert_awaited_once_with(SEARCH)


@pytest.mark.asyncio
async def test_strict_rule_inventory_can_list_all_rules_in_the_selected_compartment(monkeypatch):
    with patch('oci_logan_mcp.client.get_signer', return_value=({'region': 'us-ashburn-1'}, object())):
        client = OCILogAnalyticsClient(Settings())
    client._rate_limiter.acquire = AsyncMock()
    client._la_client = SimpleNamespace(list_scheduled_tasks=lambda **kwargs: None)
    captured = {}
    def native_list(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(id=RULE, display_name='Team rule', lifecycle_state='ACTIVE')])
    monkeypatch.setattr('oci_logan_mcp.client.list_call_get_all_results', native_list)
    rows = await client.list_scheduled_tasks_for_saved_search(compartment_id=CID)
    assert rows[0]['id'] == RULE
    assert captured['compartment_id'] == CID and captured['task_type'] == 'SAVED_SEARCH'
    assert 'saved_search_id' not in captured
