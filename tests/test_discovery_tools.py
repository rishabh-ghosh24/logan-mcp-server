"""The four discovery tools take keyword and limit and return a bounded envelope (RQ-10)."""
import json

import pytest
from unittest.mock import AsyncMock, MagicMock

from oci_logan_mcp.audit import AuditLogger
from oci_logan_mcp.config import Settings
from oci_logan_mcp.handlers import MCPHandlers
from oci_logan_mcp.preferences import PreferenceStore
from oci_logan_mcp.secret_store import SecretStore
from oci_logan_mcp.tools import get_tools
from oci_logan_mcp.user_store import UserStore

SOURCES = [
    {"name": "ociVcnFlowUnifiedSchemaLogs", "display_name": "OCI VCN Flow Unified Schema Logs",
     "description": "Flow records", "entity_types": ["OCI VCN"]},
    {"name": "ociAuditLogs", "display_name": "OCI Audit Logs", "description": "", "entity_types": []},
    {"name": "apacheWebserverAccess", "display_name": "Apache Webserver Access Logs", "description": "", "entity_types": []},
] + [{"name": f"filler{i}", "display_name": f"Filler Source {i:03d}", "description": "", "entity_types": []} for i in range(60)]
FIELDS = [
    {"name": "Host Name (Server)", "display_name": "Host Name (Server)", "data_type": "STRING", "description": "The server hostname"},
    {"name": "Severity", "display_name": "Severity", "data_type": "STRING", "description": "Log severity level"},
    {"name": "srchost", "display_name": "Source Host", "data_type": "STRING", "description": ""},
]
ENTITIES = [
    {"name": "ms-grafana", "entity_type": "Host (Linux)", "hostname": "ms-grafana.sub.oraclevcn.com", "lifecycle_state": "ACTIVE"},
    {"name": "prod-db-01", "entity_type": "Oracle Database", "hostname": "", "lifecycle_state": "ACTIVE"},
]
SEARCHES = [
    {"id": "ocid1.mss.1", "display_name": "VCN rejects by source IP", "description": "Rejected flows", "usage": "reusable"},
    {"id": "ocid1.mss.2", "display_name": "Audit failures", "description": "", "usage": "detection_rule"},
]
VOLUME = {"data": {"columns": [{"name": "Log Source"}, {"name": "count"}],
                   "rows": [["OCI Audit Logs", 1375109], ["OCI VCN Flow Unified Schema Logs", 1876384]]}}


def make_handlers(tmp_path):
    settings = Settings()
    settings.log_analytics.namespace = "testns"
    settings.log_analytics.default_compartment_id = "ocid1.compartment.default"
    client = MagicMock()
    client.namespace = "testns"
    client.compartment_id = "ocid1.compartment.default"
    client._config = {"tenancy": "ocid1.tenancy.test"}
    client.list_log_sources = AsyncMock(return_value=[dict(s) for s in SOURCES])
    client.list_fields = AsyncMock(return_value=[dict(f) for f in FIELDS])
    client.list_entities = AsyncMock(return_value=[dict(e) for e in ENTITIES])
    cache = MagicMock(); cache.get = MagicMock(return_value=None)
    user_dir = tmp_path / "users" / "testuser"; user_dir.mkdir(parents=True)
    context = MagicMock()
    handlers = MCPHandlers(
        settings=settings, oci_client=client, cache=cache, query_logger=MagicMock(), context_manager=context,
        user_store=UserStore(base_dir=tmp_path, user_id="testuser"), preference_store=PreferenceStore(user_dir=user_dir),
        secret_store=SecretStore(tmp_path / "secret.yaml"), audit_logger=AuditLogger(tmp_path / "audit"),
    )
    handlers.query_engine = MagicMock(); handlers.query_engine.execute = AsyncMock(return_value=VOLUME)
    handlers.saved_search.list_searches = AsyncMock(return_value=[dict(s) for s in SEARCHES])
    return handlers, client, context


async def call(handlers, tool, args):
    [response] = await handlers.handle_tool_call(tool, args)
    return json.loads(response["text"])


@pytest.mark.asyncio
async def test_sources_keyword_ranks_and_annotates_with_records(tmp_path):
    handlers, _, context = make_handlers(tmp_path)

    out = await call(handlers, "list_log_sources", {"keyword": "vcn flow"})

    assert [s["display_name"] for s in out["sources"]] == ["OCI VCN Flow Unified Schema Logs"]
    row = out["sources"][0]
    assert row["name"] == "ociVcnFlowUnifiedSchemaLogs" and row["records_in_window"] == 1876384
    assert "description" in row and "entity_types" in row
    assert (out["total"], out["returned"], out["truncated"], out["keyword"]) == (1, 1, False, "vcn flow")
    context.update_log_sources.assert_called_once()
    assert len(context.update_log_sources.call_args[0][0]) == len(SOURCES)  # the whole catalog, not the page


@pytest.mark.asyncio
async def test_sources_default_limit_is_fifty_with_data_first(tmp_path):
    handlers, _, _ = make_handlers(tmp_path)

    out = await call(handlers, "list_log_sources", {})

    assert (out["total"], out["returned"], out["truncated"]) == (63, 50, True)
    assert [s["display_name"] for s in out["sources"][:2]] == ["OCI VCN Flow Unified Schema Logs", "OCI Audit Logs"]
    assert "records_in_window" not in out["sources"][2]  # no data in the window: key dropped, not null
    small = await call(handlers, "list_log_sources", {"limit": 5})
    assert small["returned"] == 5 and small["truncated"] is True


@pytest.mark.asyncio
async def test_sources_without_a_match_suggest_names(tmp_path):
    handlers, _, _ = make_handlers(tmp_path)

    out = await call(handlers, "list_log_sources", {"keyword": "audti"})

    assert out["sources"] == [] and out["total"] == 0
    assert out["did_you_mean"] == ["OCI Audit Logs"]


@pytest.mark.asyncio
async def test_sources_still_list_when_the_volume_query_fails(tmp_path):
    handlers, _, _ = make_handlers(tmp_path)
    handlers.query_engine.execute = AsyncMock(side_effect=TimeoutError("slow"))

    out = await call(handlers, "list_log_sources", {"keyword": "audit"})

    assert [s["display_name"] for s in out["sources"]] == ["OCI Audit Logs"]
    assert "records_in_window" not in out["sources"][0]


@pytest.mark.asyncio
async def test_fields_keyword_and_compact_rows(tmp_path):
    handlers, client, _ = make_handlers(tmp_path)

    out = await call(handlers, "list_fields", {"keyword": "host", "source_name": "Linux Secure Logs"})

    assert [f["name"] for f in out["fields"]] == ["Host Name (Server)", "srchost"]
    assert out["fields"][1] == {"name": "srchost", "display_name": "Source Host", "data_type": "STRING"}  # no null hint/values
    assert "hint" in out["fields"][0]
    client.list_fields.assert_awaited_once_with("Linux Secure Logs")
    assert out["total"] == 2 and out["truncated"] is False


@pytest.mark.asyncio
async def test_entities_keyword_matches_name_type_and_hostname(tmp_path):
    handlers, client, _ = make_handlers(tmp_path)

    out = await call(handlers, "list_entities", {"keyword": "linux", "entity_type": "Host (Linux)"})

    assert [e["name"] for e in out["entities"]] == ["ms-grafana"]
    client.list_entities.assert_awaited_once_with("Host (Linux)")
    assert "hostname" not in (await call(handlers, "list_entities", {"keyword": "db"}))["entities"][0]  # empty dropped


@pytest.mark.asyncio
async def test_saved_searches_keyword_and_envelope_keep_row_keys(tmp_path):
    handlers, _, _ = make_handlers(tmp_path)

    out = await call(handlers, "list_saved_searches", {"keyword": "vcn"})

    assert out["saved_searches"] == [SEARCHES[0]]
    assert (out["total"], out["returned"]) == (1, 1)
    everything = await call(handlers, "list_saved_searches", {})
    assert everything["saved_searches"] == SEARCHES and everything["keyword"] is None


def test_tool_definitions_declare_keyword_and_limit_within_the_payload_bound():
    tools = {tool["name"]: tool for tool in get_tools()}
    for name in ("list_log_sources", "list_fields", "list_entities", "list_saved_searches"):
        props = tools[name]["inputSchema"]["properties"]
        assert props["keyword"]["type"] == "string" and props["limit"]["type"] == "integer"
        assert "50" in tools[name]["description"]
    assert len(json.dumps(get_tools())) <= 60_000
