"""Exercise the real entry point over MCP stdio, outside the checkout."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shlex
import sys
import time
import subprocess

import pytest
import matplotlib
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from oci_logan_mcp import __version__
from oci_logan_mcp.tools import get_tools

FAKE_OCI = Path(__file__).parent / "fixtures" / "fake_oci"
BAD_CONFIG = (
    "oci:\n  config_path: /nonexistent/oci/config\n  auth_type: config_file\n"
    "log_analytics:\n  namespace: smoke\n  default_compartment_id: ocid1.compartment.oc1..smoke\n"
)
GOOD_CONFIG = BAD_CONFIG.replace("/nonexistent/oci/config", "/fake/oci/config")


def _params(tmp_path, config, fake_oci):
    home = tmp_path / "home"
    (home / ".oci-logan-mcp").mkdir(parents=True)
    (home / ".oci-logan-mcp" / "config.yaml").write_text(config)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("OCI_LA_", "OCI_LOGAN_", "OCI_CONFIG_"))
                   and key != "PYTHONPATH"}
    environment.update(HOME=str(home), USERPROFILE=str(home),
                       MPLCONFIGDIR=matplotlib.get_cachedir(), PYTHONDONTWRITEBYTECODE="1")
    if fake_oci:
        environment["PYTHONPATH"] = str(FAKE_OCI)
        barrier = tmp_path / "barrier"
        barrier.mkdir()
        environment["LOGAN_FAKE_OCI_BARRIER_DIR"] = str(barrier)
    override = os.environ.get("LOGAN_SMOKE_COMMAND")
    command = shlex.split(override) if override else [sys.executable, "-m", "oci_logan_mcp"]
    return StdioServerParameters(command=command[0], args=[*command[1:], "--user", "smoke"],
                                 env=environment, cwd=str(tmp_path))


@asynccontextmanager
async def _session(tmp_path, fake_oci):
    params = _params(tmp_path, GOOD_CONFIG if fake_oci else BAD_CONFIG, fake_oci)
    with (tmp_path / "server.stderr").open("w") as errors:
        async with asyncio.timeout(20):
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    yield session


@pytest.mark.asyncio
async def test_stdio_server_explains_failed_init(tmp_path):
    async with _session(tmp_path, fake_oci=False) as session:
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "oci-log-analytics"
        assert initialized.serverInfo.version == __version__
        assert "investigate_incident" in initialized.instructions
        tools = (await session.list_tools()).tools
        assert len(tools) == 64
        assert {tool.name for tool in tools} == {tool["name"] for tool in get_tools()}
        assert all(tool.title and tool.annotations is not None for tool in tools)
        assert len((await session.list_resources()).resources) == 6
        response = await session.call_tool("get_current_context", {})
        payload = json.loads(response.content[0].text)
        assert response.isError is True
        assert payload["error"] == "server_not_initialized"
        assert "ConfigFileNotFound" in payload["detail"]
        assert any("--setup" in step for step in payload["how_to_fix"])


@pytest.mark.asyncio
async def test_stdio_server_succeeds_against_fake_oci(tmp_path):
    async with _session(tmp_path, fake_oci=True) as session:
        await session.initialize()
        response = await session.call_tool("get_current_context", {})
        assert not response.isError and json.loads(response.content[0].text)["namespace"] == "smoke"
        resources = (await session.list_resources()).resources
        assert len(resources) == 6
        contents = {}
        for resource in resources:
            result = await session.read_resource(resource.uri)
            assert result.contents and result.contents[0].text
            contents[str(resource.uri)] = result.contents[0].text
        assert "stats" in contents["loganalytics://syntax-guide"]
        templates = json.loads(contents["loganalytics://query-templates"])["templates"]
        assert any(item["name"] == "log_volume_by_source" for item in templates)
        assert json.loads(contents["loganalytics://schema"])["log_sources"] == []
        response = await session.call_tool("run_query", {
            "query": "* | stats count", "time_range": "last_15_min",
        })
        assert not response.isError
        assert json.loads(response.content[0].text)["data"]["rows"] == [[42, "smoke"]]
        audit_path = tmp_path / "home" / ".oci-logan-mcp" / "logs" / "audit.log"
        events = [json.loads(line) for line in audit_path.read_text().splitlines()]
        query_events = [event for event in events if event["tool"] == "run_query"]
        assert len(query_events) == 2
        assert all(event["client_metadata"]["claimed_client"] == "mcp" for event in query_events)
        assert all(event["client_metadata"]["claimed_version"] != "unknown" for event in query_events)
        unknown = await session.call_tool("no_such_tool", {})
        assert unknown.isError is True
        assert "Unknown tool" in unknown.content[0].text
        missing = await session.call_tool("run_saved_search", {})
        assert missing.isError is True
        assert json.loads(missing.content[0].text)["error_code"] == "MISSING_ARGUMENT"


@pytest.mark.asyncio
async def test_fake_oci_barrier_waits_for_release_and_echoes_namespace(tmp_path):
    async with _session(tmp_path, fake_oci=True) as session:
        await session.initialize()
        pending = asyncio.create_task(session.call_tool("run_query", {
            "query": "* | stats count | slow", "time_range": "last_15_min",
        }))
        barrier = tmp_path / "barrier"
        try:
            async with asyncio.timeout(5):
                while not (barrier / "started").exists():
                    await asyncio.sleep(0.02)
            assert (barrier / "started").read_text() == "smoke"
            assert not pending.done()
            assert len((await asyncio.wait_for(session.list_tools(), 2)).tools) == 64
        finally:
            (barrier / "release").touch()
        result = await pending
        assert json.loads(result.content[0].text)["data"]["rows"] == [[42, "smoke"]]


@pytest.mark.asyncio
async def test_scope_change_waits_for_in_flight_query(tmp_path):
    async with _session(tmp_path, fake_oci=True) as session:
        await session.initialize()
        completed = {}

        async def call(key, tool, args):
            result = await session.call_tool(tool, args)
            completed[key] = time.monotonic()
            return result

        query = asyncio.create_task(call("query", "run_query", {"query": "* | stats count | slow"}))
        barrier = tmp_path / "barrier"
        try:
            async with asyncio.timeout(5):
                while not (barrier / "started").exists():
                    await asyncio.sleep(0.02)
            change = asyncio.create_task(call("change", "set_namespace", {"namespace": "other"}))
            await asyncio.sleep(0.3)
            assert not query.done() and not change.done()
        finally:
            (barrier / "release").touch()
        result, changed = await asyncio.gather(query, change)
        assert not result.isError and not changed.isError
        assert json.loads(result.content[0].text)["data"]["rows"] == [[42, "smoke"]]
        assert completed["query"] <= completed["change"]
        after = await session.call_tool("run_query", {"query": "* | stats count"})
        assert json.loads(after.content[0].text)["data"]["rows"] == [[42, "other"]]


@pytest.mark.parametrize("fake_oci", [True, False])
def test_catalog_validation_cli_reports_every_result_and_fails_on_tool_errors(tmp_path, fake_oci):
    from importlib.resources import files
    import yaml

    params = _params(tmp_path, GOOD_CONFIG if fake_oci else BAD_CONFIG, fake_oci)
    if fake_oci:
        (tmp_path / "barrier" / "release").touch()
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate_catalog_live.py"
    result = subprocess.run([sys.executable, str(script)], cwd=tmp_path, env=params.env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == (0 if fake_oci else 1), result.stdout + result.stderr
    expected_calls = 0
    for filename in ("starter_queries.yaml", "builtin_queries.yaml"):
        entries = yaml.safe_load((files("oci_logan_mcp") / "data" / filename).read_text())["queries"]
        expected_calls += len(entries)
        for entry in entries:
            assert f"{'PASS' if fake_oci else 'FAIL'}\t{filename}\t{entry['name']}" in result.stdout
    if fake_oci:
        audit = tmp_path / "home" / ".oci-logan-mcp" / "logs" / "audit.log"
        calls = [json.loads(line) for line in audit.read_text().splitlines()]
        invoked = [call for call in calls if call["tool"] == "run_query" and call["outcome"] == "invoked"]
        assert len(invoked) == expected_calls
        assert all(call["args_redacted"]["time_range"] == "last_15_min" and call["args_redacted"]["max_results"] == 1
                   for call in invoked)
