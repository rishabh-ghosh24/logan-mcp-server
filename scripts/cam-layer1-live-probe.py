#!/usr/bin/env python3
"""Run the CAM Layer 1 MCP scope and retention probe over pinned SSH."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RETENTION_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


def _text_payload(result) -> object:
    text = "".join(block.text for block in result.content if hasattr(block, "text"))
    if not text:
        raise RuntimeError("MCP tool returned no text payload")
    return json.loads(text)


def _write_ready_file(path: str, payload: dict[str, object]) -> None:
    ready = Path(path)
    ready.parent.mkdir(parents=True, exist_ok=True)
    candidate = ready.with_name(f".{ready.name}.{os.getpid()}.tmp")
    candidate.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    candidate.chmod(0o600)
    candidate.replace(ready)


async def run(args: argparse.Namespace) -> None:
    ssh_args = [
        "-i",
        args.key,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={args.known_hosts}",
        "-p",
        str(args.port),
    ]
    if args.attempt_env_injection:
        ssh_args.extend(
            [
                "-o",
                "SetEnv=LOGAN_USER=attacker",
                "-o",
                "SetEnv=OCI_LOGAN_MCP_ENFORCE_ACCESS=0",
                "-o",
                "SetEnv=OCI_LOGAN_MCP_ACCESS_CONFIG=/tmp/attacker-policy.yaml",
            ]
        )
    ssh_args.append(f"cam@{args.host}")
    server = StdioServerParameters(
        command="ssh",
        args=ssh_args,
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            if "set_compartment" in tools:
                raise RuntimeError("blocked tool was advertised")

            entities = _text_payload(await session.call_tool("list_entities", {}))
            if not isinstance(entities, list):
                raise RuntimeError(f"unexpected list_entities response: {entities!r}")
            names = [
                item["name"] if isinstance(item, dict) else str(item)
                for item in entities
            ]
            prefix = f"{args.customer}_"
            if not names or any(
                name != str(args.customer) and not name.startswith(prefix)
                for name in names
            ):
                raise RuntimeError(f"entity scope violation: {names}")

            query_name = f"layer1_retention_{args.retention_tag}"
            saved = _text_payload(
                await session.call_tool(
                    "save_learned_query",
                    {
                        "name": query_name,
                        "query": "* | stats count",
                        "description": "Layer 1 deprovision retention probe",
                        "category": "general",
                        "tags": ["layer1", "retention-probe"],
                    },
                )
            )
            if not isinstance(saved, dict) or saved.get("status") != "saved":
                raise RuntimeError(f"retention probe was not saved: {saved!r}")

            ready_payload = {
                "status": "READY",
                "entities": names,
                "query_name": query_name,
            }
            if args.ready_file:
                _write_ready_file(args.ready_file, ready_payload)
            if args.hold_seconds:
                await asyncio.sleep(args.hold_seconds)
            print(json.dumps({**ready_payload, "status": "PASS"}, sort_keys=True))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def retention_tag(value: str) -> str:
    if not RETENTION_TAG_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "retention tag must be lowercase and path-safe"
        )
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=positive_int, default=22)
    parser.add_argument("--key", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--customer", type=positive_int, required=True)
    parser.add_argument("--retention-tag", type=retention_tag, required=True)
    parser.add_argument("--hold-seconds", type=positive_int, default=0)
    parser.add_argument("--ready-file")
    parser.add_argument("--attempt-env-injection", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
