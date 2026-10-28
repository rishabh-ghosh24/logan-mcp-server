"""Saved query categories must remain usable through the public MCP schema."""

import json

import pytest

from tests.test_protocol_smoke import _session


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["network", "audit", "general"])
async def test_saved_query_is_available_through_its_category(tmp_path, category):
    name = f"qualification_{category}"
    query = "* | stats count"
    async with _session(tmp_path, fake_oci=True) as session:
        await session.initialize()
        executed = await session.call_tool("run_query", {"query": query})
        assert not executed.isError
        assert json.loads(executed.content[0].text)["data"]["rows"] == [[42, "smoke"]]
        saved = await session.call_tool("save_learned_query", {
            "name": name, "query": query, "description": "A confirmed working example",
            "category": category,
        })
        assert not saved.isError
        assert json.loads(saved.content[0].text)["status"] == "saved"
        examples = await session.call_tool("get_query_examples", {
            "category": category, "include_shared": False,
        })
        assert not examples.isError, examples.content[0].text
        payload = json.loads(examples.content[0].text)
        assert payload["category"] == category
        personal = [entry for entry in payload["examples"] if entry["source"] == "personal"]
        assert [(entry["name"], entry["query"]) for entry in personal] == [(name, query)]
