"""Tests for the next-step suggestion engine."""

from oci_logan_mcp.next_steps import NextStep, suggest


def test_next_step_is_dataclass():
    step = NextStep(tool_name="pivot_on_entity", suggested_args={"entity": "host"}, reason="test")
    assert step.tool_name == "pivot_on_entity"
    assert step.suggested_args == {"entity": "host"}
    assert step.reason == "test"


def test_suggest_returns_list():
    result = {"data": {"rows": [], "columns": []}, "metadata": {}}
    out = suggest("* | head 1", result)
    assert isinstance(out, list)


def test_suggest_never_raises_on_malformed_result():
    assert suggest("*", {}) == []
    assert suggest("*", {"data": None}) == []
    assert suggest("*", {"data": {"rows": None}}) == []


def test_empty_result_suggests_validate_query():
    result = {"data": {"rows": [], "columns": [{"name": "Time"}]}, "metadata": {}}
    steps = suggest("'Log Source' = 'nonexistent'", result)
    tools = [s.tool_name for s in steps]
    assert "validate_query" in tools


def test_large_result_suggests_narrower_window():
    rows = [[i, f"msg-{i}"] for i in range(1500)]  # > 1000
    result = {
        "data": {"rows": rows, "columns": [{"name": "Time"}, {"name": "Message"}]},
        "metadata": {},
    }
    steps = suggest("*", result)
    tools = [s.tool_name for s in steps]
    assert "run_query" in tools
    narrower = [s for s in steps if s.tool_name == "run_query"]
    assert any("narrow" in s.reason.lower() or "tighter" in s.reason.lower() for s in narrower)


def test_small_result_does_not_suggest_narrower_window():
    rows = [[i] for i in range(50)]
    result = {"data": {"rows": rows, "columns": [{"name": "Time"}]}, "metadata": {}}
    steps = suggest("*", result)
    tools = [s.tool_name for s in steps]
    assert "run_query" not in tools


def test_request_id_field_suggests_trace():
    result = {
        "data": {
            "rows": [["2026-04-20T10:00:00Z", "abc-123-def"]],
            "columns": [{"name": "Time"}, {"name": "Request ID"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    tools = [s.tool_name for s in steps]
    assert "trace_request_id" in tools


def test_populated_trace_id_field_suggests_trace():
    result = {
        "data": {
            "rows": [["2026-04-20", "my-trace-42"]],
            "columns": [{"name": "Time"}, {"name": "Trace-ID"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert any(s.tool_name == "trace_request_id" for s in steps)


def test_empty_id_field_does_not_suggest_trace():
    result = {
        "data": {
            "rows": [["2026-04-20", None], ["2026-04-20", ""]],
            "columns": [{"name": "Time"}, {"name": "Request ID"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert not any(s.tool_name == "trace_request_id" for s in steps)


def test_no_id_field_no_suggestion():
    result = {
        "data": {
            "rows": [["a", "b"]],
            "columns": [{"name": "Host"}, {"name": "Message"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert not any(s.tool_name == "trace_request_id" for s in steps)


def test_http_5xx_status_suggests_pivot_and_stats():
    result = {
        "data": {
            "rows": [["2026-04-20", "web-01", 500], ["2026-04-20", "web-02", 503]],
            "columns": [{"name": "Time"}, {"name": "Host"}, {"name": "Status"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    tools = [s.tool_name for s in steps]
    assert "pivot_on_entity" in tools
    assert "run_query" in tools
    stats_step = next(s for s in steps if s.tool_name == "run_query" and "stats" in s.reason.lower())
    assert stats_step is not None


def test_severity_field_with_error_value_suggests_pivot():
    result = {
        "data": {
            "rows": [["host-a", "ERROR"], ["host-b", "error"]],
            "columns": [{"name": "Host"}, {"name": "Severity"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert any(s.tool_name == "pivot_on_entity" for s in steps)


def test_successful_rows_do_not_suggest_error_pivot():
    result = {
        "data": {
            "rows": [["host-a", 200], ["host-b", 201]],
            "columns": [{"name": "Host"}, {"name": "Status"}],
        },
        "metadata": {},
    }
    steps = suggest("*", result)
    assert not any(s.tool_name == "pivot_on_entity" for s in steps)
