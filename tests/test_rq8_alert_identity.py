"""Alert dependency lookup must include every producer and its metric compartment."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from oci_logan_mcp.alert_links import find_alerts_for_saved_search
from oci_logan_mcp.client import OCILogAnalyticsClient
from oci_logan_mcp.config import Settings

SEARCH = 'ocid1.managementsavedsearch.oc1..example'
CID = 'ocid1.compartment.metrics.example'


def alarm(identifier, metric='sample_metric', *, compartment=CID, tagged=False):
    tags = {'logan_managed': 'true', 'logan_kind': 'monitoring_alarm',
            'logan_backing_saved_search_id': SEARCH, 'logan_query': '* | stats count'} if tagged else {}
    return {'id': identifier, 'display_name': identifier, 'namespace': 'example_metrics',
            'resource_group': 'example_group', 'metric_compartment_id': compartment,
            'query': f'{metric}[5m].last() > 3', 'freeform_tags': tags}


def rule(identifier, metric='sample_metric', *, compartment=CID):
    return {'id': identifier, 'saved_search_id': SEARCH, 'metric_namespace': 'example_metrics',
            'metric_name': metric, 'metric_resource_group': 'example_group',
            'metric_compartment_id': compartment, 'cron': '0 */5 * ? * *'}


def lookup_client(alarms, rules):
    client = AsyncMock()
    client.compartment_id = 'ocid1.compartment.example'
    client.list_alarms.return_value = alarms
    client.list_scheduled_tasks_for_saved_search.return_value = [{'id': r['id']} for r in rules]
    client.get_scheduled_task_details.side_effect = lambda identifier: next(r for r in rules if r['id'] == identifier)
    client.get_saved_search.return_value = {'query': '* | stats count'}
    return client


@pytest.mark.asyncio
async def test_tagged_alarm_does_not_hide_an_untagged_consumer():
    client = lookup_client([alarm('tagged', tagged=True), alarm('console')], [rule('producer')])
    found = await find_alerts_for_saved_search(client, SEARCH)
    assert sorted(a.alarm_id for a in found) == ['console', 'tagged']
    assert len(found) == 2


@pytest.mark.asyncio
async def test_tagged_alarm_does_not_hide_another_rule_on_the_search():
    client = lookup_client([alarm('first', tagged=True), alarm('second', 'other_metric')],
                           [rule('producer-a'), rule('producer-b', 'other_metric')])
    found = await find_alerts_for_saved_search(client, SEARCH)
    assert {a.alarm_id: a.backing_metric_task_id for a in found} == {
        'first': 'producer-a', 'second': 'producer-b'}


@pytest.mark.asyncio
async def test_equal_metric_names_in_different_compartments_do_not_match():
    client = lookup_client([alarm('wrong', compartment='ocid1.compartment.other'), alarm('right')],
                           [rule('producer')])
    found = await find_alerts_for_saved_search(client, SEARCH)
    assert [a.alarm_id for a in found] == ['right']
    assert found[0].backing_metric_task_id == 'producer'


@pytest.mark.asyncio
async def test_a_cached_tag_cannot_override_a_known_metric_compartment_mismatch():
    client = lookup_client([alarm('wrong-tag', compartment='ocid1.compartment.other', tagged=True),
                            alarm('right')], [rule('producer')])
    found = await find_alerts_for_saved_search(client, SEARCH)
    assert [alert.alarm_id for alert in found] == ['right']
    assert found[0].backing_metric_task_id == 'producer'


@pytest.mark.asyncio
async def test_metric_compartment_is_preserved_by_the_task_adapter():
    with patch('oci_logan_mcp.client.get_signer', return_value=({'region': 'us-ashburn-1'}, object())):
        client = OCILogAnalyticsClient(Settings())
    client._rate_limiter.acquire = AsyncMock()
    extraction = SimpleNamespace(compartment_id=CID, namespace='example_metrics',
                                 metric_name='sample_metric', resource_group='example_group')
    task = SimpleNamespace(id='producer', display_name='rule', lifecycle_state='ACTIVE',
        action=SimpleNamespace(saved_search_id=SEARCH, metric_extraction=extraction),
        schedules=[], freeform_tags={})
    client._la_client = SimpleNamespace(get_scheduled_task=lambda **kwargs: SimpleNamespace(data=task))
    result = await client.get_scheduled_task_details('producer')
    assert result['metric_compartment_id'] == CID
    assert result['saved_search_id'] == SEARCH


@pytest.mark.asyncio
async def test_metric_compartment_is_preserved_by_the_alarm_adapter(monkeypatch):
    with patch('oci_logan_mcp.client.get_signer', return_value=({'region': 'us-ashburn-1'}, object())):
        client = OCILogAnalyticsClient(Settings())
    client._rate_limiter.acquire = AsyncMock()
    native = SimpleNamespace(id='consumer', display_name='alarm', metric_compartment_id=CID)
    client._monitoring_client = SimpleNamespace(list_alarms=lambda **kwargs: None)
    monkeypatch.setattr('oci_logan_mcp.client.list_call_get_all_results',
                        lambda *args, **kwargs: SimpleNamespace(data=[native]))
    [result] = await client.list_alarms()
    assert result['metric_compartment_id'] == CID
    assert result['id'] == 'consumer'


@pytest.mark.asyncio
async def test_native_search_adapter_preserves_its_compartment_for_scope_checks():
    client = object.__new__(OCILogAnalyticsClient)
    client._rate_limiter = SimpleNamespace(acquire=AsyncMock(), reset=lambda: None)
    native = SimpleNamespace(id=SEARCH, display_name='Team search', compartment_id=CID,
                             ui_config={'queryString': '* | stats count'}, data_config=[], lifecycle_state='ACTIVE')
    client._dashx_client = SimpleNamespace(get_management_saved_search=lambda **kwargs: SimpleNamespace(data=native))
    result = await client.get_saved_search(SEARCH)
    assert result['compartment_id'] == CID and result['query'] == '* | stats count'
