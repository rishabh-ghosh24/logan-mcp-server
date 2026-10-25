"""Alert chains are discovered through OCI's own ids; our tags are only a cache."""
import pytest
from unittest.mock import AsyncMock

from oci_logan_mcp.alert_links import (
    find_alerts_for_saved_search, find_log_alerts, parse_condition,
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

    found = await find_alerts_for_saved_search(client, "ocid1.mss.logan")

    assert [a.alarm_id for a in found] == ["ocid1.alarm.logan"]
    assert found[0].created_by == "logan"
    assert found[0].query == LOGAN_ALARM["freeform_tags"]["logan_query"]
    assert found[0].schedule == "*/15 * * * *" and found[0].schedule_format == "logan"
    client.list_scheduled_tasks_for_saved_search.assert_not_called()


@pytest.mark.asyncio
async def test_slow_path_matches_console_alarm_by_namespace_and_metric_token():
    client = make_client([CONSOLE_ALARM, LOOKALIKE_ALARM], tasks_for_search=[{"id": "ocid1.task.console"}],
                         task_details={"ocid1.task.console": CONSOLE_TASK})

    found = await find_alerts_for_saved_search(client, "ocid1.mss.console")

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
async def test_console_rule_on_a_fixed_frequency_schedule_reports_its_interval():
    fixed = {**CONSOLE_TASK, "cron": None, "interval": "PT5M"}
    client = make_client([CONSOLE_ALARM], tasks_for_search=[{"id": "ocid1.task.console"}],
                         task_details={"ocid1.task.console": fixed})

    [alert] = await find_alerts_for_saved_search(client, "ocid1.mss.console")

    assert (alert.schedule, alert.schedule_format) == ("PT5M", "interval")


@pytest.mark.asyncio
async def test_console_rule_without_a_readable_schedule_says_so():
    bare = {**CONSOLE_TASK, "cron": None, "interval": None}
    client = make_client([CONSOLE_ALARM], tasks_for_search=[{"id": "ocid1.task.console"}],
                         task_details={"ocid1.task.console": bare})

    [alert] = await find_alerts_for_saved_search(client, "ocid1.mss.console")

    assert (alert.schedule, alert.schedule_format) == ("", "unknown")


@pytest.mark.asyncio
async def test_same_metric_token_in_other_namespace_is_not_a_log_alert():
    client = make_client([LOOKALIKE_ALARM, CONSOLE_ALARM], tasks_for_search=[{"id": "ocid1.task.console"}],
                         task_details={"ocid1.task.console": CONSOLE_TASK})

    found = await find_alerts_for_saved_search(client, "ocid1.mss.console")

    assert [a.alarm_id for a in found] == ["ocid1.alarm.console"]


@pytest.mark.asyncio
async def test_task_without_metric_extraction_is_ignored():
    plain = {**CONSOLE_TASK, "id": "ocid1.task.plain", "metric_namespace": None, "metric_name": None}
    client = make_client([CONSOLE_ALARM], tasks_for_search=[{"id": "ocid1.task.plain"}, {"id": "ocid1.task.console"}],
                         task_details={"ocid1.task.plain": plain, "ocid1.task.console": CONSOLE_TASK})

    found = await find_alerts_for_saved_search(client, "ocid1.mss.console")

    assert [a.backing_metric_task_id for a in found] == ["ocid1.task.console"]


@pytest.mark.asyncio
async def test_find_log_alerts_merges_tagged_and_console_and_reads_live_every_time():
    """Nothing about alerts is cached: every call sees the alarms and rules as they are now."""
    logan_task = {**CONSOLE_TASK, "id": "ocid1.task.logan", "saved_search_id": "ocid1.mss.logan",
                  "metric_namespace": "logan_custom_metrics", "metric_name": "logan_alert_abc",
                  "metric_resource_group": "logan_alerts"}
    client = make_client([LOGAN_ALARM, CONSOLE_ALARM, LOOKALIKE_ALARM],
                         all_tasks=[{"id": "ocid1.task.console"}, {"id": "ocid1.task.logan"}],
                         task_details={"ocid1.task.console": CONSOLE_TASK, "ocid1.task.logan": logan_task})

    found = await find_log_alerts(client)

    assert {a.alarm_id: a.created_by for a in found} == {
        "ocid1.alarm.logan": "logan", "ocid1.alarm.console": "console"}
    again = await find_log_alerts(client)
    assert [a.alarm_id for a in again] == [a.alarm_id for a in found]
    assert client.list_alarms.await_count == 2
    assert client.list_saved_searches.await_count == 2
    assert client.get_scheduled_task_details.await_count == 4


@pytest.mark.asyncio
async def test_find_log_alerts_reflects_alarm_and_rule_changes_made_anywhere_at_once():
    """An alarm or rule deleted in the console, or created a second ago, shows on the next call."""
    client = make_client([LOGAN_ALARM, CONSOLE_ALARM], all_tasks=[{"id": "ocid1.task.console"}],
                         task_details={"ocid1.task.console": CONSOLE_TASK})
    first = await find_log_alerts(client)
    assert {a.alarm_id for a in first} == {"ocid1.alarm.logan", "ocid1.alarm.console"}

    client.list_alarms.return_value = [LOGAN_ALARM]  # console alarm deleted outside the server
    second = await find_log_alerts(client)
    assert {a.alarm_id for a in second} == {"ocid1.alarm.logan"}

    client.list_alarms.return_value = [CONSOLE_ALARM]  # logan alarm deleted, console one recreated
    client.list_saved_searches.return_value = []  # and its detection rule removed in the console
    third = await find_log_alerts(client)
    assert third == []
    client.list_scheduled_tasks_for_saved_search.assert_not_called()


def test_parse_condition_gives_no_threshold_for_compound_or_absence_conditions():
    """A compound condition must not have one half's threshold reported as the whole."""
    assert parse_condition("m1[1m].mean() > 8 && m2[1m].max() < 3") == ("m1", None, None)
    assert parse_condition("m1[1m].mean() > 80 || m1[1m].mean() < 5") == ("m1", None, None)
    assert parse_condition("CpuUtilization[1m].absent(5m)") == ("CpuUtilization", None, None)
    assert parse_condition("break_in_attempts[5m].groupBy(host).mean() > 8") == ("break_in_attempts", "gt", "8")


class TestListAlerts:
    """list_alerts shows every log alert, ours and console-made, with its live condition."""

    def make_service(self, client):
        from oci_logan_mcp.alarm_service import AlarmService
        return AlarmService(client, cache())

    @pytest.mark.asyncio
    async def test_list_alerts_includes_console_alert_with_resolved_query(self):
        client = make_client([LOGAN_ALARM, CONSOLE_ALARM, LOOKALIKE_ALARM],
                             all_tasks=[{"id": "ocid1.task.console"}],
                             task_details={"ocid1.task.console": CONSOLE_TASK})

        rows = await self.make_service(client).list_alerts()

        console = next(r for r in rows if r["alarm_id"] == "ocid1.alarm.console")
        assert console["created_by"] == "console"
        assert console["query"] == "* | stats count"
        assert console["condition"] == "test_metric[5m].max() > 0"
        assert (console["threshold_operator"], console["threshold_value"]) == ("gt", "0")
        assert (console["schedule"], console["schedule_format"]) == ("0 */5 * ? * *", "oci")
        assert console["backing_saved_search_id"] == "ocid1.mss.console"
        assert console["backing_metric_task_id"] == "ocid1.task.console"
        assert "ocid1.alarm.cpu" not in {r["alarm_id"] for r in rows}

    @pytest.mark.asyncio
    async def test_list_alerts_keeps_existing_keys_for_logan_alerts(self):
        tagged = {**LOGAN_ALARM, "freeform_tags": {**LOGAN_ALARM["freeform_tags"], "logan_group_id": "g1"}}
        client = make_client([tagged])

        rows = await self.make_service(client).list_alerts()

        assert len(rows) == 1
        row = rows[0]
        assert row["created_by"] == "logan"
        assert row["alert_group"] == "g1"
        assert row["query"] == LOGAN_ALARM["freeform_tags"]["logan_query"]
        assert (row["schedule"], row["schedule_format"]) == ("*/15 * * * *", "logan")
        assert row["condition"] == "logan_alert_abc[15m].last() > 50"
        assert (row["threshold_operator"], row["threshold_value"]) == ("gt", "50")
        for key in ("alarm_id", "display_name", "severity", "lifecycle_state",
                    "backing_saved_search_id", "backing_metric_task_id"):
            assert key in row


class TestSavedSearchUpdate:
    """Editing a search that backs alerts refreshes those alarms inside the same operation."""

    NEW_QUERY = "'Log Source' = 'OCI VCN Flow Unified Schema Logs' and Action = 'REJECT' | stats count"
    SEARCH_ID = "ocid1.managementsavedsearch.oc1..edited"

    def make_service(self, client):
        from oci_logan_mcp.alarm_service import AlarmService
        from oci_logan_mcp.saved_search import SavedSearchService
        store = cache()
        return SavedSearchService(client, store, alarm_service=AlarmService(client, store))

    @pytest.mark.asyncio
    async def test_query_update_refreshes_each_linked_logan_alarm_and_reports_them(self):
        tagged = {**LOGAN_ALARM, "freeform_tags": {**LOGAN_ALARM["freeform_tags"],
                                                    "logan_backing_saved_search_id": self.SEARCH_ID}}
        client = make_client([tagged])
        client.get_saved_search.return_value = {"id": self.SEARCH_ID, "ui_config": {"queryString": "old"}}
        client.get_alarm.return_value = tagged
        client.update_management_saved_search.return_value = {"id": self.SEARCH_ID}
        client.update_alarm.return_value = {"id": "ocid1.alarm.logan"}

        result = await self.make_service(client).update_search(self.SEARCH_ID, query=self.NEW_QUERY)

        client.update_alarm.assert_called_once()
        alarm = client.update_alarm.call_args[0][1]
        assert alarm.freeform_tags["logan_query"] == self.NEW_QUERY
        assert alarm.query == "logan_alert_abc[15m].last() > 50"
        assert alarm.resource_group == "logan_alerts" and alarm.pending_duration == "PT1M"
        assert result["linked_alerts_updated"] == [
            {"alarm_id": "ocid1.alarm.logan", "display_name": "VCN flow spike", "created_by": "logan"}]

    @pytest.mark.asyncio
    async def test_console_alarm_is_stamped_with_our_tags_and_keeps_its_own(self):
        task = {**CONSOLE_TASK, "saved_search_id": self.SEARCH_ID}
        client = make_client([CONSOLE_ALARM], tasks_for_search=[{"id": "ocid1.task.console"}],
                             task_details={"ocid1.task.console": task})
        client.get_saved_search.return_value = {"id": self.SEARCH_ID, "query": "* | stats count",
                                                "ui_config": {"queryString": "old"}}
        client.get_alarm.return_value = CONSOLE_ALARM
        client.update_management_saved_search.return_value = {"id": self.SEARCH_ID}
        client.update_alarm.return_value = {"id": "ocid1.alarm.console"}

        result = await self.make_service(client).update_search(self.SEARCH_ID, query=self.NEW_QUERY)

        client.update_alarm.assert_called_once()
        alarm = client.update_alarm.call_args[0][1]
        tags = alarm.freeform_tags
        assert tags["team"] == "x"
        assert tags["logan_managed"] == "true" and tags["logan_kind"] == "monitoring_alarm"
        assert tags["logan_origin"] == "console"
        assert tags["logan_backing_saved_search_id"] == self.SEARCH_ID
        assert tags["logan_backing_metric_task_id"] == "ocid1.task.console"
        assert tags["logan_query"] == self.NEW_QUERY
        assert alarm.query is None and alarm.body is None and alarm.pending_duration is None
        assert result["linked_alerts_updated"] == [
            {"alarm_id": "ocid1.alarm.console", "display_name": "Console VCN", "created_by": "console"}]

    @pytest.mark.asyncio
    async def test_stamped_console_alarm_keeps_its_provenance_in_list_alerts(self):
        stamped = {**CONSOLE_ALARM, "freeform_tags": {
            "team": "x", "logan_managed": "true", "logan_kind": "monitoring_alarm", "logan_origin": "console",
            "logan_backing_saved_search_id": self.SEARCH_ID, "logan_backing_metric_task_id": "ocid1.task.console",
            "logan_query": self.NEW_QUERY}}
        client = make_client([stamped])

        found = await find_alerts_for_saved_search(client, self.SEARCH_ID)

        assert [(a.alarm_id, a.created_by, a.query) for a in found] == [
            ("ocid1.alarm.console", "console", self.NEW_QUERY)]
        client.list_scheduled_tasks_for_saved_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_linked_alert_means_no_alarm_calls_and_no_key(self):
        client = make_client([LOOKALIKE_ALARM])
        client.get_saved_search.return_value = {"id": self.SEARCH_ID, "ui_config": {}}
        client.update_management_saved_search.return_value = {"id": self.SEARCH_ID}

        result = await self.make_service(client).update_search(self.SEARCH_ID, query=self.NEW_QUERY)

        client.update_alarm.assert_not_called()
        assert "linked_alerts_updated" not in result
