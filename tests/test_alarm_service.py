"""Tests for AlarmService."""
import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

from oci_logan_mcp.alarm_service import AlarmService
from oci_logan_mcp.cache import CacheManager
from oci_logan_mcp.config import CacheConfig


def make_client():
    client = AsyncMock()
    client.compartment_id = "ocid1.compartment.test"
    client.get_topic.return_value = {"id": "ocid1.topic.1", "lifecycle_state": "ACTIVE"}
    client.create_management_saved_search.return_value = {
        "id": "ocid1.mss.1", "display_name": "logan-alert-test"
    }
    client.create_scheduled_task.return_value = {
        "id": "ocid1.task.1", "display_name": "logan-alert-task-test"
    }
    client.create_alarm.return_value = {
        "id": "ocid1.alarm.1", "display_name": "test alert",
        "lifecycle_state": "ACTIVE", "freeform_tags": {}
    }
    return client


def make_svc(client=None):
    return AlarmService(client or make_client(), CacheManager(CacheConfig(enabled=True)))


class TestCreateAlert:
    @pytest.mark.asyncio
    async def test_creates_all_4_resources(self):
        client = make_client()
        svc = make_svc(client)
        result = await svc.create_alert(
            display_name="test alert",
            query="* | stats count",
            destination_topic_id="ocid1.topic.1",
            compartment_id="ocid1.compartment.test",
        )
        assert client.get_topic.called
        assert client.create_management_saved_search.called
        assert client.create_scheduled_task.called
        assert client.create_alarm.called
        assert result["alarm_id"] == "ocid1.alarm.1"
        assert result["backing_saved_search_id"] == "ocid1.mss.1"
        assert result["backing_metric_task_id"] == "ocid1.task.1"

    @pytest.mark.asyncio
    async def test_metric_name_is_uuid_derived(self):
        client = make_client()
        svc = make_svc(client)
        await svc.create_alert(
            display_name="test",
            query="* | stats count",
            destination_topic_id="ocid1.topic.1",
        )
        alarm_call = client.create_alarm.call_args[0][0]
        assert "logan_alert_" in alarm_call.query

    @pytest.mark.asyncio
    async def test_rollback_if_alarm_creation_fails(self):
        client = make_client()
        client.create_alarm.side_effect = Exception("alarm API error")
        svc = make_svc(client)
        with pytest.raises(Exception, match="alarm API error"):
            await svc.create_alert(
                display_name="test",
                query="* | stats count",
                destination_topic_id="ocid1.topic.1",
            )
        assert client.delete_scheduled_task.called
        assert client.delete_management_saved_search.called

    @pytest.mark.asyncio
    async def test_rollback_if_task_creation_fails(self):
        client = make_client()
        client.create_scheduled_task.side_effect = Exception("task error")
        svc = make_svc(client)
        with pytest.raises(Exception):
            await svc.create_alert(
                display_name="test",
                query="* | stats count",
                destination_topic_id="ocid1.topic.1",
            )
        assert client.delete_management_saved_search.called
        assert not client.delete_scheduled_task.called

    @pytest.mark.asyncio
    async def test_invalid_cron_raises(self):
        svc = make_svc()
        with pytest.raises(ValueError, match="cron"):
            await svc.create_alert(
                display_name="test",
                query="* | stats count",
                destination_topic_id="ocid1.topic.1",
                schedule="not-a-cron",
            )

    @pytest.mark.asyncio
    async def test_non_aggregating_query_raises(self):
        svc = make_svc()
        with pytest.raises(ValueError, match="numeric aggregation"):
            await svc.create_alert(
                display_name="test",
                query="* | where Severity = 'ERROR'",
                destination_topic_id="ocid1.topic.1",
            )


class TestDeleteAlert:
    @pytest.mark.asyncio
    async def test_deletes_backing_first_alarm_last(self):
        client = AsyncMock()
        client.get_alarm.return_value = {
            "id": "ocid1.alarm.1",
            "freeform_tags": {
                "logan_backing_saved_search_id": "ocid1.mss.1",
                "logan_backing_metric_task_id": "ocid1.task.1",
            }
        }
        svc = make_svc(client)
        call_order = []
        client.delete_scheduled_task.side_effect = lambda *a, **kw: call_order.append("task")
        client.delete_management_saved_search.side_effect = lambda *a, **kw: call_order.append("mss")
        client.delete_alarm.side_effect = lambda *a, **kw: call_order.append("alarm")

        await svc.delete_alert("ocid1.alarm.1")

        assert call_order == ["task", "mss", "alarm"]

    @pytest.mark.asyncio
    async def test_handles_404_gracefully(self):
        client = AsyncMock()
        client.get_alarm.return_value = {
            "id": "ocid1.alarm.1",
            "freeform_tags": {
                "logan_backing_saved_search_id": "ocid1.mss.1",
                "logan_backing_metric_task_id": "ocid1.task.1",
            }
        }
        import oci
        not_found = oci.exceptions.ServiceError(404, "NotFound", {}, "not found")
        client.delete_scheduled_task.side_effect = not_found
        svc = make_svc(client)

        await svc.delete_alert("ocid1.alarm.1")
        assert client.delete_management_saved_search.called
        assert client.delete_alarm.called

    @pytest.mark.asyncio
    async def test_partial_failure_returns_report(self):
        client = AsyncMock()
        client.get_alarm.return_value = {
            "id": "ocid1.alarm.1",
            "freeform_tags": {
                "logan_backing_saved_search_id": "ocid1.mss.1",
                "logan_backing_metric_task_id": "ocid1.task.1",
            }
        }
        import oci
        client.delete_management_saved_search.side_effect = oci.exceptions.ServiceError(
            500, "InternalError", {}, "server error"
        )
        svc = make_svc(client)

        result = await svc.delete_alert("ocid1.alarm.1")
        assert result["partial_failure"] is True
        assert "ocid1.mss.1" in result["remaining"]


class TestListAlerts:
    @pytest.mark.asyncio
    async def test_filters_by_logan_managed_and_kind(self):
        client = AsyncMock()
        client.list_alarms.return_value = [
            {"id": "ocid1.alarm.1", "display_name": "my alert",
             "freeform_tags": {"logan_managed": "true", "logan_kind": "monitoring_alarm",
                               "logan_query": "* | stats count", "logan_schedule": "0 */15 * * *"}},
            {"id": "ocid1.alarm.2", "display_name": "user alarm",
             "freeform_tags": {}},
            {"id": "ocid1.alarm.3", "display_name": "other",
             "freeform_tags": {"logan_managed": "true", "logan_kind": "something_else"}},
        ]
        svc = make_svc(client)
        results = await svc.list_alerts()
        assert len(results) == 1
        assert results[0]["alarm_id"] == "ocid1.alarm.1"


class TestUpdateAlert:
    @pytest.mark.asyncio
    async def test_query_update_only_touches_saved_search(self):
        client = AsyncMock()
        client.get_alarm.return_value = {
            "id": "ocid1.alarm.1",
            "freeform_tags": {
                "logan_backing_saved_search_id": "ocid1.mss.1",
                "logan_backing_metric_task_id": "ocid1.task.1",
                "logan_query": "* | stats count",
                "logan_schedule": "0 */15 * * *",
            },
            "query": "logan_alert_abc123[1m].count() > 0",
            "severity": "CRITICAL",
            "destinations": ["ocid1.topic.1"],
        }
        client.update_management_saved_search.return_value = {"id": "ocid1.mss.1"}
        svc = make_svc(client)

        await svc.update_alert("ocid1.alarm.1", query="* | stats avg(duration)")

        assert client.update_management_saved_search.called
        assert not client.update_scheduled_task.called
        assert not client.update_alarm.called

    @pytest.mark.asyncio
    async def test_display_name_updates_all_three(self):
        client = AsyncMock()
        client.get_alarm.return_value = {
            "id": "ocid1.alarm.1",
            "freeform_tags": {
                "logan_backing_saved_search_id": "ocid1.mss.1",
                "logan_backing_metric_task_id": "ocid1.task.1",
                "logan_query": "* | stats count",
                "logan_schedule": "0 */15 * * *",
            },
            "query": "logan_alert_abc123[1m].count() > 0",
            "severity": "CRITICAL",
            "destinations": ["ocid1.topic.1"],
        }
        client.update_management_saved_search.return_value = {"id": "ocid1.mss.1"}
        client.update_scheduled_task.return_value = {"id": "ocid1.task.1"}
        client.update_alarm.return_value = {"id": "ocid1.alarm.1", "freeform_tags": {}}
        svc = make_svc(client)

        await svc.update_alert("ocid1.alarm.1", display_name="renamed")

        assert client.update_management_saved_search.called
        assert client.update_scheduled_task.called
        assert client.update_alarm.called
