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
