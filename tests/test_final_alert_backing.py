"""Alert tags cannot authorize a mutation of a stale or ambiguous native chain."""
from copy import deepcopy

import pytest

from oci_logan_mcp.alarm_service import AlarmService
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.saved_search import SavedSearchService
from tests.final_review_helpers import ALARM, NEW_QUERY, OLD_QUERY, RULE, SEARCH, MemoryOCI

STALE_SEARCH = "ocid1.managementsavedsearch.oc1..stale"
STALE_RULE = "ocid1.loganalyticsscheduledtask.oc1..stale"


def stale_client():
    client = MemoryOCI()
    client.searches[STALE_SEARCH] = {**deepcopy(client.searches[SEARCH]), "id": STALE_SEARCH}
    client.rules[STALE_RULE] = {**deepcopy(client.rules[RULE]), "id": STALE_RULE,
                              "saved_search_id": STALE_SEARCH, "metric_name": "metric_old"}
    client.alarms[ALARM]["freeform_tags"].update(logan_backing_saved_search_id=STALE_SEARCH,
                                               logan_backing_metric_task_id=STALE_RULE)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_stale_tags_never_redirect_the_requested_native_mutation(operation):
    client = stale_client()
    service = AlarmService(client, CacheManager())
    if operation == "update":
        result = await service.update_alert(ALARM, query=NEW_QUERY)
        assert client.searches[SEARCH]["query"] == NEW_QUERY
        assert client.searches[STALE_SEARCH]["query"] == OLD_QUERY
        assert ("search", STALE_SEARCH) not in client.writes
        assert result["alarm_id"] == ALARM
    else:
        result = await service.delete_alert(ALARM)
        assert RULE not in client.rules and STALE_RULE in client.rules
        assert ALARM not in client.alarms
        assert result["deleted"] == ["metric_task", "alarm"]
    assert client.searches[STALE_SEARCH]["query"] == OLD_QUERY


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_ambiguous_native_producers_refuse_before_writes_then_recover(operation):
    client = MemoryOCI()
    client.rules[STALE_RULE] = {**deepcopy(client.rules[RULE]), "id": STALE_RULE}
    service = AlarmService(client, CacheManager())
    with pytest.raises(ValueError, match="[Aa]mbiguous|[Mm]ultiple"):
        if operation == "update":
            await service.update_alert(ALARM, query=NEW_QUERY)
        else:
            await service.delete_alert(ALARM)
    assert client.writes == []
    del client.rules[STALE_RULE]
    if operation == "update":
        await service.update_alert(ALARM, query=NEW_QUERY)
        assert client.searches[SEARCH]["query"] == NEW_QUERY
    else:
        await service.delete_alert(ALARM)
        assert ALARM not in client.alarms and RULE not in client.rules


@pytest.mark.asyncio
async def test_unverified_inventory_cannot_fall_back_to_tags_then_valid_inventory_executes():
    client = MemoryOCI()
    original = client.list_scheduled_tasks_for_saved_search
    async def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic inventory unavailable")
    client.list_scheduled_tasks_for_saved_search = unavailable
    service = AlarmService(client, CacheManager())
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        await service.update_alert(ALARM, query=NEW_QUERY)
    assert client.writes == []
    client.list_scheduled_tasks_for_saved_search = original
    await service.update_alert(ALARM, query=NEW_QUERY)
    assert client.searches[SEARCH]["query"] == NEW_QUERY


@pytest.mark.asyncio
async def test_listing_uses_current_native_query_and_backing_ids_over_stale_tags():
    client = stale_client()
    client.searches[SEARCH]["query"] = NEW_QUERY
    client.rules[RULE]["cron"] = "0 */10 * ? * *"
    [row] = await AlarmService(client, CacheManager()).list_alerts()
    assert row["backing_saved_search_id"] == SEARCH
    assert row["backing_metric_task_id"] == RULE
    assert row["query"] == NEW_QUERY
    assert row["schedule"] == "0 */10 * ? * *" and row["schedule_format"] == "oci"


@pytest.mark.asyncio
async def test_search_edit_never_refreshes_an_alarm_from_an_unverified_stale_tag():
    client = stale_client()
    cache = CacheManager()
    service = SavedSearchService(client, cache, alarm_service=AlarmService(client, cache))
    alarm_before = deepcopy(client.alarms[ALARM])
    await service.update_search(STALE_SEARCH, query=NEW_QUERY)
    assert client.searches[STALE_SEARCH]["query"] == NEW_QUERY
    assert client.alarms[ALARM] == alarm_before, "A stale tag must not make this alarm a dependent"
    await service.update_search(SEARCH, query=NEW_QUERY)
    assert client.alarms[ALARM]["freeform_tags"]["logan_query"] == NEW_QUERY
    assert client.alarms[ALARM]["body"].endswith(NEW_QUERY)
