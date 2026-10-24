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
        assert alarm.query == f"{metric}[15m].max() > 0"
        assert alarm.pending_duration == "PT1M"
        assert alarm.freeform_tags["logan_schedule"] == "*/15 * * * *"
        task = client.create_scheduled_task.call_args[0][0]
        assert task.schedules[0].expression == "0 */15 * ? * *"

    @pytest.mark.asyncio
    async def test_lower_bound_thresholds_use_the_minimum_value(self):
        client = make_client()

        await make_svc(client).create_alert(
            display_name="t", query="* | stats count", destination_topic_id="ocid1.topic.1",
            threshold_operator="lt", threshold_value=5, schedule="*/5 * * * *",
        )

        alarm = client.create_alarm.call_args[0][0]
        assert alarm.query.endswith("[5m].min() < 5")


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
        assert alarm.query == "logan_alert_abc[5m].max() > 3"
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
        assert alarm.query == "logan_alert_abc[1h].max() >= 5"

    @pytest.mark.asyncio
    async def test_operator_only_update_keeps_the_current_threshold(self):
        client = make_client()
        client.get_alarm.return_value = existing_alarm("logan_alert_abc[15m].max() > 7")
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", threshold_operator="lte")

        alarm = client.update_alarm.call_args[0][1]
        assert alarm.query == "logan_alert_abc[15m].min() <= 7"

    @pytest.mark.asyncio
    async def test_legacy_count_condition_is_rewritten_on_update(self):
        client = make_client()
        client.get_alarm.return_value = existing_alarm("logan_alert_abc123[1m].count() > 0", "0 */15 * * *")
        client.update_alarm.return_value = {"id": "ocid1.alarm.1"}

        await make_svc(client).update_alert("ocid1.alarm.1", threshold_value=2)

        alarm = client.update_alarm.call_args[0][1]
        # Runs at 00:00 and 15:00 UTC: the longest gap is 15 hours.
        assert alarm.query == "logan_alert_abc123[15h].max() > 2"


class TestDefaultSchedule:
    def test_service_handler_and_tool_share_one_default(self):
        assert DEFAULT_ALERT_SCHEDULE == "*/15 * * * *"
        assert inspect.signature(AlarmService.create_alert).parameters["schedule"].default == DEFAULT_ALERT_SCHEDULE
        description = next(tool for tool in get_tools() if tool["name"] == "create_alert")
        schedule_doc = description["inputSchema"]["properties"]["schedule"]["description"]
        assert DEFAULT_ALERT_SCHEDULE in schedule_doc
        assert "0 */15 * * *" not in schedule_doc
        assert "24/7" not in description["description"]
