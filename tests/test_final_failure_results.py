"""Partial failures retain their evidence and fail at protocol and audit boundaries."""
import pytest

from oci_logan_mcp.audit import AuditLogger
from oci_logan_mcp.protocol_results import build_call_tool_result
from tests.final_review_helpers import ALARM, NEW_QUERY, SEARCH, confirmed_call, make_handlers, payload


async def assert_failure_and_diagnostic_control(handlers, result):
    assert build_call_tool_result(result).isError is True
    terminal = list(handlers.audit_logger.iter_entries())[-1]
    assert terminal["result_summary"]["success"] is False
    diagnostic = await handlers.handle_tool_call("validate_query", {"query": ""})
    assert payload(diagnostic)["valid"] is False
    assert build_call_tool_result(diagnostic).isError is False


@pytest.mark.asyncio
async def test_confirmed_search_partial_refresh_preserves_work_and_reports_failure(tmp_path):
    # Dropping is_error used to turn a real partial mutation into audit success.
    h = make_handlers(tmp_path)
    h.oci_client.fail_operations.add(("alarm", ALARM))
    result = await confirmed_call(h, "update_saved_search", {"saved_search_id": SEARCH, "query": NEW_QUERY})
    body = payload(result)
    assert h.oci_client.searches[SEARCH]["query"] == NEW_QUERY
    assert body["saved_search_updated"] is True and body["remaining"] == [ALARM]
    assert body["partial_failure"] is True
    await assert_failure_and_diagnostic_control(h, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1], ids=["all-failed", "partial"])
async def test_failed_batch_flags_failure_without_losing_completed_rows(tmp_path, limit):
    h = make_handlers(tmp_path)
    h._budget_tracker.limits.max_queries_per_session = limit
    result = await h.handle_tool_call("run_batch_queries", {"queries": [
        {"query": "* | stats count"}, {"query": "* | head 1"}]})
    body = payload(result)
    assert [row["success"] for row in body] == ([False, False] if limit == 0 else [True, False])
    if limit:
        assert body[0]["result"]["data"]["rows"] == [[7]]
    assert body[-1]["error_type"] == "BudgetExceededError"
    if not limit:
        await assert_failure_and_diagnostic_control(h, result)
    else:
        assert build_call_tool_result(result).isError is False
        summary = list(h.audit_logger.iter_entries())[-1]["result_summary"]
        assert summary["success"] is True and summary["status"] == "partial"
        assert summary["succeeded_count"] == 1 and summary["failed_count"] == 1
        assert summary["partial_failure"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,flags,counts,partial", [
    (0, [False, False], (0, 2), False),
    (1, [True, False], (1, 1), True),
    (2, [True, True], (2, 0), False),
])
async def test_batch_counts_and_partial_outcome_survive_no_text_audit(tmp_path, limit, flags, counts, partial):
    h = make_handlers(tmp_path)
    h.audit_logger = AuditLogger(tmp_path / "private-audit", store_arguments=False)
    h._budget_tracker.limits.max_queries_per_session = limit

    result = await h.handle_tool_call("run_batch_queries", {"queries": [
        {"query": "* | stats count"}, {"query": "* | head 1"}]})

    body = payload(result)
    assert [row["success"] for row in body] == flags
    assert build_call_tool_result(result).isError is (limit == 0)
    summary = list(h.audit_logger.iter_entries())[-1]["result_summary"]
    assert summary["succeeded_count"] == counts[0] and summary["failed_count"] == counts[1]
    assert summary["partial_failure"] is partial
    assert summary["success"] is (limit > 0)
    if limit:
        assert body[0]["result"]["data"]["rows"] == [[7]]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["alarm", "dashboard", "delivery"])
async def test_mutation_failure_payload_is_not_reported_as_success(tmp_path, operation):
    h = make_handlers(tmp_path)
    if operation == "alarm":
        h.oci_client.fail_operations.add(("delete_alarm", ALARM))
        result = await confirmed_call(h, "delete_alert", {"alert_id": ALARM})
        body = payload(result)
        assert body["partial_failure"] is True and body["remaining"] == [ALARM]
        assert body["deleted"] == ["metric_task"]
    elif operation == "dashboard":
        h.oci_client.fail_operations.add(("delete_dashboard", "example-dashboard"))
        result = await confirmed_call(h, "delete_dashboard", {"dashboard_id": "example-dashboard"})
        body = payload(result)
        assert body["partial_failure"] is True and body["remaining"] == ["example-dashboard"]
    else:
        result = await h.handle_tool_call("deliver_report", {"report": {"markdown": "# Synthetic"},
                                          "channels": ["slack"], "format": "markdown"})
        body = payload(result)
        assert body["status"] == "failed" and body["delivered"][0]["status"] == "failed"
        assert "not configured" in body["delivered"][0]["error"]
    await assert_failure_and_diagnostic_control(h, result)
