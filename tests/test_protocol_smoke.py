"""Exercise the real entry point over MCP stdio, outside the checkout."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shlex
import sys

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
        unknown = await session.call_tool("no_such_tool", {})
        # Task 13 adds the isError contract; checkpoint A verifies the response text.
        assert "Unknown tool" in unknown.content[0].text


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
