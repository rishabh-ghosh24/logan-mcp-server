"""Regression cases from the independent alert review, using real services."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oci_logan_mcp.alarm_service import AlarmService
from oci_logan_mcp.alert_links import alarms_watching, parse_condition
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.saved_search import SavedSearchService

SEARCH = 'ocid1.managementsavedsearch.oc1..example'
RULE_ID = 'ocid1.loganalyticsscheduledtask.oc1..example'
CID = 'ocid1.compartment.example'
RULE = {'id': RULE_ID, 'display_name': 'Example rule', 'saved_search_id': SEARCH,
        'metric_name': 'metric_a', 'metric_namespace': 'example_metrics',
        'metric_compartment_id': CID, 'metric_resource_group': 'example_group',
        'cron': '0 0 0 ? * *'}
CONSOLE = {'id': 'alarm-a', 'display_name': 'Example console alarm', 'compartment_id': CID,
           'namespace': 'example_metrics', 'metric_compartment_id': CID,
           'resource_group': 'example_group', 'query': 'metric_a[5m].max() > 4',
           'pending_duration': 'PT5M', 'body': 'Console author notification',
           'freeform_tags': {'team': 'example'}, 'lifecycle_state': 'ACTIVE'}


def client_with_rule():
    client = AsyncMock()
    client.namespace = 'example'
    client.compartment_id = CID
    client.get_alarm.return_value = deepcopy(CONSOLE)
    client.list_alarms.return_value = [deepcopy(CONSOLE)]
    client.list_saved_searches.return_value = [{'id': RULE_ID}]
    client.list_scheduled_tasks_for_saved_search.return_value = [{'id': RULE_ID}]
    client.get_scheduled_task_details.return_value = deepcopy(RULE)
    client.get_saved_search.return_value = {'id': SEARCH, 'query': '* | stats count',
                                           'ui_config': {'queryString': '* | stats count', 'visualizationType': 'bar'}}
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize('has_backing_ids', [False, True])
async def test_console_query_update_resolves_backing_and_never_repairs_author_fields(has_backing_ids):
    client = client_with_rule()
    if has_backing_ids:
        client.get_alarm.return_value['freeform_tags'].update({
            'logan_backing_saved_search_id': SEARCH, 'logan_backing_metric_task_id': RULE_ID})
    result = await AlarmService(client, CacheManager()).update_alert('alarm-a', query='* | stats count as total')
    details = client.update_management_saved_search.call_args.args[1]
    assert client.update_management_saved_search.call_args.args[0] == SEARCH
    assert details.data_config == [{'query': '* | stats count as total'}]
    assert details.ui_config['visualizationType'] == 'bar'
    client.update_alarm.assert_awaited_once()
    alarm = client.update_alarm.call_args.args[1]
    assert alarm.query is None and alarm.body is None and alarm.resource_group is None
    assert alarm.pending_duration is None
    assert alarm.freeform_tags['team'] == 'example'
    assert alarm.freeform_tags['logan_origin'] == 'console'
    assert alarm.freeform_tags['logan_backing_saved_search_id'] == SEARCH
    assert alarm.freeform_tags['logan_query'] == '* | stats count as total'
    assert result['alarm_id'] == 'alarm-a'


@pytest.mark.asyncio
async def test_unstamped_console_threshold_changes_only_the_requested_comparison():
    client = client_with_rule()
    await AlarmService(client, CacheManager()).update_alert('alarm-a', threshold_value=9, threshold_operator='gte')
    alarm = client.update_alarm.call_args.args[1]
    assert alarm.query == 'metric_a[5m].max() >= 9'
    assert alarm.body is None and alarm.resource_group is None and alarm.pending_duration is None
    client.update_management_saved_search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('known_search', [False, True])
async def test_unresolved_console_query_fails_before_any_write_with_positive_control(known_search):
    client = client_with_rule()
    if known_search:
        client.get_alarm.return_value['freeform_tags']['logan_backing_saved_search_id'] = SEARCH
    client.list_saved_searches.return_value = []
    client.list_scheduled_tasks_for_saved_search.return_value = []
    service = AlarmService(client, CacheManager())
    with pytest.raises(ValueError, match='backing'):
        await service.update_alert('alarm-a', query='* | stats count as total')
    client.update_management_saved_search.assert_not_awaited()
    client.update_scheduled_task.assert_not_awaited()
    client.update_alarm.assert_not_awaited()
    client.list_saved_searches.return_value = [{'id': RULE_ID}]
    client.list_scheduled_tasks_for_saved_search.return_value = [{'id': RULE_ID}]
    await service.update_alert('alarm-a', query='* | stats count as total')
    client.update_management_saved_search.assert_awaited_once()
    assert client.update_alarm.call_args.args[1].query is None


@pytest.mark.parametrize('condition,expected', [
    ('metric_b[5m].max() > 1 && metric_a[5m].max() > 4', True),
    ('(metric_b[5m].max() > 1 || metric_a[5m].max() > 4)', True),
    ('metric_b[5m]{note="metric_a[5m]"}.max() > 4', False),
    (r'metric_b[5m]{note="say \"metric_a[5m]\""}.max() > 4', False),
    ('prefix_metric_a[5m].max() > 4', False),
])
def test_matching_uses_all_real_metric_operands_and_ignores_quoted_text(condition, expected):
    alarm = {**CONSOLE, 'query': condition}
    control = {**CONSOLE, 'id': 'control'}
    found = alarms_watching([alarm, control], RULE)
    assert [row['id'] for row in found] == (['alarm-a', 'control'] if expected else ['control'])
    if condition.startswith('metric_b') and expected:
        assert parse_condition(condition) == ('metric_b', None, None)


def test_compound_display_keeps_the_first_real_metric_with_parentheses():
    assert parse_condition('(metric_b[5m].max() > 1 || metric_a[5m].max() > 4)') == ('metric_b', None, None)
    assert parse_condition('metric_a[5m].max() > 4') == ('metric_a', 'gt', '4')


@pytest.mark.asyncio
async def test_delete_keeps_rule_read_by_second_operand_then_deletes_unshared_rule():
    client = client_with_rule()
    client.get_alarm.return_value['freeform_tags'].update({
        'logan_origin': 'console', 'logan_backing_saved_search_id': SEARCH,
        'logan_backing_metric_task_id': RULE_ID})
    other = {**CONSOLE, 'id': 'alarm-b', 'display_name': 'Compound consumer',
             'query': 'metric_b[5m].max() > 1 && metric_a[5m].max() > 4'}
    client.list_alarms.return_value.append(other)
    service = AlarmService(client, CacheManager())
    result = await service.delete_alert('alarm-a')
    client.delete_scheduled_task.assert_not_awaited()
    client.delete_alarm.assert_awaited_once_with('alarm-a')
    assert any(row['resource'] == 'detection_rule' and 'Compound consumer' in row['reason']
               for row in result['kept'])
    client.delete_alarm.reset_mock()
    client.list_alarms.return_value = [CONSOLE]
    await service.delete_alert('alarm-a')
    client.delete_scheduled_task.assert_awaited_once_with(RULE_ID)
    client.delete_management_saved_search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('failure_at', ['lookup', 'alarm_read'])
async def test_saved_search_preflight_failure_causes_zero_writes_then_can_recover(legacy, failure_at):
    client = client_with_rule()
    search = deepcopy(client.get_saved_search.return_value)
    client.get_saved_search.side_effect = lambda identifier: (
        {'id': RULE_ID, '_action': SimpleNamespace(saved_search_id=SEARCH)}
        if identifier == RULE_ID else search)
    service = SavedSearchService(client, CacheManager(), alarm_service=AlarmService(client, CacheManager()))
    operation = client.list_alarms if failure_at == 'lookup' else client.get_alarm
    operation.side_effect = RuntimeError('preflight unavailable')
    target = RULE_ID if legacy else SEARCH
    with pytest.raises(RuntimeError, match='preflight unavailable'):
        await service.update_search(target, query='* | stats count as total')
    client.update_management_saved_search.assert_not_awaited()
    client.update_scheduled_task.assert_not_awaited()
    client.update_alarm.assert_not_awaited()
    operation.side_effect = None
    await service.update_search(target, query='* | stats count as total')
    client.update_management_saved_search.assert_awaited_once()
    client.update_alarm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_saved_search_partial_refresh_reports_completed_and_pending_alarm_ids(legacy):
    import oci
    client = client_with_rule()
    second = {**deepcopy(CONSOLE), 'id': 'alarm-b', 'display_name': 'Second consumer'}
    client.list_alarms.return_value.append(second)
    client.get_alarm.side_effect = lambda identifier: deepcopy(CONSOLE if identifier == 'alarm-a' else second)
    search = deepcopy(client.get_saved_search.return_value)
    client.get_saved_search.side_effect = lambda identifier: (
        {'id': RULE_ID, '_action': SimpleNamespace(saved_search_id=SEARCH)}
        if identifier == RULE_ID else search)
    client.update_alarm.side_effect = [oci.exceptions.ServiceError(503, 'Unavailable', {}, 'PRIVATE_PROVIDER_TEXT'), {}]
    service = SavedSearchService(client, CacheManager(), alarm_service=AlarmService(client, CacheManager()))
    result = await service.update_search(RULE_ID if legacy else SEARCH, query='* | stats count as total')
    assert result['partial_failure'] is True and result['saved_search_updated'] is True
    assert result['remaining'] == ['alarm-a']
    assert [row['alarm_id'] for row in result['linked_alerts_updated']] == ['alarm-b']
    assert result['details'][0]['alarm_id'] == 'alarm-a'
    assert 'PRIVATE_PROVIDER_TEXT' not in json.dumps(result)
    assert client.update_alarm.await_count == 2
    client.update_management_saved_search.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 404])
@pytest.mark.parametrize('listed', [False, True])
async def test_missing_rule_is_confirmed_by_inventory_before_any_delete(status, listed):
    import oci
    client = client_with_rule()
    client.get_alarm.return_value['freeform_tags'].update({
        'logan_backing_saved_search_id': SEARCH, 'logan_backing_metric_task_id': RULE_ID})
    client.get_scheduled_task_details.side_effect = oci.exceptions.ServiceError(status, 'CannotParseRequest', {}, 'read failed')
    client.list_scheduled_tasks_for_saved_search.return_value = [{'id': RULE_ID}] if listed else []
    service = AlarmService(client, CacheManager())
    if listed:
        with pytest.raises(oci.exceptions.ServiceError):
            await service.delete_alert('alarm-a')
        client.delete_alarm.assert_not_awaited()
        client.delete_scheduled_task.assert_not_awaited()
    else:
        result = await service.delete_alert('alarm-a')
        assert result['deleted'] == ['alarm'] and result['partial_failure'] is False
        client.delete_alarm.assert_awaited_once_with('alarm-a')
        client.delete_scheduled_task.assert_not_awaited()
    client.list_scheduled_tasks_for_saved_search.assert_awaited_once()
    client.delete_alarm.reset_mock()
    client.get_scheduled_task_details.side_effect = None
    client.list_scheduled_tasks_for_saved_search.return_value = [{'id': RULE_ID}]
    await service.delete_alert('alarm-a')
    client.delete_scheduled_task.assert_awaited_once_with(RULE_ID)
    client.delete_alarm.assert_awaited_once_with('alarm-a')
    client.delete_management_saved_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_rule_inventory_does_not_treat_a_400_as_absence():
    import oci
    client = client_with_rule()
    client.get_alarm.return_value['freeform_tags'].update({
        'logan_backing_saved_search_id': SEARCH, 'logan_backing_metric_task_id': RULE_ID})
    client.get_scheduled_task_details.side_effect = oci.exceptions.ServiceError(400, 'CannotParseRequest', {}, 'read failed')
    client.list_scheduled_tasks_for_saved_search.side_effect = RuntimeError('inventory unavailable')
    service = AlarmService(client, CacheManager())
    with pytest.raises(RuntimeError, match='inventory unavailable'):
        await service.delete_alert('alarm-a')
    client.delete_scheduled_task.assert_not_awaited()
    client.delete_alarm.assert_not_awaited()
    client.list_scheduled_tasks_for_saved_search.side_effect = None
    client.list_scheduled_tasks_for_saved_search.return_value = []
    result = await service.delete_alert('alarm-a')
    assert result['deleted'] == ['alarm']
    client.delete_alarm.assert_awaited_once_with('alarm-a')
