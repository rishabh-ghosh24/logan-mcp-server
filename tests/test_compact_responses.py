"""Tool results are serialised compactly: same content, no whitespace tokens."""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.handlers import MCPHandlers, _dumps
from tests.test_discovery_tools import make_handlers


def test_dumps_is_compact_keeps_unicode_and_round_trips():
    payload = {"display_name": "Zürich VCN", "rows": [[1, 2.5, None, True]], "nested": {"a": "b"}}
    text = _dumps(payload)
    assert "\n" not in text and ": " not in text and ", " not in text
    assert "Zürich" in text  # not escaped as ü
    assert json.loads(text) == payload
    assert _dumps({"when": __import__("datetime").date(2026, 10, 25)}) == '{"when":"2026-10-25"}'


def test_compact_is_materially_smaller_than_pretty():
    payload = {"sources": [{"display_name": f"Source {i}", "name": f"src{i}", "records_in_window": i} for i in range(50)],
               "total": 50, "returned": 50, "truncated": False}
    pretty = json.dumps(payload, indent=2)
    assert len(_dumps(payload)) <= 0.7 * len(pretty)


@pytest.mark.asyncio
async def test_tool_results_carry_no_indentation(tmp_path):
    handlers, _, _ = make_handlers(tmp_path)
    for tool, args in (("list_log_sources", {"limit": 3}), ("list_saved_searches", {}), ("get_current_context", {})):
        [response] = await handlers.handle_tool_call(tool, args)
        assert "\n" not in response["text"], tool
        json.loads(response["text"])


@pytest.mark.asyncio
async def test_error_results_are_compact_too(tmp_path):
    handlers, client, _ = make_handlers(tmp_path)
    client.list_fields = AsyncMock(side_effect=ValueError("MARKER"))
    [response] = await handlers.handle_tool_call("list_fields", {})
    assert "\n" not in response["text"]
