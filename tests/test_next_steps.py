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
