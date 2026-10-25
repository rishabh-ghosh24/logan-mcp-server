"""Alert chains are discovered through OCI's own ids; our tags are only a cache."""
import pytest
from unittest.mock import AsyncMock

from oci_logan_mcp.alert_links import (
    find_alerts_for_saved_search, find_log_alerts, invalidate, parse_condition,
)
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import CacheConfig

COMPARTMENT = "ocid1.compartment.test"
LOGAN_ALARM = {
    "id": "ocid1.alarm.logan", "display_name": "VCN flow spike", "severity": "WARNING",
    "lifecycle_state": "ACTIVE", "namespace": "logan_custom_metrics", "resource_group": "logan_alerts",
    "query": "logan_alert_abc[15m].last() > 50",
    "freeform_tags": {
        "logan_managed": "true", "logan_kind": "monitoring_alarm",
        "logan_backing_saved_search_id": "ocid1.mss.logan", "logan_backing_metric_task_id": "ocid1.task.logan",
        "logan_query": "'Log Source' = 'OCI VCN Flow Unified Schema Logs' | stats count",
        "logan_schedule": "*/15 * * * *",
    },
}
CONSOLE_ALARM = {
    "id": "ocid1.alarm.console", "display_name": "Console VCN", "severity": "CRITICAL",
    "lifecycle_state": "ACTIVE", "namespace": "test_detection_rule", "resource_group": None,
    "query": "test_metric[5m].max() > 0", "freeform_tags": {"team": "x"},
}
LOOKALIKE_ALARM = {
    "id": "ocid1.alarm.cpu", "display_name": "CPU", "severity": "CRITICAL",
    "lifecycle_state": "ACTIVE", "namespace": "oci_computeagent", "resource_group": None,
    "query": "test_metric[1m].mean() > 90", "freeform_tags": {},
}
CONSOLE_TASK = {
    "id": "ocid1.task.console", "display_name": "console rule", "saved_search_id": "ocid1.mss.console",
    "cron": "0 */5 * ? * *", "metric_namespace": "test_detection_rule", "metric_name": "test_metric",
    "metric_resource_group": None, "freeform_tags": {},
}


def make_client(alarms, tasks_for_search=(), task_details=None, all_tasks=()):
    client = AsyncMock()
    client.compartment_id = COMPARTMENT
    client.list_alarms.return_value = list(alarms)
    client.list_scheduled_tasks_for_saved_search.return_value = list(tasks_for_search)
    client.list_saved_searches.return_value = list(all_tasks)
    client.get_scheduled_task_details.side_effect = lambda task_id: dict((task_details or {})[task_id])
    client.get_saved_search.return_value = {"id": "ocid1.mss.console", "query": "* | stats count"}
    return client


def cache():
    return CacheManager(CacheConfig(enabled=True))


def test_parse_condition_reads_metric_operator_and_threshold():
    assert parse_condition("logan_alert_x[5m].last() > 0") == ("logan_alert_x", "gt", "0")
    assert parse_condition("test_metric[1m].max() <= 2.5") == ("test_metric", "lte", "2.5")
    assert parse_condition("not an mql condition") == (None, None, None)


@pytest.mark.asyncio
async def test_fast_path_uses_backing_tag_without_listing_tasks():
    client = make_client([LOGAN_ALARM, CONSOLE_ALARM])

    found = await find_alerts_for_saved_search(client, cache(), "ocid1.mss.logan")

    assert [a.alarm_id for a in found] == ["ocid1.alarm.logan"]
    assert found[0].created_by == "logan"
    assert found[0].query == LOGAN_ALARM["freeform_tags"]["logan_query"]
    assert found[0].schedule == "*/15 * * * *" and found[0].schedule_format == "logan"
    client.list_scheduled_tasks_for_saved_search.assert_not_called()


@pytest.mark.asyncio
async def test_slow_path_matches_console_alarm_by_namespace_and_metric_token():
    client = make_client([CONSOLE_ALARM, LOOKALIKE_ALARM], tasks_for_search=[{"id": "ocid1.task.console"}],
                         task_details={"ocid1.task.console": CONSOLE_TASK})

    found = await find_alerts_for_saved_search(client, cache(), "ocid1.mss.console")

    assert [a.alarm_id for a in found] == ["ocid1.alarm.console"]
    alert = found[0]
    assert alert.created_by == "console"
    assert alert.condition == "test_metric[5m].max() > 0"
    assert (alert.threshold_operator, alert.threshold_value) == ("gt", "0")
    assert (alert.metric_namespace, alert.metric_name) == ("test_detection_rule", "test_metric")
    assert alert.backing_saved_search_id == "ocid1.mss.console"
    assert alert.backing_metric_task_id == "ocid1.task.console"
    assert alert.query == "* | stats count"
    client.get_saved_search.assert_awaited_once_with("ocid1.mss.console")
    assert alert.schedule == "0 */5 * ? * *" and alert.schedule_format == "oci"


@pytest.mark.asyncio
async def test_same_metric_token_in_other_namespace_is_not_a_log_alert():
    client = make_client([LOOKALIKE_ALARM, CONSOLE_ALARM], tasks_for_search=[{"id": "ocid1.task.console"}],
                         task_details={"ocid1.task.console": CONSOLE_TASK})

    found = await find_alerts_for_saved_search(client, cache(), "ocid1.mss.console")

    assert [a.alarm_id for a in found] == ["ocid1.alarm.console"]


@pytest.mark.asyncio
async def test_task_without_metric_extraction_is_ignored():
    plain = {**CONSOLE_TASK, "id": "ocid1.task.plain", "metric_namespace": None, "metric_name": None}
    client = make_client([CONSOLE_ALARM], tasks_for_search=[{"id": "ocid1.task.plain"}, {"id": "ocid1.task.console"}],
                         task_details={"ocid1.task.plain": plain, "ocid1.task.console": CONSOLE_TASK})

    found = await find_alerts_for_saved_search(client, cache(), "ocid1.mss.console")

    assert [a.backing_metric_task_id for a in found] == ["ocid1.task.console"]


@pytest.mark.asyncio
async def test_find_log_alerts_merges_tagged_and_console_and_caches():
    logan_task = {**CONSOLE_TASK, "id": "ocid1.task.logan", "saved_search_id": "ocid1.mss.logan",
                  "metric_namespace": "logan_custom_metrics", "metric_name": "logan_alert_abc",
                  "metric_resource_group": "logan_alerts"}
    client = make_client([LOGAN_ALARM, CONSOLE_ALARM, LOOKALIKE_ALARM],
                         all_tasks=[{"id": "ocid1.task.console"}, {"id": "ocid1.task.logan"}],
                         task_details={"ocid1.task.console": CONSOLE_TASK, "ocid1.task.logan": logan_task})
    store = cache()

    found = await find_log_alerts(client, store)

    assert {a.alarm_id: a.created_by for a in found} == {
        "ocid1.alarm.logan": "logan", "ocid1.alarm.console": "console"}
    client.list_alarms.reset_mock()
    client.get_scheduled_task_details.reset_mock()
    again = await find_log_alerts(client, store)
    assert [a.alarm_id for a in again] == [a.alarm_id for a in found]
    client.list_alarms.assert_not_called()
    client.get_scheduled_task_details.assert_not_called()

    invalidate(store, COMPARTMENT)
    await find_log_alerts(client, store)
    client.list_alarms.assert_awaited_once()
