"""The alarm must compare the extracted value, keep each run's datapoint in its window, and
watch continuously by default (release review RQ-1 to RQ-3)."""
import inspect

import pytest
from unittest.mock import AsyncMock

from oci_logan_mcp.alarm_service import DEFAULT_ALERT_SCHEDULE, AlarmService
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import CacheConfig
from oci_logan_mcp.tools import get_tools


def make_client():
    client = AsyncMock()
    client.compartment_id = "ocid1.compartment.test"
    client.tenancy_id = "ocid1.tenancy.test"
    client._config = {"region": "us-ashburn-1"}
    client.get_topic.return_value = {"id": "ocid1.topic.1", "lifecycle_state": "ACTIVE"}
    client.create_management_saved_search.return_value = {"id": "ocid1.mss.1"}
    client.create_scheduled_task.return_value = {"id": "ocid1.task.1"}
    client.create_alarm.return_value = {"id": "ocid1.alarm.1", "freeform_tags": {}}
    return client


def make_svc(client):
    return AlarmService(client, CacheManager(CacheConfig(enabled=True)))


def existing_alarm(query, schedule="*/15 * * * *"):
    return {
        "id": "ocid1.alarm.1",
        "freeform_tags": {
            "logan_backing_saved_search_id": "ocid1.mss.1",
            "logan_backing_metric_task_id": "ocid1.task.1",
            "logan_query": "* | stats count",
            "logan_schedule": schedule,
        },
        "query": query,
        "pending_duration": "PT5M",
        "severity": "CRITICAL",
        "destinations": ["ocid1.topic.1"],
    }


class TestCreateCondition:
    @pytest.mark.asyncio
    async def test_default_alert_compares_the_extracted_value_every_fifteen_minutes(self):
        client = make_client()

        await make_svc(client).create_alert(
            display_name="t", query="* | stats count", destination_topic_id="ocid1.topic.1",
        )

        alarm = client.create_alarm.call_args[0][0]
        metric = alarm.query.split("[")[0]
        assert metric.startswith("logan_alert_")
        assert alarm.query == f"{metric}[15m].last() > 0"
        assert alarm.pending_duration == "PT1M"
        assert alarm.freeform_tags["logan_schedule"] == "*/15 * * * *"
        task = client.create_scheduled_task.call_args[0][0]
        assert task.schedules[0].expression == "0 */15 * ? * *"

    @pytest.mark.asyncio
    async def test_lower_bound_thresholds_also_follow_the_latest_value(self):
        client = make_client()

        await make_svc(client).create_alert(
            display_name="t", query="* | stats count", destination_topic_id="ocid1.topic.1",
            threshold_operator="lt", threshold_value=5, schedule="*/5 * * * *",
        )

        alarm = client.create_alarm.call_args[0][0]
        assert alarm.query.endswith("[5m].last() < 5")


class TestResourceGroup:
    """The task posts under resource group logan_alerts; an alarm without that resource group
    only sees metrics that have none, so it never fires (live finding, 2026-10-24)."""

    @pytest.mark.asyncio
    async def test_alarm_matches_the_metric_resource_group(self):
        client = make_client()

        await make_svc(client).create_alert(
            display_name="t", query="* | stats count", destination_topic_id="ocid1.topic.1",
        )

        task = client.create_scheduled_task.call_args[0][0]
        alarm = client.create_alarm.call_args[0][0]
        assert task.action.metric_extraction.resource_group == "logan_alerts"
        assert alarm.resource_group == "logan_alerts"

    @pytest.mark.asyncio
    async def test_condition_update_repairs_a_legacy_alarm_without_resource_group(self):
        client = make_client()
        client.get_alarm.return_value = existing_alarm("logan_alert_abc[1m].count() > 0", "0 */15 * * *")
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", threshold_value=1)

        alarm = client.update_alarm.call_args[0][1]
        assert alarm.resource_group == "logan_alerts"


class TestLegacyPendingDuration:
    @pytest.mark.asyncio
    async def test_condition_repair_also_resets_the_pending_duration(self):
        client = make_client()
        client.get_alarm.return_value = existing_alarm("logan_alert_abc[1m].count() > 0", "0 */15 * * *")
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", threshold_value=1)

        alarm = client.update_alarm.call_args[0][1]
        assert alarm.pending_duration == "PT1M"

    @pytest.mark.asyncio
    async def test_display_name_only_update_leaves_the_condition_alone(self):
        client = make_client()
        client.get_alarm.return_value = existing_alarm("logan_alert_abc[15m].last() > 3")
        client.update_management_saved_search.return_value = {"id": "ocid1.mss.1"}
        client.update_scheduled_task.return_value = {"id": "ocid1.task.1"}
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", display_name="renamed")

        alarm = client.update_alarm.call_args[0][1]
        assert alarm.query is None and alarm.pending_duration is None


class TestAlarmInterval:
    @pytest.mark.parametrize("schedule, interval", [
        ("*/5 * * * *", "5m"),
        ("0,30 * * * *", "30m"),
        ("0 * * * *", "1h"),
        ("30 */6 * * *", "6h"),
        ("15 2,14 * * *", "12h"),
        ("0 9 * * *", "1d"),
        ("0 9 * * 1", "1d"),
        ("0 9 1 * *", "1d"),
    ])
    def test_interval_covers_the_longest_gap_between_runs(self, schedule, interval):
        assert AlarmService.alarm_interval(schedule) == interval

    def test_interval_is_capped_at_one_day_for_uneven_hours(self):
        assert AlarmService.alarm_interval("0,30 9 * * *") == "1d"


class TestUpdateCondition:
    @pytest.mark.asyncio
    async def test_schedule_update_rebuilds_the_window_and_keeps_the_threshold(self):
        client = make_client()
        client.get_alarm.return_value = existing_alarm("logan_alert_abc[15m].max() > 3")
        client.update_scheduled_task.return_value = {"id": "ocid1.task.1"}
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", schedule="*/5 * * * *")

        task = client.update_scheduled_task.call_args[0][1]
        assert task.schedules[0].expression == "0 */5 * ? * *"
        alarm = client.update_alarm.call_args[0][1]
        assert alarm.query == "logan_alert_abc[5m].last() > 3"
        assert alarm.freeform_tags["logan_schedule"] == "*/5 * * * *"

    @pytest.mark.asyncio
    async def test_threshold_update_keeps_the_schedule_window(self):
        client = make_client()
        client.get_alarm.return_value = existing_alarm("logan_alert_abc[1h].max() > 0", "0 * * * *")
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert(
            "ocid1.alarm.1", threshold_value=5, threshold_operator="gte",
        )

        alarm = client.update_alarm.call_args[0][1]
        assert alarm.query == "logan_alert_abc[1h].last() >= 5"

    @pytest.mark.asyncio
    async def test_operator_only_update_keeps_the_current_threshold(self):
        client = make_client()
        client.get_alarm.return_value = existing_alarm("logan_alert_abc[15m].max() > 7")
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", threshold_operator="lte")

        alarm = client.update_alarm.call_args[0][1]
        assert alarm.query == "logan_alert_abc[15m].last() <= 7"

    @pytest.mark.asyncio
    async def test_legacy_count_condition_is_rewritten_on_update(self):
        client = make_client()
        client.get_alarm.return_value = existing_alarm("logan_alert_abc123[1m].count() > 0", "0 */15 * * *")
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", threshold_value=2)

        alarm = client.update_alarm.call_args[0][1]
        # Runs at 00:00 and 15:00 UTC: the longest gap is 15 hours.
        assert alarm.query == "logan_alert_abc123[15h].last() > 2"


class TestDefaultSchedule:
    def test_service_handler_and_tool_share_one_default(self):
        assert DEFAULT_ALERT_SCHEDULE == "*/15 * * * *"
        assert inspect.signature(AlarmService.create_alert).parameters["schedule"].default == DEFAULT_ALERT_SCHEDULE
        description = next(tool for tool in get_tools() if tool["name"] == "create_alert")
        schedule_doc = description["inputSchema"]["properties"]["schedule"]["description"]
        assert DEFAULT_ALERT_SCHEDULE in schedule_doc
        assert "0 */15 * * *" not in schedule_doc
        assert "24/7" not in description["description"]


class TestQueryUpdate:
    """A query change rewrites the saved search and refreshes the alarm's stored text."""

    @pytest.mark.asyncio
    async def test_query_update_refreshes_tag_body_and_repairs(self):
        client = make_client()
        client.get_alarm.return_value = {
            **existing_alarm("logan_alert_abc[1m].count() > 0", "0 */15 * * *"), "display_name": "VCN flow spike"}
        client.get_saved_search.return_value = {"id": "ocid1.mss.1", "ui_config": {"queryString": "old"}}
        client.update_management_saved_search.return_value = {"id": "ocid1.mss.1"}
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}
        new_query = "'Log Source' = 'OCI VCN Flow Unified Schema Logs' and Action = 'REJECT' | stats count"

        result = await make_svc(client).update_alert("ocid1.alarm.1", query=new_query)

        client.update_alarm.assert_called_once()
        alarm = client.update_alarm.call_args[0][1]
        assert alarm.freeform_tags["logan_query"] == new_query
        assert alarm.freeform_tags["logan_backing_saved_search_id"] == "ocid1.mss.1"
        assert alarm.body == f"Logan alert: VCN flow spike\nQuery: {new_query}"
        assert alarm.query == "logan_alert_abc[15h].last() > 0"
        assert alarm.resource_group == "logan_alerts"
        assert alarm.pending_duration == "PT1M"
        assert "query" in result["updated"]


class TestConsoleOriginAlarm:
    """A console-made alarm keeps its own interval and statistic; we change only what was asked."""

    @pytest.mark.asyncio
    async def test_threshold_update_keeps_console_condition_shape(self):
        client = make_client()
        client.get_alarm.return_value = {
            **existing_alarm("test_metric[5m].max() > 0", "*/15 * * * *"),
            "freeform_tags": {"logan_origin": "console", "logan_managed": "true",
                              "logan_kind": "monitoring_alarm", "logan_schedule": ""},
        }
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", threshold_value=3, threshold_operator="gte")

        alarm = client.update_alarm.call_args[0][1]
        assert alarm.query == "test_metric[5m].max() >= 3"
        assert alarm.resource_group is None and alarm.pending_duration is None

    @pytest.mark.asyncio
    async def test_query_update_on_console_alarm_refreshes_tags_only(self):
        client = make_client()
        client.get_alarm.return_value = {
            **existing_alarm("test_metric[5m].max() > 0", ""),
            "freeform_tags": {"logan_origin": "console", "logan_managed": "true", "logan_kind": "monitoring_alarm",
                              "logan_backing_saved_search_id": "ocid1.mss.1", "logan_backing_metric_task_id": "ocid1.task.1"},
        }
        client.get_saved_search.return_value = {"id": "ocid1.mss.1", "ui_config": {}}
        client.update_management_saved_search.return_value = {"id": "ocid1.mss.1"}
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", query="* | stats count as n")

        alarm = client.update_alarm.call_args[0][1]
        assert alarm.freeform_tags["logan_query"] == "* | stats count as n"
        assert alarm.query is None and alarm.body is None and alarm.pending_duration is None
