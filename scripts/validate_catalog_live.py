#!/usr/bin/env python3
"""Run all packaged query examples over real MCP stdio using the caller's OCI config.

The server runs in read-only mode. Results are limited to one row over 15 minutes;
the table reports execution success, not whether each query finds matching data.
LOGAN_SMOKE_COMMAND can select an installed console entry point for verification.
"""

import asyncio
from importlib.resources import files
import json
import os
import shlex
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import yaml


def packaged_queries():
    substitutions = {"{field}": "Log Source", "{n}": "5", "{keyword}": "error", "{level}": "ERROR"}
    for filename in ("starter_queries.yaml", "builtin_queries.yaml"):
        document = yaml.safe_load((files("oci_logan_mcp") / "data" / filename).read_text())
        for entry in document["queries"]:
            query = entry["query"]
            for placeholder, value in substitutions.items():
                query = query.replace(placeholder, value)
            yield filename, entry["name"], query


async def validate_catalog() -> int:
    override = os.environ.get("LOGAN_SMOKE_COMMAND")
    command = shlex.split(override) if override else [sys.executable, "-m", "oci_logan_mcp"]
    params = StdioServerParameters(
        command=command[0], args=[*command[1:], "--read-only", "--user", "catalog-validation"],
        env=dict(os.environ),
    )
    failed = 0
    print("RESULT\tCATALOG\tNAME\tDETAIL", flush=True)
    async with asyncio.timeout(300):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for filename, name, query in packaged_queries():
                    try:
                        result = await session.call_tool("run_query", {
                            "query": query, "time_range": "last_15_min", "max_results": 1,
                        })
                        text = next((item.text for item in result.content if item.type == "text"), "")
                        try:
                            payload = json.loads(text)
                        except ValueError:
                            payload = {}
                        success = not result.isError and isinstance(payload, dict) and "data" in payload
                        detail = "executed" if success else text.replace("\n", " ")[:180]
                    except Exception as error:
                        success, detail = False, type(error).__name__
                    failed += not success
                    print(f"{'PASS' if success else 'FAIL'}\t{filename}\t{name}\t{detail}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = asyncio.run(validate_catalog())
    except Exception as error:
        print(f"FAIL\ttransport\t{type(error).__name__}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
