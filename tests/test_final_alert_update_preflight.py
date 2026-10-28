"""A confirmed alert edit validates completely before any cloud mutation."""
from copy import deepcopy

import pytest

from oci_logan_mcp.protocol_results import build_call_tool_result
from tests.final_review_helpers import ALARM, NEW_QUERY, OLD_QUERY, RULE, SEARCH, confirmed_call, make_handlers, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["schedule", "compound-threshold"])
async def test_invalid_combined_edit_refuses_without_writes_and_valid_edit_still_runs(tmp_path, invalid):
    h = make_handlers(tmp_path)
    client = h.oci_client
    client.alarms[ALARM]["freeform_tags"]["logan_origin"] = "console"
    arguments = {"alert_id": ALARM, "query": NEW_QUERY}
    if invalid == "schedule":
        arguments["schedule"] = "0 0 32 * *"
    else:
        client.alarms[ALARM]["query"] = "metric_current[5m].max() > 4 && metric_other[5m].max() > 1"
        arguments["threshold_value"] = 9
    before = deepcopy((client.searches, client.rules, client.alarms))

    result = await confirmed_call(h, "update_alert", arguments)

    assert build_call_tool_result(result).isError is True
    assert client.writes == [], "Validation refusal must precede every saved-search/rule/alarm write"
    assert (client.searches, client.rules, client.alarms) == before

    # A no-op implementation must fail this control rather than pass the refusal assertion.
    client.alarms[ALARM]["query"] = "metric_current[5m].max() > 4"
    valid = {"alert_id": ALARM, "query": NEW_QUERY, "threshold_value": 9}
    if invalid == "schedule":
        valid["schedule"] = "*/10 * * * *"
    accepted = await confirmed_call(h, "update_alert", valid)
    assert build_call_tool_result(accepted).isError is False, "A valid edit must execute"
    assert client.searches[SEARCH]["query"] == NEW_QUERY
    assert client.alarms[ALARM]["query"] == "metric_current[5m].max() > 9"
    assert "query" in payload(accepted)["updated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rule", "alarm"])
async def test_provider_failure_after_search_write_reports_completed_and_pending_resources(tmp_path, failure):
    h = make_handlers(tmp_path)
    client = h.oci_client
    failed_id = RULE if failure == "rule" else ALARM
    client.fail_operations.add((failure, failed_id))

    result = await confirmed_call(h, "update_alert", {
        "alert_id": ALARM, "query": NEW_QUERY, "schedule": "*/10 * * * *",
    })

    assert client.searches[SEARCH]["query"] == NEW_QUERY
    assert build_call_tool_result(result).isError is True
    assert result[0]["text"].startswith("{"), "Partial writes need structured recovery evidence"
    body = payload(result)
    assert body["partial_failure"] is True
    assert body["completed"][0] == {"resource": "saved_search", "id": SEARCH}
    assert body["remaining"] == ([RULE, ALARM] if failure == "rule" else [ALARM])
    if failure == "alarm":
        assert body["completed"][1] == {"resource": "detection_rule", "id": RULE}
    assert client.alarms[ALARM]["freeform_tags"]["logan_query"] == OLD_QUERY
