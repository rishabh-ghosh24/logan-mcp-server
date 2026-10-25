"""Before a query change is confirmed, the user is told which alerts depend on it."""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.alert_links import LinkedAlert, describe_linked_alerts
from oci_logan_mcp.audit import AuditLogger
from oci_logan_mcp.config import Settings
from oci_logan_mcp.handlers import MCPHandlers
from oci_logan_mcp.preferences import PreferenceStore
from oci_logan_mcp.secret_store import SecretStore
from oci_logan_mcp.user_store import UserStore

SEARCH_ID = "ocid1.managementsavedsearch.oc1..vcn"
NEW_QUERY = "'Log Source' = 'OCI VCN Flow Unified Schema Logs' and Action = 'REJECT' | stats count"
LOGAN_ALARM = {
    "id": "ocid1.alarm.logan", "display_name": "VCN flow spike", "severity": "WARNING",
    "lifecycle_state": "ACTIVE", "namespace": "logan_custom_metrics", "resource_group": "logan_alerts",
    "query": "logan_alert_abc[15m].last() > 50",
    "freeform_tags": {"logan_managed": "true", "logan_kind": "monitoring_alarm",
                      "logan_backing_saved_search_id": SEARCH_ID, "logan_backing_metric_task_id": "ocid1.task.logan",
                      "logan_query": "* | stats count", "logan_schedule": "*/15 * * * *"},
}


def make_handlers(tmp_path, alarms):
    settings = Settings()
    settings.log_analytics.namespace = "testns"
    settings.log_analytics.default_compartment_id = "ocid1.compartment.default"
    client = MagicMock()
    client.namespace = "testns"
    client.compartment_id = "ocid1.compartment.default"
    client._config = {"tenancy": "ocid1.tenancy.test"}
    client.list_alarms = AsyncMock(return_value=list(alarms))
    client.get_alarm = AsyncMock(return_value=alarms[0] if alarms else {})
    client.list_scheduled_tasks_for_saved_search = AsyncMock(return_value=[])
    cache = MagicMock()
    cache.get = MagicMock(return_value=None)
    user_dir = tmp_path / "users" / "testuser"
    user_dir.mkdir(parents=True)
    secret_store = SecretStore(tmp_path / "secret.yaml")
    secret_store.set_secret("correct-secret")
    handlers = MCPHandlers(
        settings=settings, oci_client=client, cache=cache, query_logger=MagicMock(),
        context_manager=MagicMock(), user_store=UserStore(base_dir=tmp_path, user_id="testuser"),
        preference_store=PreferenceStore(user_dir=user_dir), secret_store=secret_store,
        audit_logger=AuditLogger(tmp_path / "audit"),
    )
    return handlers, client


async def first_call_summary(handlers, tool, args):
    [response] = await handlers.handle_tool_call(tool, args)
    payload = json.loads(response["text"])
    assert payload["status"] == "confirmation_required"
    return payload["summary"]


def test_describe_linked_alerts_wording():
    alert = LinkedAlert(
        alarm_id="ocid1.alarm.c", display_name="Console VCN", severity="CRITICAL", lifecycle_state="ACTIVE",
        created_by="console", condition="test_metric[5m].max() > 0", threshold_operator="gt",
        threshold_value="0", backing_saved_search_id=SEARCH_ID, backing_metric_task_id="ocid1.task.c",
        metric_namespace="test_detection_rule", metric_name="test_metric", query="* | stats count",
        schedule="0 */5 * ? * *", schedule_format="oci",
    )
    text = describe_linked_alerts([alert], via="saved_search")
    assert text == ("This search feeds alert 'Console VCN' (console-made; fires when value > 0). "
                    "The alert will use the new query from its next run. "
                    "Its threshold stays unless you change it with update_alert.")
    assert describe_linked_alerts([], via="saved_search") is None


@pytest.mark.asyncio
async def test_update_saved_search_confirmation_names_linked_alert_and_threshold(tmp_path):
    handlers, client = make_handlers(tmp_path, [LOGAN_ALARM])

    summary = await first_call_summary(handlers, "update_saved_search",
                                       {"saved_search_id": SEARCH_ID, "query": NEW_QUERY})

    assert "linked_alerts:" in summary
    assert "feeds alert 'VCN flow spike' (logan-made; fires when value > 50)" in summary
    assert "threshold stays" in summary
    client.update_management_saved_search.assert_not_called()


@pytest.mark.asyncio
async def test_update_alert_with_query_warns_about_threshold(tmp_path):
    handlers, client = make_handlers(tmp_path, [LOGAN_ALARM])

    summary = await first_call_summary(handlers, "update_alert",
                                       {"alert_id": "ocid1.alarm.logan", "query": NEW_QUERY})

    assert "Alert 'VCN flow spike' fires when value > 50" in summary
    assert "threshold stays unless you also pass threshold_value" in summary


@pytest.mark.asyncio
async def test_no_linked_alert_no_line(tmp_path):
    handlers, client = make_handlers(tmp_path, [])

    summary = await first_call_summary(handlers, "update_saved_search",
                                       {"saved_search_id": SEARCH_ID, "query": NEW_QUERY})

    assert "linked_alerts" not in summary
    client.list_alarms.assert_awaited()


@pytest.mark.asyncio
async def test_lookup_failure_never_blocks_the_confirmation(tmp_path):
    handlers, client = make_handlers(tmp_path, [])
    client.list_alarms = AsyncMock(side_effect=RuntimeError("monitoring down"))

    summary = await first_call_summary(handlers, "update_saved_search",
                                       {"saved_search_id": SEARCH_ID, "query": NEW_QUERY})

    assert "linked_alerts" not in summary
    client.list_alarms.assert_awaited()


CONSOLE_ALARM = {
    "id": "ocid1.alarm.console", "display_name": "MS-breakins", "severity": "CRITICAL",
    "lifecycle_state": "ACTIVE", "namespace": "ms_rules", "resource_group": None,
    "query": "ms_breakin[5m].sum() > 70", "freeform_tags": {"team": "ms"},
    "defined_tags": {"Oracle-Tags": {"CreatedBy": "example-idcs/test.user@example.invalid",
                                     "CreatedOn": "2026-08-04T16:19:00.414Z"}},
}


@pytest.mark.asyncio
async def test_delete_alert_confirmation_says_the_saved_search_stays(tmp_path):
    handlers, client = make_handlers(tmp_path, [LOGAN_ALARM])

    summary = await first_call_summary(handlers, "delete_alert", {"alert_id": "ocid1.alarm.logan"})

    assert "deletion:" in summary
    assert "saved search" in summary and "stays" in summary and "delete_saved_search" in summary
    assert "OCI Console" not in summary
    client.delete_alarm.assert_not_called()


@pytest.mark.asyncio
async def test_delete_alert_confirmation_names_the_console_creator(tmp_path):
    handlers, client = make_handlers(tmp_path, [CONSOLE_ALARM])

    summary = await first_call_summary(handlers, "delete_alert", {"alert_id": "ocid1.alarm.console"})

    assert ("origin: Created in the OCI Console by example-idcs/test.user@example.invalid on 2026-08-04, "
            "not by this server.") in summary
    assert "deletion:" in summary


@pytest.mark.asyncio
async def test_update_alert_confirmation_names_the_console_creator(tmp_path):
    handlers, client = make_handlers(tmp_path, [CONSOLE_ALARM])

    summary = await first_call_summary(handlers, "update_alert",
                                       {"alert_id": "ocid1.alarm.console", "severity": "WARNING"})

    assert "origin: Created in the OCI Console by example-idcs/test.user@example.invalid" in summary
    assert "linked_alerts" not in summary
